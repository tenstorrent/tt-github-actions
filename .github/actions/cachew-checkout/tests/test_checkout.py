# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Real smart-HTTP repositories, fault injection, and the pinned checkout main/post bundle."""

import base64
from contextlib import redirect_stdout
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse


ACTION = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("checkout", ACTION / "checkout.py")
cachew_checkout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cachew_checkout)
CHECKOUT_JS = os.environ.get("CHECKOUT_ACTION_JS")
TOKEN = "cachew-test-token-do-not-persist"


def git(path, *arguments, env=None):
    executable = (env or os.environ).get("CACHEW_REAL_GIT", "git")
    return (
        subprocess.check_output([executable, "-C", str(path), *arguments], env=env, stderr=subprocess.PIPE)
        .decode()
        .strip()
    )


def runner_file(path):
    values = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            content = []
            for item in lines:
                if item == delimiter:
                    break
                content.append(item)
            values[key] = "\n".join(content)
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class GitServer:
    def __init__(self, project_root, upstream_host=None, real_git=None):
        self.root = project_root
        self.real_git = real_git or os.environ.get("CACHEW_REAL_GIT") or shutil.which("git")
        self.upstream_host = upstream_host
        self.requests = []
        self.health = 200
        self.health_delay = 0
        self.fail_repo = None
        self.fail_post = False
        self.fail_status = 503
        self.disconnect = False
        self.fetch_delay = 0
        self.request_delay = 0
        self.block_submodules = False
        self.required_auth = {}
        self.duplicate_auth = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.serve()

            def do_POST(self):
                self.serve()

            def serve(self):
                url = urllib.parse.urlsplit(self.path)
                owner.requests.append((self.command, url.path, self.headers.get("Authorization")))
                if len(self.headers.get_all("Authorization") or []) > 1:
                    # GitHub rejects a request carrying two Authorization headers.
                    owner.duplicate_auth.append(url.path)
                    self.send_error(400, 'Duplicate header: "Authorization"')
                    return
                if url.path == "/_liveness":
                    time.sleep(owner.health_delay)
                    self.send_response(owner.health)
                    if owner.health == 302:
                        self.send_header("Location", "/redirected-health")
                    self.end_headers()
                    return
                time.sleep(owner.request_delay)
                path = url.path
                if owner.upstream_host:
                    prefix = f"/git/{owner.upstream_host}/"
                    if not path.startswith(prefix):
                        self.send_error(404)
                        return
                    path = "/" + path[len(prefix) :]
                repo = path.split("/")[2]
                if (
                    repo in owner.required_auth
                    and self.headers.get("Authorization", "").lower() != owner.required_auth[repo].lower()
                ):
                    # Like GitHub, challenge so clients can answer with URL credentials.
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if (owner.fail_repo == repo and (not owner.fail_post or self.command == "POST")) or (
                    owner.block_submodules and repo != "main"
                ):
                    time.sleep(owner.fetch_delay)
                    if owner.disconnect:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                    else:
                        self.send_response(owner.fail_status)
                        if owner.fail_status == 302:
                            self.send_header("Location", "/redirected-fetch")
                        self.end_headers()
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                env = dict(
                    os.environ,
                    GIT_PROJECT_ROOT=str(owner.root),
                    GIT_HTTP_EXPORT_ALL="1",
                    PATH_INFO=path,
                    REQUEST_METHOD=self.command,
                    QUERY_STRING=url.query,
                    CONTENT_TYPE=self.headers.get("Content-Type", ""),
                    CONTENT_LENGTH=str(len(body)),
                    SERVER_PROTOCOL="HTTP/1.1",
                    REMOTE_USER="fixture",
                    GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_CONFIG_NOSYSTEM="1",
                    # GitHub and cachew both serve partial-clone filters.
                    GIT_CONFIG_COUNT="2",
                    GIT_CONFIG_KEY_0="uploadpack.allowFilter",
                    GIT_CONFIG_VALUE_0="true",
                    # Lazy fetches ask for unadvertised objects, which GitHub serves.
                    GIT_CONFIG_KEY_1="uploadpack.allowAnySHA1InWant",
                    GIT_CONFIG_VALUE_1="true",
                )
                response = subprocess.check_output(
                    [owner.real_git, "http-backend"], input=body, env=env, stderr=subprocess.PIPE
                )
                headers, payload = response.split(b"\r\n\r\n", 1)
                status = 200
                parsed = []
                for line in headers.decode().splitlines():
                    key, value = line.split(":", 1)
                    if key.lower() == "status":
                        status = int(value.strip().split()[0])
                    else:
                        parsed.append((key, value.strip()))
                self.send_response(status)
                for key, value in parsed:
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def discoveries(self):
        return [path for method, path, _ in self.requests if method == "GET" and path.endswith("/info/refs")]


