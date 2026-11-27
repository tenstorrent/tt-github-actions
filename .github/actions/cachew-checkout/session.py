# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Session lifecycle for cachew checkout: `session.py prepare | activate`.

Files in the action-owned session directory under RUNNER_TEMP:
  global.config    The job's GIT_CONFIG_GLOBAL after activation. Starts as a
                   copy of the caller's global settings (inherited cachew
                   rewrites removed). The job's own `git config --global` writes
                   land here and are kept: cachew appends its own keys (state
                   "managed") as text, keeping auth headers out of process
                   arguments, and removes exactly those with `git config`.
  checkout.config  GIT_CONFIG_GLOBAL while actions/checkout runs: a flattened
                   copy of global.config without cachew's keys, so checkout sees
                   the job's settings while its temporary global writes
                   (submodule auth, its transient safe.directory) stay here.
  state.json       Session state, including cache-scoped auth headers.
  cache-disabled   Present once caching is off for the rest of the job.
  pins/            Promisor-remote pins, each included by its repository.
  pins.lock        Held while pins are written or removed.
  pins-off         Present once caching is off and its rewrite gone: the pins
                   are removed and no more are written.

Upstream authentication is never added to the global file: actions/checkout
already persists repository-scoped credentials, and a job-wide Authorization
header would override the credentials of every later upstream request.

There is no cleanup step. The runner empties RUNNER_TEMP, and with it the
session and actions/checkout's own credentials file, when the job completes.
Until then later steps and post steps keep a working GIT_CONFIG_GLOBAL.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required")

import base64  # noqa: E402
import contextlib  # noqa: E402
import fcntl  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import urllib.parse  # noqa: E402
import uuid  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout import (  # noqa: E402
    ACTION,
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    MAX_FETCH_TIMEOUT_SECONDS,
    MAX_HEALTH_TIMEOUT_SECONDS,
    SECRET_ENVIRONMENT,
    TLS_OVERRIDES,
    TRUSTED_CACHES,
    Bypass,
    SecurityViolation,
    cache_candidates,
    checkout_flag,
    checkout_input,
    duration,
    git_version_at_least,
    healthy,
    mask,
    origin,
    refuse_attack_inputs,
    report_violation,
    repository_name,
)

PERFORMANCE = [
    ("checkout.workers", "0"),
    ("checkout.thresholdForParallelism", "50"),
    ("submodule.fetchJobs", "0"),
    ("gc.auto", "0"),
]
# The upstream fallback aborts a fetch that averages under this rate for this
# long, so actions/checkout's own fetch retry (3 attempts) reconnects. Some
# connections to GitHub from CI networks lose packets for their whole life and
# crawl at ~140KB/s until the job times out; a new connection usually takes a
# healthy path. Healthy fetches run at several MB/s.
FALLBACK_LOW_SPEED_LIMIT = "524288"
FALLBACK_LOW_SPEED_SECONDS = "60"
MAX_INCLUDED_FILES = 30
# Git's own limit on nested includes; also ends an include cycle.
MAX_INCLUDE_DEPTH = 10


# The one file `git config` reads and writes when no option names another. No
# other Git command reads this variable, so a `git config` run with it does not
# see the settings the job's commands get.
LEGACY_CONFIG = "GIT_CONFIG"


def config_environment(environ):
    """The environment for Git commands that must see only the named file."""
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(("GIT_CONFIG_", "GIT_TRACE")) and key not in (LEGACY_CONFIG, *SECRET_ENVIRONMENT)
    }


def git(real_git, environ, *arguments, allowed=(0,)):
    # Configuration values are bytes: decoded here without newline translation,
    # which would turn a CR into LF, and with surrogateescape, which carries
    # bytes that are not UTF-8 through to the files written from them unchanged.
    result = subprocess.run([real_git, *arguments], env=config_environment(environ), capture_output=True)
    result.stdout = result.stdout.decode("utf-8", "surrogateescape")
    result.stderr = result.stderr.decode("utf-8", "surrogateescape")
    if result.returncode not in allowed:
        raise RuntimeError("Git configuration command failed")
    return result


def config_entries(real_git, environ, *selection):
    """Every (key, value) of `git config <selection> --list`. A key without a value
    has None: Git reads it as true for a boolean but as empty for a string, so it
    is written back bare."""
    entries = []
    for item in git(real_git, environ, "config", *selection, "--null", "--list").stdout.split("\0"):
        if item:
            key, separator, value = item.partition("\n")
            entries.append((key, value if separator else None))
    return entries


def get_all(real_git, environ, file, key, includes=False):
    if not Path(file).exists():
        return []
    selection = ["--file", str(file), "--includes" if includes else "--no-includes"]
    result = git(real_git, environ, "config", *selection, "--null", "--get-all", key, allowed=(0, 1))
    return result.stdout.split("\0")[:-1] if result.returncode == 0 else []


def quote(value):
    # Git has no escape for CR, and keeps a raw one inside quotes.
    for character, escaped in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"), ("\t", "\\t")):
        value = value.replace(character, escaped)
    return f'"{value}"'


def render(entries):
    """Entries in Git's config file syntax. Written as text, never as Git
    arguments, since values may be credentials."""
    text = []
    for key, value in entries:
        section, _, rest = key.partition(".")
        middle, _, name = rest.rpartition(".")
        if not section or not name:
            raise ValueError("Invalid Git configuration key")
        assignment = "" if value is None else f" = {quote(value)}"
        text.append(f"[{section}{f' {quote(middle)}' if middle else ''}]\n\t{name}{assignment}\n")
    return "".join(text)


