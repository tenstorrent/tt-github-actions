# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Try downstream Git reads once through cachew, falling back to upstream.

Any failed cache read is retried once against upstream. Only failures of the
cache endpoint itself (connection, TLS, 3xx/5xx, stall, deadline) remove the
job's cache rewrite; answers about one repository or ref (401/403/404, missing
ref, `ls-remote --exit-code` finding nothing) keep caching on for later reads,
as does a cache attempt whose Git alone was killed by a signal.
Reads whose effective URL is not the cache (other hosts, token-in-URL or SSH
remotes, a promisor origin pinned upstream) run unchanged and never affect it.
After a clone or fetch, partial-clone remotes are pinned upstream the same way,
since Git's lazy fetches do not pass through this wrapper.
"""

import fnmatch
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout import (  # noqa: E402
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    HARD_DEADLINE_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_FETCH_TIMEOUT_SECONDS,
    TLS_OVERRIDES,
    apply_git_config,
    transport_error,
)
from session import (
    LEGACY_CONFIG,
    load_state,
    pin_promisor_remotes,
    remove_cache_keys,
    remove_managed,
    subsection,
)  # noqa: E402

OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
    "--attr-source",
}
CLONE_OPTIONS_WITH_VALUE = {
    "-o",
    "--origin",
    "-b",
    "--branch",
    "-u",
    "--upload-pack",
    "--reference",
    "--reference-if-able",
    "--separate-git-dir",
    "--depth",
    "--shallow-since",
    "--shallow-exclude",
    "-c",
    "--config",
    "--server-option",
    "-j",
    "--jobs",
    "--filter",
    "--template",
    "--bundle-uri",
    "--ref-format",
    "--revision",
}
READ_OPTIONS_WITH_VALUE = {
    "clone": CLONE_OPTIONS_WITH_VALUE,
    "fetch": {
        "--depth",
        "--deepen",
        "--shallow-since",
        "--shallow-exclude",
        "--upload-pack",
        "--refmap",
        "-o",
        "--server-option",
        "--filter",
        "-j",
        "--jobs",
        "--negotiation-tip",
        "--recurse-submodules-default",
        "--submodule-prefix",
    },
    "ls-remote": {"--upload-pack", "--sort", "-o", "--server-option"},
}
# Options without a value whose whole name starts a value-taking option's name
# (--recurse-submodules-default): Git reads the exact name first.
EXACT_FLAGS = ("--recurse-submodules",)
NETWORK_COMMANDS = ("clone", "fetch", "ls-remote", "push", "pull", "submodule", "remote")
# Git itself gives up on an alias loop; this only bounds the lookups.
MAX_ALIAS_DEPTH = 10
SHELL_ALIAS = object()
TERMINATE_GRACE_SECONDS = 10
STDERR_TAIL_BYTES = 65536


def command_index(arguments):
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg in OPTIONS_WITH_VALUE:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            return index
    return None


def alias_expansion(listing, word):
    """The alias Git runs for a command word, from `git config --list -z`: either
    form, alias.<name> or alias.<name>.command (which allows any character in
    the name), and the last one set, as Git reads them."""
    plain, subsection = f"alias.{word.lower()}", f"alias.{word}.command"
    expansion = None
    for entry in (listing or "").split("\0"):
        key, _, value = entry.partition("\n")
        if key == subsection or ("." not in word and key == plain):
            expansion = value
    return expansion


def resolve_alias(real_git, arguments, index, environ):
    """Expand Git aliases as Git will, so routing sees the command that really runs
    (`alias.up = fetch`, `alias.pu = push`). Returns the expanded arguments and the
    command's index, or SHELL_ALIAS for a `!` alias, whose commands cannot be known."""

    def output(*args):
        result = subprocess.run(
            [real_git, *arguments[:index], *args], env=environ, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return result.stdout.decode(errors="replace") if result.returncode == 0 else None

    commands = None
    for _ in range(MAX_ALIAS_DEPTH):
        word = arguments[index]
        if word in NETWORK_COMMANDS:
            break
        expansion = alias_expansion(output("config", "--list", "-z"), word)
        if not expansion or not expansion.strip():
            break
        if commands is None:
            # Git ignores an alias that has the name of one of its commands.
            listed = output("--list-cmds=builtins,main,others")
            if listed is None:
                break
            commands = set(listed.split())
        if word in commands:
            break
        expansion = expansion.strip()
        if expansion.startswith("!"):
            return SHELL_ALIAS
        try:
            words = shlex.split(expansion)
        except ValueError:
            break
        if not words:
            break
        arguments = [*arguments[:index], *words, *arguments[index + 1 :]]
        # An alias may itself start with options (`-c key=value fetch`).
        index = command_index(arguments)
        if index is None:
            break
    return arguments, index


# Variables that make Git act on a repository other than the one it is run in.
REPOSITORY_ENVIRONMENT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


class UpstreamView:
    """A private copy of the job's global configuration without the cache rewrite.

    Also used once the session is disabled: until every rewrite is removed
    (a concurrent disable, or a locked file), the recorded keys are still there."""

    def __init__(self, real_git, environ, session, state):
        self.path = None
        self.environ = environ
        if not state.get("managed"):
            return
        # Opened first: the job file can be gone (the runner emptied RUNNER_TEMP),
        # and then there is no copy to leave behind.
        with (session / "global.config").open("rb") as source:
            descriptor, name = tempfile.mkstemp(dir=session, prefix="upstream-view-", suffix=".config")
            self.path = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    shutil.copyfileobj(source, target)
                remove_managed(real_git, self.path, state, environ, rewrites_only=True)
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
        self.environ = dict(environ, GIT_CONFIG_GLOBAL=str(self.path))

    def __enter__(self):
        return self.environ

    def __exit__(self, *_):
        if self.path:
            self.path.unlink(missing_ok=True)


class FetchHead:
    """FETCH_HEAD as it was before the cache attempt, to put back before a retry."""

    def __init__(self, real_git, arguments, index, environ):
        result = subprocess.run(
            [real_git, *arguments[:index], "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        path = os.fsdecode(result.stdout.rstrip(b"\n"))
        self.path = Path(path) if not result.returncode and os.path.isabs(path) else None
        try:
            self.content = self.path.read_bytes() if self.path else None
        except FileNotFoundError:
            self.content = None

    def restore(self):
        if not self.path:
            return
        if self.content is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_bytes(self.content)


def direct_network_operation(real_git, arguments, index, environ):
    """Avoid Git's internal recursive clone retries and keep pushes upstream."""
    command = arguments[index]
    tail = arguments[index + 1 :]
    # Every remote subcommand that can reach a remote (update, prune, show,
    # add -f, set-head --auto) goes upstream; the local ones are unaffected.
    if command in ("push", "pull", "submodule", "remote"):
        return True
    # A retry could not replay the refspecs the cache attempt consumed from stdin.
    if command == "fetch" and "--stdin" in tail:
        return True
    recursive = [arg for arg in tail if arg.startswith(("--recursive", "--recurse-submodules"))]
    # For clone, --recurse-submodules=<value> is a pathspec, not a boolean:
    # `--recurse-submodules=false` still runs the recursive submodule update
    # (for paths matching "false") and records submodule.active=false. So any
    # spelling makes a clone recursive; only other commands take false/no/0.
    if (command == "clone" and recursive) or any(
        arg.split("=", 1)[-1].lower() not in ("false", "no", "0") for arg in recursive
    ):
        return True
    if any(arg.lower() in ("submodule.recurse=true", "fetch.recursesubmodules=true") for arg in arguments):
        return True
    if command == "fetch" and not any(arg in ("--no-recurse-submodules", "--recurse-submodules=false") for arg in tail):
        # An ordinary fetch may recurse implicitly into initialized submodules.
        # Let upstream handle that operation; the checkout action seeds its
        # requested recursive submodules through cachew with explicit fetches.
        result = subprocess.run(
            [real_git, *arguments[:index], "rev-parse", "--path-format=absolute", "--git-path", "modules"],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        path = result.stdout.decode(errors="replace").strip()
        if result.returncode or not Path(path).is_absolute() or Path(path).is_dir():
            return True
    return False


def working_directory(arguments, index):
    directory = Path.cwd()
    position = 0
    while position < index:
        if arguments[position] == "-C":
            directory = directory / arguments[position + 1]
            position += 2
        elif arguments[position] in OPTIONS_WITH_VALUE:
            position += 2
        else:
            position += 1
    return directory


def takes_next(argument, with_value):
    """Whether an option's value is the next argument: one of with_value, the
    start of one's name (Git takes any unambiguous abbreviation: `--conf`), or
    a cluster of short options (`-nb main`) whose last letter is. A value-taking
    letter earlier in the cluster has the rest of it as its value (`-bmain`)."""
    if argument in with_value:
        return True
    if argument.startswith("--"):
        if argument in EXACT_FLAGS or len(argument) < 3 or "=" in argument:
            return False
        return any(option.startswith(argument) for option in with_value)
    if not argument.startswith("-"):
        return False
    for position, letter in enumerate(argument[1:], 1):
        if "-" + letter in with_value:
            return position == len(argument) - 1
    return False


def clone_destination(arguments, index):
    """Where `git clone` will create its repository, or None if unknown."""
    positional = []
    bare = False
    position = index + 1
    while position < len(arguments):
        arg = arguments[position]
        if arg == "--":
            positional.extend(arguments[position + 1 :])
            break
        if takes_next(arg, CLONE_OPTIONS_WITH_VALUE):
            position += 2
            continue
        # Or the start of either name, as Git takes it (--b is not one: --branch).
        if len(arg) > 3 and "--bare".startswith(arg) or len(arg) > 2 and "--mirror".startswith(arg):
            bare = True
        if not arg.startswith("-"):
            positional.append(arg)
        position += 1
    if not positional:
        return None
    if len(positional) > 1:
        name = positional[1]
    else:
        name = positional[0].rstrip("/")
        name = name.removesuffix("/.git").removesuffix(".git")
        name = name.replace(":", "/").rsplit("/", 1)[-1]
        if not name:
            return None
        if bare:
            # Git names a bare or mirror clone <humanish>.git.
            name += ".git"
    return working_directory(arguments, index) / name


def option_value(arguments, name, shortest=None):
    """The last value of `name <value>` or `name=<value>` in arguments, or None.
    With shortest, the name may be abbreviated down to it, as Git takes a
    command's long options."""
    value = None
    for position, arg in enumerate(arguments):
        if arg == "--":
            break
        given, attached, rest = arg.partition("=")
        if given != name and not (shortest and given.startswith(shortest) and name.startswith(given)):
            continue
        if attached:
            value = rest
        elif position + 1 < len(arguments):
            value = arguments[position + 1]
    return value


class CloneTarget:
    """Remove what a failed cache clone left behind before the upstream retry:
    its destination and, with `--separate-git-dir` or a separate work tree
    (`--work-tree`, GIT_WORK_TREE), the second directory Git creates."""

    def __init__(self, arguments, index, environ):
        self.path = clone_destination(arguments, index) if arguments[index] == "clone" else None
        self.created = {}
        if not self.path:
            return
        directory = working_directory(arguments, index)
        extra = (
            option_value(arguments[index + 1 :], "--separate-git-dir", "--sep"),
            option_value(arguments[:index], "--work-tree") or environ.get("GIT_WORK_TREE"),
        )
        for path in (self.path, *(directory / value for value in extra if value)):
            existed = path.exists()
            self.created[path] = (existed, existed and path.is_dir() and not any(path.iterdir()))

    def reset(self):
        for path, (existed, was_empty) in self.created.items():
            if not path.exists() or path.is_symlink():
                continue
            if not existed:
                shutil.rmtree(path, ignore_errors=True)
            elif was_empty:
                for child in path.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)


def seconds(environ, name, default, maximum):
    try:
        value = float(environ.get(name, default))
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError
        return value
    except ValueError:
        return float(default)


def repository_argument(arguments, index):
    """The repository a read names, "" for the default remote, or None if unsure."""
    command = arguments[index]
    tail = arguments[index + 1 :]
    if command == "fetch" and any(arg in ("--all", "--multiple") for arg in tail):
        return None
    with_value = READ_OPTIONS_WITH_VALUE[command]
    position = 0
    while position < len(tail):
        arg = tail[position]
        if arg == "--":
            return tail[position + 1] if position + 1 < len(tail) else None
        if takes_next(arg, with_value):
            position += 2
        elif arg.startswith("-"):
            position += 1
        else:
            return arg
    return None if command == "clone" else ""


def fetch_remotes(real_git, arguments, index, environ):
    """The remotes a fetch of several reads (--all, --multiple, or a
    remotes.<group> name with more than one member; Git fetches a one-member
    group's name as a remote), or None for a fetch of one repository."""
    tail = arguments[index + 1 :]

    def words(*args):
        result = subprocess.run(
            [real_git, *arguments[:index], *args], env=environ, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return result.stdout.decode(errors="replace").split() if result.returncode == 0 else []

    if "--all" in tail:
        return words("remote")
    names, position = [], 0
    while position < len(tail):
        if tail[position] in READ_OPTIONS_WITH_VALUE["fetch"]:
            position += 2
            continue
        if not tail[position].startswith("-"):
            names.append(tail[position])
        position += 1
    if "--multiple" not in tail:
        if not names:
            # fetch.all (Git 2.44) makes a fetch that names no remote one of
            # every remote, unless --no-all is given.
            if "--no-all" not in tail and words("config", "--type=bool", "--get", "fetch.all") == ["true"]:
                return words("remote")
            return None
        if len(words("config", "--get-all", f"remotes.{names[0]}")) < 2:
            return None
        names = names[:1]
    return [remote for name in names for remote in words("config", "--get-all", f"remotes.{name}") or [name]]


def reaches_cache(real_git, arguments, index, environ, cache, repository=""):
    """Whether the read's effective URL is the cache. Unknown counts as the cache,
    so only reads that provably go elsewhere skip the cache attempt."""
    repository = repository or repository_argument(arguments, index)
    if repository is None or not cache:
        return True
    if arguments[index] == "clone":
        # A clone reads no configuration of the repository it runs in (a pin or
        # a token-in-URL rewrite there would hide the cache rewrite): resolve its
        # URL outside any repository, with the settings it does read, Git's -c
        # options before the command and its own -c/--config before it fetches.
        options = [option for pair in clone_options(arguments, index) for option in pair]
        options += [option for setting in clone_settings(arguments[index + 1 :])[0] for option in ("-c", setting)]
        environ = {key: value for key, value in environ.items() if key not in REPOSITORY_ENVIRONMENT}
        with tempfile.TemporaryDirectory() as empty:
            environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(empty)
            result = subprocess.run(
                [real_git, *options, "ls-remote", "--get-url", repository],
                env=environ,
                cwd=empty,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
    else:
        result = subprocess.run(
            [real_git, *arguments[:index], "ls-remote", "--get-url", *([repository] if repository else [])],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    if result.returncode:
        return True
    return result.stdout.decode(errors="replace").strip().startswith(cache.rstrip("/") + "/git/")


def clone_options(arguments, index):
    """The configuration options among Git's options before the command, as
    (option, value) pairs or one-element tuples for an attached value; -C,
    --git-dir and the others that choose a repository are left out."""
    position = 0
    while position < index:
        argument = arguments[position]
        if argument in ("-c", "--config-env") and position + 1 < index:
            yield argument, arguments[position + 1]
        elif argument.startswith("--config-env="):
            yield (argument,)
        position += 2 if argument in OPTIONS_WITH_VALUE else 1


def clone_settings(tail):
    """(the values of a clone's -c/--config options, its --template directory or
    None), in every form Git accepts: `-c k=v`, `-ck=v`, a short option after
    other flags (`-qc k=v`), `--config k=v`, `--config=k=v` and an abbreviated
    long option (`--conf`)."""
    settings, template, position = [], None, 0
    while position < len(tail):
        argument = tail[position]
        position += 1
        if argument == "--":
            break
        name, attached, value = argument.partition("=")
        if argument.startswith("--"):
            # Git takes any unambiguous start of a long option's name.
            long = next((full for full in ("--config", "--template") if len(name) > 3 and full.startswith(name)), None)
            if long is None:
                position += takes_next(argument, CLONE_OPTIONS_WITH_VALUE)
                continue
            if not attached:
                value, position = (tail[position] if position < len(tail) else ""), position + 1
            if long == "--config":
                settings.append(value)
            else:
                template = value
        elif argument.startswith("-") and len(argument) > 1:
            # Flags may share one dash; the first that takes a value ends them.
            for at, flag in enumerate(argument[1:], 2):
                if flag in CLONE_SHORT_WITH_VALUE:
                    value = argument[at:]
                    if not value:
                        value, position = (tail[position] if position < len(tail) else ""), position + 1
                    if flag == "c":
                        settings.append(value)
                    break
    return settings, template


MAX_CONDITIONAL_INCLUDES = 30
MAX_SUBMODULE_REPOSITORIES = 1000
CLONE_SHORT_WITH_VALUE = "bcjou"
GIT_TRUE = ("true", "yes", "on", "1")
GIT_FALSE = ("false", "no", "off", "0", "")


def submodule_git_dirs(real_git, location, environ):
    """The Git directories of the repository's submodules, at any depth, as a
    recursive fetch reaches them: under <git dir>/modules, through symbolic
    links too, and wherever a populated submodule's own .git leads (a Git
    directory kept in its work tree, say). None past MAX_SUBMODULE_REPOSITORIES."""
    own = {key: value for key, value in environ.items() if key not in REPOSITORY_ENVIRONMENT}

    def ask(where, env, *query):
        result = subprocess.run(
            [real_git, *where, "rev-parse", "--path-format=absolute", *query],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return "" if result.returncode else result.stdout.decode(errors="surrogateescape").rstrip("\n")

    found, seen, walked = [], set(), set()

    def add(directory):
        real = os.path.realpath(directory)
        if real in seen:
            return False
        seen.add(real)
        found.append(directory)
        return True

    pending = [(list(location), environ, False)]
    while pending:
        if len(found) > MAX_SUBMODULE_REPOSITORIES:
            return None
        where, env, submodule = pending.pop()
        if submodule:
            directory = ask(where, env, "--git-dir")
            if not directory or not add(directory):
                continue
        modules = ask(where, env, "--git-path", "modules")
        # Each directory once in the whole search: a nested submodule's modules
        # directory is reached from the top and again from its parent's work tree.
        if modules and os.path.isdir(modules) and os.path.realpath(modules) not in walked:
            for directory, children, files in os.walk(modules, followlinks=True):
                walked.add(os.path.realpath(directory))
                if "config" in files and "HEAD" in files:
                    add(directory)
                    # A Git directory holds its own submodules only under modules/:
                    # objects/, refs/ and the rest are never searched.
                    children[:] = [child for child in children if child == "modules"]
                children[:] = [
                    child for child in children if os.path.realpath(os.path.join(directory, child)) not in walked
                ]
        top = ask(where, env, "--show-toplevel")
        if not top:
            continue
        listing = subprocess.run(
            [real_git, *where, "ls-files", "-z", "--stage", "--full-name"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for entry in listing.stdout.decode(errors="surrogateescape").split("\0"):
            metadata, _, path = entry.partition("\t")
            if metadata.startswith("160000 ") and os.path.lexists(os.path.join(top, path, ".git")):
                pending.append((["-C", os.path.join(top, path)], own, True))
    return found


def weakened(real_git, arguments, index, environ, cache):
    """The setting, if any, with which the job turns off certificate verification
    or allows redirects for the cache's host, as this command reads its
    configuration (files, GIT_CONFIG_COUNT, -c, and for a command that can
    recurse, the files of the repository's submodules). A request to the cache can
    carry a token, and Git applies the most specific http.<url>.* it finds, so
    cachew's own settings cannot outrank one the job scoped more narrowly."""

    def read(*options):
        listing = subprocess.run(
            [real_git, *arguments[:index], "config", "--null", "--includes", *options],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return listing.stdout.decode(errors="surrogateescape").split("\0")

    def listed(*source):
        return [entry.partition("\n") for entry in read(*source, "--list")]

    def located(path, directory):
        if path.startswith("~/"):
            return environ.get("HOME", "") + path[1:]
        # Relative to the file that includes it.
        return os.path.join(directory, path)

    # Where the new repository's Git directory will be, and the template its
    # first files are copied from.
    homes, copied = [], None

    def placed(directory):
        """Where Git resolves the relative includes of a file in `directory`:
        there and, for a file the clone copies from its template, in the new
        Git directory, which does not exist yet."""
        yield directory
        if copied:
            inside = os.path.relpath(os.path.realpath(directory), copied)
            if inside.split(os.sep)[0] != os.pardir:
                for home in homes:
                    yield os.path.normpath(os.path.join(home, inside))

    def included(*source):
        """The files a clone's new repository includes that the listing of
        `source` does not follow: those of conditional includes, whatever their
        condition (the Git directory or the remote URL of a repository that does
        not exist yet), and those a copied file names relative to itself."""
        fields = read(*source, "--show-origin", "--get-regexp", r"^include(if\..*)?\.path$")
        for origin, entry in zip(fields[0::2], fields[1::2]):
            key, _, path = entry.partition("\n")
            file = origin[5:] if origin.startswith("file:") else ""
            directory = os.path.dirname(working_directory(arguments, index) / file)
            for copy, place in enumerate(placed(directory)):
                if copy:
                    # Git resolves it once that directory exists.
                    yield os.path.normpath(located(path, place))
                elif key.startswith("includeif."):
                    yield located(path, place)

    entries = listed()
    if arguments[index] in ("fetch", "pull", "submodule", "push", "remote"):
        # These may recurse into submodules, whose own files Git then reads: a
        # push with --recurse-submodules or push.recurseSubmodules, and a remote
        # update, which fetches as `git fetch` does.
        directories = submodule_git_dirs(real_git, arguments[:index], environ)
        if directories is None:
            return f"more than {MAX_SUBMODULE_REPOSITORIES} submodule repositories"
        own = {key: value for key, value in environ.items() if key not in REPOSITORY_ENVIRONMENT}
        for directory in directories:
            # In the submodule's own context, as Git reads it: its conditional
            # includes match its Git directory and branch, not the superproject's.
            listing = subprocess.run(
                [real_git, f"--git-dir={directory}", "config", "--null", "--includes", "--list"],
                env=own,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            entries += [entry.partition("\n") for entry in listing.stdout.decode(errors="surrogateescape").split("\0")]
    if arguments[index] == "clone":
        # A clone gives its new repository the settings of its -c/--config
        # options and of its template directory before it fetches.
        tail = arguments[index + 1 :]
        settings, template = clone_settings(tail)
        own = [setting.partition("=") for setting in settings]
        template = template or environ.get("GIT_TEMPLATE_DIR")
        if template is None:
            # The last one set, as Git reads a single-valued setting, and not one
            # of the clone's own options: Git copies the template before those.
            template = next((value for key, _, value in reversed(entries) if key == "init.templatedir"), None)
        entries += own
        directory = working_directory(arguments, index)
        destination = clone_destination(arguments, index)
        separate = option_value(tail, "--separate-git-dir", "--sep")
        if separate:
            homes = [os.path.realpath(directory / separate)]
        elif destination:
            # Itself for a bare or mirror clone: reading both only keeps more
            # clones off the cache.
            homes = [os.path.realpath(destination), os.path.realpath(destination / ".git")]
        pending, seen = [], set()
        if template:
            template = located(template, "")
            copied = os.path.realpath(directory / template)
            entries += listed("--file", os.path.join(template, "config"))
            pending += included("--file", os.path.join(template, "config"))
        pending += included()
        for key, _, value in own:
            # The new repository's configuration then includes the file, from its
            # Git directory, which starts as a copy of the template.
            section, _, variable = key.lower().rpartition(".")
            if variable == "path" and section.partition(".")[0] in ("include", "includeif"):
                if not homes and not os.path.isabs(located(value, "")):
                    return f"{key}, relative to a Git directory the guard cannot place"
                pending += [os.path.normpath(located(value, home)) for home in homes or [""]]
                if copied:
                    pending.append(located(value, template))
        while pending and len(seen) < MAX_CONDITIONAL_INCLUDES:
            path = pending.pop()
            if path not in seen:
                seen.add(path)
                entries += listed("--file", path)
                pending += included("--file", path)
        if any(path not in seen for path in pending):
            # Unread files could hold anything: the guard never passes on a guess.
            return f"a conditional include past the {MAX_CONDITIONAL_INCLUDES} files read"
    target = urllib.parse.urlsplit(cache)
    for key, separator, value in entries:
        # A key with no value is true.
        value = value.lower() if separator else "true"
        for name, safe in ((".sslverify", GIT_TRUE), (".followredirects", GIT_FALSE)):
            url = subsection(key, "http.", name)
            if not url or value in safe:
                continue
            try:
                scope = urllib.parse.urlsplit(url)
                # Git reads a host with a trailing dot as the same host.
                host = (scope.hostname or "").lower().removesuffix(".")
            except ValueError:
                return key
            # Git's host wildcard matches one label; this match is wider, which
            # only keeps more reads off the cache.
            if scope.scheme.lower() == target.scheme and fnmatch.fnmatchcase(target.hostname or "", host):
                return key
    return None


def cache_attempt(real_git, arguments, environ, index):
    """Run once through the cache. Returns (exit status, stderr tail)."""
    stall = seconds(environ, "CACHEW_GIT_FETCH_TIMEOUT", DEFAULT_FETCH_TIMEOUT_SECONDS, MAX_FETCH_TIMEOUT_SECONDS)
    deadline = seconds(environ, "CACHEW_HARD_DEADLINE", HARD_DEADLINE_SECONDS, MAX_DEADLINE_SECONDS)

    def terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            # Let clone remove its partially created destination before fallback.
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    command = [
        real_git,
        *arguments[:index],
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        # Abandon a stalled transfer, never a slow but progressing one.
        "-c",
        "http.lowSpeedLimit=1000",
        "-c",
        f"http.lowSpeedTime={max(1, math.ceil(stall))}",
        *arguments[index:],
    ]
    # transport_error matches Git's English messages.
    cache_env = dict(environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false", LC_ALL="C", LANGUAGE="")
    for name in TLS_OVERRIDES:
        cache_env.pop(name, None)
    tail = bytearray()

    def relay(stream):
        output = sys.stderr.buffer
        for chunk in iter(lambda: stream.read1(8192), b""):
            output.write(chunk)
            output.flush()
            tail.extend(chunk)
            del tail[:-STDERR_TAIL_BYTES]

    with subprocess.Popen(command, env=cache_env, stderr=subprocess.PIPE, start_new_session=True) as process:
        reader = threading.Thread(target=relay, args=(process.stderr,), daemon=True)
        reader.start()
        try:
            status = process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            terminate(process)
            status = 124
        except BaseException:
            if process.poll() is None:
                terminate(process)
            raise
        reader.join(timeout=5)
    return status, bytes(tail)


def run(arguments, environ):
    real_git = environ["CACHEW_REAL_GIT"]
    session = Path(environ["CACHEW_SESSION_DIR"])
    index = command_index(arguments)
    if index is None:
        return subprocess.call([real_git, *arguments], env=environ)
    owned_config = Path(environ.get("GIT_CONFIG_GLOBAL", "")).resolve() == (session / "global.config").resolve()
    if not owned_config:
        return subprocess.call([real_git, *arguments], env=environ)
    if not (session / "global.config").exists():
        # A missing session must not prevent Git from running after the runner empties RUNNER_TEMP.
        return subprocess.call([real_git, *arguments], env=environ)
    # A corrupt or missing state.json turns caching off, taking cachew's keys
    # out of the job file, rather than leaving them to redirect this read.
    state = load_state(session, environ)
    disabled = (session / "cache-disabled").exists()
    if not disabled and not state.get("active"):
        return subprocess.call([real_git, *arguments], env=environ)
    # Before any route is chosen, so that each one, the disabled session's
    # included, sees the command that really runs and a clone's destination.
    # The wrapper's own `git config` lookups read every file, as the command
    # does: GIT_CONFIG would have them read one file only.
    reading = {key: value for key, value in environ.items() if key != LEGACY_CONFIG}
    resolved = resolve_alias(real_git, arguments, index, reading)
    shell_alias = resolved is SHELL_ALIAS
    if not shell_alias and resolved[1] is not None:
        # Run the expansion, which is what Git would run: every check below then
        # reads the same command line that executes.
        arguments, index = resolved
    command = arguments[index]
    target = CloneTarget(arguments, index, environ)
    # For every route but the cache attempt, which drops GIT_SSL_NO_VERIFY and is
    # not made under a weakening setting. Run as it is or upstream, a command may
    # still reach the cache on its own: a lazy fetch, a helper such as git-lfs, a
    # remote whose URL names the cache. Where Git would not verify that request
    # or would follow its redirect, it carries no token: the empty values, read
    # after the job's files, clear cachew's headers.
    cache = state.get("cache")
    setting = weakened(real_git, arguments, index, reading, cache) if cache else None
    direct = environ
    if setting or any(name in environ for name in TLS_OVERRIDES):
        headers = dict.fromkeys(key for key, _ in state.get("managed", []) if key.lower().endswith(".extraheader"))
        direct = dict(environ)
        apply_git_config(direct, [(key, "") for key in headers], append=True)

    def done(result):
        # Whatever the command's exit status: one that fails partway (a submodule
        # that cannot be cloned) leaves partial clones whose lazy fetches would
        # use the rewrite. Pins only remotes the rewrite still reaches, so it is
        # a no-op without one, and where there is no repository.
        if command not in ("clone", "fetch", "pull", "remote", "submodule"):
            return result
        location, pin_env = arguments[:index], environ
        if command == "clone":
            if not target.path:
                return result
            # git clone ignores the variables that select another repository.
            location = ["-C", str(target.path)]
            pin_env = {key: value for key, value in environ.items() if key not in REPOSITORY_ENVIRONMENT}
        cache = state.get("cache")
        complete = True
        if command != "submodule":
            complete = pin_promisor_remotes(real_git, location, pin_env, cache, session)
        if command in ("clone", "submodule"):
            # Filtered submodules are partial clones of their own. NUL-separated:
            # a path may hold a line feed.
            listing = subprocess.run(
                [real_git, *location, "submodule", "--quiet", "foreach", "--recursive", 'printf "%s\\0" "$PWD"'],
                env=pin_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            for path in filter(None, listing.stdout.decode(errors="surrogateescape").split("\0")):
                modules = {key: value for key, value in pin_env.items() if key not in REPOSITORY_ENVIRONMENT}
                complete = pin_promisor_remotes(real_git, ["-C", path], modules, cache, session) and complete
        if not complete:
            # Its lazy fetches would follow the rewrite with no upstream retry:
            # removing the rewrite sends them upstream.
            print(
                "Cachew could not pin a partial clone to upstream; caching is off for the rest of the job.",
                file=sys.stderr,
            )
            remove_cache_keys(real_git, session, load_state(session, environ), environ)
        return result

    if disabled:
        if state.get("managed"):
            # The disable could not remove every key (the file was locked, or a
            # concurrent disable is midway). Try again: Git's own lazy fetches
            # read the job file, not the upstream view, and would follow a
            # leftover rewrite to the failed cache with no retry.
            state = remove_cache_keys(real_git, session, state, environ)
        with UpstreamView(real_git, direct, session, state) as upstream:
            return done(subprocess.call([real_git, *arguments], env=upstream))
    if shell_alias:
        # Git runs a shell alias's commands itself, past this wrapper, so they get
        # no cache attempt and no retry: its reads and pushes all go upstream.
        with UpstreamView(real_git, direct, session, state) as upstream:
            return subprocess.call([real_git, *arguments], env=upstream)

    if command in NETWORK_COMMANDS and direct_network_operation(real_git, arguments, index, environ):
        with UpstreamView(real_git, direct, session, state) as upstream:
            return done(subprocess.call([real_git, *arguments], env=upstream))
    if command not in ("clone", "fetch", "ls-remote"):
        return subprocess.call([real_git, *arguments], env=direct)
    remotes = fetch_remotes(real_git, arguments, index, reading) if command == "fetch" else None
    if remotes is None and not reaches_cache(real_git, arguments, index, environ, state.get("cache")):
        return subprocess.call([real_git, *arguments], env=direct)
    if remotes is not None and not all(
        reaches_cache(real_git, arguments, index, environ, state.get("cache"), remote) for remote in remotes
    ):
        # One remote's failure could not be told from the cache's, so a fetch that
        # also reads elsewhere goes upstream once, without the cache rewrite.
        with UpstreamView(real_git, direct, session, state) as upstream:
            return done(subprocess.call([real_git, *arguments], env=upstream))
    if setting:
        print(f"Cachew is skipped for this operation: {setting} could weaken requests to the cache.", file=sys.stderr)
        with UpstreamView(real_git, direct, session, state) as upstream:
            return done(subprocess.call([real_git, *arguments], env=upstream))
    head = FetchHead(real_git, arguments, index, environ) if command == "fetch" else None
    result, errors = cache_attempt(real_git, arguments, environ, index)
    if result == 0:
        return done(result)
    target.reset()
    if head:
        # The retry fetches every remote again: an appending fetch would record
        # the refs the attempt already wrote a second time.
        head.restore()
    if result < 0:
        # Only the attempt's Git was killed (the wrapper's own stop raises out of
        # the attempt): the cache did not fail, so caching stays on.
        print("Cachew read was stopped by a signal; retrying this operation upstream once.", file=sys.stderr)
    elif result == 124 or transport_error(errors):
        state = remove_cache_keys(real_git, session, state, environ)
        print("Cachew is unavailable; removed the job's cache rewrite and retrying upstream once.", file=sys.stderr)
    else:
        print("Cachew read did not succeed; retrying this operation upstream once.", file=sys.stderr)
    with UpstreamView(real_git, direct, session, state) as upstream:
        return done(subprocess.call([real_git, *arguments], env=upstream))


def main():
    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    result = run(sys.argv[1:], dict(os.environ))
    return 128 - result if result < 0 else result


if __name__ == "__main__":
    sys.exit(main())
