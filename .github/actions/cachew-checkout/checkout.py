# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Complete checkout from cachew, leaving the repository actions/checkout would create.

Any failure removes everything this attempt created, so actions/checkout (which
runs only then) starts from the same empty target."""

import base64
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import random
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.parse
import uuid

ACTION = Path(__file__).resolve().parent
TRUSTED_CACHES = tuple(json.loads((ACTION / "trusted_caches.json").read_text())["origins"])
STAGE_PREFIX = ".cachew-checkout-"
# Git messages that mean the cache endpoint itself is unusable, as opposed to a
# per-repository or per-ref answer. Only these turn caching off for the job.
TRANSPORT_ERRORS = re.compile(
    rb"could not resolve|failed to connect|connection (?:refused|reset)|"
    rb"ssl certificate|ssl_connect|ssl_read|handshake|certificate verif|server certificate|rpc failed|early eof|remote end hung up|operation too slow|timed out|"
    rb"returned error: (?:3|5)\d\d",
    re.IGNORECASE,
)
# A 4xx on the pack request also prints "RPC failed" and "remote end hung up",
# but it is the cache answering about one repository.
CLIENT_ANSWER = re.compile(rb"(?:HTTP |returned error: )4\d\d", re.IGNORECASE)
# Transport failures that a retry cannot change: a name that does not resolve (off
# the CI VLAN), a redirect (never followed) and a certificate that does not verify.
PERMANENT_ERRORS = re.compile(
    rb"could not resolve|returned error: 3\d\d|ssl certificate|server certificate|certificate verif",
    re.IGNORECASE,
)


def transport_error(errors):
    return bool(TRANSPORT_ERRORS.search(errors)) and not CLIENT_ANSWER.search(errors)


def transient_error(errors):
    """A transport failure that a retry may get past."""
    return transport_error(errors) and not PERMANENT_ERRORS.search(errors)


# Input limits for cachew-health-timeout and cachew-fetch-timeout.
MAX_HEALTH_TIMEOUT_SECONDS = 30
MAX_FETCH_TIMEOUT_SECONDS = 600
DEFAULT_FETCH_TIMEOUT_SECONDS = 120
# The whole cache checkout (every fetch, the lazy blob fetches of a sparse
# checkout, and the local steps) must leave the upstream fallback room inside
# consumers' `timeout-minutes: 20`: a degraded cache that still moves above the
# low-speed limit is abandoned after this budget. Retries share this budget.
CHECKOUT_BUDGET_SECONDS = 300
# Waits before each retry of a transient cache failure (no answer, a timeout, a
# 5xx or a transport error mid-fetch). Each wait is lengthened by up to
# RETRY_JITTER of itself at random, so runners that failed together do not return
# together. Tests replace `pause` to skip the waits.
RETRY_DELAYS_SECONDS = (3, 6)
RETRY_JITTER = 0.5
pause = time.sleep
# Downstream reads through the wrapper have no step-level budget; this cap only
# guards against a transfer that trickles forever just above the low-speed limit.
HARD_DEADLINE_SECONDS = 3600
MAX_DEADLINE_SECONDS = 86400
MAX_SUBMODULE_DEPTH = 10
MAX_SUBMODULES = 100
SHA = re.compile(r"[0-9a-fA-F]{40}")


class Bypass(Exception):
    """Stop the cache attempt and proceed directly to upstream checkout. transport
    marks a failure of the cache endpoint; transient marks one a retry may get past."""

    def __init__(self, message, transport=False, transient=False):
        super().__init__(message)
        self.transport = transport
        self.transient = transient


class SecurityViolation(Bypass):
    """An input no supported configuration produces and that only makes sense as
    an attack: shell or option injection, path traversal, or sending the token
    somewhere untrusted. Unlike a Bypass there is no fallback: the action fails."""


def mask(value):
    """A GitHub workflow command that hides value wherever the log would show it."""
    if value:
        print("::add-mask::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), flush=True)


def mask_token(environ):
    """Hide the checkout token and the Authorization header made from it, before
    any Git process can print either."""
    token = checkout_input(environ, "CACHEW_TOKEN")
    if token:
        mask(token)
        mask(base64.b64encode(f"x-access-token:{token}".encode()).decode())


def report_violation(error):
    """A GitHub error annotation. The message is one of this module's fixed
    strings, never the offending value, which could itself carry workflow commands."""
    print(
        "::error title=cachew-checkout: possible security compromise attempt::"
        f"{error}. The checkout was stopped without falling back to the standard checkout. "
        "If this input is intentional, it is not supported; otherwise treat the workflow run as hostile."
    )


def loopback_http_allowed(environ):
    """Plain HTTP is only for the loopback fixtures in tests/. action.yml clears
    this test-only variable, so no workflow input can select plain HTTP."""
    return environ.get("CACHEW_TEST_ALLOW_LOOPBACK_HTTP") == "1"


# A server origin ends up in Git configuration keys, cache URL paths and the
# shell command of `git submodule foreach`, so its host is limited to DNS names
# and IP literals: no quotes, spaces or shell metacharacters.
HOST = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::(?P<port>[0-9]{1,5}))?"
)
# Never handed to child processes as such. Git still receives the token as an
# Authorization header in command-scoped configuration (GIT_CONFIG_VALUE_n),
# which every process Git starts inherits: the job's hooks, filter drivers and
# core.fsmonitor command, and the hooks of its GIT_TEMPLATE_DIR. Those run as the
# runner user, which can read this process's environment anyway, so the header
# is masked in the log before any Git process starts (as actions/checkout does).
SECRET_ENVIRONMENT = ("CACHEW_TOKEN",)
# Git lets this variable override http.sslVerify, so it is dropped wherever a
# request can carry the token.
TLS_OVERRIDES = ("GIT_SSL_NO_VERIFY",)


def as_parsed(value):
    """A URL as every parser reads it: tabs and line breaks dropped, then C0
    controls and spaces trimmed from both ends."""
    return re.sub(r"[\t\n\r]", "", value).strip("".join(map(chr, range(0x21))))


# The authority of a URL as written: after the scheme and its slashes, up to the
# first character that ends it for any of Python, Git or a WHATWG parser.
AUTHORITY = re.compile(r"[^:/?#\\]*:[/\\]*([^/?#\\]*)")
METACHARACTER_FREE_HOST = re.compile(r"[A-Za-z0-9.:\[\]-]*")


def origin(value, environ):
    # First, whatever else the URL holds: the standard checkout this would fall
    # back to reads only the URL's origin, so a path, query, fragment, userinfo
    # or scheme that cachew does not support does not keep the host from its
    # `git submodule foreach` shell commands. The host is read as written, as
    # well as as parsed, so a URL Python's parser rejects (a malformed IPv6
    # literal, a character NFKC maps to a delimiter) is still refused. Tabs and
    # line breaks are dropped and the ends trimmed first, as every parser does.
    written = as_parsed(value)
    authority = AUTHORITY.match(written)
    hosts = [authority[1].rpartition("@")[2]] if authority else []
    try:
        url = urllib.parse.urlsplit(value)
    except ValueError:
        url = None
    else:
        hosts.append(url.netloc.rpartition("@")[2])
    if any(not METACHARACTER_FREE_HOST.fullmatch(host) for host in hosts):
        raise SecurityViolation("server URL host contains shell or configuration metacharacters")
    if url is None:
        raise Bypass("unsupported server URL")
    # Never weaken TLS for cachew or upstream outside the test fixtures.
    local_http = (
        loopback_http_allowed(environ) and url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    )
    if (
        (url.scheme != "https" and not local_http)
        or not url.netloc
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise Bypass("unsupported server URL")
    host = HOST.fullmatch(url.netloc)
    if not host:
        raise Bypass("unsupported server URL")
    # The origin as actions/checkout reads it (URL.origin): the host in lower case
    # and no default port, so `https://GitHub.com:443` is the same server, and a
    # trusted cache spelled that way is still the trusted cache.
    name, port = host["name"].lower(), host["port"]
    if port is not None:
        if int(port) > 65535:
            raise Bypass("unsupported server URL")
        if int(port) != (443 if url.scheme == "https" else 80):
            name = f"{name}:{int(port)}"
    return f"{url.scheme}://{name}"


# The whitespace JavaScript's String.prototype.trim strips, as core.getInput does.
# Python's str.strip() with no argument differs: it keeps U+FEFF and strips
# U+001C-U+001F and U+0085.
JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
# actions/checkout classifies a SHA `ref` after trimming only ASCII whitespace, so a
# branch named U+FEFF followed by 40 hex digits stays a branch.
ASCII_WHITESPACE = "\t\n\v\f\r "
ANY_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
PULL_REQUEST_REF = re.compile(r"refs/pull/[0-9]+/(?:head|merge)")


def sha_ref(value):
    """The commit a ref input names as actions/checkout reads it, or None."""
    exact = value.strip(ASCII_WHITESPACE)
    return exact if ANY_SHA.fullmatch(exact) else None


def checkout_input(environ, name, default=""):
    """An input as actions/checkout reads it (core.getInput): trimmed, the default when empty."""
    return environ.get(name, "").strip(JS_WHITESPACE) or default


def submodules_input(environ):
    """ "true", "recursive" or "false", folded as actions/checkout does: its
    toUpperCase() maps characters such as U+0131 to ASCII, which lower() does not."""
    value = checkout_input(environ, "CACHEW_SUBMODULES", "false").upper()
    return {"TRUE": "true", "RECURSIVE": "recursive"}.get(value, "false")


def checkout_flag(environ, name, default):
    """A boolean input as actions/checkout reads it: true only for TRUE in any case.
    default is the fallback in its input-helper (getInput(name) || default), which
    can differ from the action.yml default: an input that trims to nothing gets it."""
    return checkout_input(environ, name, default).upper() == "TRUE"


def unsafe_pr_checkout(environ, repository, ref, commit):
    """Whether actions/checkout (v7.0.1 unsafe-pr-checkout-helper) refuses this
    checkout: fork pull request code fetched into a pull_request_target or
    workflow_run workflow without allow-unsafe-pr-checkout."""
    event = environ.get("GITHUB_EVENT_NAME", "")
    allowed = checkout_flag(environ, "CACHEW_ALLOW_UNSAFE_PR_CHECKOUT", "false")
    if allowed or event not in ("pull_request_target", "workflow_run"):
        return False
    # As actions/checkout reads it: no payload without the file, and UTF-8
    # whatever the interpreter's locale. A payload that cannot be read or parsed
    # fails that checkout, so it is refused here: an empty payload would pass.
    path = environ.get("GITHUB_EVENT_PATH")
    payload = {}
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as stream:
                payload = json.loads(stream.read().decode("utf-8", "replace"))
        except (OSError, ValueError, RecursionError):
            return True

    def select(path):
        value = payload
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    base = select("repository.id")
    if not number(base):
        return False
    if event == "pull_request_target":
        head = "pull_request.head.repo"
        shas = [select("pull_request.head.sha"), select("pull_request.merge_commit_sha")]
    else:
        trigger = select("workflow_run.event")
        if not isinstance(trigger, str) or not trigger.startswith("pull_request"):
            return False
        head = "workflow_run.head_repository"
        shas = [select("workflow_run.head_commit.id")]
        # For a pull_request_target run, head_sha is the base branch's commit.
        if trigger != "pull_request_target":
            shas.append(select("workflow_run.head_sha"))
    head_id, head_name = select(f"{head}.id"), select(f"{head}.full_name")
    if not number(head_id) or head_id == base:
        return False
    shas = [sha.lower() for sha in shas if isinstance(sha, str) and sha]
    return (
        (isinstance(head_name, str) and repository.lower() == head_name.lower())
        or bool(PULL_REQUEST_REF.fullmatch(ref))
        or bool(commit and commit.lower() in shas)
    )


def relative_path(value):
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or ".." in parts or ".git" in parts or "\\" in value or any(ord(c) < 32 for c in value):
        raise SecurityViolation("repository or submodule path escapes its directory or contains control characters")
    if not value or "" in parts or "." in parts:
        raise Bypass("unsafe repository or submodule path")
    return path


def container_path(*parts):
    """Node's path.posix.join, which also collapses '..' segments."""
    return posixpath.normpath(posixpath.join("/github/workspace", *parts))