class ForwardingProxy:
    """An HTTP proxy. `routes` maps an address nothing listens on to a real one, so
    that address is reachable only through the proxy, as on a runner that needs one."""

    def __init__(self):
        self.routes = {}
        self.seen = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def forward(self):
                url = urllib.parse.urlsplit(self.path)
                owner.seen.append(url.netloc)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                skipped = ("proxy-connection", "connection")
                headers = {key: value for key, value in self.headers.items() if key.lower() not in skipped}
                connection = http.client.HTTPConnection(owner.routes.get(url.netloc, url.netloc), timeout=20)
                connection.request(self.command, url.path + ("?" + url.query if url.query else ""), body, headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                connection.close()

            do_GET = do_POST = forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def unused_address(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return f"127.0.0.1:{probe.getsockname()[1]}"


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        inherited = dict(os.environ)
        real_git = inherited.get("CACHEW_REAL_GIT") or shutil.which("git")
        real_git = str(Path(real_git).resolve())
        for key in list(inherited):
            if key.startswith("CACHEW_"):
                del inherited[key]
        self.temp = tempfile.TemporaryDirectory(prefix="cachew-checkout-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        (binaries / "git").symlink_to(real_git)
        self.project_root = self.root / "repositories"
        self.project_root.mkdir()
        self.upstream = GitServer(self.project_root, real_git=real_git)
        self.addCleanup(self.upstream.close)
        self.cache = GitServer(self.project_root, urllib.parse.urlsplit(self.upstream.url).netloc, real_git=real_git)
        self.addCleanup(self.cache.close)
        self.env = dict(
            inherited,
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            PATH=str(binaries) + os.pathsep + inherited.get("PATH", ""),
            GIT_CONFIG_NOSYSTEM="1",
            GITHUB_WORKSPACE=str(self.workspace),
            GITHUB_SERVER_URL=self.upstream.url,
            GITHUB_REPOSITORY="org/main",
            GITHUB_REF="refs/heads/main",
            GITHUB_EVENT_NAME="push",
            RUNNER_TEMP=str(self.runtime),
            GITHUB_OUTPUT=str(self.root / "output"),
            GITHUB_STATE=str(self.root / "state"),
            GITHUB_ENV=str(self.root / "env"),
            GITHUB_PATH=str(self.root / "path"),
            GITHUB_EVENT_PATH=str(self.root / "event.json"),
            CACHEW_CACHEW_URL=self.cache.url,
            CACHEW_CACHEW_HEALTH_TIMEOUT="0.1",
            CACHEW_CACHEW_FETCH_TIMEOUT="5",
            CACHEW_REPOSITORY="org/main",
            CACHEW_REF="",
            CACHEW_PATH="checkout",
            CACHEW_PERSIST_CREDENTIALS="true",
            CACHEW_SET_SAFE_DIRECTORY="true",
            CACHEW_FETCH_DEPTH="1",
            CACHEW_FETCH_TAGS="false",
            CACHEW_SUBMODULES="recursive",
            CACHEW_TOKEN=TOKEN,
            CACHEW_REAL_GIT=real_git,
            # The fixtures are plain-HTTP loopback servers; production never allows this.
            CACHEW_TEST_ALLOW_LOOPBACK_HTTP="1",
        )
        self.env.pop("GIT_CONFIG_GLOBAL", None)
        (self.home / ".gitconfig").write_text("[user]\n\tname = Fixture\n\temail = fixture@example.com\n")
        (self.root / "event.json").write_text(
            json.dumps({"repository": {"default_branch": "main", "owner": {"id": 1234}}})
        )
        for name in ("output", "state", "env", "path"):
            (self.root / name).touch()
        grand = self.repository("grand")
        child = self.repository("child", [("nested-name", "deep/grand", "../grand", grand)])
        self.sha = self.repository("main", [("child-name", "deps/child", "../child", child)])
        self.env["GITHUB_SHA"] = self.sha
        self.target = self.workspace / "checkout"
        # Record the retry waits instead of sleeping through them.
        self.pauses = []
        pausing = patch.object(cachew_checkout, "pause", self.pauses.append)
        pausing.start()
        self.addCleanup(pausing.stop)

    def repository(self, name, children=()):
        path = self.project_root / "org" / name
        path.mkdir(parents=True)
        git(path, "init", "--initial-branch=main", env=self.env)
        (path / "content.txt").write_text(name)
        git(path, "add", "content.txt", env=self.env)
        git(path, "commit", "-m", "initial", env=self.env)
        if children:
            modules = []
            for module_name, module_path, url, sha in children:
                modules.append(f'[submodule "{module_name}"]\n\tpath = {module_path}\n\turl = {url}\n')
                git(path, "update-index", "--add", "--cacheinfo", f"160000,{sha},{module_path}", env=self.env)
            (path / ".gitmodules").write_text("".join(modules))
            git(path, "add", ".gitmodules", env=self.env)
            git(path, "commit", "-m", "submodules", env=self.env)
        git(path, "tag", "v1.0.0", env=self.env)
        return git(path, "rev-parse", "HEAD", env=self.env)

    def attempt(self):
        capture = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(capture):
            cachew_checkout.main()
        self.unmasked(capture.getvalue())
        return runner_file(self.root / "output")["checked-out"] == "true"

    def unmasked(self, output):
        """The output after the token masks, which must come first, before any Git
        process could print the token; the token appears nowhere else."""
        lines = output.splitlines()
        token = cachew_checkout.checkout_input(self.env, "CACHEW_TOKEN")
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        masks = [f"::add-mask::{token}", f"::add-mask::{header}"] if token else []
        self.assertEqual(lines[: len(masks)], masks)
        rest = "\n".join(lines[len(masks) :])
        self.assertNotIn(TOKEN, rest)
        self.assertNotIn(base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(), rest)
        return rest

    def assert_cache_attempts(self, repositories, attempts=1):
        paths = self.cache.discoveries()
        self.assertEqual([path.split("/")[-3] for path in paths], repositories * attempts)
        self.assertEqual(sum(path == "/_liveness" for _, path, _ in self.cache.requests), attempts)
        for _, path, auth in self.cache.requests:
            if path == "/_liveness":
                self.assertIsNone(auth)
            else:
                expected = "basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
                self.assertEqual(auth.lower(), expected.lower())

    def assert_no_cache_credentials(self):
        encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode())
        configs = [self.home / ".gitconfig"]
        if self.target.exists():
            configs.extend(self.target.rglob("config"))
        for path in configs:
            data = path.read_bytes()
            self.assertNotIn(TOKEN.encode(), data)
            self.assertNotIn(encoded, data)
            self.assertNotIn(self.cache.url.encode(), data)

    def checkout_inputs(self, **inputs):
        return {
            "repository": self.env["CACHEW_REPOSITORY"],
            "ref": self.env["CACHEW_REF"],
            "token": TOKEN,
            "path": "checkout",
            "fetch-depth": self.env["CACHEW_FETCH_DEPTH"],
            "fetch-tags": self.env["CACHEW_FETCH_TAGS"],
            "submodules": self.env["CACHEW_SUBMODULES"],
            "filter": self.env.get("CACHEW_FILTER", ""),
            "sparse-checkout": self.env.get("CACHEW_SPARSE_CHECKOUT", ""),
            "sparse-checkout-cone-mode": self.env.get("CACHEW_SPARSE_CHECKOUT_CONE_MODE", ""),
            "clean": "true",
            "persist-credentials": self.env["CACHEW_PERSIST_CREDENTIALS"],
            "set-safe-directory": self.env["CACHEW_SET_SAFE_DIRECTORY"],
            **inputs,
        }

    def official_checkout(self, **inputs):
        env = dict(self.env)
        for name, value in self.checkout_inputs(**inputs).items():
            env["INPUT_" + name.upper()] = value
        before = list(self.cache.requests)
        result = subprocess.run(
            ["node", CHECKOUT_JS], cwd=self.workspace, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cache.requests, before, "official checkout must never use the cache")
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        if self.env["CACHEW_SUBMODULES"] == "recursive":
            self.assertEqual((self.target / "deps/child/deep/grand/content.txt").read_text(), "grand")
        self.assertEqual(runner_file(self.root / "output")["commit"], self.sha)
        # Run the real action's registered post handler using its saved runner state.
        env.update({"STATE_" + key: value for key, value in runner_file(self.root / "state").items()})
        post = subprocess.run(
            ["node", CHECKOUT_JS], cwd=self.workspace, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(post.returncode, 0, post.stdout + post.stderr)
        self.assert_no_cache_credentials()
        for path in self.runtime.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN.encode(), path.read_bytes())
                self.assertNotIn(base64.b64encode(f"x-access-token:{TOKEN}".encode()), path.read_bytes())

    def snapshot(self, target, workspace, outputs):
        """Everything actions/checkout leaves behind that later steps can observe."""

        def normalize(text):
            for path in (str(workspace), os.path.realpath(workspace)):
                text = text.replace(path, "<workspace>")
            for path in (str(self.runtime), os.path.realpath(self.runtime)):
                text = text.replace(path, "<temp>")
            return re.sub(r"git-credentials-[0-9a-f-]{36}\.config", "git-credentials-<uuid>.config", text)

        def run(*arguments):
            return git(target, *arguments, env=self.env)

        def config(*arguments):
            return sorted(normalize(run(*arguments, "config", "--local", "--list")).splitlines())

        symbolic = subprocess.run(
            ["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], env=self.env, capture_output=True, text=True
        ).stdout.strip()
        includes = [line.split("=", 1)[1] for line in run("config", "--local", "--list").splitlines()]
        credentials = sorted({Path(path).read_text() for path in includes if path.startswith(str(self.runtime))})
        modules = {}
        if (target / ".gitmodules").exists() and self.env["CACHEW_SUBMODULES"] != "false":
            for line in run("submodule", "status", "--recursive").splitlines():
                if line.startswith("-"):
                    continue
                path = line.split()[1]
                modules[path] = {
                    # Checkout clones each submodule's default branch from upstream: a
                    # tracking branch and, when shallow, a single-branch refspec. Cachew
                    # fetches only the gitlink and keeps the default refspec.
                    "config": [
                        line for line in config("-C", path) if not line.startswith(("branch.", "remote.origin.fetch="))
                    ],
                    "shallow": run("-C", path, "rev-parse", "--is-shallow-repository"),
                    "head": run("-C", path, "rev-parse", "HEAD"),
                }
        sparse = {}
        for name in ("info/sparse-checkout", "config.worktree"):
            if (target / ".git" / name).exists():
                sparse[name] = (target / ".git" / name).read_text()
        files = sorted(
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(target).parts
        )
        return {
            "head": run("rev-parse", "HEAD"),
            "symbolic": symbolic,
            "refs": run("for-each-ref", "--format=%(refname) %(objectname)"),
            "shallow": sorted((target / ".git/shallow").read_text().split())
            if (target / ".git/shallow").exists()
            else [],
            "config": config(),
            "index": run("ls-files", "-s"),
            "skip-worktree": run("ls-files", "-t"),
            "sparse": sparse,
            "files": files,
            "status": run("status", "--porcelain", "--ignore-submodules=none"),
            "submodules": run("submodule", "status", "--recursive") if modules else "",
            "modules": modules,
            "credentials": [normalize(text) for text in credentials],
            "outputs": {key: outputs.get(key) for key in ("ref", "commit")},
        }

    def reference(self):
        """The pinned official checkout of the same inputs, in its own workspace."""
        workspace = self.root / "reference"
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir()
        output = self.root / "reference-output"
        output.write_text("")
        env = dict(self.env, GITHUB_WORKSPACE=str(workspace), GITHUB_OUTPUT=str(output))
        env["GITHUB_STATE"] = str(self.root / "reference-state")
        (self.root / "reference-state").write_text("")
        for name, value in self.checkout_inputs().items():
            env["INPUT_" + name.upper()] = value
        before = list(self.cache.requests)
        result = subprocess.run(
            ["node", CHECKOUT_JS], cwd=workspace, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cache.requests, before)
        return self.snapshot(workspace / "checkout", workspace, runner_file(output))

    def assert_matches_checkout(self, upstream_lookups):
        (self.root / "output").write_text("")
        self.upstream.requests.clear()
        self.assertTrue(self.attempt())
        self.assertEqual(self.upstream.discoveries(), upstream_lookups)
        ours = self.snapshot(self.target, self.workspace, self.output())
        self.assertEqual(ours, self.reference())
        self.assert_no_cache_credentials()
        return ours

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_clone_matches_pinned_checkout(self):
        source = self.project_root / "org/main"
        git(source, "update-ref", "refs/pull/7/merge", self.sha)
        old = self.sha
        self.update_main()
        self.add_layout()
        git(source, "tag", "-a", "-m", "annotated", "v2.0.0", env=self.env)
        cases = {
            "push event SHA": dict(CACHEW_REF=""),
            "unqualified branch with tags": dict(CACHEW_REF="main", CACHEW_FETCH_TAGS="true"),
            "unqualified tag": dict(CACHEW_REF="v1.0.0", CACHEW_FETCH_TAGS="false"),
            "qualified branch, full history": dict(CACHEW_REF="refs/heads/main", CACHEW_FETCH_DEPTH="0"),
            "annotated tag event": dict(CACHEW_REF="", GITHUB_REF="refs/tags/v2.0.0"),
            "lightweight tag, full history": dict(CACHEW_REF="refs/tags/v1.0.0", CACHEW_FETCH_DEPTH="0"),
            "explicit SHA, direct submodules, no credentials": dict(
                CACHEW_REF=old, CACHEW_SUBMODULES="true", CACHEW_PERSIST_CREDENTIALS="false"
            ),
            "pull request merge ref": dict(CACHEW_REF="", GITHUB_REF="refs/pull/7/merge", GITHUB_SHA=old),
            # actions/checkout trims every input (core.getInput).
            "inputs with surrounding whitespace": dict(
                CACHEW_REF="", CACHEW_SUBMODULES=" recursive\n", CACHEW_PATH="checkout\n"
            ),
            # Empty after the trim: actions/checkout then reads the flag as false,
            # not as action.yml's default.
            "whitespace-only persist-credentials": dict(CACHEW_REF="", CACHEW_PERSIST_CREDENTIALS=" \n"),
            "filtered history": dict(CACHEW_REF="", CACHEW_FILTER="tree:0"),
            "filtered full history": dict(CACHEW_REF="main", CACHEW_FILTER="blob:none", CACHEW_FETCH_DEPTH="0"),
            "sparse cone": dict(
                CACHEW_REF="", CACHEW_SUBMODULES="false", CACHEW_SPARSE_CHECKOUT=" .github\n\n  src/deep  \n"
            ),
            "sparse non-cone by branch name": dict(
                CACHEW_REF="main",
                CACHEW_SUBMODULES="false",
                CACHEW_SPARSE_CHECKOUT="src/b.txt\n/content.txt",
                CACHEW_SPARSE_CHECKOUT_CONE_MODE="false",
            ),
            "sparse, explicit filter, full history": dict(
                CACHEW_REF="",
                CACHEW_SUBMODULES="false",
                CACHEW_SPARSE_CHECKOUT=".github",
                CACHEW_FILTER="tree:0",
                CACHEW_FETCH_DEPTH="0",
            ),
        }
        names = ("main", "v1.0.0", "refs/heads/main", "refs/tags/v1.0.0")
        base = dict(self.env)
        for case, values in cases.items():
            with self.subTest(case=case):
                self.env = {**base, "CACHEW_FETCH_DEPTH": "500", "CACHEW_FETCH_TAGS": "false", **values}
                shutil.rmtree(self.target, ignore_errors=True)
                self.cache.requests.clear()
                by_name = (
                    self.env["CACHEW_REF"] in names
                    or (not self.env["CACHEW_REF"] and self.env["GITHUB_REF"].startswith("refs/tags/"))
                    or self.env["CACHEW_FETCH_DEPTH"] == "0"
                    or self.env["CACHEW_FETCH_TAGS"] == "true"
                )
                # Names (including every branch and tag a refspec would fetch) cost one
                # upstream ls-remote; anything else never contacts upstream.
                self.assert_matches_checkout(["/org/main/info/refs"] if by_name else [])

    def test_filtered_clone_is_offline_beyond_the_cache(self):
        self.update_main()
        self.env.update(CACHEW_REF="", CACHEW_FILTER="tree:0")
        self.upstream.fail_repo = "main"
        self.assertTrue(self.attempt())
        self.assertEqual((self.target / "content.txt").read_text(), "updated main")
        self.assertEqual((self.target / "deps/child/deep/grand/content.txt").read_text(), "grand")
        self.assertEqual(self.upstream.requests, [])

    def test_the_legacy_git_config_variable_does_not_redirect_the_checkouts_settings(self):
        # GIT_CONFIG names the one file `git config` reads and writes when no
        # option names another; no other Git command reads it. The settings of
        # a checkout belong in its repository, as actions/checkout writes them.
        legacy = self.root / "legacy.config"
        legacy.write_text("")
        self.env.update(
            GIT_CONFIG=str(legacy),
            CACHEW_SUBMODULES="false",
            CACHEW_SPARSE_CHECKOUT="/content.txt",
            CACHEW_SPARSE_CHECKOUT_CONE_MODE="false",
        )
        self.assertTrue(self.attempt())
        self.assertEqual(legacy.read_text(), "")
        self.assertEqual(git(self.target, "config", "--local", "core.sparseCheckout"), "true")
        self.assertEqual(git(self.target, "config", "--local", "core.bare"), "false")

    def test_success_checks_out_recursively_from_the_cache_only(self):
        self.assertTrue(self.attempt())
        self.assert_cache_attempts(["main", "child", "grand"])
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(git(self.target, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(git(self.target, "status", "--porcelain", "--ignore-submodules=none"), "")
        self.assertEqual((self.target / "deps/child/deep/grand/content.txt").read_text(), "grand")
        self.assertEqual(self.output()["commit"], self.sha)
        self.assertEqual(self.output()["ref"], "refs/heads/main")
        self.assertTrue((self.target / ".git/modules/child-name/modules/nested-name/objects").is_dir())
        self.assertEqual(git(self.target, "remote", "get-url", "origin"), self.upstream.url + "/org/main")
        self.assertEqual(
            git(self.target, "config", "--get", "remote.origin.fetch"), "+refs/heads/*:refs/remotes/origin/*"
        )
        self.assertEqual(
            git(self.target, "--git-dir=.git/modules/child-name", "config", "--get", "remote.origin.fetch"),
            "+refs/heads/*:refs/remotes/origin/*",
        )
        self.assert_no_cache_credentials()

    def test_submodules_auto_follow_tags_independently_of_main_fetch_tags(self):
        child = self.project_root / "org/child"
        git(child, "checkout", "-b", "unrelated", env=self.env)
        (child / "other.txt").write_text("unrelated tagged object")
        git(child, "add", "other.txt", env=self.env)
        git(child, "commit", "-m", "unrelated", env=self.env)
        git(child, "tag", "unrelated-tag", env=self.env)
        git(child, "branch", "-f", "main", "HEAD", env=self.env)
        git(child, "checkout", "main", env=self.env)
        for setting in ("false", "true"):
            with self.subTest(main_fetch_tags=setting):
                self.env["CACHEW_FETCH_TAGS"] = setting
                self.assertTrue(self.attempt())
                self.assertEqual(git(self.target, "--git-dir=.git/modules/child-name", "tag", "--list"), "v1.0.0")
                self.assertEqual(git(self.target, "tag", "--list"), "v1.0.0" if setting == "true" else "")
                self.assert_cache_attempts(["main", "child", "grand"])
                shutil.rmtree(self.target)
                self.cache.requests.clear()

    def test_depth_zero_seeds_other_branches_and_tags_in_the_one_main_fetch(self):
        source = self.project_root / "org/main"
        git(source, "checkout", "-b", "another-branch", env=self.env)
        (source / "other.txt").write_text("unique branch object")
        git(source, "add", "other.txt", env=self.env)
        git(source, "commit", "-m", "other branch", env=self.env)
        other = git(source, "rev-parse", "HEAD")
        git(source, "tag", "other-tag", env=self.env)
        git(source, "checkout", "main", env=self.env)
        self.env.update(CACHEW_FETCH_DEPTH="0", CACHEW_FETCH_TAGS="false")
        self.assertTrue(self.attempt())
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(git(self.target, "rev-parse", "refs/remotes/origin/another-branch"), other)
        self.assertEqual(git(self.target, "rev-parse", "refs/tags/other-tag"), other)
        self.assert_cache_attempts(["main", "child", "grand"])

    def test_branches_and_tags_come_from_upstream_names_not_the_cache_advertisement(self):
        # The cache serves its own (possibly stale) copy; upstream has a newer tag and
        # branch, and the cache still advertises a branch upstream has deleted.
        cache_root = self.root / "cache-repositories"
        shutil.copytree(self.project_root, cache_root, symlinks=True)
        self.cache.root = cache_root
        stale = cache_root / "org/main"
        git(stale, "branch", "deleted-upstream", env=self.env)
        source = self.project_root / "org/main"
        git(source, "tag", "-a", "-m", "release", "v9.9.9", env=self.env)
        git(source, "branch", "new-branch", env=self.env)
        # The cache can still serve the new objects (pull-through), just not the names.
        for name in ("refs/tags/v9.9.9", "refs/heads/new-branch"):
            sha = git(source, "rev-parse", name)
            git(stale, "fetch", str(source), f"+{name}:refs/hidden/{name.rsplit('/', 1)[-1]}", env=self.env)
            self.assertEqual(git(stale, "rev-parse", f"refs/hidden/{name.rsplit('/', 1)[-1]}"), sha)
        for depth, tags in (("0", "false"), ("1", "true")):
            with self.subTest(depth=depth, tags=tags):
                self.env.update(CACHEW_REF=self.sha, CACHEW_FETCH_DEPTH=depth, CACHEW_FETCH_TAGS=tags)
                self.upstream.requests.clear()
                self.assertTrue(self.attempt())
                self.assertEqual(self.upstream.discoveries(), ["/org/main/info/refs"])
                self.assertEqual(
                    git(self.target, "rev-parse", "refs/tags/v9.9.9"), git(source, "rev-parse", "refs/tags/v9.9.9")
                )
                branches = git(self.target, "for-each-ref", "--format=%(refname)", "refs/remotes/").splitlines()
                self.assertNotIn("refs/remotes/origin/deleted-upstream", branches)
                if depth == "0":
                    self.assertIn("refs/remotes/origin/new-branch", branches)
                shutil.rmtree(self.target)

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_health_failure_retries_then_uses_upstream(self):
        self.cache.health = 503
        self.assertFalse(self.attempt())
        self.assert_cache_attempts([], attempts=3)
        self.assertEqual(len(self.pauses), 2)
        self.assertFalse(self.target.exists())
        self.official_checkout()

    def test_health_redirect_is_not_followed(self):
        self.cache.health = 302
        self.assertFalse(self.attempt())
        self.assertEqual([p for _, p, _ in self.cache.requests], ["/_liveness"])

    def test_retry_waits_grow_and_stay_within_jitter(self):
        self.cache.health = 503
        self.assertFalse(self.attempt())
        self.assertEqual(len(self.pauses), 2)
        self.assertTrue(3 <= self.pauses[0] <= 4.5, self.pauses)
        self.assertTrue(6 <= self.pauses[1] <= 9, self.pauses)
        self.assertEqual(self.output()["available"], "false")

    def test_retry_uses_the_cache_once_it_recovers(self):
        self.cache.health = 503

        def recover(delay):
            self.pauses.append(delay)
            self.cache.health = 200

        with patch.object(cachew_checkout, "pause", recover):
            self.assertTrue(self.attempt())
        self.assertEqual(len(self.pauses), 1)
        self.assertEqual([path.split("/")[-3] for path in self.cache.discoveries()], ["main", "child", "grand"])
        self.assertEqual(sum(path == "/_liveness" for _, path, _ in self.cache.requests), 2)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(self.output()["available"], "true")

    def test_permanent_probe_failure_is_not_retried(self):
        with patch.object(cachew_checkout, "liveness", return_value="fail") as probe:
            self.assertFalse(self.attempt())
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.pauses, [])
        self.assertEqual(self.output()["available"], "false")

    def test_retries_stop_when_the_shared_budget_has_no_room(self):
        # The first wait (at most 4.5 seconds) fits in 6 seconds; the second (at least 6) does not.
        self.cache.health = 503
        self.env["CACHEW_CHECKOUT_BUDGET"] = "6"
        self.assertFalse(self.attempt())
        self.assertEqual(len(self.pauses), 1)

    def test_liveness_classifies_probe_outcomes(self):
        cases = [
            (0, b"200", "ok"),
            (0, b"204", "fail"),
            (0, b"302", "fail"),
            (22, b"404", "fail"),
            (22, b"503", "retry"),
            (7, b"000", "retry"),
            (28, b"000", "retry"),
            (6, b"000", "fail"),
            (60, b"000", "fail"),
            (51, b"000", "fail"),
            (77, b"000", "fail"),
            (35, b"000", "retry"),
        ]
        for code, status, expected in cases:
            with self.subTest(code=code, status=status):
                result = subprocess.CompletedProcess([], code, stdout=status, stderr=b"")
                with patch.object(cachew_checkout.subprocess, "run", return_value=result):
                    self.assertEqual(cachew_checkout.liveness("https://cachew.example", 1, {}), expected)
        with patch.object(cachew_checkout.subprocess, "run", side_effect=subprocess.TimeoutExpired("curl", 1)):
            self.assertEqual(cachew_checkout.liveness("https://cachew.example", 1, {}), "retry")

    def test_a_proxy_password_stays_out_of_the_probes_command_line(self):
        # Other users of the runner can read a process's arguments.
        seen = []

        def run(command, **options):
            seen.append((command, options.get("input")))
            return subprocess.CompletedProcess(command, 0, b"200", b"")

        transport = {"http.proxy": 'http://user:pa"ss@proxy.example:3128'}
        with patch.object(cachew_checkout.subprocess, "run", side_effect=run):
            self.assertEqual(cachew_checkout.liveness("https://cache.example", 5, {}, transport), "ok")
        (command, given), = seen
        self.assertNotIn("ss@", " ".join(command))
        self.assertEqual(command[command.index("--config") + 1], "-")
        self.assertEqual(given, b'proxy = "http://user:pa\\"ss@proxy.example:3128"\n')

    def test_curl_configuration_cannot_add_health_auth_or_retry(self):
        (self.home / ".curlrc").write_text('header = "Authorization: unsafe-header"\nretry = 3\nlocation\n')
        self.cache.health = 503
        self.assertFalse(self.attempt())
        # Exactly one probe per attempt: curl's own retry stays off.
        self.assert_cache_attempts([], attempts=3)

    def test_git_auth_failure_and_redirect_are_not_retried(self):
        self.cache.fail_repo = "main"
        for status in (401, 302):
            with self.subTest(status=status):
                self.cache.requests.clear()
                self.cache.fail_status = status
                self.assertFalse(self.attempt())
                self.assert_cache_attempts(["main"])
                self.assertEqual(len(self.cache.requests), 2)
                self.assertEqual(self.pauses, [])
                self.assertEqual(self.upstream.requests, [])

    def test_only_http_200_is_live(self):
        self.cache.health = 204
        self.assertFalse(self.attempt())
        self.assert_cache_attempts([])

    def test_connection_refusal_is_retried_then_uses_upstream(self):
        self.env["CACHEW_CACHEW_URL"] = "http://127.0.0.1:1"
        self.assertFalse(self.attempt())
        self.assertEqual(len(self.pauses), 2)
        self.assertFalse(self.target.exists())

    def test_untrusted_cache_origins_cannot_receive_checkout_token(self):
        for url in (
            "https://example.com",
            "https://cachew.dev.tenstorrent.net:444",
            "https://cachew.dev.tenstorrent.net.evil.test",
        ):
            self.env["CACHEW_CACHEW_URL"] = url
            self.assertFalse(self.attempt())
        self.env.update(GITHUB_SERVER_URL="https://github.com", CACHEW_CACHEW_URL=self.cache.url)
        self.assertFalse(self.attempt())
        self.assertEqual(self.cache.requests, [])
        self.assertFalse(self.target.exists())

    def violation(self):
        """Run the cache checkout; return whether it reported a security violation."""
        (self.root / "output").write_text("")
        capture = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(capture):
            cachew_checkout.main()
        outputs = runner_file(self.root / "output")
        reported = outputs["security-violation"] == "true"
        annotated = "::error title=cachew-checkout: possible security compromise attempt::" in capture.getvalue()
        self.assertEqual(reported, annotated)
        if reported:
            self.assertEqual(outputs["checked-out"], "false")
            # One annotation line: no input text that could add workflow commands.
            rest = self.unmasked(capture.getvalue())
            self.assertEqual(rest.count("::"), 2, capture.getvalue())
        return reported

    def test_attack_shaped_inputs_fail_with_an_error_annotation(self):
        ordinary = dict(self.env)
        self.assertFalse(self.violation())
        shutil.rmtree(self.target)
        self.cache.requests.clear()
        self.upstream.requests.clear()
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "link").symlink_to(outside)
        for change in (
            {"CACHEW_GITHUB_SERVER_URL": "https://github.com';touch pwned;'"},
            {"CACHEW_GITHUB_SERVER_URL": "https://a`id`.example\n::add-mask::x"},
            {"CACHEW_CACHEW_URL": "https://evil.example"},
            {"CACHEW_CACHEW_URL": "https://cachew.dev.tenstorrent.net.evil.test"},
            # Refused whatever spelling of either origin cachew cannot use: the
            # check must not depend on reading the server URL or the cache URL.
            {"CACHEW_GITHUB_SERVER_URL": "https://github.com.", "CACHEW_CACHEW_URL": "https://evil.example"},
            {"CACHEW_GITHUB_SERVER_URL": "https://user@github.com", "CACHEW_CACHEW_URL": "https://evil.example"},
            {"CACHEW_GITHUB_SERVER_URL": "https://github.com/x", "CACHEW_CACHEW_URL": "https://evil.example"},
            {"CACHEW_CACHEW_URL": "https://evil.example/path"},
            {"CACHEW_CACHEW_URL": "https://user@evil.example"},
            {"CACHEW_CACHEW_URL": "HTTPS://evil.example?x"},
            {"CACHEW_CACHEW_URL": "https://evil.example."},
            {"CACHEW_PATH": "../escape"},
            {"CACHEW_PATH": "link/escape"},
            {"CACHEW_REF": "--upload-pack=touch pwned"},
            {"CACHEW_REPOSITORY": "org/.."},
        ):
            with self.subTest(change=change):
                self.env = dict(ordinary, **change)
                self.assertTrue(self.violation())
                self.assertEqual(self.cache.requests, [])
                self.assertEqual(self.upstream.requests, [])
                self.assertFalse(self.target.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsupported_but_harmless_inputs_still_fall_back_quietly(self):
        ordinary = dict(self.env)
        for change in (
            {"CACHEW_GITHUB_SERVER_URL": "http://github.com"},
            {"CACHEW_GITHUB_SERVER_URL": "https://github.com:port"},
            {"CACHEW_CACHEW_URL": "http://cachew.cachew.svc.cluster.local"},
            {"CACHEW_CACHEW_URL": "https://cachew.ci.tenstorrent.net/x"},
            {"CACHEW_CACHEW_URL": "https://cachew.ci.tenstorrent.net."},
            {"CACHEW_GITHUB_SERVER_URL": "https://github.com."},
            {"CACHEW_REF": "bad ref"},
            {"CACHEW_FILTER": "bad filter"},
            {"CACHEW_LFS": "true"},
        ):
            with self.subTest(change=change):
                self.env = dict(ordinary, **change)
                self.assertFalse(self.violation())

    def test_a_whitespace_only_token_falls_back_to_the_standard_checkout(self):
        # actions/checkout reads the token with required: true, so one that trims
        # to nothing fails that checkout; cachew must not check out without it.
        self.env["CACHEW_TOKEN"] = " \n"
        self.assertFalse(self.attempt())
        self.assertFalse(self.violation())
        self.assertEqual(self.cache.requests, [])
        self.assertFalse((self.target / "content.txt").exists())

    def test_server_origins_with_shell_or_config_metacharacters_are_rejected(self):
        # The origin reaches `git submodule foreach` shell commands and Git configuration keys.
        for url in (
            "https://x';touch pwned;'",
            "https://git hub.com",
            "https://a`id`.example",
            "https://exa$(id)mple.com",
            'https://a"b.example',
            "https://-leading.example",
            "https://github.com:port",
        ):
            with self.assertRaises(cachew_checkout.Bypass, msg=url):
                cachew_checkout.origin(url, {})
        for url in ("https://github.com", "https://ghe.example.com:8443/", "https://[::1]:8443"):
            cachew_checkout.origin(url, {})
        self.env["CACHEW_GITHUB_SERVER_URL"] = "https://github.com';touch pwned;'"
        self.assertFalse(self.attempt())
        self.assertEqual(self.cache.requests, [])

    def test_a_metacharacter_host_is_refused_whatever_else_the_url_holds(self):
        # The standard checkout reads only the URL's origin, so a path, query,
        # fragment or userinfo does not stop the host reaching its shell commands.
        for url in (
            "https://x$(id)/path",
            "https://a`id`.example/?q",
            "https://user@exa$(id)mple.com",
            "https://exa$(id)mple.com#f",
            "http://a`id`.example",
            # Forms Python's URL parser rejects outright: the host is still
            # read as written and refused, not passed over as unparsable.
            "https://[::1;id]",
            "https://[::1]`id`",
            "https://a[;id]",
            "https://x]$(id)",
            "https://a\u2100b;id",
            # Git and WHATWG URL parsers end the host at a backslash: the part
            # before it is the host the standard checkout uses.
            "https://a$(id)\\.github.com",
        ):
            with self.subTest(url=url):
                with self.assertRaises(cachew_checkout.SecurityViolation):
                    cachew_checkout.origin(url, {})
                self.env["CACHEW_GITHUB_SERVER_URL"] = url
                self.assertTrue(self.violation())
        self.assertEqual(self.cache.requests, [])

    def test_child_processes_inherit_neither_the_token_nor_a_tls_override(self):
        self.env["GIT_SSL_NO_VERIFY"] = "1"
        attempt = cachew_checkout.Checkout(self.env)
        attempt.cache = self.cache.url
        attempt.configure_transport()
        for environment in (attempt.env, attempt.local_env, attempt.upstream_env):
            self.assertNotIn("CACHEW_TOKEN", environment)
            self.assertNotIn("GIT_SSL_NO_VERIFY", environment)
            self.assertNotIn(TOKEN, environment.values())
        self.assertIn(("http.sslVerify", "true"), attempt.cache_config)
        self.assertIn((f"http.{self.cache.url}/.sslVerify", "true"), attempt.cache_config)

    def test_an_origin_is_read_as_actions_checkout_reads_it(self):
        # URL.origin: the scheme and host in lower case, no default port.
        for value, expected in (
            ("HTTPS://GitHub.COM:443/", "https://github.com"),
            ("https://github.com:0443", "https://github.com"),
            ("https://GHE.example:8443", "https://ghe.example:8443"),
            ("https://[::1]:443", "https://[::1]"),
        ):
            with self.subTest(value=value):
                self.assertEqual(cachew_checkout.origin(value, {}), expected)
        with self.assertRaises(cachew_checkout.Bypass):
            cachew_checkout.origin("https://github.com:65536", {})
        trusted = cachew_checkout.TRUSTED_CACHES[0]
        spelled = trusted.replace("https://cachew", "HTTPS://Cachew") + ":443"
        self.assertEqual(cachew_checkout.cache_candidates(spelled, "https://github.com", {}), [trusted])

    def test_a_ref_is_classified_by_its_uppercase_form_as_actions_checkout_does(self):
        # U+017F uppercases to S: actions/checkout reads this ref as a branch.
        self.env["CACHEW_REF"] = "ref\u017f/heads/main"
        attempt = cachew_checkout.Checkout(self.env)
        with patch.object(attempt, "lookup", return_value="0" * 40):
            attempt.resolve()
        self.assertEqual(attempt.checkout_target, ("main", "refs/remotes/origin/main"))

    def proxied(self):
        proxy = ForwardingProxy()
        self.addCleanup(proxy.close)
        for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            self.env.pop(name, None)
            self.env.pop(name.upper(), None)
        return proxy

    def test_the_proxy_in_the_jobs_git_configuration_reaches_upstream(self):
        # The server answers only through a proxy that the job's Git configuration
        # names. The ref lookup read no configuration, so every such checkout fell back.
        proxy = self.proxied()
        far = proxy.unused_address()
        proxy.routes[far] = urllib.parse.urlsplit(self.upstream.url).netloc
        cache = GitServer(self.project_root, far, real_git=self.env["CACHEW_REAL_GIT"])
        self.addCleanup(cache.close)
        with (self.home / ".gitconfig").open("a") as config:
            config.write(f'[http "http://{far}/"]\n\tproxy = {proxy.url}\n')
        self.env.update(
            GITHUB_SERVER_URL=f"http://{far}",
            CACHEW_CACHEW_URL=cache.url,
            CACHEW_SUBMODULES="false",
            CACHEW_REF="v1.0.0",
        )
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(io.StringIO()):
            cachew_checkout.main()
        self.assertEqual(runner_file(self.root / "output")["checked-out"], "true")
        self.assertIn(far, proxy.seen)

    def test_the_liveness_probe_uses_the_proxy_git_would_use_for_the_cache(self):
        proxy = self.proxied()
        far = proxy.unused_address()
        proxy.routes[far] = urllib.parse.urlsplit(self.cache.url).netloc
        with (self.home / ".gitconfig").open("a") as config:
            config.write(f'[http "http://{far}/"]\n\tproxy = {proxy.url}\n')
        env = {key: value for key, value in self.env.items() if not key.startswith("CACHEW_")}
        env["CACHEW_REAL_GIT"] = self.env["CACHEW_REAL_GIT"]
        self.assertEqual(cachew_checkout.liveness(f"http://{far}", 5, env), "ok")
        self.assertIn(far, proxy.seen)

    def test_curl_trusts_the_certificates_git_is_told_to_trust(self):
        home = {"HOME": "/home/runner"}
        cases = [
            ({"http.sslcainfo": "~/ca.pem"}, {}, ["--cacert", "/home/runner/ca.pem"]),
            ({"http.sslcainfo": "/a.pem"}, {"GIT_SSL_CAINFO": "/b.pem"}, ["--cacert", "/b.pem"]),
            ({"http.sslcapath": "/certs"}, {}, ["--capath", "/certs"]),
            ({"http.proxy": ""}, {}, ["--noproxy", "*"]),
            ({"http.proxy": "http://p", "http.noproxy": "a,b"}, {}, ["--proxy", "http://p", "--noproxy", "a,b"]),
            ({"http.sslcert": "/c.pem", "http.sslkey": "/k.pem"}, {}, ["--cert", "/c.pem", "--key", "/k.pem"]),
            ({}, {}, []),
        ]
        for transport, environ, expected in cases:
            self.assertEqual(cachew_checkout.curl_transport(transport, {**home, **environ}), expected)

    def test_sparse_lines_are_trimmed_as_javascript_trims_them(self):
        # actions/checkout trims each line with String.prototype.trim: U+FEFF goes,
        # U+001F and U+0085 stay. Python's str.strip does the opposite.
        self.env["CACHEW_SUBMODULES"] = "false"
        self.env["CACHEW_SPARSE_CHECKOUT"] = ".github\n\ufeffsrc\u00a0\na\x1f \nb\x85"
        self.assertEqual(cachew_checkout.Checkout(self.env).sparse, [".github", "src", "a\x1f", "b\x85"])

    def test_a_fetch_depth_javascript_reads_differently_uses_the_standard_checkout(self):
        # Number("1_0") is NaN (full history) and Number("1e1") is 10; int() reads 10 and fails.
        for value in ("1_0", "1e1", "0x10", "1.5", "\u0661"):
            self.env["CACHEW_FETCH_DEPTH"] = value
            with self.assertRaises(cachew_checkout.Bypass, msg=value):
                cachew_checkout.Checkout(self.env)
        self.env["CACHEW_FETCH_DEPTH"] = "+7"
        self.assertEqual(cachew_checkout.Checkout(self.env).depth, 7)

    def test_the_liveness_probe_counts_against_the_checkout_budget(self):
        attempt = cachew_checkout.Checkout(self.env, deadline=time.monotonic() - 1)
        with self.assertRaises(cachew_checkout.Bypass):
            attempt.probe()
        self.assertEqual(self.cache.requests, [])

    def test_a_lazy_fetch_never_follows_a_redirect_the_callers_configuration_allows(self):
        # Git prefers a value scoped to the URL over an unscoped one, whatever
        # their sources: the caller's scoped setting would beat an unscoped false.
        attempt = cachew_checkout.Checkout(self.env)
        attempt.cache = self.cache.url
        attempt.configure_transport()
        with tempfile.TemporaryDirectory() as directory:
            caller = Path(directory) / "caller.config"
            caller.write_text(f'[http "{self.cache.url}/"]\n\tfollowRedirects = true\n')
            env = dict(attempt.local_env, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(caller))
            cachew_checkout.apply_git_config(env, attempt.cache_config, append=True)
            value = subprocess.run(
                ["git", "config", "--get-urlmatch", "http.followRedirects", f"{self.cache.url}/git/github.com/o/r"],
                env=env,
                cwd=directory,
                capture_output=True,
                text=True,
            ).stdout.strip()
        self.assertEqual(value, "false")

    def test_submodule_url_must_be_exactly_owner_and_name(self):
        child = git(self.project_root / "org" / "child", "rev-parse", "HEAD", env=self.env)
        for index, url in enumerate(("../child?ref=1", "../child%2f..%2f..%2fadmin", "../child@x")):
            name = f"odd{index}"
            sha = self.repository(name, [("child-name", "deps/child", url, child)])
            self.env.update(CACHEW_REPOSITORY=f"org/{name}", CACHEW_REF=sha)
            self.assertTrue(self.violation(), url)
            self.assertFalse(any("child" in path for _, path, _ in self.cache.requests), url)
            self.assertEqual(list(self.target.iterdir()), [], url)

    def test_ref_listing_is_split_on_line_feeds_only(self):
        attempt = cachew_checkout.Checkout(self.env)
        listing = f"{'a' * 40}\trefs/heads/x\u2028{'b' * 40}\trefs/heads/main\n".encode()
        with patch.object(cachew_checkout.Checkout, "run_git", lambda *_, **__: listing):
            refs, _ = attempt.ls_remote("--refs", attempt.url)
        self.assertNotIn("refs/heads/main", refs)

    def test_outputs_cannot_inject_another_output(self):
        self.env["CACHEW_SUBMODULES"] = "false"
        resolve = cachew_checkout.Checkout.resolve

        def renamed(attempt):
            resolve(attempt)
            attempt.ref = "x\nchecked-out=false"

        with patch.object(cachew_checkout.Checkout, "resolve", renamed):
            self.assertTrue(self.attempt())
        self.assertEqual(runner_file(self.root / "output")["ref"], "x\nchecked-out=false")

    def test_health_timeout_is_retried_then_uses_upstream(self):
        self.cache.health_delay = 0.4
        started = time.monotonic()
        self.assertFalse(self.attempt())
        # Each probe is abandoned after the 0.1-second health timeout; the waits are skipped.
        self.assertLess(time.monotonic() - started, 2)
        self.assert_cache_attempts([], attempts=3)

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_main_failure_retries_then_uses_upstream(self):
        self.cache.fail_repo = "main"
        self.assertFalse(self.attempt())
        self.assert_cache_attempts(["main"], attempts=3)
        self.assertEqual(list(self.target.iterdir()), [])
        self.official_checkout()

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_nested_failure_stops_cache_traversal_and_uses_upstream(self):
        repository = self.project_root / "org/main"
        sibling_sha = git(self.project_root / "org/grand", "rev-parse", "HEAD")
        with (repository / ".gitmodules").open("a") as modules:
            modules.write('[submodule "later-sibling"]\n\tpath = deps/sibling\n\turl = ../grand\n')
        git(repository, "update-index", "--add", "--cacheinfo", f"160000,{sibling_sha},deps/sibling", env=self.env)
        git(repository, "add", ".gitmodules", env=self.env)
        git(repository, "commit", "-m", "later sibling", env=self.env)
        self.sha = git(repository, "rev-parse", "HEAD")
        self.env["GITHUB_SHA"] = self.sha
        self.cache.fail_repo = "grand"
        self.assertFalse(self.attempt())
        self.assert_cache_attempts(["main", "child", "grand"], attempts=3)
        self.assertEqual(list(self.target.iterdir()), [])
        self.official_checkout()
        self.assertEqual((self.target / "deps/sibling/content.txt").read_text(), "grand")

    def test_mid_fetch_disconnect_is_retried_then_uses_upstream(self):
        self.cache.fail_repo = "child"
        self.cache.fail_post = True
        self.cache.disconnect = True
        self.assertFalse(self.attempt())
        self.assert_cache_attempts(["main", "child"], attempts=3)
        posts = [p for method, p, _ in self.cache.requests if method == "POST" and "/child/" in p]
        self.assertEqual(len(posts), 3)
        self.assertEqual(len(self.pauses), 2)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_fetch_timeout_kills_transport_and_is_retried(self):
        self.env["CACHEW_CACHEW_FETCH_TIMEOUT"] = "0.3"
        self.cache.fail_repo = "main"
        self.cache.fetch_delay = 0.6
        self.assertFalse(self.attempt())
        self.assert_cache_attempts(["main"], attempts=3)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_slow_but_moving_cache_is_abandoned_after_the_checkout_budget(self):
        # Every cache request answers, slowly: no single fetch stalls, but the
        # main repository plus two submodules overrun the whole-checkout budget.
        self.cache.request_delay = 0.4
        self.env["CACHEW_CHECKOUT_BUDGET"] = "1.5"
        started = time.monotonic()
        self.assertFalse(self.attempt())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.output()["available"], "false")
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual([p for p in self.runtime.iterdir() if p.name.startswith("git-credentials-")], [])

    def test_unsupported_modes_and_existing_checkout_skip_without_health_probe(self):
        for name, value in [
            ("SSH_KEY", "private key"),
            ("LFS", "true"),
            ("FILTER", "blob:none --upload-pack=touch"),
            # Sparse with submodules (the fixture default is recursive).
            ("SPARSE_CHECKOUT", ".github"),
        ]:
            with self.subTest(name=name):
                self.env["CACHEW_" + name] = value
                self.assertFalse(self.attempt())
                del self.env["CACHEW_" + name]
        self.target.mkdir()
        (self.target / "keep.txt").write_text("keep")
        self.assertFalse(self.attempt())
        self.assertEqual((self.target / "keep.txt").read_text(), "keep")
        self.assertEqual(self.cache.requests, [])

    def test_path_escape_and_symlink_outside_workspace_skip_before_mutation(self):
        for path in ("../outside", str(self.root / "absolute-outside")):
            self.env["CACHEW_PATH"] = path
            self.assertFalse(self.attempt())
        (self.workspace / "link").symlink_to(self.home, target_is_directory=True)
        self.env["CACHEW_PATH"] = "link/escape"
        self.assertFalse(self.attempt())
        self.assertFalse((self.home / "escape").exists())
        self.assertEqual(self.cache.requests, [])

    def test_inputs_without_a_fallback_are_refused_whatever_else_chooses_the_standard_checkout(self):
        (self.workspace / "link").symlink_to(self.home, target_is_directory=True)
        attacks = ({"CACHEW_PATH": "link/escape"}, {"CACHEW_REF": "--upload-pack=x"})
        fallbacks = ({"CACHEW_LFS": "true"}, {"CACHEW_SSH_KEY_SET": "true"}, {"CACHEW_FILTER": "bad filter"})
        base = dict(self.env)
        for attack in attacks:
            for fallback in fallbacks:
                with self.subTest(attack=attack, fallback=fallback):
                    self.env = dict(base, **attack, **fallback)
                    self.assertFalse(self.attempt())
                    self.assertEqual(self.output()["security-violation"], "true")
        self.assertEqual(self.cache.requests, [])

    def test_explicit_sha_and_direct_submodules_at_depth_500(self):
        self.env.update(CACHEW_REF=self.sha, CACHEW_SUBMODULES="true", CACHEW_FETCH_DEPTH="500")
        self.assertTrue(self.attempt())
        self.assert_cache_attempts(["main", "child"])
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual((self.target / "deps/child/content.txt").read_text(), "child")
        self.assertFalse((self.target / "deps/child/deep/grand/content.txt").exists())
        self.assertEqual(self.output()["ref"], "")
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)

    def update_main(self):
        source = self.project_root / "org/main"
        (source / "content.txt").write_text("updated main")
        git(source, "add", "content.txt", env=self.env)
        git(source, "commit", "-m", "update", env=self.env)
        self.sha = git(source, "rev-parse", "HEAD")
        self.env.update(GITHUB_SHA=self.sha, CACHEW_REF=self.sha, CACHEW_FETCH_DEPTH="500")

    def add_layout(self):
        """Files in several directories, for sparse checkouts to include and exclude."""
        source = self.project_root / "org/main"
        names = (".github/workflows/ci.yml", "src/b.txt", "src/deep/c.txt", "docs/d.md")
        for name in names:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(name)
        # By name: the fixture's gitlinks have no worktree, so `add .` would delete them.
        git(source, "add", *names, env=self.env)
        git(source, "commit", "-m", "layout", env=self.env)
        self.sha = git(source, "rev-parse", "HEAD")
        self.env.update(GITHUB_SHA=self.sha, CACHEW_REF=self.sha)

    def missing(self, *revisions, gitdir=".git"):
        listed = git(self.target, f"--git-dir={gitdir}", "rev-list", "--objects", "--missing=print", *revisions)
        return [line[1:] for line in listed.splitlines() if line.startswith("?")]

    def test_filtered_seed_has_complete_tip_and_filtered_history(self):
        self.update_main()
        old_blob = git(self.project_root / "org/main", "rev-parse", "HEAD~1:content.txt")
        old_tree = git(self.project_root / "org/main", "rev-parse", "HEAD~1^{tree}")
        for spec, missing in (("blob:none", [old_blob]), ("tree:0", [old_tree])):
            with self.subTest(filter=spec):
                self.env["CACHEW_FILTER"] = spec
                self.assertTrue(self.attempt())
                self.assert_cache_attempts(["main", "main", "child", "grand"])
                self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
                self.assertEqual(self.missing("-n1", "HEAD"), [])
                self.assertIn(missing[0], self.missing("HEAD"))
                self.assertEqual(git(self.target, "config", "remote.origin.promisor"), "true")
                self.assertEqual(git(self.target, "config", "remote.origin.partialclonefilter"), spec)
                self.assertEqual(self.missing("HEAD", gitdir=".git/modules/child-name"), [])
                self.assert_no_cache_credentials()
                self.assertEqual(self.upstream.requests, [])
                shutil.rmtree(self.target)
                self.cache.requests.clear()

    def test_sparse_checkout_fetches_only_its_blobs_from_the_cache(self):
        self.add_layout()
        source = self.project_root / "org/main"
        excluded = git(source, "rev-parse", "HEAD:src/b.txt")
        included = git(source, "rev-parse", "HEAD:.github/workflows/ci.yml")
        self.env.update(CACHEW_SUBMODULES="false", CACHEW_SPARSE_CHECKOUT=".github")
        self.upstream.fail_repo = "main"
        self.assertTrue(self.attempt())
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual((self.target / ".github/workflows/ci.yml").read_text(), ".github/workflows/ci.yml")
        self.assertFalse((self.target / "src").exists())
        missing = self.missing("-n1", "HEAD")
        self.assertIn(excluded, missing)
        self.assertNotIn(included, missing)
        self.assertEqual(git(self.target, "config", "remote.origin.url"), f"{self.upstream.url}/org/main")
        self.assertEqual(git(self.target, "config", "remote.origin.partialclonefilter"), "blob:none")
        self.assert_no_cache_credentials()

    def test_sparse_lazy_fetches_send_the_cache_only_its_own_header(self):
        # The caller's unscoped header is meant for upstream; the lazy fetches read
        # the caller's configuration but must not repeat it to the cache.
        self.add_layout()
        with (self.home / ".gitconfig").open("a") as config:
            config.write("[http]\n\textraHeader = Authorization: basic aW5oZXJpdGVk\n")
        self.env.update(CACHEW_SUBMODULES="false", CACHEW_SPARSE_CHECKOUT=".github")
        self.assertTrue(self.attempt())
        self.assertEqual(self.upstream.requests, [])
        # The seed fetch and the lazy blob fetch, each with only the checkout's header.
        self.assertEqual(len(self.cache.discoveries()), 2)
        expected = "basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        for _, path, auth in self.cache.requests:
            if path != "/_liveness":
                self.assertEqual(auth.lower(), expected.lower(), path)

    def test_a_narrower_setting_of_the_caller_keeps_the_token_off_a_weakened_lazy_fetch(self):
        # The lazy fetches read the caller's configuration, and Git applies the
        # most specific http.<url>.* whatever its source: a setting scoped more
        # narrowly than cachew's own wins.
        self.add_layout()
        for setting in ("followRedirects = true", "sslVerify = false"):
            with self.subTest(setting):
                (self.home / ".gitconfig").write_text(f'[http "{self.cache.url}/git/"]\n\t{setting}\n')
                self.env.update(CACHEW_SUBMODULES="false", CACHEW_SPARSE_CHECKOUT=".github")
                self.assertFalse(self.attempt())
                # The seed fetch, which reads none of the caller's files, and no other.
                self.assertEqual(len(self.cache.discoveries()), 1)
                self.assertEqual(list(self.target.iterdir()), [])
                self.cache.requests.clear()

    def test_a_template_that_weakens_cache_requests_keeps_the_checkout_off_the_cache(self):
        # git init copies the template's configuration into the new repository,
        # the only file the checkout's fetches read.
        weak = self.root / "weak.config"
        weak.write_text(f'[http "{self.cache.url}/git/"]\n\tsslVerify = false\n')
        template = self.root / "template"
        template.mkdir()
        self.env["GIT_TEMPLATE_DIR"] = str(template)
        for content in (weak.read_text(), f"[include]\n\tpath = {weak}\n"):
            with self.subTest(content):
                (template / "config").write_text(content)
                self.assertFalse(self.attempt())
                self.assertEqual(self.cache.discoveries(), [])
                self.assertEqual(list(self.target.iterdir()), [])

    def test_sparse_lazy_fetch_failure_discards_and_falls_back(self):
        self.add_layout()
        self.env.update(CACHEW_SUBMODULES="false", CACHEW_SPARSE_CHECKOUT=".github")
        seed_main = cachew_checkout.Checkout.seed_main

        def then_fail(attempt, gitdir):
            commit = seed_main(attempt, gitdir)
            self.cache.fail_repo = "main"
            return commit

        with patch.object(cachew_checkout.Checkout, "seed_main", then_fail):
            self.assertFalse(self.attempt())
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual([p for p in self.runtime.iterdir() if p.name.startswith("git-credentials-")], [])

    def test_sparse_checkout_with_submodules_uses_upstream(self):
        for submodules in ("true", "recursive"):
            with self.subTest(submodules=submodules):
                self.env.update(CACHEW_SUBMODULES=submodules, CACHEW_SPARSE_CHECKOUT=".github")
                self.assertFalse(self.attempt())
                self.assertEqual(self.cache.requests, [])

    def test_filtered_full_history_is_not_shallow(self):
        self.update_main()
        self.env.update(CACHEW_FILTER="tree:0", CACHEW_FETCH_DEPTH="0", CACHEW_REF="main")
        self.assertTrue(self.attempt())
        self.assertEqual(git(self.target, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(self.missing("-n1", "HEAD"), [])
        self.assertEqual(
            git(self.target, "rev-parse", "refs/tags/v1.0.0^{commit}"), git(self.target, "rev-parse", "HEAD~1")
        )

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_fork_pull_request_guard_matches_the_pinned_checkout(self):
        # actions/checkout refuses fork pull request code in pull_request_target and
        # workflow_run workflows unless allow-unsafe-pr-checkout is set; cachew must
        # never check out what it refuses, so the official checkout reports it.
        fork, base = 222, 111
        target = {"repository": {"id": base, "owner": {"id": 1234}}}
        pull = {**target, "pull_request": {"head": {"repo": {"id": fork, "full_name": "Fork/Main"}, "sha": self.sha}}}
        same = {**target, "pull_request": {"head": {"repo": {"id": base, "full_name": "org/main"}, "sha": self.sha}}}

        def run(trigger, head_sha):
            head = {"id": fork, "full_name": "fork/main"}
            return {**target, "workflow_run": {"event": trigger, "head_repository": head, "head_sha": head_sha}}

        cases = [
            ("pull_request_target", pull, {"CACHEW_REF": "refs/pull/1/merge"}, True),
            ("pull_request_target", pull, {"CACHEW_REF": self.sha.upper()}, True),
            ("pull_request_target", pull, {"CACHEW_REPOSITORY": "fork/main", "CACHEW_REF": "main"}, True),
            ("pull_request_target", pull, {"CACHEW_REF": "refs/pull/1/merge", "CACHEW_ALLOW_UNSAFE_PR_CHECKOUT": " TRUE\n"}, False),
            ("pull_request_target", pull, {"CACHEW_REF": "main"}, False),
            ("pull_request_target", pull, {"CACHEW_REF": ""}, False),
            ("pull_request_target", same, {"CACHEW_REF": "refs/pull/1/merge"}, False),
            ("workflow_run", run("pull_request", self.sha), {"CACHEW_REF": self.sha}, True),
            ("workflow_run", run("pull_request_target", self.sha), {"CACHEW_REF": self.sha}, False),
            ("push", pull, {"CACHEW_REF": "refs/pull/1/merge"}, False),
        ]
        event = self.root / "guard-event.json"
        for name, payload, inputs, refused in cases:
            with self.subTest(event=name, inputs=inputs):
                event.write_text(json.dumps(payload))
                env = dict(self.env, GITHUB_EVENT_NAME=name, GITHUB_EVENT_PATH=str(event), **inputs)
                try:
                    cachew_checkout.Checkout(env)
                    reason = ""
                except cachew_checkout.Bypass as error:
                    reason = str(error)
                self.assertEqual("allow-unsafe-pr-checkout" in reason, refused, reason)
                official = dict(env)
                for key, value in {
                    "repository": env.get("CACHEW_REPOSITORY", "org/main"),
                    "ref": env["CACHEW_REF"],
                    "token": TOKEN,
                    "path": "official-guard",
                    "allow-unsafe-pr-checkout": env.get("CACHEW_ALLOW_UNSAFE_PR_CHECKOUT", "false"),
                }.items():
                    official["INPUT_" + key.upper()] = value
                # The refusal comes while reading inputs; an allowed checkout is
                # stopped once it is past that point (its fetch may retry for long).
                try:
                    output = subprocess.run(
                        ["node", CHECKOUT_JS], cwd=self.workspace, env=official, capture_output=True, text=True, timeout=10
                    ).stdout
                except subprocess.TimeoutExpired as stopped:
                    output = stopped.stdout.decode() if isinstance(stopped.stdout, bytes) else stopped.stdout or ""
                self.assertEqual("Refusing to check out fork pull request code" in output, refused, output)
                shutil.rmtree(self.workspace / "official-guard", ignore_errors=True)

    def test_named_ref_after_a_workspace_root_checkout_sends_one_authorization(self):
        # Composite steps run in the workspace. A checkout at its root persists
        # credentials there; the upstream name lookup must not add them to its own.
        self.env["CACHEW_PATH"] = ""
        self.target = self.workspace
        self.assertTrue(self.attempt())
        directory = os.getcwd()
        self.addCleanup(os.chdir, directory)
        os.chdir(self.workspace)
        self.env.update(CACHEW_PATH="second", CACHEW_REF="main")
        self.target = self.workspace / "second"
        self.assertTrue(self.attempt())
        self.assertEqual(self.upstream.duplicate_auth, [])

    def test_upstream_name_lookup_uses_the_token_and_failure_falls_back(self):
        expected = "basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.upstream.required_auth["main"] = expected
        self.env["CACHEW_REF"] = "main"
        self.assertTrue(self.attempt())
        self.assertEqual(git(self.target, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(self.output()["ref"], "main")
        shutil.rmtree(self.target)
        self.cache.requests.clear()
        self.env["CACHEW_TOKEN"] = "wrong"
        self.assertFalse(self.attempt())
        # A private repository's answer is not a cache outage.
        self.assertEqual(self.output()["available"], "true")
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(self.cache.discoveries(), [])

    def test_missing_ref_upstream_falls_back(self):
        self.env["CACHEW_REF"] = "no-such-branch"
        self.assertFalse(self.attempt())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_moved_tag_is_rejected_like_checkout(self):
        old = self.sha
        self.update_main()
        self.env.update(CACHEW_REF="", GITHUB_REF="refs/tags/v1.0.0", GITHUB_SHA=self.sha)
        self.assertFalse(self.attempt())
        self.assertEqual(list(self.target.iterdir()), [])
        self.env["GITHUB_SHA"] = old
        self.assertTrue(self.attempt())
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), old)

    def test_default_branch_of_another_repository(self):
        self.env.update(CACHEW_REPOSITORY="org/child", CACHEW_REF="")
        self.assertTrue(self.attempt())
        self.assertEqual(self.upstream.discoveries(), ["/org/child/info/refs"])
        self.assertEqual(git(self.target, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(self.output()["ref"], "refs/heads/main")
        self.assertEqual((self.target / "deep/grand/content.txt").read_text(), "grand")

    def test_missing_submodule_object_fails_offline_and_discards(self):
        # Without the seeded submodule repository, `submodule update` would clone it upstream.
        with patch.object(cachew_checkout.Checkout, "seed_children", lambda *_: None):
            self.assertFalse(self.attempt())
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual([p for p in self.runtime.iterdir() if p.name.startswith("git-credentials-")], [])

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_late_failure_discards_everything_for_the_fallback_checkout(self):
        finish = cachew_checkout.Checkout.finish

        def failing(attempt):
            finish(attempt)
            raise cachew_checkout.Bypass("injected")

        with patch.object(cachew_checkout.Checkout, "finish", failing):
            self.assertFalse(self.attempt())
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.official_checkout()

    def test_safe_directory_is_written_to_the_checkout_global_config(self):
        global_config = self.root / "checkout.config"
        global_config.write_text("")
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
        self.assertTrue(self.attempt())
        values = git(self.root, "config", "--file", str(global_config), "--get-all", "safe.directory")
        self.assertEqual(values, str(self.target))

    def test_default_event_sha_without_submodules(self):
        self.env.update(CACHEW_REF="", CACHEW_SUBMODULES="false", CACHEW_FETCH_TAGS="false")
        self.assertTrue(self.attempt())
        self.assert_cache_attempts(["main"])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        self.assertFalse((self.target / ".git/modules").exists())

    def test_gitmodules_entry_without_a_gitlink_is_skipped(self):
        # git submodule update skips a .gitmodules entry the tree has no gitlink
        # for (a removed submodule left in the file), so the cache does too.
        repository = self.project_root / "org/main"
        with (repository / ".gitmodules").open("a") as modules:
            modules.write('[submodule "removed"]\n\tpath = deps/removed\n\turl = ../grand\n')
        git(repository, "add", ".gitmodules", env=self.env)
        git(repository, "commit", "-m", "stale entry", env=self.env)
        self.sha = git(repository, "rev-parse", "HEAD")
        self.env["GITHUB_SHA"] = self.sha
        self.assertTrue(self.attempt())
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.sha)
        self.assertFalse((self.target / "deps/removed").exists())

    def test_unsafe_module_path_discards_seed_and_never_escapes_target(self):
        repository = self.project_root / "org/main"
        git(repository, "config", "-f", ".gitmodules", "submodule.child-name.path", "../escape")
        git(repository, "add", ".gitmodules", env=self.env)
        git(repository, "commit", "-m", "unsafe path", env=self.env)
        self.env["CACHEW_REF"] = "main"
        self.assertFalse(self.attempt())
        self.assert_cache_attempts(["main"])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertFalse((self.workspace / "escape").exists())

    def output(self):
        return runner_file(self.root / "output")

    def test_repository_answer_keeps_cache_available_for_later_dependencies(self):
        # M3: a 4xx about one repository or ref is not a cache outage.
        self.cache.fail_repo = "main"
        for status, available in ((404, "true"), (403, "true"), (503, "false"), (302, "false")):
            with self.subTest(status=status):
                self.cache.fail_status = status
                self.assertFalse(self.attempt())
                self.assertEqual(self.output()["available"], available)

    def test_stale_staging_directory_from_cancelled_run_is_removed(self):
        # M4: a cancelled attempt must not block later seeding or leak into the worktree.
        self.target.mkdir()
        stale = self.target / ".cachew-checkout-a1b2c3_d"
        (stale / ".git").mkdir(parents=True)
        self.assertTrue(self.attempt())
        self.assertFalse(stale.exists())

    def test_stage_like_directories_in_a_used_target_are_kept(self):
        # A target holding anything else goes upstream; nothing in it is deleted.
        self.target.mkdir()
        for name in (".cachew-checkout-a1b2c3d4", ".cachew-checkout-tools", "README"):
            (self.target / name).mkdir()
        self.assertFalse(self.attempt())
        self.assertEqual(
            sorted(p.name for p in self.target.iterdir()),
            [".cachew-checkout-a1b2c3d4", ".cachew-checkout-tools", "README"],
        )

    def test_old_git_bypasses_before_contacting_cache(self):
        # M6: GIT_CONFIG_COUNT needs Git 2.31; older Git would ignore the transport settings.
        fake = self.root / "old-git"
        fake.write_text('#!/bin/sh\nif [ "$1" = version ]; then echo "git version 2.25.1"; exit 0; fi\nexit 1\n')
        fake.chmod(0o700)
        self.env["CACHEW_REAL_GIT"] = str(fake)
        self.assertFalse(self.attempt())
        self.assertEqual(self.cache.requests, [])
        self.assertEqual(self.output()["available"], "unknown")

    def test_auto_url_selects_first_healthy_trusted_origin(self):
        self.assertEqual(
            cachew_checkout.cache_candidates("auto", "https://github.com", {}), list(cachew_checkout.TRUSTED_CACHES)
        )
        self.assertEqual(
            cachew_checkout.cache_candidates("", "https://github.com", {}), list(cachew_checkout.TRUSTED_CACHES)
        )
        with patch.dict(os.environ, self.env, clear=True):
            attempt = cachew_checkout.Checkout(self.env)
        attempt.caches = ["http://127.0.0.1:1", self.cache.url]
        attempt.probe()
        self.assertEqual(attempt.cache, self.cache.url)
        self.assertNotIn("http://127.0.0.1:1", json.dumps(dict(attempt.env)))

    def rejected_cache_urls(self):
        return [
            "http://cachew.cachew.svc.cluster.local",
            "https://cachew.cachew.svc.cluster.local",
            "http://10.43.0.100:8080",
            "http://cachew.ci.tenstorrent.net",
            "https://evil.example",
        ]

    def test_4xx_pack_answers_are_not_transport_errors(self):
        answer = b"error: RPC failed; HTTP 404 curl 22 The requested URL returned error: 404\nfatal: the remote end hung up unexpectedly\n"
        self.assertFalse(cachew_checkout.transport_error(answer))
        self.assertTrue(cachew_checkout.transport_error(answer.replace(b"404", b"503")))
        self.assertTrue(cachew_checkout.transport_error(b"fatal: unable to access: Failed to connect to cachew\n"))

    def test_submodules_input_folds_case_like_actions_checkout(self):
        # actions/checkout compares toUpperCase() with TRUE and RECURSIVE, so a
        # dotless i (U+0131) or long s (U+017F) still spells recursive.
        for value, expected in (
            ("Recursive", "recursive"),
            ("recurs\u0131ve", "recursive"),
            ("recur\u017five", "recursive"),
            (" TRUE ", "true"),
            ("yes", "false"),
            ("", "false"),
        ):
            self.assertEqual(cachew_checkout.submodules_input({"CACHEW_SUBMODULES": value}), expected, repr(value))

    def test_boolean_inputs_parse_like_actions_checkout(self):
        for value, default, expected in (("", "true", True), (" false\n", "true", False), ("True", "false", True), ("yes", "true", False)):
            self.assertEqual(cachew_checkout.checkout_flag({"X": value}, "X", default), expected, repr(value))

    def test_production_rejects_plain_http_and_untrusted_cache_origins(self):
        for url in self.rejected_cache_urls():
            with self.subTest(url=url):
                self.env["CACHEW_CACHEW_URL"] = url
                self.assertFalse(self.attempt())
                self.assertEqual(self.output()["available"], "unknown")
                self.assertEqual(self.cache.requests, [])
                self.assertFalse(self.target.exists())

    def test_loopback_http_requires_the_test_only_variable(self):
        del self.env["CACHEW_TEST_ALLOW_LOOPBACK_HTTP"]
        for upstream in (self.upstream.url, "https://github.com"):
            with self.subTest(upstream=upstream):
                self.env.update(GITHUB_SERVER_URL=upstream, CACHEW_CACHEW_URL=self.cache.url)
                self.assertFalse(self.attempt())
                self.assertEqual(self.cache.requests, [])
                self.assertFalse(self.target.exists())
        for value in ("http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1"):
            with self.assertRaises(cachew_checkout.Bypass):
                cachew_checkout.origin(value, {})
            self.assertEqual(cachew_checkout.origin(value, {"CACHEW_TEST_ALLOW_LOOPBACK_HTTP": "1"}), value)
        with self.assertRaises(cachew_checkout.Bypass):
            cachew_checkout.origin("http://127.0.0.1:1", {"CACHEW_TEST_ALLOW_LOOPBACK_HTTP": "true"})

    def test_trusted_origins_match_deployment(self):
        # tt-flux apps/cachew/README.md: one public name per cluster.
        self.assertEqual(
            set(cachew_checkout.TRUSTED_CACHES),
            {
                "https://cachew.ci.tenstorrent.net",
                "https://cachew.yyz2.ci.tenstorrent.net",
                "https://cachew.dev.tenstorrent.net",
            },
        )

    @unittest.skipUnless(CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout dist/index.js")
    def test_composite_forwards_every_pinned_checkout_input(self):
        # The pinned checkout's own action.yml, beside its dist/ directory.
        pinned = (Path(CHECKOUT_JS).resolve().parents[1] / "action.yml").read_text()
        names = re.findall(r"^  ([a-z-]+):", pinned.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0], re.M)
        action = (ACTION / "action.yml").read_text()
        declared = re.findall(r"^  ([a-z-]+):", action.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0], re.M)
        self.assertEqual([name for name in declared if not name.startswith("cachew")], names)
        forwarding = action.split("      with:\n", 1)[1]
        self.assertEqual(set(re.findall(r"^        ([a-z-]+):", forwarding, re.M)), set(names))
        for name in names:
            self.assertIn(f"{name}: ${{{{ inputs.{name} }}}}", forwarding)
        self.assertIn("continue-on-error: true", action)
        for name in ("ref", "commit"):
            self.assertIn(
                f"value: ${{{{ steps.cachew.outputs.checked-out == 'true' && steps.cachew.outputs.{name} "
                f"|| steps.checkout.outputs.{name} }}}}",
                action,
            )
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", action)


class ForkGuardPayloadTests(unittest.TestCase):
    """actions/checkout reads the event payload as UTF-8 and fails on one it
    cannot parse. The guard must refuse wherever that checkout would not run."""

    SHA = "a1b2c3d4" * 5
    PAYLOAD = {
        "repository": {"id": 111},
        "pull_request": {"title": "caf\u00e9 \u65e5\u672c", "head": {"repo": {"id": 222, "full_name": "fork/main"}, "sha": SHA}},
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.event = Path(self.directory.name, "event.json")
        self.environ = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(self.event)}

    def refused(self):
        return cachew_checkout.unsafe_pr_checkout(self.environ, "org/main", "", self.SHA)

    def test_a_payload_that_cannot_be_parsed_is_refused(self):
        for content in (b"", b"{", b"\xff\xfe{}", json.dumps(self.PAYLOAD).encode() + b"x", b"[" * 100000):
            with self.subTest(content=content[:8]):
                self.event.write_bytes(content)
                self.assertTrue(self.refused())
        # Invalid UTF-8 inside a string is replaced, as Node does, and the guard still applies.
        self.event.write_bytes(json.dumps(self.PAYLOAD).encode().replace(b"caf", b"caf\xff"))
        self.assertTrue(self.refused())

    def test_no_payload_file_is_an_empty_payload(self):
        self.assertFalse(self.refused())
        del self.environ["GITHUB_EVENT_PATH"]
        self.assertFalse(self.refused())

    def test_a_non_ascii_payload_is_read_in_any_locale(self):
        # Under a non-UTF-8 locale the payload did not decode and read as empty,
        # which let a fork pull request with an accent in its title through.
        self.event.write_text(json.dumps(self.PAYLOAD, ensure_ascii=False), encoding="utf-8")
        script = (
            "import importlib.util, os, sys\n"
            "spec = importlib.util.spec_from_file_location('checkout', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.unsafe_pr_checkout(dict(os.environ), 'org/main', '', sys.argv[2]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(ACTION / "checkout.py"), self.SHA],
            env={**os.environ, **self.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.stdout.strip(), "True", result.stderr)


if __name__ == "__main__":
    unittest.main()