def write_private(file, content):
    temporary = file.with_name(f"{file.name}.{uuid.uuid4().hex}")
    temporary.touch(mode=0o600)
    temporary.write_text(content, encoding="utf-8", errors="surrogateescape", newline="")
    os.replace(temporary, file)


def append_locked(file, text):
    """Append as `git config` writes: through <file>.lock, then an atomic
    replacement, so a concurrent `git config --global` neither interleaves with
    this write nor replaces the file with a copy read before it. Like Git, an
    existing lock fails the write instead of waiting."""
    lock = file.with_name(file.name + ".lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape", newline="") as stream:
            with open(file, encoding="utf-8", errors="surrogateescape", newline="") as current:
                content = current.read()
            stream.write(content + ("\n" if content and not content.endswith("\n") else "") + text)
        os.replace(lock, file)
    except BaseException:
        lock.unlink(missing_ok=True)
        raise


def file_command(variable, key, value):
    if file := os.environ.get(variable):
        delimiter = f"cachew_{uuid.uuid4().hex}"
        with open(file, "a") as stream:
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def output(key, value):
    file_command("GITHUB_OUTPUT", key, value)


def environment(key, value):
    file_command("GITHUB_ENV", key, value)


def find_git(environ):
    """The real Git on PATH, never an earlier session's wrapper."""
    for directory in environ.get("PATH", "").split(os.pathsep):
        folder = Path(directory)
        if not directory or (folder.name == "bin" and folder.parent.name.startswith("cachew-")):
            continue
        candidate = folder / "git"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise RuntimeError("Git unavailable")


class Settings:
    """The validated inputs; each is None when unusable, and any unusable input
    means no caching at all."""

    def __init__(self, environ):
        def parse(read):
            try:
                return read()
            except SecurityViolation:
                raise
            except (Bypass, KeyError, ValueError):
                return None

        self.upstream = parse(
            lambda: origin(checkout_input(environ, "CACHEW_GITHUB_SERVER_URL") or environ["GITHUB_SERVER_URL"], environ)
        )
        self.caches = self.upstream and parse(
            lambda: cache_candidates(checkout_input(environ, "CACHEW_CACHEW_URL", "auto"), self.upstream, environ)
        )
        self.repository = parse(
            lambda: repository_name(checkout_input(environ, "CACHEW_REPOSITORY") or environ["GITHUB_REPOSITORY"])
        )
        self.fetch_timeout = parse(lambda: duration(environ["CACHEW_CACHEW_FETCH_TIMEOUT"], MAX_FETCH_TIMEOUT_SECONDS))
        self.health_timeout = parse(
            lambda: duration(environ["CACHEW_CACHEW_HEALTH_TIMEOUT"], MAX_HEALTH_TIMEOUT_SECONDS)
        )
        self.valid = None not in (self.upstream, self.caches, self.repository, self.fetch_timeout, self.health_timeout)


def owned_session(environ):
    directory = Path(environ["CACHEW_SESSION_DIR"]).resolve()
    if directory.parent != Path(environ["RUNNER_TEMP"]).resolve() or not directory.name.startswith("cachew-"):
        raise SecurityViolation("CACHEW_SESSION_DIR was redirected outside the action's own RUNNER_TEMP session")
    return directory


def read_state(session):
    state = json.loads((session / "state.json").read_text())
    managed = state.get("managed", []) if isinstance(state, dict) else None
    if not isinstance(managed, list) or not all(
        isinstance(entry, list) and len(entry) == 2 and all(isinstance(part, str) for part in entry)
        for entry in managed
    ):
        raise ValueError("The cachew session state is not readable")
    return state


def load_state(session, environ):
    """The session state, or, when state.json is missing or corrupt, the state of
    a session whose caching is now off. The job file may still hold cachew's
    rewrite and tokens, and only the state records them: a read run as it is
    would go to the cache with no upstream retry, and a fallback copy of the
    file would send the standard checkout there too. Recovery finds cachew's
    keys in the file by their shape and takes them out."""
    try:
        return read_state(session)
    except (OSError, ValueError):
        pass
    real_git = environ.get("CACHEW_REAL_GIT") or find_git(environ)
    entries = config_entries(real_git, environ, "--file", str(session / "global.config"), "--no-includes")
    caches, upstreams = set(TRUSTED_CACHES), set()
    for key, value in entries:
        # Cachew's rewrite, whatever the cache: url.<cache>/git/<host>/.insteadOf <scheme>://<host>/.
        rewritten = subsection(key, "url.", ".insteadof")
        if rewritten is None or value is None:
            continue
        try:
            source, target = urllib.parse.urlsplit(rewritten), urllib.parse.urlsplit(value)
        except ValueError:
            continue
        if target.netloc and source.path == f"/git/{target.netloc}/" and value == f"{target.scheme}://{target.netloc}/":
            caches.add(f"{source.scheme}://{source.netloc}".lower())
            upstreams.add(value)
    managed = []
    for key, value in entries:
        if value is None:
            continue
        rewritten = subsection(key, "url.", ".insteadof")
        pushed = subsection(key, "url.", ".pushinsteadof")
        scoped = next(
            filter(
                None,
                (subsection(key, "http.", suffix) for suffix in (".extraheader", ".sslverify", ".followredirects")),
            ),
            None,
        )
        if (
            (rewritten is not None and value in upstreams and cache_host(rewritten, caches))
            or (pushed is not None and value in upstreams and pushed + "/" == value)
            or (scoped is not None and cache_host(scoped, caches))
        ):
            managed.append([key, value])
    print("::warning::The cachew session state could not be read; caching is off for the rest of the job.")
    state = {"active": False, "real_git": real_git, "managed": managed, "cache": None}
    state.update(tokens={}, defaults={}, paths={})
    return remove_cache_keys(real_git, session, state, environ)