def apply_git_config(env, entries, append=False):
    """Command-scoped Git configuration through GIT_CONFIG_COUNT/KEY_n/VALUE_n."""
    base = int(env.get("GIT_CONFIG_COUNT") or 0) if append else 0
    for index, (key, value) in enumerate(entries, base):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(base + len(entries))


def duration(value, maximum):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= maximum:
        raise Bypass("unsupported cache timeout")
    return seconds


def checkout_deadline(environ):
    """The monotonic time at which the cache checkout, retries included, must end."""
    budget = duration(environ.get("CACHEW_CHECKOUT_BUDGET", str(CHECKOUT_BUDGET_SECONDS)), MAX_DEADLINE_SECONDS)
    return time.monotonic() + budget


def cache_candidates(value, upstream, environ):
    """Return the trusted cache origins to probe, in order. 'auto' tries every
    trusted origin; only the runner's own cluster answers with a valid
    certificate, and off-VLAN runners fail DNS immediately."""
    if (value or "auto").strip().lower() == "auto":
        return list(TRUSTED_CACHES)
    try:
        cache = origin(value, environ)
    except SecurityViolation:
        raise
    except Bypass:
        # A spelling cachew does not use (a path, userinfo, a trailing dot) is
        # only a fallback for a trusted cache; for any other HTTPS host it is still
        # an attempt to point the token elsewhere.
        if untrusted_https(value):
            raise SecurityViolation(
                "cachew-url is not a trusted cache origin and would have received the checkout token"
            )
        raise
    # upstream is None when the server URL cannot be read: the refusal check then
    # only asks whether the cache could be the test fixture.
    loopback_fixture = (
        loopback_http_allowed(environ)
        and (upstream is None or upstream.startswith("http://"))
        and cache.startswith("http://")
    )
    if cache not in TRUSTED_CACHES and not loopback_fixture:
        raise SecurityViolation("cachew-url is not a trusted cache origin and would have received the checkout token")
    return [cache]


def untrusted_https(value):
    """Whether a URL cachew cannot read is an HTTPS URL whose host, read loosely
    (any case, without userinfo, port or a trailing dot), is no trusted cache's."""
    written = as_parsed(value)
    scheme = re.match(r"(?i)https:[/\\]*([^/?#\\]+)", written)
    if not scheme:
        return False
    host = scheme[1].rpartition("@")[2]
    host = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.partition(":")[0]
    trusted = {urllib.parse.urlsplit(cache).hostname for cache in TRUSTED_CACHES}
    return host.lower().rstrip(".") not in trusted


# curl exits that a retry cannot change, although curl reports HTTP status 000 for
# them like any unanswered request: 6 is a name that does not resolve; 51, 60 and
# 77 are a server certificate that does not verify (60; 51 before curl 7.62) or CA
# certificates that cannot be read.
PERMANENT_CURL_EXITS = frozenset({6, 51, 60, 77})


# The job's http.* settings that choose a route or what to trust: a proxy, the
# certificates to verify against, a client certificate. A runner that needs them
# to reach the server or the cache needs them in every request made here too.
# Never the ones that weaken a request (sslVerify, followRedirects) or add to
# it (extraHeader, cookieFile).
TRANSPORT_KEYS = (
    "proxy",
    "proxyauthmethod",
    "noproxy",
    "proxysslcainfo",
    "proxysslcert",
    "proxysslkey",
    "sslcainfo",
    "sslcapath",
    "sslcert",
    "sslkey",
    "sslbackend",
    "sslversion",
    "version",
)