def own_tokens(state):
    """The tokens and per-upstream defaults the cache receives: those of the
    checkout paths that still own one, a later checkout over an earlier one."""
    state["tokens"], state["defaults"] = {}, {}
    for owned in state["paths"].values():
        state["tokens"][owned["repository"]] = owned["header"]
        if owned["default"]:
            state["defaults"][owned["default"]] = owned["header"]


def write_state(session, state):
    write_private(session / "state.json", json.dumps(state))


def remove_managed(real_git, config, state, environ, rewrites_only=False):
    """Unset cachew's entries; return the keys still present (e.g. the file was
    locked for one of them).

    In an order that is safe wherever it stops. The cache rewrites go first:
    without them no request reaches the cache. Then the tokens. The settings
    that protect a cache request (verified TLS, no redirects, no inherited
    header) go last, and only once every rewrite and every token is gone: a
    rewrite left behind would otherwise follow the job's own http.sslVerify, and
    a token left behind still goes with any request that names the cache.

    A rewrite is removed by its own value, so a value the job gave the same key
    stays. A token never is, since it must not appear in a process's arguments:
    it is matched by the fixed start of the header cachew writes."""

    def unset(*match):
        # `git config` takes the file's lock, so concurrent job writes are safe.
        result = git(real_git, environ, "config", "--file", str(config), *match, allowed=range(256))
        return result.returncode in (0, 5)  # 5: the entry was already gone

    entries = list(dict.fromkeys((key, value) for key, value in state.get("managed", [])))
    remaining = set()
    for key, value in entries:
        if key.lower().endswith(".insteadof") and not unset("--fixed-value", "--unset-all", key, value):
            remaining.add(key)
    if rewrites_only:
        # A view without the cache rewrite needs no push guard either. Kept, it
        # would stop Git applying the job's own insteadOf (a credential) to push
        # URLs, and outrank a pushInsteadOf (to SSH) the job wrote after it.
        for key, value in entries if not remaining else ():
            if key.lower().endswith(".pushinsteadof") and not unset("--fixed-value", "--unset-all", key, value):
                remaining.add(key)
        return remaining
    headers = dict.fromkeys(key for key, value in entries if key.lower().endswith(".extraheader") and value)
    for key in headers:
        if not unset("--unset-all", key, "^AUTHORIZATION: basic "):
            remaining.add(key)
    exposed = bool(remaining)
    for key, value in entries:
        if key.lower().endswith(".insteadof") or (key in headers and value):
            continue
        if exposed or not unset("--fixed-value", "--unset-all", key, value):
            remaining.add(key)
    return remaining


def remove_cache_keys(real_git, session, state, environ, disable=True):
    """Take cachew's keys out of the job's global file, keeping everything the job
    wrote. With disable, caching stays off for the rest of the job; so it does
    when a key cannot be removed, and such keys stay recorded so every copy and
    upstream view keeps leaving them out."""
    marker = session / "cache-disabled"
    if disable:
        # First, so concurrent and later wrappers stop using the cache at once.
        marker.touch(mode=0o600, exist_ok=True)
    remaining = remove_managed(real_git, session / "global.config", state, environ)
    if remaining:
        marker.touch(mode=0o600, exist_ok=True)
    state = dict(state, active=False, managed=[entry for entry in state.get("managed", []) if entry[0] in remaining])
    if marker.exists():
        # Nothing reads a recorded token once caching is off for the job.
        state.update(tokens={}, defaults={}, paths={})
        if not any(key.lower().endswith(".insteadof") for key in remaining):
            remove_pins(session)
    write_state(session, state)
    return state


def session_pin(session, target, url):
    """Write a pin to a file of the session directory, for the owner only, and
    return its path (None if it cannot be written exactly). A credential in the
    target appears in no process's arguments, and the pin ends with the session:
    the runner empties RUNNER_TEMP when the job completes, and Git ignores an
    include whose file is gone, so a later job in a workspace the runner kept
    has no pin outranking its own rewrites."""

    def quoted(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    if not session or any(character in target + url for character in "\n\r\0"):
        return None
    pins = Path(session) / "pins"
    pins.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=pins, suffix=".config")
    with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape") as file:
        file.write(f"[url {quoted(target)}]\n\tinsteadOf = {quoted(url)}\n")
    return name


def git_true(value):
    """Whether Git reads a boolean setting as true: a key with no value (None),
    true/yes/on in any case, or a nonzero integer with an optional k/m/g unit.
    An empty value is false. A value Git cannot read counts as true here, so
    anything Git might take for a promisor remote is pinned."""
    if value is None:
        return True
    value = value.strip().lower()
    if value in ("false", "no", "off", ""):
        return False
    if value in ("true", "yes", "on"):
        return True
    number = re.fullmatch(r"[+-]?([0-9]+)[kmg]?", value)
    return not number or int(number[1]) != 0