def job_transport(git, environ, url):
    """The job's TRANSPORT_KEYS settings for a URL, as Git selects them
    (`http.<url>.*` over `http.*`), read outside any repository."""
    with tempfile.TemporaryDirectory(prefix="cachew-config-") as empty:
        env = {key: value for key, value in environ.items() if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG")}
        env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(empty)
        command = [git, "config", "--null", "--get-urlmatch", "http", url.rstrip("/") + "/"]
        try:
            result = subprocess.run(command, env=env, cwd=empty, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return {}
    settings = {}
    for entry in result.stdout.decode(errors="surrogateescape").split("\0"):
        key, separator, value = entry.partition("\n")
        if separator and key.lower().removeprefix("http.") in TRANSPORT_KEYS:
            settings[key.lower()] = value
    return settings


def curl_transport(transport, environ):
    """curl options for the route and trust settings Git would use: curl reads
    neither Git's configuration nor its GIT_SSL_* variables."""

    def setting(name, variable=None):
        value = environ.get(variable) if variable else None
        return transport.get(f"http.{name}") if value is None else value

    def path(value):
        home = environ.get("HOME")
        return home + value[1:] if home and value.startswith("~/") else value

    options = []
    proxy = setting("proxy")
    if proxy == "":
        # An empty http.proxy turns off a proxy from the environment.
        options += ["--noproxy", "*"]
    elif proxy:
        options += ["--proxy", proxy]
        if setting("noproxy"):
            options += ["--noproxy", setting("noproxy")]
    for name, variable, option in (
        ("sslcainfo", "GIT_SSL_CAINFO", "--cacert"),
        ("sslcapath", "GIT_SSL_CAPATH", "--capath"),
        ("sslcert", "GIT_SSL_CERT", "--cert"),
        ("sslkey", "GIT_SSL_KEY", "--key"),
    ):
        value = setting(name, variable)
        if value:
            options += [option, path(value)]
    return options


def liveness(cache, timeout, env, transport=None):
    """Classify a trusted cache origin's unauthenticated liveness probe: "ok" for a
    200, "retry" for a failure that may pass (no answer, a timeout or a 5xx), and
    "fail" for one that will not: a name that does not resolve (every runner off the
    CI VLAN), a certificate that does not verify, a redirect, a 4xx or any other answer."""
    # --disable ignores ~/.curlrc, which could otherwise add auth or retries.
    # The process deadline also bounds DNS resolution, unlike a socket timeout.
    if transport is None:
        transport = job_transport(env.get("CACHEW_REAL_GIT") or "git", env, cache)
    command = ["curl", "--disable", "--silent", "--fail", "--retry", "0", "--max-time", str(timeout)]
    options, proxy = curl_transport(transport, env), None
    if "--proxy" in options:
        # A proxy URL may hold a password: curl reads it from standard input, so
        # it is in no process's arguments.
        at = options.index("--proxy")
        proxy = options[at + 1].replace("\\", "\\\\").replace('"', '\\"')
        options[at : at + 2] = ["--config", "-"]
    command += options
    command += ["--output", os.devnull, "--write-out", "%{http_code}", cache + "/_liveness"]
    given = f'proxy = "{proxy}"\n'.encode(errors="surrogateescape") if proxy is not None else None
    try:
        health = subprocess.run(command, env=env, input=given, capture_output=True, timeout=timeout + 0.5)
    except subprocess.TimeoutExpired:
        return "retry"
    if health.returncode == 0 and health.stdout == b"200":
        return "ok"
    if health.returncode in PERMANENT_CURL_EXITS:
        return "fail"
    if health.stdout == b"000" or health.stdout[:1] == b"5":
        return "retry"
    return "fail"


def healthy(cache, timeout, env):
    """Whether a trusted cache origin answers its unauthenticated liveness probe.
    `env` is the job's: its Git configuration gives the proxy and certificates."""
    return liveness(cache, timeout, env) == "ok"


def repository_name(value):
    relative_path(value)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value, re.ASCII):
        raise Bypass("unsupported repository name")
    return value


def git_version_at_least(executable, minimum):
    try:
        output = subprocess.run(
            [executable, "version"], capture_output=True, timeout=10, env={"PATH": os.environ.get("PATH", "")}
        ).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return False
    version = git_version(output)
    return version is not None and version >= minimum


def git_version(output):
    """(major, minor) from `git version` output, or None."""
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else None


def partial_clone_filter(value):
    """A filter spec as Git and a cache URL can carry it unquoted."""
    if not re.fullmatch(r"[\w:=+.%-]*", value, re.ASCII):
        raise Bypass("unsupported partial-clone filter")
    return value


def fetch_depth(value):
    """Only the spellings int() and JavaScript's Number read alike: Number takes
    1e1 and 0x10 and rejects 1_0 and non-ASCII digits, which int() accepts."""
    if not re.fullmatch(r"\+?[0-9]+", value):
        raise Bypass("unsupported fetch depth")
    return int(value)


def ref_listing(output):
    """({name: sha}, {name: symref target}) from `git ls-remote` output.

    LF only: str.splitlines also splits on U+2028 and others a ref name may contain."""
    refs, symrefs = {}, {}
    for line in output.split("\n"):
        value, _, name = line.partition("\t")
        if value.startswith("ref: "):
            symrefs[name] = value[len("ref: ") :]
        elif SHA.fullmatch(value):
            refs[name] = value
    return refs, symrefs


def submodule_repository(child_url, url, upstream):
    """The owner/name a submodule URL from .gitmodules names on upstream, read
    relative to its superproject's url."""
    if child_url.startswith(("./", "../")):
        child_url = urllib.parse.urljoin(url + "/", child_url)
    if child_url.startswith("git@github.com:") and upstream == "https://github.com":
        child_url = "https://github.com/" + child_url[len("git@github.com:") :]
    if not child_url.startswith(upstream + "/"):
        raise Bypass("submodule uses another upstream")
    # Exactly owner/name: no query, fragment or encoded segments in the cache request path.
    child_path = child_url[len(upstream) + 1 :]
    if not re.fullmatch(r"[\w./-]*", child_path, re.ASCII):
        raise SecurityViolation("submodule URL carries a query, encoded segment or other request smuggling")
    return repository_name(child_path)


def submodule_config_files(listing):
    """Config files named by `git submodule foreach` running
    `git config --local --show-origin --name-only --get-regexp remote.origin.url`."""
    return re.findall(r"^file:([^\t]+)\tremote\.origin\.url", listing, re.M)


# tempfile.TemporaryDirectory's names: the prefix and eight random characters.
STAGE_NAME = re.compile(re.escape(STAGE_PREFIX) + r"[a-z0-9_]{8}")


def remove_stale_stages(target):
    """A cancelled earlier attempt can leave its private staging directory behind.
    Stages are removed only when they are all the target holds: a target with
    anything else goes to upstream untouched, so nothing of the caller's is lost."""
    if not target.is_dir():
        return
    entries = list(target.iterdir())
    if entries and all(STAGE_NAME.fullmatch(e.name) and e.is_dir() and not e.is_symlink() for e in entries):
        for entry in entries:
            shutil.rmtree(entry, ignore_errors=True)


def refuse_attack_inputs(environ):
    """The checks that have no fallback, before anything can choose the standard
    checkout: an unsupported mode (LFS, an SSH key), an old Git or a cache turned
    off earlier in the job would otherwise hand the same input to that checkout,
    which follows a symlinked path out of the workspace."""

    def check(read):
        try:
            read()
        except SecurityViolation:
            raise
        except (Bypass, KeyError, ValueError):
            pass

    def path():
        workspace = Path(environ["GITHUB_WORKSPACE"]).resolve()
        if not (workspace / checkout_input(environ, "CACHEW_PATH")).resolve().is_relative_to(workspace):
            raise SecurityViolation("checkout path is outside the workspace")

    def ref():
        if checkout_input(environ, "CACHEW_REF").startswith("-"):
            raise SecurityViolation("ref starts with '-' and would be read as a Git option")

    def cache():
        # Whatever the server URL: one cachew cannot use still leaves the cache
        # URL an attempt to send the token elsewhere, and must not skip this check.
        try:
            upstream = origin(
                checkout_input(environ, "CACHEW_GITHUB_SERVER_URL") or environ["GITHUB_SERVER_URL"], environ
            )
        except SecurityViolation:
            raise
        except (Bypass, KeyError, ValueError):
            upstream = None
        cache_candidates(checkout_input(environ, "CACHEW_CACHEW_URL", "auto"), upstream, environ)

    check(path)
    check(ref)
    check(lambda: repository_name(checkout_input(environ, "CACHEW_REPOSITORY") or environ["GITHUB_REPOSITORY"]))
    check(cache)


class Checkout:
    def __init__(self, environ, deadline=None):
        self.git_executable = environ.get("CACHEW_REAL_GIT", "git")
        for key in ("SSH_KEY", "SSH_KEY_SET", "LFS"):
            value = checkout_input(environ, f"CACHEW_{key}")
            if value and value.lower() != "false":
                raise Bypass("checkout mode uses upstream directly")
        # Required by actions/checkout, which fails without one: that checkout reports it.
        if not checkout_input(environ, "CACHEW_TOKEN"):
            raise Bypass("no token")
        # actions/checkout's getMultilineInput: trim the input, drop empty lines, trim each.
        sparse = checkout_input(environ, "CACHEW_SPARSE_CHECKOUT")
        self.sparse = [line.strip(JS_WHITESPACE) for line in sparse.split("\n") if line] if sparse else []
        self.sparse_cone = checkout_flag(environ, "CACHEW_SPARSE_CHECKOUT_CONE_MODE", "true")
        self.filter = checkout_input(environ, "CACHEW_FILTER")
        if not self.filter and self.sparse:
            # Standard checkout's implied filter for sparse checkouts.
            self.filter = "blob:none"
        partial_clone_filter(self.filter)
        # GIT_CONFIG_COUNT (2.31) carries the transport settings below and
        # GIT_CONFIG_GLOBAL (2.32) isolates them; older Git would silently ignore
        # them and use inherited helpers and rewrites.
        if not git_version_at_least(self.git_executable, (2, 32)):
            raise Bypass("Git 2.32 or newer is required")
        self.upstream = origin(
            checkout_input(environ, "CACHEW_GITHUB_SERVER_URL") or environ["GITHUB_SERVER_URL"], environ
        )
        self.caches = cache_candidates(checkout_input(environ, "CACHEW_CACHEW_URL", "auto"), self.upstream, environ)
        self.cache = None
        self.health_timeout = duration(environ["CACHEW_CACHEW_HEALTH_TIMEOUT"], MAX_HEALTH_TIMEOUT_SECONDS)
        # Seconds without progress before a cache transfer is abandoned.
        self.fetch_timeout = duration(environ["CACHEW_CACHEW_FETCH_TIMEOUT"], MAX_FETCH_TIMEOUT_SECONDS)
        self.deadline = deadline if deadline is not None else checkout_deadline(environ)
        repository = checkout_input(environ, "CACHEW_REPOSITORY") or environ["GITHUB_REPOSITORY"]
        repository_name(repository)
        self.url = f"{self.upstream}/{repository}"
        # The same ref and commit as actions/checkout's input helper.
        self.ref = checkout_input(environ, "CACHEW_REF")
        self.commit = ""
        workflow_repository = repository.lower() == environ["GITHUB_REPOSITORY"].lower()
        exact = sha_ref(environ.get("CACHEW_REF", ""))
        if not self.ref:
            if workflow_repository:
                self.ref = environ.get("GITHUB_REF", "")
                self.commit = environ.get("GITHUB_SHA", "")
                if self.commit and self.ref and not self.ref.startswith("refs/"):
                    self.ref = f"refs/heads/{self.ref}"
        elif exact:
            self.commit, self.ref = exact, ""
        # The default self-checkout always resolves to the event's trusted ref.
        defaulted = workflow_repository and not checkout_input(environ, "CACHEW_REF")
        if not defaulted and unsafe_pr_checkout(environ, repository, self.ref, self.commit):
            raise Bypass("fork pull request code needs allow-unsafe-pr-checkout")
        # SHA-256 repositories, like everything else unsupported, use the standard checkout.
        if self.commit and not SHA.fullmatch(self.commit):
            raise Bypass("unsupported checkout commit")
        self.commit = self.commit.lower()
        if self.ref.startswith("-"):
            raise SecurityViolation("ref starts with '-' and would be read as a Git option")
        if self.ref.startswith("+") or any(c.isspace() or c in ":*?[^~\\" for c in self.ref):
            raise Bypass("unsupported checkout ref")
        self.depth = fetch_depth(checkout_input(environ, "CACHEW_FETCH_DEPTH", "1"))
        self.tags = checkout_flag(environ, "CACHEW_FETCH_TAGS", "false")
        self.submodules = submodules_input(environ)
        if self.sparse and self.submodules in ("true", "recursive"):
            raise Bypass("sparse checkout with submodules uses upstream directly")
        self.persist = checkout_flag(environ, "CACHEW_PERSIST_CREDENTIALS", "false")
        self.safe_directory = checkout_flag(environ, "CACHEW_SET_SAFE_DIRECTORY", "true")
        self.runner_temp = environ.get("RUNNER_TEMP", "")
        if self.persist and not self.runner_temp:
            raise Bypass("RUNNER_TEMP is not defined")
        self.organization = None
        try:
            with open(environ["GITHUB_EVENT_PATH"]) as stream:
                owner_id = json.load(stream)["repository"]["owner"]["id"]
            if isinstance(owner_id, int) and not isinstance(owner_id, bool):
                self.organization = owner_id
        except (KeyError, TypeError, OSError, ValueError):
            pass
        # Unresolved, as actions/checkout writes them into persisted configuration.
        self.workspace_path = os.path.abspath(environ["GITHUB_WORKSPACE"])
        path = checkout_input(environ, "CACHEW_PATH")
        self.repository_path = os.path.abspath(os.path.join(self.workspace_path, path))
        workspace = Path(environ["GITHUB_WORKSPACE"]).resolve()
        target = workspace / path
        # Resolve existing ancestors before creating anything, including symlinks.
        self.target = target.resolve()
        if not self.target.is_relative_to(workspace):
            raise SecurityViolation("checkout path is outside the workspace")
        if self.target.is_dir() and not self.target.is_symlink():
            remove_stale_stages(self.target)
        if self.target.exists() and (not self.target.is_dir() or any(self.target.iterdir())):
            raise Bypass("existing checkout uses upstream directly")
        # Worktree commands see the caller's Git configuration, as actions/checkout's do,
        # but can never reach the network: a missing object fails the attempt instead.
        # GIT_CONFIG names the one file `git config` reads and writes when no
        # option names another, and no other Git command reads it: with it, the
        # settings of this checkout would land in that file.
        dropped = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", *SECRET_ENVIRONMENT, *TLS_OVERRIDES)
        self.local_env = {key: value for key, value in environ.items() if key not in dropped}
        self.local_env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_LFS_SKIP_SMUDGE="1")
        self.env = dict(environ)
        for key in list(self.env):
            if key.startswith(("GIT_CONFIG_", "GIT_TRACE")) or key in (*dropped, "GIT_CURL_VERBOSE"):
                del self.env[key]
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="/bin/false",
            GIT_LFS_SKIP_SMUDGE="1",
            # transport_error matches Git's English messages.
            LC_ALL="C",
            LANGUAGE="",
        )
        self.token = checkout_input(environ, "CACHEW_TOKEN")
        self.auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        self.upstream_env = dict(self.env)
        upstream_config = [
            ("credential.helper", ""),
            ("http.lowSpeedLimit", "1000"),
            ("http.lowSpeedTime", str(max(1, math.ceil(self.fetch_timeout)))),
        ]
        if self.token:
            upstream_config.append((f"http.{self.upstream}/.extraHeader", f"AUTHORIZATION: basic {self.auth}"))
        # This Git reads no configuration file, so the job's route to the server
        # (a proxy, a private certificate authority) is carried over by name.
        upstream_config += job_transport(self.git_executable, self.local_env, self.upstream).items()
        apply_git_config(self.upstream_env, upstream_config)
        self.cache_transport = {}
        self.modules = 0
        self.refs = {}
        self.listed = None
        self.want = ""
        self.checkout_target = None
        self.credentials = None

    def configure_transport(self):
        """Transport settings for the selected cache only. The token is never
        attached before a trusted origin has answered the liveness probe."""
        config = [
            ("http.followRedirects", "false"),
            # Also for lazy fetches, which read the caller's own configuration: there
            # a value scoped to the cache's URL would win over the unscoped one.
            (f"http.{self.cache}/.followRedirects", "false"),
            ("http.sslVerify", "true"),
            (f"http.{self.cache}/.sslVerify", "true"),
            ("credential.helper", ""),
            ("fetch.recurseSubmodules", "false"),
            ("http.lowSpeedLimit", "1000"),
            ("http.lowSpeedTime", str(max(1, math.ceil(self.fetch_timeout)))),
            # A sparse checkout's lazy fetches read the caller's configuration, whose
            # less specific extraHeader values (an unscoped http.extraHeader, say)
            # would otherwise go to the cache too. The empty value clears them.
            (f"http.{self.cache}/.extraHeader", ""),
        ]
        if self.token:
            config.append((f"http.{self.cache}/.extraHeader", f"AUTHORIZATION: basic {self.auth}"))
        # The job's route to the cache, scoped to it like everything above.
        for key, value in self.cache_transport.get(self.cache, {}).items():
            config.append((f"http.{self.cache}/.{key.removeprefix('http.')}", value))
        self.cache_config = config
        apply_git_config(self.env, config)

    def run_git(self, command, env, message, upstream=False, input=None, cwd=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Bypass("cache checkout time budget exhausted", transport=not upstream)
        with subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                output, errors = process.communicate(input=input, timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise Bypass(f"{message} exceeded the cache checkout time budget", transport=not upstream) from None
        if process.returncode:
            # Git's diagnostics may contain credentials or untrusted server text,
            # so they are only classified, never printed.
            transport = not upstream and transport_error(errors or b"")
            transient = not upstream and transient_error(errors or b"")
            raise Bypass(f"{message} failed", transport=transport, transient=transient)
        return output

    def protect(self, git, env, url):
        """Leave the cache unless Git, run as `git` with `env`, verifies the
        certificate and follows no redirect for `url`. Git applies the most
        specific http.<url>.* it reads, whatever its source, so a file may hold
        one that outranks cachew's own: the new repository's configuration,
        copied from a template, or the caller's files, which lazy fetches read."""
        for name, wanted in (("http.sslVerify", "true"), ("http.followRedirects", "false")):
            command = [*git, "config", "--type=bool", "--get-urlmatch", name, url]
            # A value that is not a boolean (followRedirects=initial) fails here.
            if self.run_git(command, env, "reading the cache's settings").decode().strip() != wanted:
                raise Bypass(f"the job's Git configuration sets {name} for the cache")

    def git(self, gitdir, *arguments, input=None):
        command = [self.git_executable, f"--git-dir={gitdir}", *arguments]
        return self.run_git(command, self.env, "cache Git operation", input=input)

    def local(self, *arguments, cwd=None):
        command = [self.git_executable, "-c", "protocol.allow=never", "-C", str(cwd or self.target), *arguments]
        return self.run_git(command, self.local_env, "local checkout")

    def lazy(self, *arguments):
        """A worktree command that may fetch missing blobs, as a sparse standard
        checkout does from upstream, but only from the selected cache. The
        persisted origin stays upstream."""
        env = dict(self.local_env)
        apply_git_config(env, self.cache_config, append=True)
        scheme = urllib.parse.urlsplit(self.cache).scheme
        command = [
            self.git_executable,
            "-c",
            "protocol.allow=never",
            "-c",
            f"protocol.{scheme}.allow=always",
            "-c",
            f"url.{self.cache_url(self.url)}.insteadOf={self.url}",
            "-C",
            str(self.target),
        ]
        self.protect(command, env, self.cache_url(self.url))
        return self.run_git([*command, *arguments], env, "sparse checkout")

    def ls_remote(self, *arguments):
        """The only upstream contact: names resolve there, objects come from the cache.

        Runs outside any repository. From the step's working directory, a checkout
        at the workspace root would add its persisted credentials (a second
        Authorization header, which GitHub rejects) and its own URL rewrites."""
        with tempfile.TemporaryDirectory(prefix="cachew-lookup-") as empty:
            env = dict(self.upstream_env, GIT_CEILING_DIRECTORIES=os.path.dirname(empty))
            output = self.run_git(
                [self.git_executable, "ls-remote", *arguments], env, "upstream ref lookup", upstream=True, cwd=empty
            )
        return ref_listing(output.decode())

    def lookup(self, ref, *options):
        if self.listed is not None and ref.startswith(("refs/heads/", "refs/tags/")):
            sha = self.listed.get(ref)
        else:
            sha = self.ls_remote("--refs", *options, self.url, ref)[0].get(ref)
        if not sha:
            raise Bypass("ref not found upstream")
        return sha

    def resolve(self):
        """The local refs and checkout target that actions/checkout's fetch and
        getCheckoutInfo produce for the same input, keyed by object name."""
        ref, commit = self.ref, self.commit
        if self.depth == 0 or self.tags:
            # Standard checkout fetches every tag (and, for full history, every
            # branch) by refspec. Their names resolve upstream too: the cache's
            # advertisement can lag a tag pushed minutes ago.
            self.listed = self.ls_remote("--refs", "--heads", "--tags", self.url)[0]
            for name, sha in self.listed.items():
                if name.startswith("refs/tags/"):
                    self.refs[name] = sha
                elif self.depth == 0:
                    self.refs[f"refs/remotes/origin/{name[len('refs/heads/'):]}"] = sha
        if not ref and not commit:
            refs, symrefs = self.ls_remote("--symref", self.url, "HEAD")
            ref = symrefs.get("HEAD", "")
            if not ref.startswith("refs/heads/") or "HEAD" not in refs:
                raise Bypass("default branch not found upstream")
            self.ref, commit = ref, refs["HEAD"]
        # As actions/checkout's ref helper classifies it: by the uppercased ref,
        # which is not the lowercased one for every character (U+017F uppercases to S).
        upper = ref.upper()
        if upper.startswith("REFS/HEADS/"):
            branch = ref[len("refs/heads/") :]
            commit = commit or self.lookup(ref, "--heads")
            self.refs[f"refs/remotes/origin/{branch}"] = commit
            self.want, self.checkout_target = commit, (branch, f"refs/remotes/origin/{branch}")
        elif upper.startswith("REFS/PULL/"):
            local = f"refs/remotes/pull/{ref[len('refs/pull/'):]}"
            self.refs[local] = commit or self.lookup(ref)
            self.want, self.checkout_target = self.refs[local], (local, None)
        elif upper.startswith("REFS/TAGS/"):
            # Tags are fetched by name; the commit only verifies them.
            self.refs[ref] = self.lookup(ref, "--tags")
            self.want, self.checkout_target = self.refs[ref], (ref, None)
        elif upper.startswith("REFS/"):
            if commit:
                self.want, self.checkout_target = commit, (commit, None)
            else:
                self.refs[ref] = self.lookup(ref)
                self.want, self.checkout_target = self.refs[ref], (ref, None)
        elif ref:
            # The refspecs refs/heads/<ref>* and refs/tags/<ref>*, then a branch before a tag.
            listed = (
                self.listed if self.listed is not None else self.ls_remote("--refs", "--heads", "--tags", self.url)[0]
            )
            for name, sha in listed.items():
                if name.startswith(f"refs/heads/{ref}"):
                    self.refs[f"refs/remotes/origin/{name[len('refs/heads/'):]}"] = sha
                elif name.startswith(f"refs/tags/{ref}") and (not self.tags or name == f"refs/tags/{ref}"):
                    self.refs[name] = sha
            if f"refs/remotes/origin/{ref}" in self.refs:
                self.want = self.refs[f"refs/remotes/origin/{ref}"]
                self.checkout_target = (ref, f"refs/remotes/origin/{ref}")
            elif f"refs/tags/{ref}" in self.refs:
                self.want, self.checkout_target = self.refs[f"refs/tags/{ref}"], (f"refs/tags/{ref}", None)
            else:
                raise Bypass("branch or tag not found upstream")
        else:
            self.want, self.checkout_target = commit, (commit, None)
        self.commit = commit

    def cache_url(self, url):
        return f"{self.cache}/git/{urllib.parse.urlsplit(self.upstream).netloc}/{url[len(self.upstream) + 1:]}"

    def seed_main(self, gitdir):
        self.git(gitdir, "init", "--bare", str(gitdir))
        self.git(gitdir, "remote", "add", "origin", self.url)
        cache_url = self.cache_url(self.url)
        self.protect([self.git_executable, f"--git-dir={gitdir}"], self.env, cache_url)
        options = ["--force", "--no-recurse-submodules", "--no-tags"]
        if self.depth:
            options.append(f"--depth={self.depth}")
        # Explicit URL, never a persisted rewrite.
        if self.filter:
            if not self.sparse:
                # Standard checkout passes the filter to the main repository only. The
                # worktree checkout cannot fetch lazily, so seed the complete tip first;
                # the filtered fetch then adds only history. A sparse checkout instead
                # fetches just its own blobs from the cache (see lazy).
                self.git(
                    gitdir,
                    "fetch",
                    "--force",
                    "--no-recurse-submodules",
                    "--no-tags",
                    "--depth=1",
                    "--",
                    cache_url,
                    self.want,
                )
                if not self.depth:
                    options.append("--unshallow")
            # Later lazy fetches belong to upstream, exactly as standard checkout configures them.
            self.git(gitdir, "config", "remote.origin.promisor", "true")
            self.git(gitdir, "config", "remote.origin.partialclonefilter", self.filter)
            options.append(f"--filter={self.filter}")
        # Exact objects only: every name came from upstream, never from the cache's
        # advertisement, which may lag upstream.
        wants = sorted({self.want, *self.refs.values()})
        self.git(gitdir, "fetch", *options, "--stdin", cache_url, input="".join(f"{sha}\n" for sha in wants).encode())
        if self.filter:
            # A filtered fetch from a URL registers that URL as a promisor remote.
            section = f"remote.{cache_url}."
            if any(
                line.startswith(section)
                for line in self.git(gitdir, "config", "--local", "--list").decode().splitlines()
            ):
                self.git(gitdir, "config", "--remove-section", section[:-1])
        if self.refs:
            self.git(
                gitdir,
                "update-ref",
                "--stdin",
                input="".join(f"update {name} {sha}\n" for name, sha in self.refs.items()).encode(),
            )
        commit = self.git(gitdir, "rev-parse", f"{self.want}^{{commit}}").decode().strip()
        if self.commit and commit != self.commit:
            raise Bypass("ref does not point to the expected commit")
        self.git(gitdir, "config", "core.bare", "false")
        self.git(gitdir, "config", "core.logallrefupdates", "true")
        return commit

    def seed(self, gitdir, url, sha):
        self.git(gitdir, "init", "--bare", str(gitdir))
        self.git(gitdir, "remote", "add", "origin", url)
        self.protect([self.git_executable, f"--git-dir={gitdir}"], self.env, self.cache_url(url))
        options = ["--force", "--no-recurse-submodules"]
        if self.depth:
            options.append(f"--depth={self.depth}")
        # A raw SHA fetch does not auto-follow tags without a destination.
        self.git(gitdir, "fetch", *options, "--", self.cache_url(url), f"{sha}:refs/cachew/gitlink")
        commit = self.git(gitdir, "rev-parse", "refs/cachew/gitlink^{commit}").decode().strip()
        self.git(gitdir, "update-ref", "--no-deref", "HEAD", commit)
        self.git(gitdir, "update-ref", "-d", "refs/cachew/gitlink")
        self.git(gitdir, "config", "core.bare", "false")
        self.git(gitdir, "config", "core.logallrefupdates", "true")
        return commit

    def seed_children(self, gitdir, url, commit, level=0):
        if self.submodules not in ("true", "recursive") or (level and self.submodules != "recursive"):
            return
        blob = f"{commit}:.gitmodules"
        if not self.git(gitdir, "ls-tree", commit, "--", ".gitmodules"):
            return
        entries = self.git(gitdir, "config", f"--blob={blob}", "-z", "--list")
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            key, _, path = entry.decode().partition("\n")
            if not key.startswith("submodule.") or not key.endswith(".path"):
                continue
            name = key[len("submodule.") : -len(".path")]
            relative_path(name)
            relative_path(path)
            tree = self.git(gitdir, "ls-tree", "-z", commit, "--", path)
            if not tree:
                # A .gitmodules entry with nothing at its path (a removed
                # submodule left in the file): git submodule update skips it.
                continue
            metadata, tree_path = tree.rstrip(b"\0").split(b"\t", 1)
            mode, kind, sha = metadata.split()
            if mode != b"160000" or kind != b"commit" or tree_path.decode() != path:
                raise Bypass("unsupported submodule gitlink")
            child_url = self.git(gitdir, "config", f"--blob={blob}", "--get", f"submodule.{name}.url").decode().strip()
            child_url = f"{self.upstream}/{submodule_repository(child_url, url, self.upstream)}"
            self.modules += 1
            if level >= MAX_SUBMODULE_DEPTH:
                raise Bypass(f"submodule nesting deeper than {MAX_SUBMODULE_DEPTH}")
            if self.modules > MAX_SUBMODULES:
                raise Bypass(f"more than {MAX_SUBMODULES} submodules")
            child_dir = gitdir / "modules" / name
            if child_dir.exists() or any(
                (parent / "config").is_file()
                for parent in child_dir.parents
                if parent.is_relative_to(gitdir / "modules")
            ):
                raise Bypass("overlapping submodule names")
            child_commit = self.seed(child_dir, child_url, sha.decode())
            self.seed_children(child_dir, child_url, child_commit, level + 1)

    def probe(self):
        transient = False
        for cache in self.caches:
            # The probes count against the checkout budget like every Git command.
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise Bypass("cache checkout time budget exhausted", transport=True)
            if cache not in self.cache_transport:
                self.cache_transport[cache] = job_transport(self.git_executable, self.local_env, cache)
            state = liveness(cache, min(self.health_timeout, remaining), self.env, self.cache_transport[cache])
            if state == "ok":
                self.cache = cache
                self.configure_transport()
                return
            transient = transient or state == "retry"
        raise Bypass("cache liveness failed", transport=transient, transient=transient)

    def persist_credentials(self):
        """actions/checkout's persisted token layout: a RUNNER_TEMP credentials file
        included only for this repository's Git directories, host and container paths."""
        self.credentials = os.path.join(self.runner_temp, f"git-credentials-{uuid.uuid4()}.config")
        placeholder = "AUTHORIZATION: basic ***"
        # The token never appears in a command line.
        self.local("config", "--file", self.credentials, f"http.{self.upstream}/.extraheader", placeholder)
        content = Path(self.credentials).read_text()
        if content.count(placeholder) != 1:
            raise Bypass("unable to write checkout credentials")
        Path(self.credentials).write_text(content.replace(placeholder, f"AUTHORIZATION: basic {self.auth}"))
        container_credentials = f"/github/runner_temp/{os.path.basename(self.credentials)}"
        gitdir = f"{self.repository_path}/.git"
        relative = os.path.relpath(self.repository_path, self.workspace_path)
        container_gitdir = container_path(relative, ".git")
        for key, value in (
            (f"includeIf.gitdir:{gitdir}.path", self.credentials),
            (f"includeIf.gitdir:{gitdir}/worktrees/*.path", self.credentials),
            (f"includeIf.gitdir:{container_gitdir}.path", container_credentials),
            (f"includeIf.gitdir:{container_gitdir}/worktrees/*.path", container_credentials),
        ):
            self.local("config", "--local", key, value)

    def persist_submodule_credentials(self, recursive):
        container_credentials = f"/github/runner_temp/{os.path.basename(self.credentials)}"
        listed = self.local(
            "submodule",
            "foreach",
            *recursive,
            "git config --local --show-origin --name-only --get-regexp remote.origin.url",
        ).decode()
        for config in submodule_config_files(listed):
            gitdir = os.path.dirname(config)
            container = container_path(os.path.relpath(gitdir, self.workspace_path))
            self.local("config", "--file", config, f"includeIf.gitdir:{gitdir}.path", self.credentials)
            self.local("config", "--file", config, f"includeIf.gitdir:{container}.path", container_credentials)
        rewrites = [f"git@{urllib.parse.urlsplit(self.upstream).hostname}:"]
        if self.organization is not None:
            rewrites.append(f"org-{self.organization}@github.com:")
        for rewrite in rewrites:
            command = f"git config --local --add 'url.{self.upstream}/.insteadOf' '{rewrite}'"
            self.local("submodule", "foreach", *recursive, command)

    def finish(self):
        """actions/checkout's steps after its fetch, offline."""
        if self.safe_directory:
            # GIT_CONFIG_GLOBAL is the session's checkout.config; activation keeps the entry.
            self.local("config", "--global", "--add", "safe.directory", self.repository_path)
        self.local("config", "--local", "gc.auto", "0")
        if self.persist:
            self.persist_credentials()
        branch, start = self.checkout_target
        checkout = ("checkout", "--force", *(("-B", branch, start) if start else (branch,)))
        if self.sparse:
            if self.sparse_cone:
                self.lazy("sparse-checkout", "set", *self.sparse)
            else:
                self.local("config", "core.sparseCheckout", "true")
                relative = self.local("rev-parse", "--git-path", "info/sparse-checkout").decode().rstrip()
                with open(os.path.join(self.target, relative), "a") as stream:
                    stream.write("\n" + "\n".join(self.sparse) + "\n")
            self.lazy(*checkout)
        else:
            self.local("sparse-checkout", "disable")
            # Disabling leaves this behind even in a pristine repository; checkout removes it too.
            if self.local("config", "--local", "--get", "--default=", "extensions.worktreeConfig").strip():
                self.local("config", "--local", "--unset-all", "extensions.worktreeConfig")
            self.local(*checkout)
        if self.submodules in ("true", "recursive"):
            recursive = ["--recursive"] if self.submodules == "recursive" else []
            depth = [f"--depth={self.depth}"] if self.depth else []
            self.local("submodule", "sync", *recursive)
            self.local("submodule", "update", "--init", "--force", *depth, *recursive)
            self.local("submodule", "foreach", *recursive, "git config --local gc.auto 0")
            if self.persist:
                self.persist_submodule_credentials(recursive)
        return self.local("log", "-1", "--format=%H").decode().strip()

    def discard(self):
        """Leave the empty target actions/checkout would have found."""
        if self.credentials:
            Path(self.credentials).unlink(missing_ok=True)
        if not self.target.is_dir():
            return
        for entry in self.target.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    def run(self):
        self.target.mkdir(parents=True, exist_ok=True)
        try:
            self.resolve()
            # Stage on the target filesystem; publish only a complete object seed.
            with tempfile.TemporaryDirectory(prefix=STAGE_PREFIX, dir=self.target) as stage:
                gitdir = Path(stage) / ".git"
                commit = self.seed_main(gitdir)
                self.seed_children(gitdir, self.url, commit)
                gitdir.rename(self.target / ".git")
            return self.finish()
        except BaseException:
            self.discard()
            raise


def cache_checkout(environ, status):
    """One cache checkout, retried while its failure is transient and the checkout
    budget, shared by every attempt, has room for the wait. `status` records what
    the attempts learned for the outputs: the availability and the selected cache."""
    deadline = checkout_deadline(environ)
    retries = 0
    while True:
        attempt = Checkout(environ, deadline)
        status["available"] = "false"
        status["selected"] = ""
        try:
            attempt.probe()
            status["available"] = "true"
            status["selected"] = attempt.cache
            return attempt, attempt.run()
        except Bypass as error:
            if not error.transient or retries == len(RETRY_DELAYS_SECONDS):
                raise
            delay = RETRY_DELAYS_SECONDS[retries] * (1 + random.uniform(0, RETRY_JITTER))
            if time.monotonic() + delay >= deadline:
                raise
            retries += 1
            print(
                f"Cachew checkout failed ({error}); retry {retries} of {len(RETRY_DELAYS_SECONDS)} "
                f"in {delay:.1f} seconds."
            )
            pause(delay)


def main():
    checked_out = False
    outputs = {}
    # unknown: no probe happened, so activation probes for itself.
    # true: a trusted cache answered; later per-command failures fall back individually.
    # false: the cache endpoint itself failed, so later dependencies skip it.
    status = {"available": "unknown", "selected": ""}
    violation = False
    mask_token(os.environ)
    try:
        refuse_attack_inputs(os.environ)
        session = os.environ.get("CACHEW_SESSION_DIR")
        if session and (Path(session) / "cache-disabled").exists():
            status["available"] = "false"
            raise Bypass("cache disabled after an earlier failure")
        attempt, commit = cache_checkout(os.environ, status)
        checked_out = True
        # The same outputs as actions/checkout, which does not run.
        outputs = {"ref": attempt.ref, "commit": commit}
        print(f"Checked out {commit} from cachew; upstream checkout is not needed.")
    except SecurityViolation as error:
        # action.yml fails the job on this output instead of running the upstream checkout.
        violation = True
        report_violation(error)
    except Bypass as error:
        if error.transport:
            status["available"] = "false"
        print(f"Cachew checkout bypassed: {error}. Continuing with upstream checkout.")
    except Exception:
        # A local error, not the cache endpoint: leave the verdict to activation's probe.
        status["available"] = "unknown"
        print("Cachew checkout unavailable. Continuing immediately with upstream checkout.")
    if output := os.environ.get("GITHUB_OUTPUT"):
        outputs.update(
            {
                "checked-out": str(checked_out).lower(),
                "available": status["available"],
                "cache-url": status["selected"],
            }
        )
        outputs["security-violation"] = str(violation).lower()
        with open(output, "a") as stream:
            for key, value in outputs.items():
                # A random delimiter: a value can never end its own entry or start another.
                delimiter = f"cachew_{uuid.uuid4().hex}"
                stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


if __name__ == "__main__":
    main()