def pin_promisor_remotes(real_git, location, environ, cache, session=None):
    """Git runs a partial clone's lazy fetches itself, not through the wrapper,
    so they would follow the cache rewrite with no upstream retry. Pin each
    promisor remote the rewrite reaches to the URL Git would use without cachew:
    a repository-local mapping of the whole URL is a longer insteadOf match.

    The pin is a file of the session directory that the repository includes
    (see session_pin), so a credential the job's own rewrite adds is not copied
    into the work tree, and the pin does not outlive the job.

    Returns False when a remote the rewrite reaches is left unpinned (no session
    directory, a pin file or include that cannot be written): the caller then
    turns caching off, which removes the rewrite those lazy fetches follow."""
    if not cache:
        return True
    prefix = cache.rstrip("/") + "/git/"
    environ = {key: value for key, value in environ.items() if key != LEGACY_CONFIG}

    def run(*arguments):
        result = subprocess.run(
            [real_git, *location, *arguments], env=environ, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return result.stdout.decode(errors="surrogateescape") if result.returncode == 0 else ""

    def without_cachew(url):
        """The URL as Git rewrites it when cachew's rewrite is absent: the job's
        own longest insteadOf for it. (Where the job's rewrite matches as much as
        cachew's, Git uses whichever is defined first; when that is the job's,
        the remote never reaches the cache and is not pinned.)"""
        best = ("", "")
        for entry in run("config", "--null", "--get-regexp", r"^url\..*\.insteadof$").split("\0"):
            key, _, value = entry.partition("\n")
            base = key[len("url.") : -len(".insteadof")]
            if value and url.startswith(value) and len(value) > len(best[1]) and not base.startswith(prefix):
                best = (base, value)
        return best[0] + url[len(best[1]) :]

    def pinned(url, target):
        try:
            pin = session_pin(session, target, url)
        except OSError:
            return False
        if not pin:
            return False
        read = run("config", "--file", pin, "--null", "--get-regexp", r"^url\..*\.insteadof$")
        if read == f"url.{target}.insteadof\n{url}\0":
            added = subprocess.run(
                [real_git, *location, "config", "--local", "--add", "include.path", pin],
                env=environ,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if added.returncode == 0:
                return True
        os.unlink(pin)
        return False

    def reached():
        """The URLs of the promisor remotes the cache rewrite reaches."""
        # NUL-separated: a remote's name may hold a space (or a tab), which the
        # line listing would read as the end of the key. Every file Git reads,
        # with includes: Git takes a promisor from any of them (an included file,
        # the worktree's configuration), and `--local` reads .git/config alone.
        seen = set()
        for entry in filter(None, run("config", "--null", "--get-regexp", r"^remote\..*\.promisor$").split("\0")):
            key, separator, value = entry.partition("\n")
            remote = key[len("remote.") : -len(".promisor")]
            # A filtered fetch from a URL records the URL itself as the promisor remote.
            url = run("config", "--get", f"remote.{remote}.url").strip() or remote
            if url in seen:
                continue
            if git_true(value if separator else None) and run("ls-remote", "--get-url", remote).startswith(prefix):
                seen.add(url)
                yield url

    if not session or not Path(session).is_dir():
        return not any(True for _ in reached())
    try:
        with pins_lock(session):
            # Once caching is off and its rewrite gone, no pin is needed, whatever
            # this command read before: one written now would never be removed.
            if (Path(session) / "pins-off").exists():
                return True
            # Every remote is tried, whichever fails.
            return all([pinned(url, without_cachew(url)) for url in reached()])
    except OSError:
        return False


WORKSPACE_SCAN_LIMIT = 100_000


def workspace_repositories(workspace):
    """The work trees under the workspace, its own included, and whether every
    directory was read within WORKSPACE_SCAN_LIMIT. Submodule work trees are
    found by their .git file. Symbolic links are not followed, and .git
    directories are not entered."""
    found, visited = [], 0
    for directory, subdirectories, files in os.walk(workspace):
        visited += 1
        if visited > WORKSPACE_SCAN_LIMIT:
            return found, False
        if ".git" in subdirectories or ".git" in files:
            found.append(Path(directory))
        subdirectories[:] = [name for name in subdirectories if name != ".git"]
    return found, True


def unpinned(real_git, session, state, environ):
    """Turn caching off after a partial clone could not be pinned: its lazy
    fetches would follow the rewrite with no upstream retry."""
    print("::warning::Cachew could not pin a partial clone to upstream; caching is off for the rest of the job.")
    return remove_cache_keys(real_git, session, state, environ)


@contextlib.contextmanager
def pins_lock(session):
    """Held while pins are written or removed, so that a command that read the
    configuration before caching was turned off cannot pin after the removal."""
    with open(Path(session) / "pins.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def remove_pins(session):
    """Remove the pins pin_promisor_remotes wrote, once no cache rewrite is left
    for them to outrank, and stop any more being written. Kept, a pin would send
    lazy fetches past any rewrite the job writes later (a mirror, a credential),
    which plain Git would apply. Each repository's include then names a file
    that is gone, which Git ignores, as it does once the job completes."""
    with pins_lock(session):
        (Path(session) / "pins-off").touch(mode=0o600, exist_ok=True)
        for pin in (Path(session) / "pins").glob("*.config"):
            pin.unlink(missing_ok=True)


def subsection(key, prefix, suffix):
    """The subsection of `<prefix><subsection><suffix>` (matched case-insensitively), or None."""
    lower = key.lower()
    if lower.startswith(prefix) and lower.endswith(suffix) and len(key) >= len(prefix) + len(suffix):
        return key[len(prefix) : len(key) - len(suffix)]
    return None


def cache_host(value, caches):
    try:
        url = urllib.parse.urlsplit(value)
        host = url.hostname or ""
    except ValueError:
        return False
    return f"{url.scheme}://{url.netloc}".lower() in caches or host == "cachew" or host.startswith("cachew.")


def include_target(value, source, environ):
    """The file an include path names, resolved as Git does: `~/` against HOME,
    a relative path against the directory of the file that includes it."""
    if value.startswith("~/"):
        return os.path.join(home_directory(environ), value[2:])
    return os.path.join(os.path.dirname(os.path.abspath(source)), value)


def conditional_include(key):
    """Whether a config key is an includeIf.<condition>.path directive."""
    return re.fullmatch(r"includeif\..*\.path", key.lower()) is not None


def anchored_condition(key, source, environ):
    """An includeIf key whose `gitdir:./` or `gitdir:~/` pattern no longer depends
    on which file holds it: `./` means the directory of that file."""
    relative = {"./": os.path.dirname(os.path.abspath(source)), "~/": home_directory(environ)}
    return re.sub(
        r"^(includeif\.gitdir(?:/i)?:)(\./|~/)", lambda m: f"{m[1]}{relative[m[2]]}/", key, flags=re.IGNORECASE
    )


def home_directory(environ):
    return environ.get("HOME") or os.path.expanduser("~")


def copy_globals(session, real_git, environ, caches):
    """Include directives for private copies of the caller's global files,
    following includes, without any inherited cachew rewrite or cache header."""
    selected = environ.get("GIT_CONFIG_GLOBAL")
    if selected == "":
        return ""
    home = home_directory(environ)
    xdg = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    originals = (
        [selected] if selected is not None else [os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")]
    )
    copies = {}

    def copy(source):
        source = os.path.abspath(source)
        if source in copies or not os.path.exists(source):
            return copies.get(source)
        if len(copies) > MAX_INCLUDED_FILES:
            raise RuntimeError("Too many included Git configuration files")
        destination = copies[source] = session / f"base-{len(copies)}.config"
        entries = []
        for key, value in config_entries(real_git, environ, "--file", source, "--no-includes"):
            rewritten = subsection(key, "url.", ".insteadof")
            if (
                rewritten is not None
                and urllib.parse.urlsplit(rewritten).path.startswith("/git/")
                and cache_host(rewritten, caches)
            ):
                continue
            header = subsection(key, "http.", ".extraheader")
            if header is not None and cache_host(header, caches):
                continue
            if value is not None and (key.lower() == "include.path" or conditional_include(key)):
                # A file that does not exist yet is read once a later step writes it,
                # as Git does: the directive keeps its absolute path.
                target = include_target(value, source, environ)
                key, value = anchored_condition(key, source, environ), str(copy(target) or target)
            entries.append((key, value))
        write_private(destination, render(entries))
        return destination

    return render(("include.path", str(copied)) for copied in map(copy, originals) if copied)


def flatten(real_git, environ, file, skip, depth=0):
    """Entries of a config file with unconditional includes inlined, except the
    (key, value) pairs in skip. Conditional includes stay directives, evaluated when Git reads
    the result; inlining them would apply one repository's settings everywhere.
    Their paths are made absolute, since the result lives in another directory."""
    if depth > MAX_INCLUDE_DEPTH:
        raise RuntimeError("Git configuration includes nest too deeply")
    entries = []
    for key, value in config_entries(real_git, environ, "--file", str(file), "--no-includes"):
        if value is None:
            # Bare, as Git reads it; an include without a path is Git's error to report.
            entries.append((key, value))
        elif key.lower() == "include.path":
            included = include_target(value, file, environ)
            entries += flatten(real_git, environ, included, skip, depth + 1) if os.path.exists(included) else []
        elif conditional_include(key):
            entries.append((anchored_condition(key, file, environ), include_target(value, file, environ)))
        elif (key.lower(), value) not in skip:
            entries.append((key, value))
    return entries


def write_checkout_config(session, state, settings, environ):
    """A flattened copy of global.config rather than an include of it: with
    submodules, actions/checkout runs `git config --global include.path
    <credentials>`, which replaces a single include.path (and fails on several).

    The low-speed abort is scoped to the checked-out repository: actions/checkout
    retries its fetch but not its submodule update, so a submodule must keep
    crawling rather than fail."""
    skip = {(key.lower(), value) for key, value in state["managed"]}
    entries = flatten(state["real_git"], environ, session / "global.config", skip)
    if settings.upstream and settings.repository:
        scope = f"http.{settings.upstream}/{settings.repository}"
        entries += [
            (f"{scope}.lowSpeedLimit", FALLBACK_LOW_SPEED_LIMIT),
            (f"{scope}.lowSpeedTime", FALLBACK_LOW_SPEED_SECONDS),
        ]
    # activate copies back only what checkout adds after this snapshot.
    state["checkout_safe_directories"] = sum(key.lower() == "safe.directory" for key, _ in entries)
    write_private(session / "checkout.config", render(entries))


def prepare(environ):
    # Here as well as in checkout.py, which does not run for a checkout that opts
    # out of cachew in a job that already has a session.
    refuse_attack_inputs(environ)
    settings = Settings(environ)
    if existing := environ.get("CACHEW_SESSION_DIR"):
        # A later checkout in the same job: take the cache rewrite out of the job's
        # file in place, keeping the job's writes, so this checkout talks to upstream.
        session = owned_session(environ)
        state = load_state(session, environ)
        state = remove_cache_keys(state["real_git"], session, state, environ, disable=False)
    else:
        # An unusable or untrusted cache or upstream origin means no session at all:
        # plain checkout, no token anywhere, no wrapper and no configuration change.
        if not settings.valid:
            raise RuntimeError("Unsupported cachew configuration")
        real_git = find_git(environ)
        # GIT_CONFIG_GLOBAL needs Git 2.32. Older Git would silently ignore the isolation.
        if not git_version_at_least(real_git, (2, 32)):
            raise RuntimeError("Git 2.32 or newer is required")
        session = Path(tempfile.mkdtemp(prefix="cachew-", dir=Path(environ["RUNNER_TEMP"]).resolve()))
        state = {"active": False, "real_git": real_git, "tokens": {}, "defaults": {}, "managed": [], "cache": None}
    try:
        if not existing:
            base = copy_globals(session, state["real_git"], environ, settings.caches)
            write_private(session / "global.config", base + render(PERFORMANCE))
        write_checkout_config(session, state, settings, environ)
        write_state(session, state)
    except BaseException:
        if not existing:
            shutil.rmtree(session, ignore_errors=True)
        raise
    output("ready", "true")
    environment("CACHEW_SESSION_DIR", str(session))
    environment("CACHEW_REAL_GIT", state["real_git"])
    # Later steps of the job run this interpreter, which the Git wrapper needs anyway.
    environment("CACHEW_PYTHON", interpreter())
    environment("GIT_CONFIG_GLOBAL", str(session / "checkout.config"))


def keep_safe_directories(real_git, environ, checkout_config, skip, global_config):
    """Copy the safe.directory entries checkout added (after the first skip) to the
    job's file. An empty value resets the list in Git, so on both sides only the
    entries after the last reset count."""
    added = []
    for value in get_all(real_git, environ, checkout_config, "safe.directory")[skip:]:
        if value == "":
            added.clear()
        if value not in added:
            added.append(value)
    present = get_all(real_git, environ, global_config, "safe.directory", includes=True)
    present = present[len(present) - present[::-1].index("") :] if "" in present else present
    for value in added:
        if value and value in present:
            continue
        git(real_git, environ, "config", "--file", str(global_config), "--add", "safe.directory", value)
        present = present + [value] if value else []


def auth_entries(url, header):
    # The empty value resets less specific headers, so exactly one is sent.
    return [(f"http.{url}/.extraHeader", ""), (f"http.{url}/.extraHeader", header)]


def interpreter():
    """The base interpreter's real file: a virtual environment the job removes
    later, whether its python is a link or a copy (`venv --copies`), must not
    take the session's Python with it. Only the standard library is used."""
    base = Path(sys.base_prefix, "bin", f"python{sys.version_info.major}.{sys.version_info.minor}")
    if sys.prefix != sys.base_prefix and os.access(base, os.X_OK):
        return os.path.realpath(base)
    return os.path.realpath(sys.executable)


def shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


# Frequent Git commands that name no remote. Git ignores an alias with the name
# of one of its commands, so the shim skips the alias lookup for these.
LOCAL_COMMANDS = (
    "add|am|apply|branch|cat-file|checkout|cherry-pick|commit|config|describe|diff|for-each-ref|hash-object|init|"
    "log|ls-files|ls-tree|merge|merge-base|rebase|reset|restore|rev-list|rev-parse|rm|show|show-ref|stash|status|"
    "switch|symbolic-ref|tag|update-index|update-ref|version|worktree|write-tree"
)


def shim(real_git, session):
    """Only commands that can reach a remote pay for the Python wrapper: the
    network commands, and aliases, which may expand to one. So does a command
    that may weaken a request to the cache: see the comments in the script."""
    wrapper = f'exec {shell_quote(interpreter())} {shell_quote(str(ACTION / "git_wrapper.py"))} "$@"'
    return f"""#!/bin/sh
export CACHEW_REAL_GIT={shell_quote(real_git)}
export CACHEW_SESSION_DIR={shell_quote(str(session))}
# Whether the wrapper must run the command: Git has an alias named $2 in either
# form (alias.<name>, or alias.<name>.command, whose name may hold any
# character; no $2, no lookup), or the job's settings may weaken a request to
# the cache. Read with the first $1 of the arguments that follow (the options
# before the command, which can select or define either).
wrapped() (
  # Only `git config` reads this variable, as the one file to read: with it,
  # these lookups would miss what the command itself gets from every file.
  unset GIT_CONFIG
  keep=$1 word=$2
  shift 2
  position=0 total=$#
  while [ "$position" -lt "$total" ]; do
    argument=$1
    shift
    [ "$position" -lt "$keep" ] && set -- "$@" "$argument"
    position=$((position + 1))
  done
  if [ -n "$word" ]; then
    [ -z "$("$CACHEW_REAL_GIT" "$@" config --get "alias.$word" 2>/dev/null)" ] || exit 0
    [ -z "$("$CACHEW_REAL_GIT" "$@" config --get "alias.$word.command" 2>/dev/null)" ] || exit 0
  fi
  # A setting that turns off certificate verification or allows redirects for
  # one URL (a value other than cachew's own). The wrapper reads which URL,
  # and keeps the token off a request to the cache that the setting weakens:
  # any command may make one (a lazy fetch, the git-lfs filter of a checkout).
  "$CACHEW_REAL_GIT" "$@" config --get-regexp '^http\\..*\\.(sslverify|followredirects)$' 2>/dev/null | {{
    while IFS= read -r setting; do
      case "$setting" in *.sslverify\\ true|*.followredirects\\ false) ;; *) exit 0 ;; esac
    done
    exit 1
  }}
)
# Git lets GIT_SSL_NO_VERIFY override http.sslVerify, for any command that
# reaches the cache: the wrapper then keeps the token off its requests. So it
# does under configuration given for one command, in the environment or with
# -c, which may hold an http.* setting or include a file that does.
[ -z "${{GIT_SSL_NO_VERIFY+set}}${{GIT_CONFIG_COUNT+set}}${{GIT_CONFIG_PARAMETERS+set}}" ] || {wrapper}
skip=0
options=0
for argument in "$@"; do
  if [ "$skip" = 1 ]; then
    case "$argument" in [Hh][Tt][Tt][Pp].*|[Ii][Nn][Cc][Ll][Uu][Dd][Ee]*) {wrapper} ;; esac
    skip=0; options=$((options + 1)); continue
  fi
  case "$argument" in
    --config-env=[Hh][Tt][Tt][Pp].*|--config-env=[Ii][Nn][Cc][Ll][Uu][Dd][Ee]*) {wrapper} ;;
    -C|-c|--git-dir|--work-tree|--namespace|--config-env|--super-prefix|--attr-source) skip=1 ;;
    -*) ;;
    clone|fetch|ls-remote|push|pull|submodule|remote) {wrapper} ;;
    {LOCAL_COMMANDS}) if wrapped "$options" "" "$@"; then {wrapper}; fi; break ;;
    *) if wrapped "$options" "$argument" "$@"; then {wrapper}; fi; break ;;
  esac
  options=$((options + 1))
done
exec "$CACHEW_REAL_GIT" "$@"
"""


def activate(environ):
    token = checkout_input(environ, "CACHEW_TOKEN")
    # No child process (Git, curl) inherits the token; it travels only in configuration files.
    environ = {key: value for key, value in environ.items() if key not in SECRET_ENVIRONMENT}
    session = owned_session(environ)
    state = load_state(session, environ)
    real_git = state["real_git"]
    global_config = session / "global.config"
    workspace = Path(environ.get("GITHUB_WORKSPACE", ".")).resolve()
    checkout = (workspace / checkout_input(environ, "CACHEW_PATH")).resolve()
    # This step also runs after a refused checkout: a path the checkout rejects
    # (outside the workspace) is never handed to Git here either.
    inside = checkout.is_relative_to(workspace)
    # Each checkout path owns the token its last checkout recorded, as
    # actions/checkout keeps a path's credentials in that path: it removes them
    # before every checkout into it, so a checkout with persist-credentials: false
    # leaves none for later steps to read. What the cache receives is derived
    # from the paths that remain; a repository's latest checkout wins.
    # Dropped and saved first: a failure below must not keep the earlier token.
    paths = state.setdefault("paths", {})
    if inside:
        paths.pop(str(checkout), None)
    # A repository removed since its checkout (a step deleted it, or a checkout
    # at a parent path cleaned it away) took its credentials with it.
    for path in [path for path in paths if not os.path.lexists(os.path.join(path, ".git"))]:
        paths.pop(path)
    own_tokens(state)
    write_state(session, state)
    view = dict(config_environment(environ), GIT_CONFIG_GLOBAL=str(global_config))
    # Then, whatever fails below: a rewrite an earlier activation left in a
    # locked file still redirects this checkout's lazy fetches.
    if inside and not pin_promisor_remotes(real_git, ["-C", str(checkout)], view, state.get("cache"), session):
        state = unpinned(real_git, session, state, environ)
    settings = Settings(environ)
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    mask(token)
    mask(encoded)
    if (
        inside
        and token
        and settings.upstream
        and settings.repository
        and checkout_flag(environ, "CACHEW_PERSIST_CREDENTIALS", "false")
        # This step also runs after a checkout that failed or was refused, which
        # leaves no credentials: neither does this.
        and checkout_flag(environ, "CACHEW_CHECKED_OUT", "false")
    ):
        # Cache-scoped only. Upstream credentials stay where actions/checkout put them.
        workflow = settings.repository.lower() == environ.get("GITHUB_REPOSITORY", "").lower()
        paths[str(checkout)] = {
            "repository": f"{settings.upstream}/{settings.repository}",
            "header": f"AUTHORIZATION: basic {encoded}",
            "default": settings.upstream if workflow else None,
        }
        own_tokens(state)
    keep_safe_directories(
        real_git, environ, session / "checkout.config", state["checkout_safe_directories"], global_config
    )
    disabled = session / "cache-disabled"
    available = environ.get("CACHEW_AVAILABLE") or "unknown"
    cache = environ.get("CACHEW_SELECTED_URL", "")
    if cache not in (settings.caches or []):
        cache = ""
    # Git lets GIT_SSL_NO_VERIFY override every http.sslVerify. The wrapper drops
    # it from its own cache attempts, but Git run past the wrapper (by its full
    # path, a lazy fetch) would send the token to the cache unverified.
    unverified = [name for name in TLS_OVERRIDES if name in environ]
    if unverified:
        print(f"::notice::Cachew is off for later steps: {unverified[0]} is set, and cache requests carry a token.")
    if not settings.valid or disabled.exists() or unverified:
        available = "false"
    elif available == "true" and not cache:
        available = "unknown"
    if available == "unknown":
        cache = next(
            (candidate for candidate in settings.caches if healthy(candidate, settings.health_timeout, environ)), ""
        )
        available = "true" if cache else "false"
    cache = cache if available == "true" else None
    state = remove_cache_keys(real_git, session, state, environ, disable=not cache)
    # While a locked file keeps the old rewrite, the session keeps recording the
    # cache it points to, so partial clones are still pinned against it.
    if not state["managed"]:
        state["cache"] = cache
        write_state(session, state)
    if not disabled.exists():
        host = urllib.parse.urlsplit(settings.upstream).netloc
        entries = [
            (f"url.{cache}/git/{host}/.insteadOf", settings.upstream + "/"),
            # Pushes never go to the cache, whichever way Git is reached (an alias,
            # a tool that runs Git by its full path): with a matching pushInsteadOf,
            # Git does not apply insteadOf to a push URL.
            (f"url.{settings.upstream}/.pushInsteadOf", settings.upstream + "/"),
            # Every request to the cache may carry a token: always verified TLS, and
            # never a redirect, which would repeat the header to another host.
            (f"http.{cache}/.sslVerify", "true"),
            (f"http.{cache}/.followRedirects", "false"),
            # Headers the job configured less specifically (an unscoped
            # http.extraHeader, say) are meant for upstream, not the cache: the
            # empty value clears them for cache URLs even when no token is recorded.
            (f"http.{cache}/.extraHeader", ""),
        ]
        # The workflow repository's token, for this upstream's repositories only:
        # scoped to the cache's /git/<host>/ paths, so a URL a later step takes
        # from repository content (a submodule naming the cache with another
        # host in its path) is requested without it.
        if default := state["defaults"].get(settings.upstream):
            entries += auth_entries(f"{cache}/git/{host}", default)
        for repository, header in state["tokens"].items():
            if repository.startswith(settings.upstream + "/"):
                cached = f"{cache}/git/{host}/{repository[len(settings.upstream) + 1 :]}"
                entries += auth_entries(cached, header)
                if not cached.endswith(".git"):
                    entries += auth_entries(cached + ".git", header)
        # Recorded first, so a partial failure is still removed by the next cleanup.
        state["managed"] = [list(entry) for entry in entries]
        write_state(session, state)
        append_locked(global_config, render(entries))
        state["active"] = True
        # Partial clones the wrapper never saw (an earlier step's filtered
        # checkout or clone, or one a self-hosted runner kept from an earlier
        # job) would now send their lazy fetches through the new rewrite.
        repositories, complete = workspace_repositories(workspace)
        if not complete:
            print(
                f"::warning::Cachew checked only the first {WORKSPACE_SCAN_LIMIT} directories of the workspace for "
                "partial clones to pin; lazy fetches of one it did not reach use the cache with no upstream retry."
            )
        if inside:
            repositories.insert(0, checkout)
        for repository in dict.fromkeys(repositories):
            if not pin_promisor_remotes(real_git, ["-C", str(repository)], view, cache, session):
                state = unpinned(real_git, session, state, environ)
                break
        write_state(session, state)
        wrapper = session / "bin"
        wrapper.mkdir(mode=0o700, exist_ok=True)
        write_private(wrapper / "git", shim(real_git, session))
        (wrapper / "git").chmod(0o700)
        with open(environ["GITHUB_PATH"], "a") as stream:
            stream.write(f"{wrapper}\n")
    environment("GIT_CONFIG_GLOBAL", str(global_config))
    environment("CACHEW_GIT_FETCH_TIMEOUT", str(settings.fetch_timeout or DEFAULT_FETCH_TIMEOUT_SECONDS))
    output("configured", str(state["active"]).lower())


def keep_job_file(session, environ):
    """After a later checkout's prepare failed: the standard checkout that runs
    instead writes to the global file (with submodules, an include of its
    credentials file, which would then apply to every later step). Give it a
    private copy of the job's file, built as checkout.config is: flattened, and
    without cachew's keys a locked file still holds. The restore step hands the
    job's file back to later steps, with the safe.directory entries checkout
    added, as configure would have."""
    global_config = session / "global.config"
    copy = session / f"fallback-{uuid.uuid4().hex}.config"
    try:
        state = load_state(session, environ)
        skip = {(key.lower(), value) for key, value in state["managed"]}
        entries = flatten(state["real_git"], environ, global_config, skip)
        write_private(copy, render(entries))
        output("restore-from", str(copy))
        output("restore-safe-directories", str(sum(key.lower() == "safe.directory" for key, _ in entries)))
    except Exception:
        # Whatever stops the flattening (an include loop, a missing state file),
        # checkout's writes must still stay out of the job's file.
        copy.touch(mode=0o600)
        shutil.copyfile(global_config, copy)
    environment("GIT_CONFIG_GLOBAL", str(copy))
    output("restore-global", str(global_config))


def restore(environ):
    """The restore step: copy back the safe.directory entries the standard
    checkout added to its copy, then give later steps the job's file. The file
    goes back even when the copy fails. Without a flattened copy (restore-from)
    there is no snapshot count to copy after, so nothing is copied."""
    global_config = Path(environ["CACHEW_RESTORE_GLOBAL"])
    try:
        if environ.get("CACHEW_RESTORE_FROM"):
            keep_safe_directories(
                load_state(owned_session(environ), environ)["real_git"],
                environ,
                Path(environ["CACHEW_RESTORE_FROM"]),
                int(environ["CACHEW_RESTORE_SAFE_DIRECTORIES"]),
                global_config,
            )
    except Exception as error:
        print(f"::warning::Cachew could not keep the standard checkout's safe.directory entries: {error}")
    environment("GIT_CONFIG_GLOBAL", str(global_config))


def main(phase):
    environ = dict(os.environ)
    if phase == "restore":
        restore(environ)
        return
    try:
        {"prepare": prepare, "activate": activate}[phase](environ)
    except Exception as error:
        if isinstance(error, SecurityViolation):
            # action.yml fails the job on this output; there is no fallback checkout.
            report_violation(error)
            output("security-violation", "true")
        # A session an earlier checkout activated must not rewrite later reads to
        # the cache once this step cannot keep it consistent.
        try:
            session = owned_session(environ)
            state = load_state(session, environ)
            remove_cache_keys(state["real_git"], session, state, environ)
            if phase == "activate":
                environment("GIT_CONFIG_GLOBAL", str(session / "global.config"))
        except Exception:
            pass
        if phase == "prepare":
            print("Cachew session could not start; using standard checkout only.")
            output("ready", "false")
            try:
                if environ.get("CACHEW_SESSION_DIR"):
                    keep_job_file(owned_session(environ), environ)
            except Exception:
                pass
        else:
            print("Cachew configuration unavailable; later Git operations use upstream.")
            output("configured", "false")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else "")
