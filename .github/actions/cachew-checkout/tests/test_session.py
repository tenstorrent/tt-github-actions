# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import unittest
from unittest.mock import patch
import urllib.parse

import test_action
import test_checkout as fixtures


NODE = shutil.which("node")
STEPS = {step["id"]: step for step in test_action.steps() if "id" in step}


@unittest.skipUnless(fixtures.CHECKOUT_JS, "set CHECKOUT_ACTION_JS to the pinned checkout bundle")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CheckoutTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.env = self.fixture.env
        # Checkout v6 scopes its credential include to the absolute gitdir.
        # macOS /var aliases must agree with Git's canonical /private/var path.
        self.env["GITHUB_WORKSPACE"] = str(self.fixture.workspace.resolve())
        self.env["CACHEW_ACTION_PATH"] = str(fixtures.ACTION)
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "true"
        self.env["CACHEW_CHECKED_OUT"] = "true"
        self.posts = []
        self.prepared = False
        self.sequence = 0
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()

    def run_node(self, script, *args, env=None):
        result = subprocess.run(
            [NODE, str(script), *args],
            cwd=self.fixture.workspace,
            env=env or self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_step(self, name, env=None):
        """A run step of action.yml, exactly as the runner starts it."""
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", test_action.script(STEPS[name])],
            cwd=self.fixture.workspace,
            env=dict(env or self.env, GITHUB_ACTION_PATH=str(fixtures.ACTION)),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare(self):
        if not self.prepared:
            self.prepared = True
            self.original_selection = self.env.get("GIT_CONFIG_GLOBAL")
            original = subprocess.run(
                ["git", "config", "--get", "user.name"],
                cwd=self.fixture.workspace,
                env=self.env,
                text=True,
                capture_output=True,
            )
            self.original_name = original.stdout.strip() if original.returncode == 0 else None
        result = self.run_step("session")
        self.assertEqual(fixtures.runner_file(self.fixture.root / "output").get("ready"), "true", result.stdout)
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))

    def checkout(self):
        state = self.fixture.root / f"checkout-state-{self.sequence}"
        self.sequence += 1
        state.touch()
        env = dict(self.env, GITHUB_STATE=str(state))
        for name, value in {
            "repository": self.env["CACHEW_REPOSITORY"],
            "ref": self.env["CACHEW_REF"],
            "token": self.env["CACHEW_TOKEN"],
            "path": self.env["CACHEW_PATH"],
            "fetch-depth": self.env["CACHEW_FETCH_DEPTH"],
            "fetch-tags": self.env["CACHEW_FETCH_TAGS"],
            "submodules": self.env["CACHEW_SUBMODULES"],
            "filter": self.env.get("CACHEW_FILTER", ""),
            "clean": "true",
            "persist-credentials": self.env["CACHEW_PERSIST_CREDENTIALS"],
            "set-safe-directory": self.env.get("CACHEW_SET_SAFE_DIRECTORY", "true"),
        }.items():
            env["INPUT_" + name.upper()] = value
        before = list(self.fixture.cache.requests)
        self.run_node(fixtures.CHECKOUT_JS, env=env)
        self.assertEqual(self.fixture.cache.requests, before, "official checkout must bypass every cache rewrite")
        self.posts.append((fixtures.CHECKOUT_JS, fixtures.runner_file(state)))

    def activate(self, available=None):
        self.env["CACHEW_AVAILABLE"] = available or fixtures.runner_file(self.fixture.root / "output").get(
            "available", "unknown"
        )
        self.run_step("configure")
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        bins = (self.fixture.root / "path").read_text().splitlines()
        self.env["PATH"] = os.pathsep.join(dict.fromkeys([*reversed(bins), *self.env["PATH"].split(os.pathsep)]))
        return fixtures.runner_file(self.fixture.root / "output")["configured"] == "true"

    def finish(self):
        self.end_job()
        self.fixture.assert_no_cache_credentials()

    def end_job(self):
        """Run checkout's post steps with the session still in place, then do what
        the runner does when the job completes: empty RUNNER_TEMP."""
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        self.assert_caller_settings_visible()
        for script, state in reversed(self.posts):
            env = dict(self.env)
            env.update({"STATE_" + key: value for key, value in state.items()})
            self.run_node(script, env=env)
            self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        self.posts.clear()
        self.assertTrue(directory.exists(), "the session lives until the runner empties RUNNER_TEMP")
        self.assertEqual((self.fixture.home / ".gitconfig").read_bytes(), self.original_global)
        if self.original_selection:
            self.assertTrue(Path(self.original_selection).exists())
        for entry in self.fixture.runtime.iterdir():
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        self.assertEqual((self.fixture.home / ".gitconfig").read_bytes(), self.original_global)

    def assert_caller_settings_visible(self):
        result = subprocess.run(
            [self.env["CACHEW_REAL_GIT"], "config", "--get", "user.name"],
            cwd=self.fixture.workspace,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.stdout.strip() if result.returncode == 0 else None, self.original_name)

    def flow(self):
        self.prepare()
        seeded = self.fixture.attempt()
        # As in action.yml: the official checkout runs only when cachew did not check out.
        if not seeded:
            self.checkout()
        active = self.activate()
        return seeded, active

    def real_git(self, *args):
        result = subprocess.run(
            [self.env["CACHEW_REAL_GIT"], *args],
            cwd=self.fixture.workspace,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def safe_directories(self, file=None):
        selection = ["--file", str(file)] if file else ["--global"]
        result = subprocess.run(
            [self.env["CACHEW_REAL_GIT"], "config", *selection, "--includes", "--null", "--get-all", "safe.directory"],
            cwd=self.fixture.workspace,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.stdout.split("\0")[:-1] if result.returncode == 0 else []

    def test_default_safe_directory_survives_activation_and_later_prepare(self):
        self.prepare()
        self.assertTrue(self.fixture.attempt())
        before = self.safe_directories()
        self.assertIn(str(self.fixture.target.resolve()), [str(Path(value).resolve()) for value in before if value])
        self.assertTrue(self.activate())
        self.assertEqual(self.safe_directories(), before)
        self.prepare()
        self.assertEqual(self.safe_directories(), before)
        self.assertTrue(self.activate("true"))
        self.assertEqual(self.safe_directories(), before)
        self.finish()

    def test_repeated_default_checkouts_keep_both_safe_directories_in_fallback(self):
        self.assertEqual(self.flow(), (True, True))
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        wrapper = load_wrapper()
        state = json.loads((directory / "state.json").read_text())
        wrapper.remove_cache_keys(self.env["CACHEW_REAL_GIT"], directory, state, self.env)
        self.env["CACHEW_PATH"] = "second-checkout"
        self.assertEqual(self.flow(), (False, False))
        expected = {str(self.fixture.target.resolve()), str((self.fixture.workspace / "second-checkout").resolve())}
        config = directory / "global.config"
        self.assertEqual({str(Path(value).resolve()) for value in self.safe_directories(config) if value}, expected)
        self.finish()

    def test_checkout_path_before_a_reset_is_trusted_again(self):
        # The job reset the list after the first checkout trusted its path; the
        # next checkout of that path must be trusted again, not skipped as known.
        self.assertEqual(self.flow(), (True, True))
        self.real_git("config", "--global", "--add", "safe.directory", "")
        shutil.rmtree(self.fixture.target)
        self.assertEqual(self.flow(), (True, True))
        values = self.safe_directories()
        effective = values[len(values) - values[::-1].index(""):]
        self.assertEqual([str(Path(value).resolve()) for value in effective], [str(self.fixture.target.resolve())])
        self.finish()

    def test_safe_directory_false_does_not_add_checkout_path(self):
        self.env["CACHEW_SET_SAFE_DIRECTORY"] = "false"
        self.assertEqual(self.flow(), (True, True))
        self.assertEqual(self.safe_directories(), [])
        self.finish()

    def test_safe_directory_empty_reset_and_deduplication_survive_rebuild(self):
        self.prepare()
        self.assertTrue(self.fixture.attempt())
        trusted = str(self.fixture.workspace / "explicitly-trusted")
        self.real_git("config", "--global", "--add", "safe.directory", "")
        self.real_git("config", "--global", "--add", "safe.directory", trusted)
        self.real_git("config", "--global", "--add", "safe.directory", trusted)
        self.assertTrue(self.activate())
        self.assertEqual(self.safe_directories(), ["", trusted])
        self.prepare()
        self.assertEqual(self.safe_directories(), ["", trusted])
        self.assertTrue(self.activate("true"))
        self.assertEqual(self.safe_directories(), ["", trusted])
        self.finish()

    def test_downstream_new_repository_uses_global_cache_and_leaves_no_credentials_after_the_job(self):
        self.fixture.repository("dependency")
        expected = "basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.fixture.cache.required_auth["dependency"] = expected
        self.assertEqual(self.flow(), (True, True))
        self.assertEqual(self.real_git("config", "--global", "--get", "checkout.workers"), "0")
        self.assertEqual(self.real_git("config", "--global", "--get", "checkout.thresholdForParallelism"), "50")
        self.assertEqual(self.real_git("config", "--global", "--get", "submodule.fetchJobs"), "0")
        self.assertEqual(self.real_git("config", "--global", "--get", "gc.auto"), "0")
        upstream_before = list(self.fixture.upstream.requests)
        self.real_git("clone", self.fixture.upstream.url + "/org/dependency", "dependency")
        self.assertEqual(self.fixture.upstream.requests, upstream_before)
        requests = [request for request in self.fixture.cache.requests if "/dependency/" in request[1]]
        self.assertTrue(requests)
        self.assertTrue(all(auth.lower() == expected.lower() for _, _, auth in requests))
        self.finish()

    def test_workflow_token_is_not_sent_to_cache_paths_of_another_host(self):
        # The cache serves /git/<host>/...: a URL taken from repository content
        # (a submodule, say) can name the cache with any host in its path.
        self.assertEqual(self.flow(), (True, True))
        for command in ("git", self.env["CACHEW_REAL_GIT"]):
            subprocess.run(
                [command, "ls-remote", self.fixture.cache.url + "/git/other.example/org/main"],
                cwd=self.fixture.root,
                env=self.env,
                capture_output=True,
                timeout=30,
            )
        requests = [request for request in self.fixture.cache.requests if "/other.example/" in request[1]]
        self.assertTrue(requests)
        self.assertEqual([auth for _, _, auth in requests if auth], [])
        self.finish()

    def test_activation_leaves_a_repository_outside_the_workspace_alone(self):
        # The configure step runs even after the checkout refused its path.
        self.assertEqual(self.flow(), (True, True))
        outside = self.fixture.root / "outside"
        upstream = self.fixture.upstream.url + "/org/main"
        self.real_git("init", "-q", str(outside))
        self.real_git("-C", str(outside), "config", "remote.origin.url", upstream)
        self.real_git("-C", str(outside), "config", "remote.origin.promisor", "true")
        before = (outside / ".git" / "config").read_bytes()
        self.env["CACHEW_PATH"] = os.path.relpath(outside, self.env["GITHUB_WORKSPACE"])
        self.activate()
        self.assertEqual((outside / ".git" / "config").read_bytes(), before)
        self.finish()

    def path_git(self, *args):
        """Git as a later step runs it: the shim on PATH."""
        result = subprocess.run(
            ["git", *args], cwd=self.fixture.workspace, env=self.env, text=True, capture_output=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def shim_hands_to_the_wrapper(self, *arguments):
        """Only the wrapper turns caching off and retries a failed cache read upstream."""
        self.fixture.repository("dependency")
        self.assertEqual(self.flow(), (True, True))
        self.path_git("clone", self.fixture.upstream.url + "/org/dependency", "dependency")
        self.path_git("config", "--global", "alias.f.x.command", "fetch origin")
        self.fixture.cache.fail_repo = "dependency"
        self.path_git("-C", "dependency", *arguments)
        self.assertTrue((Path(self.env["CACHEW_SESSION_DIR"]) / "cache-disabled").exists())
        self.finish()

    def test_the_shim_hands_an_alias_in_its_subsection_form_to_the_wrapper(self):
        self.shim_hands_to_the_wrapper("f.x")

    def test_the_shim_hands_a_fetch_after_an_attribute_source_option_to_the_wrapper(self):
        self.shim_hands_to_the_wrapper("--attr-source", "HEAD", "fetch", "origin")

    def test_aliases_are_routed_like_the_commands_they_expand_to(self):
        self.fixture.repository("dependency")
        self.assertEqual(self.flow(), (True, True))
        upstream = self.fixture.upstream.url
        self.assertEqual(self.real_git("config", "--global", "--get", f"url.{upstream}/.pushInsteadOf"), upstream + "/")
        self.path_git("config", "--global", "alias.get", "clone")
        self.path_git("config", "--global", "alias.pu", "push")
        self.path_git("get", upstream + "/org/dependency", "dependency")
        cached = [request for request in self.fixture.cache.requests if "/dependency/" in request[1]]
        self.assertTrue(cached)
        requests = list(self.fixture.cache.requests)
        self.path_git("-C", "dependency", "pu", "origin", "HEAD:refs/heads/aliased")
        # An alias given only on the command line, and a push that never passes the shim.
        self.path_git("-C", "dependency", "-c", "alias.send=push", "send", "origin", "HEAD:refs/heads/inline")
        self.real_git("-C", "dependency", "push", "origin", "HEAD:refs/heads/direct")
        self.assertEqual(self.fixture.cache.requests, requests)
        remote = self.fixture.project_root / "org/dependency"
        for branch in ("aliased", "inline", "direct"):
            self.assertEqual(fixtures.git(remote, "rev-parse", branch), fixtures.git(remote, "rev-parse", "main"))
        # A failing cache read through an alias falls back and turns caching off.
        self.fixture.cache.fail_repo = "dependency"
        self.path_git("config", "--global", "alias.up", "fetch")
        self.path_git("-C", "dependency", "up", "origin")
        self.assertTrue((Path(self.env["CACHEW_SESSION_DIR"]) / "cache-disabled").exists())
        self.finish()

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required for the downstream FetchContent regression")
    def test_real_cmake_fetchcontent_uses_the_post_checkout_global_configuration(self):
        self.fixture.repository("cmake-dependency")
        dependency = self.fixture.project_root / "org/cmake-dependency"
        (dependency / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.24)\nproject(dependency NONE)\n")
        fixtures.git(dependency, "add", "CMakeLists.txt", env=self.env)
        fixtures.git(dependency, "commit", "-m", "CMake dependency", env=self.env)
        commit = fixtures.git(dependency, "rev-parse", "HEAD")
        self.assertEqual(self.flow(), (True, True))
        self.fixture.upstream.block_submodules = True
        source = self.fixture.root / "cmake-source"
        source.mkdir()
        (source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.24)\nproject(consumer NONE)\ninclude(FetchContent)\n"
            f"FetchContent_Declare(downstream GIT_REPOSITORY {self.fixture.upstream.url}/org/cmake-dependency GIT_TAG {commit})\n"
            "FetchContent_MakeAvailable(downstream)\n"
        )
        result = subprocess.run(
            ["cmake", "-S", str(source), "-B", str(self.fixture.root / "cmake-build")],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([url for _, url, _ in self.fixture.cache.requests if "/cmake-dependency/" in url])
        self.assertFalse([url for _, url, _ in self.fixture.upstream.requests if "/cmake-dependency/" in url])
        self.finish()

    def config_entries(self, file):
        result = subprocess.run(
            [self.env["CACHEW_REAL_GIT"], "config", "--file", str(file), "--null", "--list"],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [tuple(item.split("\n", 1)) for item in result.stdout.split("\0")[:-1]]

    def url_setting(self, url, key):
        result = subprocess.run(
            [self.env["CACHEW_REAL_GIT"], "config", "--get-urlmatch", key, url],
            cwd=self.fixture.workspace,
            env=self.env,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def test_fallback_aborts_only_the_checked_out_repository_when_it_crawls(self):
        # A fetch stuck on a lossy connection must fail fast so actions/checkout
        # retries it on a new connection, instead of crawling until the job
        # times out. Submodule updates are not retried, so they stay unlimited.
        self.fixture.cache.health = 503
        self.prepare()
        repository = f"{self.fixture.upstream.url}/{self.env['CACHEW_REPOSITORY']}"
        self.assertEqual(self.url_setting(repository, "http.lowSpeedLimit"), "524288")
        self.assertEqual(self.url_setting(repository, "http.lowSpeedTime"), "60")
        self.assertIsNone(self.url_setting(f"{self.fixture.upstream.url}/org/submodule", "http.lowSpeedLimit"))
        self.assertFalse(self.fixture.attempt())
        self.checkout()
        self.assertTrue(self.fixture.upstream.requests)
        self.assertFalse(self.activate())
        # Later steps use global.config, which never carries the fallback limit.
        self.assertIsNone(self.url_setting(repository, "http.lowSpeedLimit"))
        self.finish()

    def test_inherited_rewrite_cannot_redirect_unhealthy_fallback(self):
        key = f"url.{self.fixture.cache.url}/git/{self.fixture.upstream.server.server_address[0]}:{self.fixture.upstream.server.server_port}/.insteadOf"
        fixtures.git(self.fixture.workspace, "config", "--global", key, self.fixture.upstream.url + "/", env=self.env)
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()
        self.fixture.cache.health = 503
        self.assertEqual(self.flow(), (False, False))
        # Three probes: the checkout's attempts, each retried after a 503; activation adds none.
        self.fixture.assert_cache_attempts([], attempts=3)
        self.assertTrue(self.fixture.upstream.requests)
        # The caller's inherited rewrite survives unchanged, outside this job's configuration.
        self.finish_without_credential_assertion()

    def finish_without_credential_assertion(self):
        self.end_job()

    def test_cache_requests_keep_verified_tls_and_no_redirects_and_are_removed_with_the_rewrite(self):
        self.assertEqual(self.flow(), (True, True))
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        cache = self.fixture.cache.url

        def value(key):
            result = subprocess.run(
                ["git", "config", "--file", str(directory / "global.config"), "--get", key],
                env=self.env,
                text=True,
                capture_output=True,
            )
            return result.stdout.strip()

        self.assertEqual(value(f"http.{cache}/.sslVerify"), "true")
        self.assertEqual(value(f"http.{cache}/.followRedirects"), "false")
        # A later checkout takes every cachew key out again.
        self.prepare()
        self.assertEqual(value(f"http.{cache}/.sslVerify"), "")
        self.assertEqual(value(f"http.{cache}/.followRedirects"), "")
        self.assertTrue(self.activate())
        self.assertEqual(value(f"http.{cache}/.sslVerify"), "true")
        self.finish()

    def test_persist_false_does_not_leave_current_token_in_globals(self):
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "false"
        self.assertEqual(self.flow(), (True, True))
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        encoded = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode())
        for file in directory.rglob("*"):
            if file.is_file():
                self.assertNotIn(fixtures.TOKEN.encode(), file.read_bytes())
                self.assertNotIn(encoded, file.read_bytes())
        self.finish()

    def test_a_checkout_that_did_not_succeed_leaves_no_token_for_the_cache(self):
        # Activation also runs after a checkout that failed or was refused (a
        # fork's pull request code, say), in a step the job lets continue.
        # actions/checkout then leaves no credentials for later steps.
        self.fixture.repository("dependency")
        self.prepare()
        for outcome in ("false", ""):
            with self.subTest(outcome=outcome):
                self.env["CACHEW_CHECKED_OUT"] = outcome
                self.assertTrue(self.activate(available="true"))
                directory = Path(self.env["CACHEW_SESSION_DIR"])
                encoded = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode())
                for file in directory.rglob("*"):
                    if file.is_file():
                        self.assertNotIn(encoded, file.read_bytes())
        before = len(self.fixture.cache.requests)
        self.real_git("clone", self.fixture.upstream.url + "/org/dependency", "dependency")
        requests = self.fixture.cache.requests[before:]
        self.assertTrue(requests)
        self.assertEqual({auth for _, _, auth in requests}, {None})
        self.env["CACHEW_CHECKED_OUT"] = "true"
        self.finish()

    def test_later_reads_without_a_token_do_not_send_inherited_headers_to_the_cache(self):
        # No token is recorded with persist-credentials false, so nothing but the
        # cache-scoped reset keeps the job's unscoped header from the cache.
        self.fixture.repository("dependency")
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "false"
        self.assertEqual(self.flow(), (True, True))
        self.real_git("config", "--global", "http.extraHeader", "Authorization: basic aW5oZXJpdGVk")
        before = len(self.fixture.cache.requests)
        self.real_git("clone", self.fixture.upstream.url + "/org/dependency", "dependency")
        requests = self.fixture.cache.requests[before:]
        self.assertTrue(requests)
        self.assertEqual({auth for _, _, auth in requests}, {None})
        self.real_git("config", "--global", "--unset", "http.extraHeader")
        self.finish()

    def test_secondary_checkout_keeps_primary_credentials_and_uses_its_own_scope(self):
        self.fixture.repository("secondary")
        self.fixture.repository("dependency")
        self.assertEqual(self.flow(), (True, True))
        self.env.update(
            CACHEW_REPOSITORY="org/secondary",
            CACHEW_PATH="secondary",
            CACHEW_REF="main",
            CACHEW_TOKEN="secondary-private-token",
        )
        self.assertEqual(self.flow(), (True, True))
        self.real_git("ls-remote", self.fixture.upstream.url + "/org/dependency")
        self.real_git("ls-remote", self.fixture.upstream.url + "/org/secondary")
        expected = {
            "dependency": "basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode(),
            "secondary": "basic " + base64.b64encode(b"x-access-token:secondary-private-token").decode(),
        }
        for name, header in expected.items():
            last = [auth for _, url, auth in self.fixture.cache.requests if f"/{name}/" in url][-1]
            self.assertEqual(last.lower(), header.lower())
        self.finish()

    def test_secondary_persist_false_does_not_erase_primary_token(self):
        self.fixture.repository("secondary")
        self.assertEqual(self.flow(), (True, True))
        self.env.update(
            CACHEW_REPOSITORY="org/secondary",
            CACHEW_PATH="secondary",
            CACHEW_REF="main",
            CACHEW_TOKEN="do-not-persist-secondary",
            # Padded, as actions/checkout trims inputs before parsing them.
            CACHEW_PERSIST_CREDENTIALS=" false\n",
        )
        self.assertEqual(self.flow(), (True, True))
        state = json.loads((Path(self.env["CACHEW_SESSION_DIR"]) / "state.json").read_text())
        self.assertNotIn("do-not-persist-secondary", json.dumps(state))
        self.assertNotIn(base64.b64encode(b"x-access-token:do-not-persist-secondary").decode(), json.dumps(state))
        self.real_git("ls-remote", self.fixture.upstream.url + "/org/main")
        expected = "basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        last = [auth for _, url, auth in self.fixture.cache.requests if "/main/" in url][-1]
        self.assertEqual(last.lower(), expected.lower())
        self.finish()

    def test_same_path_persist_false_checkout_removes_the_earlier_token(self):
        # actions/checkout removes a path's persisted credentials before checking
        # out into it again: the usual way to drop them before running PR code.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        header = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertIn(header, (session / "state.json").read_text())
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "false"
        self.assertEqual(self.flow(), (False, True))
        for file in Path(self.env["RUNNER_TEMP"]).rglob("*"):
            if file.is_file():
                content = file.read_bytes()
                self.assertNotIn(header.encode(), content, file)
                self.assertNotIn(fixtures.TOKEN.encode(), content, file)
        self.finish()

    def test_a_whitespace_only_persist_credentials_records_no_token(self):
        # actions/checkout reads (getInput('persist-credentials') || 'false'):
        # an input that trims to nothing is false, whatever action.yml's default.
        self.env["CACHEW_PERSIST_CREDENTIALS"] = " \n"
        self.assertEqual(self.flow(), (True, True))
        header = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertNotIn(header, (Path(self.env["CACHEW_SESSION_DIR"]) / "state.json").read_text())
        self.finish()

    def test_a_deleted_checkout_no_longer_gives_the_cache_its_token(self):
        # actions/checkout keeps a path's credentials in that repository, so they
        # go with it: a checkout at the root cleans away a nested one, a step
        # removes it. A later activation must not write its token again.
        self.fixture.repository("secondary")
        self.assertEqual(self.flow(), (True, True))
        inputs = ("CACHEW_REPOSITORY", "CACHEW_PATH", "CACHEW_REF", "CACHEW_TOKEN")
        primary = {name: self.env.get(name, "") for name in inputs}
        self.env.update(
            CACHEW_REPOSITORY="org/secondary",
            CACHEW_PATH="removed",
            CACHEW_REF="main",
            CACHEW_TOKEN="secondary-token",
        )
        self.assertEqual(self.flow(), (True, True))
        header = base64.b64encode(b"x-access-token:secondary-token").decode()
        session = Path(self.env["CACHEW_SESSION_DIR"])
        self.assertIn(header, (session / "state.json").read_text())
        shutil.rmtree(self.fixture.workspace / "removed")
        self.env.update(primary)
        # Into the existing checkout, so the standard checkout runs.
        self.assertEqual(self.flow(), (False, True))
        self.assertNotIn(header, (session / "state.json").read_text())
        self.assertNotIn(header, (session / "global.config").read_text())
        self.finish()

    def recorded(self):
        return json.loads((Path(self.env["CACHEW_SESSION_DIR"]) / "state.json").read_text())

    def test_dropping_another_checkouts_token_keeps_the_workflow_repositorys_default(self):
        # Both checkouts use the job's token, as most jobs do: removing the
        # secondary path's credentials must not take the primary's with them.
        self.fixture.repository("secondary")
        self.assertEqual(self.flow(), (True, True))
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertEqual(self.recorded()["defaults"], {self.fixture.upstream.url: header})
        self.env.update(CACHEW_REPOSITORY="org/secondary", CACHEW_PATH="secondary", CACHEW_REF="main")
        self.assertEqual(self.flow(), (True, True))
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "false"
        self.assertEqual(self.flow(), (False, True))
        state = self.recorded()
        self.assertEqual(state["defaults"], {self.fixture.upstream.url: header})
        self.assertEqual(state["tokens"], {f"{self.fixture.upstream.url}/org/main": header})
        self.finish()

    def test_a_dropped_paths_token_does_not_stay_for_the_same_repository_elsewhere(self):
        # The same repository at two paths with two tokens: dropping the second
        # path's credentials leaves the first path's token, not the second's.
        self.assertEqual(self.flow(), (True, True))
        self.env.update(CACHEW_PATH="again", CACHEW_TOKEN="second-path-token")
        self.assertEqual(self.flow(), (True, True))
        second = base64.b64encode(b"x-access-token:second-path-token").decode()
        self.assertIn(second, json.dumps(self.recorded()))
        self.env["CACHEW_PERSIST_CREDENTIALS"] = "false"
        self.assertEqual(self.flow(), (False, True))
        state = self.recorded()
        self.assertNotIn(second, json.dumps(state))
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertEqual(state["tokens"], {f"{self.fixture.upstream.url}/org/main": header})
        self.assertEqual(state["defaults"], {self.fixture.upstream.url: header})
        self.finish()

    def test_a_failed_activation_does_not_keep_the_token_of_a_path_checked_out_again(self):
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        header = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertIn(header, (session / "state.json").read_text())
        # The second checkout's activation fails after reading the session.
        self.env.update(CACHEW_PERSIST_CREDENTIALS="false", CACHEW_CACHEW_URL="https://evil.example")
        self.assertFalse(self.activate())
        for file in session.rglob("*"):
            if file.is_file():
                self.assertNotIn(header.encode(), file.read_bytes(), file)

    def test_disabled_cache_secondary_private_checkout_uses_its_current_token(self):
        self.fixture.repository("secondary")
        secondary_token = "secondary-private-token"
        self.fixture.upstream.required_auth.update(
            main="basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode(),
            secondary="basic " + base64.b64encode(f"x-access-token:{secondary_token}".encode()).decode(),
        )
        self.fixture.cache.fail_repo = "main"
        self.assertEqual(self.flow(), (False, False))
        cache_before = list(self.fixture.cache.requests)
        self.env.update(
            CACHEW_REPOSITORY="org/secondary", CACHEW_PATH="secondary", CACHEW_REF="main", CACHEW_TOKEN=secondary_token
        )
        self.assertEqual(self.flow(), (False, False))
        # Upstream credentials are repository-scoped by actions/checkout, exactly
        # as without cachew; they are never promoted to job-wide headers.
        for checkout in (self.fixture.target, self.fixture.workspace / "secondary"):
            result = subprocess.run(
                ["git", "-C", str(checkout), "ls-remote", "origin"],
                cwd=self.fixture.workspace,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.cache.requests, cache_before)
        self.finish()

    def test_cachew_false_after_activation_checks_out_upstream_with_throwaway_globals(self):
        self.assertEqual(self.flow(), (True, True))
        global_config = Path(self.env["CACHEW_SESSION_DIR"]) / "global.config"
        self.env["CACHEW_PATH"] = "second-checkout"
        # action.yml with cachew: false: prepare, official checkout, configure; no cache attempt.
        self.prepare()
        self.checkout()
        self.assertTrue((self.fixture.workspace / "second-checkout/deps/child/deep/grand/content.txt").exists())
        self.assertNotIn("git-credentials-", global_config.read_text())
        self.assertTrue(self.activate())
        self.assertNotIn("git-credentials-", global_config.read_text())
        self.finish()

    def test_cache_credentials_never_appear_in_git_arguments(self):
        log = self.fixture.root / "git-argv.log"
        logging = self.fixture.root / "logging-git"
        logging.mkdir()
        (logging / "git").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{shutil.which("git")}" "$@"\n')
        (logging / "git").chmod(0o700)
        self.env["PATH"] = f"{logging}{os.pathsep}{self.env['PATH']}"
        self.assertEqual(self.flow(), (True, True))
        self.assertIn(self.fixture.cache.url, (Path(self.env["CACHEW_SESSION_DIR"]) / "global.config").read_text())
        encoded = base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.assertNotIn(encoded, log.read_text())
        # Cleanup too: a later checkout's prepare removes the entries, headers included.
        self.assertIn(encoded, (Path(self.env["CACHEW_SESSION_DIR"]) / "global.config").read_text())
        self.run_step("session")
        self.assertNotIn(encoded, (Path(self.env["CACHEW_SESSION_DIR"]) / "global.config").read_text())
        self.assertIn("--unset-all", log.read_text())
        self.assertNotIn(encoded, log.read_text())
        self.finish()

    def test_conditional_includes_stay_conditional_in_checkout_copies(self):
        # A matching includeIf must not be copied unconditionally into the
        # flattened file the official checkout uses, first or later in the job.
        subprocess.run(["git", "init", "-q", str(self.fixture.workspace)], check=True)
        conditional = self.fixture.home / "conditional.gitconfig"
        conditional.write_text("[fixture]\n\tconditional = only-here\n")
        with (self.fixture.home / ".gitconfig").open("a") as config:
            config.write(f'[includeIf "gitdir:{self.fixture.workspace.resolve()}/"]\n\tpath = {conditional}\n')
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()
        self.assertEqual(self.flow(), (True, True))
        copy = Path(self.env["CACHEW_SESSION_DIR"]) / "checkout.config"
        for prepared in ("first", "later"):
            if prepared == "later":
                self.prepare()
            text = copy.read_text()
            self.assertIn("includeif", text.lower(), prepared)
            self.assertNotIn("only-here", text, prepared)
        self.activate()
        self.finish()

    def test_locked_global_file_disables_the_cache_for_a_later_checkout(self):
        # A later checkout cannot take cachew's keys out of the job's locked file:
        # neither that checkout nor later reads may be rewritten to the cache.
        self.assertEqual(self.flow(), (True, True))
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        global_config = directory / "global.config"
        self.assertIn(self.fixture.cache.url, global_config.read_text())
        # Job-owned settings: a rewrite shaped like cachew's, its header, and an
        # inherited `safe.directory=*` the job reset afterwards.
        mirror = "https://mirror.example"
        self.real_git("config", "--file", str(global_config), "--add", f"url.{mirror}/git/gitlab.com/.insteadOf", "https://gitlab.com/")
        self.real_git("config", "--file", str(global_config), "--add", f"http.{mirror}/.extraheader", "X-Mirror: job")
        inherited = self.fixture.root / "inherited.gitconfig"
        inherited.write_text("[safe]\n\tdirectory = *\n")
        self.real_git("config", "--file", str(global_config), "--add", "include.path", str(inherited))
        self.real_git("config", "--file", str(global_config), "--add", "safe.directory", "")
        lock_file = directory / "global.config.lock"
        lock_file.touch()
        try:
            self.prepare()
        finally:
            lock_file.unlink()
        self.assertTrue((directory / "cache-disabled").exists())
        # The official checkout's flattened copy leaves cachew's keys out; the job's stay.
        checkout_config = directory / "checkout.config"
        self.assertEqual(self.env["GIT_CONFIG_GLOBAL"], str(checkout_config))
        self.assertNotIn(self.fixture.cache.url, checkout_config.read_text())
        for config in (checkout_config, global_config):
            self.assertIn(f"{mirror}/git/gitlab.com/", config.read_text(), config.name)
            self.assertIn("X-Mirror: job", config.read_text(), config.name)
        self.assert_caller_settings_visible()
        upstream = f"{self.fixture.upstream.url}/org/child"
        self.assertEqual(self.real_git("ls-remote", "--get-url", upstream), upstream)
        # As action.yml runs it: the cachew step bypasses, the official checkout reaches upstream.
        self.env["CACHEW_PATH"] = "second-checkout"
        self.assertFalse(self.fixture.attempt())
        self.checkout()
        self.assertTrue((self.fixture.workspace / "second-checkout/content.txt").exists())
        self.assertFalse(self.activate())
        self.assertEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())
        # Only the checkout's own entry comes back, after the job's reset: not the inherited `*`.
        values = self.safe_directories(global_config)
        effective = values[len(values) - values[::-1].index(""):]
        target = str((self.fixture.workspace / "second-checkout").resolve())
        self.assertEqual([str(Path(value).resolve()) for value in effective], [target])
        self.finish()

    def test_relative_include_added_after_activation_reaches_a_later_checkout(self):
        # Git resolves a relative include.path against the including file's directory.
        self.assertEqual(self.flow(), (True, True))
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        (directory / "extra.config").write_text("[fixture]\n\textra = beside-the-global-file\n")
        self.real_git("config", "--global", "--add", "include.path", "extra.config")
        self.prepare()
        self.assertEqual(self.real_git("config", "--global", "--get", "fixture.extra"), "beside-the-global-file")
        self.assertTrue(self.activate("true"))
        self.finish()

    def test_conditional_includes_inside_a_job_include_keep_their_paths(self):
        # Relative paths in an included file's includeIf (the path and a gitdir:./
        # pattern) mean that file's directory, not the flattened copy's.
        self.assertEqual(self.flow(), (True, True))
        included = self.fixture.root / "job-includes"
        nested = included / "nested"
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (included / "by-path.config").write_text("[fixture]\n\tbypath = matched\n")
        (included / "by-gitdir.config").write_text("[fixture]\n\tbygitdir = matched\n")
        (included / "conditions.config").write_text(
            f'[includeIf "gitdir:{nested.resolve()}/"]\n\tpath = by-path.config\n'
            '[includeIf "gitdir:./nested/"]\n\tpath = by-gitdir.config\n'
        )
        self.real_git("config", "--global", "--add", "include.path", str(included / "conditions.config"))
        self.prepare()
        for key in ("fixture.bypath", "fixture.bygitdir"):
            self.assertEqual(self.real_git("-C", str(nested), "config", "--global", "--includes", "--get", key), "matched", key)
        self.assertTrue(self.activate("true"))
        self.finish()

    def test_activation_never_writes_past_another_writers_lock(self):
        # Activation writes the job's file under Git's lock: while another writer
        # holds it, caching stays off rather than racing that writer.
        self.env["CACHEW_SET_SAFE_DIRECTORY"] = "false"
        self.prepare()
        self.assertTrue(self.fixture.attempt())
        global_config = Path(self.env["CACHEW_SESSION_DIR"]) / "global.config"
        lock = global_config.with_name("global.config.lock")
        lock.touch()
        try:
            self.assertFalse(self.activate())
        finally:
            lock.unlink()
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())
        self.assertEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))
        self.real_git("ls-remote", f"{self.fixture.upstream.url}/org/main")
        self.finish()

    def test_session_python_outlives_a_linked_virtual_environment(self):
        self.assert_session_outlives_virtual_environment("--symlinks")

    def test_session_python_outlives_a_copied_virtual_environment(self):
        self.assert_session_outlives_virtual_environment("--copies")

    def assert_session_outlives_virtual_environment(self, layout):
        # Removing the environment the session started from, later in the job,
        # must not break the Git wrapper or a later checkout.
        python = shutil.which("python3", path=self.env["PATH"])
        environment = self.fixture.root / "venv"
        subprocess.run([python, "-m", "venv", layout, "--without-pip", str(environment)], check=True)
        self.env["PATH"] = f"{environment / 'bin'}{os.pathsep}{self.env['PATH']}"
        self.assertEqual(self.flow(), (True, True))
        self.assertFalse(Path(self.env["CACHEW_PYTHON"]).is_relative_to(environment))
        shutil.rmtree(environment)
        result = subprocess.run(
            ["git", "ls-remote", f"{self.fixture.upstream.url}/org/main"], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["CACHEW_PATH"] = "second-checkout"
        self.assertEqual(self.flow(), (True, True))
        self.finish()

    def test_checkout_include_path_write_keeps_the_job_settings_visible(self):
        # actions/checkout's submodule setup: `git config --global include.path <credentials>`,
        # without --add, against the checkout-time global file.
        self.prepare()
        credentials = self.fixture.runtime / "git-credentials-fixture.config"
        credentials.write_text("")
        self.real_git("config", "--global", "include.path", str(credentials))
        self.assertEqual(self.real_git("config", "--global", "--get", "checkout.workers"), "0")
        self.assertEqual(self.real_git("config", "--global", "--get", "user.name"), "Fixture")
        self.assertEqual(self.real_git("config", "--global", "--get-all", "include.path"), str(credentials))
        self.assertTrue(self.activate())
        self.finish()

    def test_partial_clone_while_a_locked_file_keeps_the_rewrite_is_pinned(self):
        # After activation turned caching off without being able to remove the
        # rewrite, the session still knows where it points, so a partial clone
        # made meanwhile is pinned and its lazy fetches stay upstream.
        self.assertEqual(self.flow(), (True, True))
        global_config = Path(self.env["CACHEW_SESSION_DIR"]) / "global.config"
        self.fixture.update_main()
        old_blob = self.real_git("-C", str(self.fixture.project_root / "org/main"), "rev-parse", "HEAD~1:content.txt")
        # Nothing for activation to copy back, so it gets past safe.directory under the lock.
        self.env.update(CACHEW_PATH="second", CACHEW_SET_SAFE_DIRECTORY="false")
        lock = global_config.with_name("global.config.lock")
        lock.touch()
        try:
            self.prepare()
            self.assertFalse(self.fixture.attempt())
            self.checkout()
            self.assertFalse(self.activate())
            self.assertIn(self.fixture.cache.url, global_config.read_text())
            partial = self.fixture.root / "partial"
            url = f"{self.fixture.upstream.url}/org/main"
            cloned = subprocess.run(
                ["git", "clone", "-q", "--filter=blob:none", "--no-checkout", url, str(partial)],
                env=self.env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            cache_before = list(self.fixture.cache.requests)
            self.assertEqual(self.real_git("-C", str(partial), "cat-file", "-p", old_blob), "main")
            self.assertEqual(self.fixture.cache.requests, cache_before)
        finally:
            lock.unlink()
        self.finish()

    def test_rewrite_kept_by_a_locked_file_never_takes_lazy_fetches(self):
        # A later filtered checkout cannot remove the rewrite from the locked job
        # file: its promisor origin is pinned upstream anyway, and the next
        # wrapped command removes the rewrite once the lock is gone.
        self.assertEqual(self.flow(), (True, True))
        global_config = Path(self.env["CACHEW_SESSION_DIR"]) / "global.config"
        self.fixture.update_main()
        old_blob = self.real_git("-C", str(self.fixture.project_root / "org/main"), "rev-parse", "HEAD~1:content.txt")
        self.env.update(CACHEW_FILTER="blob:none", CACHEW_SUBMODULES="false", CACHEW_PATH="filtered")
        lock = global_config.with_name("global.config.lock")
        lock.touch()
        try:
            self.prepare()
            self.assertFalse(self.fixture.attempt())
            self.checkout()
            self.assertFalse(self.activate())
            self.assertIn(self.fixture.cache.url, global_config.read_text())
            target = str(self.fixture.workspace / "filtered")
            cache_before = list(self.fixture.cache.requests)
            self.fixture.upstream.requests.clear()
            self.assertEqual(self.real_git("-C", target, "cat-file", "-p", old_blob), "main")
            self.assertEqual(self.fixture.cache.requests, cache_before)
            self.assertIn("POST", [method for method, _, _ in self.fixture.upstream.requests])
        finally:
            lock.unlink()
        wrapped = subprocess.run(
            ["git", "ls-remote", f"{self.fixture.upstream.url}/org/main"], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())
        self.finish()

    def test_partial_clone_made_before_the_session_keeps_lazy_fetches_upstream(self):
        # An earlier step made a partial clone (actions/checkout with a filter, or
        # git clone --filter) before any cachew activation: no wrapper saw it, so
        # nothing pinned it. Activation's job-wide rewrite must not take its lazy
        # fetches to the cache, where a failure has no upstream retry.
        self.fixture.update_main()
        old_blob = self.real_git("-C", str(self.fixture.project_root / "org/main"), "rev-parse", "HEAD~1:content.txt")
        early = self.fixture.workspace / "early"
        cloned = subprocess.run(
            ["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"{self.fixture.upstream.url}/org/main", str(early)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        self.env["CACHEW_PATH"] = "second"
        self.assertEqual(self.flow(), (True, True))
        cache_before = list(self.fixture.cache.requests)
        self.assertEqual(self.real_git("-C", str(early), "cat-file", "-p", old_blob), "main")
        self.assertEqual(self.fixture.cache.requests, cache_before)
        self.finish()

    def test_a_later_checkout_whose_session_cannot_prepare_keeps_checkout_writes_out(self):
        # A later checkout's prepare fails (here checkout.config cannot be
        # written). The standard checkout then runs, and with submodules it
        # writes an include of its credentials file to the global file: that
        # must be a throwaway copy, and later steps get the job's file back.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        global_config = session / "global.config"
        (session / "checkout.config").unlink()
        (session / "checkout.config").mkdir()
        self.env.update(CACHEW_PATH="second", CACHEW_SUBMODULES="true")
        self.run_step("session")
        outputs = fixtures.runner_file(self.fixture.root / "output")
        self.assertNotEqual(outputs.get("ready"), "true")
        self.env.update(
            CACHEW_RESTORE_GLOBAL=outputs["restore-global"],
            CACHEW_RESTORE_FROM=outputs["restore-from"],
            CACHEW_RESTORE_SAFE_DIRECTORIES=outputs["restore-safe-directories"],
        )
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        self.assertNotEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))
        self.checkout()
        self.run_step("restore")
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        self.assertEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))
        self.assertNotIn("git-credentials", global_config.read_text())
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())
        # set-safe-directory: the entry the standard checkout added to its copy
        # reaches the job's file, as activate would have copied it.
        safe = self.real_git("config", "--file", str(global_config), "--get-all", "safe.directory").splitlines()
        self.assertIn(str(self.fixture.workspace.resolve() / "second"), safe)

    def later_checkout_without_state(self, damage):
        # The job file holds cachew's rewrite and token, but the session state
        # that records them cannot be read. The standard checkout that follows
        # must reach no cache, and later steps must find the keys gone.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        global_config = session / "global.config"
        self.assertIn("AUTHORIZATION", global_config.read_text())
        damage(session / "state.json")
        self.env.update(CACHEW_PATH="second", CACHEW_SUBMODULES="true")
        self.run_step("session")
        outputs = fixtures.runner_file(self.fixture.root / "output")
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        checkout_config = Path(self.env["GIT_CONFIG_GLOBAL"])
        self.assertNotEqual(checkout_config, global_config)
        self.assertNotIn(self.fixture.cache.url, checkout_config.read_text())
        self.assertNotIn("AUTHORIZATION", checkout_config.read_text())
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())
        self.assertNotIn("AUTHORIZATION", global_config.read_text())
        self.checkout()
        if outputs.get("ready") == "true":
            self.assertFalse(self.activate())
        else:
            self.env.update(
                CACHEW_RESTORE_GLOBAL=outputs["restore-global"],
                CACHEW_RESTORE_FROM=outputs.get("restore-from", ""),
                CACHEW_RESTORE_SAFE_DIRECTORIES=outputs.get("restore-safe-directories", "0"),
            )
            self.run_step("restore")
            self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        self.assertEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))
        before = list(self.fixture.cache.requests)
        self.real_git("ls-remote", self.fixture.upstream.url + "/org/main")
        self.assertEqual(self.fixture.cache.requests, before)
        self.assertNotIn(self.fixture.cache.url, global_config.read_text())

    def test_a_later_checkout_after_the_session_state_is_deleted_reaches_no_cache(self):
        self.later_checkout_without_state(lambda state: state.unlink())

    def test_a_later_checkout_after_the_session_state_is_corrupted_reaches_no_cache(self):
        self.later_checkout_without_state(lambda state: state.write_text('{"managed": ['))

    def test_a_later_checkout_whose_includes_cannot_flatten_still_gets_a_private_file(self):
        # An include of itself makes flattening fail, in prepare and in the
        # fallback copy alike: the standard checkout must still write elsewhere.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        global_config = session / "global.config"
        looping = self.fixture.root / "looping.config"
        looping.write_text(f"[include]\n\tpath = {looping}\n")
        self.real_git("config", "--file", str(global_config), "--add", "include.path", str(looping))
        self.env.update(CACHEW_PATH="second")
        self.run_step("session")
        outputs = fixtures.runner_file(self.fixture.root / "output")
        self.assertNotEqual(outputs.get("ready"), "true")
        self.assertEqual(outputs.get("restore-global"), str(global_config))
        self.env.update(fixtures.runner_file(self.fixture.root / "env"))
        self.assertNotEqual(self.env["GIT_CONFIG_GLOBAL"], str(global_config))

    def test_a_later_checkout_whose_session_cannot_prepare_reaches_no_cache_from_a_locked_file(self):
        # The job's file is locked, so prepare cannot take cachew's rewrite out
        # of it. The standard checkout's copy must still leave cachew's keys out,
        # and inline the job's includes: with submodules, checkout replaces the
        # global include.path with its credentials file.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        global_config = session / "global.config"
        inherited = self.fixture.root / "inherited.config"
        inherited.write_text("[user]\n\tname = Inherited\n")
        self.real_git("config", "--file", str(global_config), "--add", "include.path", str(inherited))
        self.assertIn(self.fixture.cache.url, global_config.read_text())
        (session / "checkout.config").unlink()
        (session / "checkout.config").mkdir()
        lock = session / "global.config.lock"
        lock.touch()
        try:
            self.env.update(CACHEW_PATH="second", CACHEW_SUBMODULES="true")
            self.run_step("session")
            self.env.update(fixtures.runner_file(self.fixture.root / "env"))
            fallback = Path(self.env["GIT_CONFIG_GLOBAL"])
            self.assertNotEqual(fallback, global_config)
            self.assertNotIn(self.fixture.cache.url, fallback.read_text())
            self.assertNotIn("include", fallback.read_text().lower())
            self.assertEqual(self.real_git("config", "--file", str(fallback), "--get", "user.name"), "Inherited")
        finally:
            lock.unlink()

    def assert_lazy_fetches_stay_upstream(self, cached):
        self.fixture.update_main()
        old_blob = self.real_git("-C", str(self.fixture.project_root / "org/main"), "rev-parse", "HEAD~1:content.txt")
        self.env.update(CACHEW_FILTER="blob:none", CACHEW_SUBMODULES="false")
        if not cached:
            self.fixture.cache.fail_repo = "main"
        seeded, active = self.flow()
        self.assertEqual(seeded, cached)
        origin = self.real_git("-C", str(self.fixture.target), "ls-remote", "--get-url", "origin")
        self.assertEqual(origin, f"{self.fixture.upstream.url}/org/main")
        if active:
            # Every other upstream URL still goes through the cache.
            other = self.real_git("ls-remote", "--get-url", f"{self.fixture.upstream.url}/org/child")
            self.assertTrue(other.startswith(self.fixture.cache.url), other)
        cache_before = list(self.fixture.cache.requests)
        self.fixture.upstream.requests.clear()
        # A history blob is not in the partial clone: Git fetches it lazily.
        self.assertEqual(self.real_git("-C", str(self.fixture.target), "cat-file", "-p", old_blob), "main")
        self.assertEqual(self.fixture.cache.requests, cache_before)
        self.assertIn("POST", [method for method, _, _ in self.fixture.upstream.requests])
        self.finish()

    def test_a_checkout_that_cannot_be_pinned_leaves_caching_off(self):
        # The repository's configuration is locked, so its include of the pin
        # cannot be written: rather than leave the partial clone's lazy fetches
        # on the cache rewrite with no upstream retry, activation turns caching
        # off and the rewrite goes.
        self.fixture.update_main()
        old_blob = self.real_git("-C", str(self.fixture.project_root / "org/main"), "rev-parse", "HEAD~1:content.txt")
        self.env.update(CACHEW_FILTER="blob:none", CACHEW_SUBMODULES="false")
        self.prepare()
        self.assertTrue(self.fixture.attempt())
        lock = self.fixture.target / ".git/config.lock"
        lock.touch()
        self.assertFalse(self.activate())
        lock.unlink()
        self.assertNotIn("include.path", self.real_git("-C", str(self.fixture.target), "config", "--local", "--list"))
        self.assertNotIn(self.fixture.cache.url, self.real_git("config", "--global", "--list"))
        cache_before = list(self.fixture.cache.requests)
        self.assertEqual(self.real_git("-C", str(self.fixture.target), "cat-file", "-p", old_blob), "main")
        self.assertEqual(self.fixture.cache.requests, cache_before)
        self.finish()

    def test_cachew_filtered_checkout_lazy_fetches_stay_upstream(self):
        self.assert_lazy_fetches_stay_upstream(cached=True)

    def test_fallback_filtered_checkout_lazy_fetches_stay_upstream(self):
        self.assert_lazy_fetches_stay_upstream(cached=False)

    def test_unsupported_mode_still_configures_later_dependencies(self):
        self.prepare()
        self.env["CACHEW_LFS"] = "true"
        self.assertFalse(self.fixture.attempt())
        self.assertTrue(self.activate())
        self.fixture.assert_cache_attempts([])
        self.finish()

    def test_existing_checkout_still_configures_later_dependencies(self):
        self.assertEqual(self.flow(), (True, True))
        before = len(self.fixture.cache.discoveries())
        self.assertEqual(self.flow(), (False, True))
        self.assertEqual(len(self.fixture.cache.discoveries()), before)
        self.finish()

    def test_global_values_keep_their_exact_bytes(self):
        # Git config values are bytes: a Latin-1 name is not UTF-8, and a raw CR
        # inside quotes stays a CR (Git has no \\r escape). Neither may stop the
        # session, and the job's copy must read back the same bytes.
        with (self.fixture.home / ".gitconfig").open("ab") as config:
            config.write(b'[fixture]\n\tlatin = "caf\xe9"\n\tcarriage = "a\rb"\n\tbare\n')
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()
        self.assertEqual(self.flow(), (True, True))
        # A key with no value is true as a boolean but empty as a string: it must
        # stay bare, not become the string "true".
        for key, expected in (("fixture.latin", b"caf\xe9"), ("fixture.carriage", b"a\rb"), ("fixture.bare", b"")):
            value = subprocess.run(
                [self.env["CACHEW_REAL_GIT"], "config", "--null", "--get", key], env=self.env, capture_output=True
            ).stdout
            self.assertEqual(value, expected + b"\0", key)
        self.finish()

    def test_global_includes_of_files_written_later_still_apply(self):
        # Git reads an include whose file is missing as empty, and reads the file
        # once a later step writes it. The session's copy must keep the directive.
        subprocess.run(["git", "init", "-q", str(self.fixture.workspace)], check=True)
        later = self.fixture.home / "later.config"
        conditional = self.fixture.home / "later-if.config"
        with (self.fixture.home / ".gitconfig").open("a") as config:
            config.write("[include]\n\tpath = later.config\n")
            config.write(f'[includeIf "gitdir:{self.fixture.workspace.resolve()}/"]\n\tpath = ~/later-if.config\n')
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()
        self.assertEqual(self.flow(), (True, True))
        later.write_text("[fixture]\n\tlater = written-later\n")
        conditional.write_text("[fixture]\n\tconditional = written-later\n")
        self.assertEqual(self.real_git("config", "--get", "fixture.later"), "written-later")
        self.assertEqual(self.real_git("config", "--get", "fixture.conditional"), "written-later")
        self.finish()

    def test_global_include_files_are_copied_and_originals_stay_unchanged(self):
        include = self.fixture.home / "preferences.config"
        include.write_text(
            '[user]\n\tname = Included user\n[http "https://unrelated.example/"]\n\textraHeader = existing-unrelated-header\n'
        )
        with (self.fixture.home / ".gitconfig").open("a") as stream:
            stream.write("[include]\n\tpath = preferences.config\n")
        self.original_global = (self.fixture.home / ".gitconfig").read_bytes()
        original_include = include.read_bytes()
        self.assertEqual(self.flow(), (True, True))
        self.assertEqual(self.real_git("config", "--global", "--includes", "--get", "user.name"), "Included user")
        self.assertEqual(
            self.real_git("config", "--global", "--includes", "--get", "http.https://unrelated.example/.extraHeader"),
            "existing-unrelated-header",
        )
        self.finish()
        self.assertEqual(include.read_bytes(), original_include)

    def plain_checkout_without_cache(self, env, cachew=True):
        """The official checkout (and the cachew step, if given a rejected cache) without a session."""
        self.fixture.env = env
        if cachew:
            self.assertFalse(self.fixture.attempt())
        self.assertEqual(self.fixture.cache.requests, [])
        state = self.fixture.root / "plain-checkout-state"
        state.write_text("")
        checkout = dict(env, GITHUB_STATE=str(state))
        for name, value in {
            "repository": "org/main",
            "ref": "main",
            "token": fixtures.TOKEN,
            "path": "checkout",
            "fetch-depth": "1",
            "submodules": "recursive",
            "clean": "true",
            "persist-credentials": "true",
            "set-safe-directory": "true",
        }.items():
            checkout["INPUT_" + name.upper()] = value
        self.run_node(fixtures.CHECKOUT_JS, env=checkout)
        self.assertEqual(fixtures.git(self.fixture.target, "rev-parse", "HEAD"), self.fixture.sha)
        self.assertEqual(self.fixture.cache.requests, [], "no request, so no token, reaches any cache")
        self.assertEqual((self.fixture.root / "path").read_text(), "", "no Git wrapper on PATH")
        self.assertEqual((self.fixture.root / "env").read_text(), "", "no job environment change")
        self.assertEqual(list(self.fixture.runtime.glob("cachew-*")), [])
        self.fixture.assert_no_cache_credentials()

    def test_untrusted_or_plain_http_cache_origins_leave_plain_checkout(self):
        for url in (
            "http://cachew.cachew.svc.cluster.local",
            "https://cachew.cachew.svc.cluster.local",
            "http://10.43.0.100:8080",
            "http://cachew.ci.tenstorrent.net",
            "https://evil.example",
            "https://untrusted.example",
        ):
            with self.subTest(url=url):
                env = dict(self.env, CACHEW_CACHEW_URL=url)
                self.run_prepare_expecting_fallback(env)
        shutil.rmtree(self.fixture.target, ignore_errors=True)
        self.plain_checkout_without_cache(dict(self.env, CACHEW_CACHEW_URL="https://evil.example"))

    def test_removing_cachews_entries_keeps_a_value_the_job_gave_the_same_key(self):
        self.assertEqual(self.flow(), (True, True))
        key = f"url.{self.fixture.upstream.url}/.pushInsteadOf"
        own = self.fixture.upstream.url + "/"
        self.assertEqual(self.real_git("config", "--global", "--get-all", key).split(), [own])
        self.real_git("config", "--global", "--add", key, "git@upstream.example:")
        # A later checkout's prepare takes cachew's entries out of the job's file.
        self.run_step("session")
        self.assertEqual(self.real_git("config", "--global", "--get-all", key).split(), ["git@upstream.example:"])
        self.finish()

    def test_a_job_that_turns_off_certificate_verification_gets_no_cache_rewrite(self):
        # GIT_SSL_NO_VERIFY overrides every http.sslVerify, and Git run past the
        # wrapper would send the token to the cache unverified.
        self.env["GIT_SSL_NO_VERIFY"] = ""
        self.prepare()
        self.checkout()
        self.assertFalse(self.activate())
        self.assertNotIn(self.fixture.cache.url, self.real_git("config", "--global", "--list"))
        self.finish()

    def test_a_cleanup_that_cannot_remove_the_rewrite_keeps_its_tls_protection(self):
        # A lock held for one write: the rewrite stays, so cache requests still
        # happen. They must stay verified whatever the job's own http.sslVerify,
        # and the token goes anyway.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        cache = self.fixture.cache.url
        state = json.loads((session / "state.json").read_text())
        failing = self.fixture.root / "failing-git"
        failing.write_text(
            "#!/bin/sh\n"
            'case "$*" in *--unset-all*.insteadOf*) exit 255 ;; esac\n'
            f'exec "{state["real_git"]}" "$@"\n'
        )
        failing.chmod(0o700)
        spec = importlib.util.spec_from_file_location("cachew_session", fixtures.ACTION / "session.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        remaining = module.remove_managed(str(failing), session / "global.config", state, self.env)
        content = (session / "global.config").read_text()
        self.assertIn(f"url.{cache}/git/", " ".join(remaining))
        self.assertEqual(self.real_git("config", "--global", "--get", f"http.{cache}/.sslVerify"), "true")
        self.assertEqual(self.real_git("config", "--global", "--get", f"http.{cache}/.followRedirects"), "false")
        self.assertIn(f"http.{cache}/.sslVerify", remaining)
        self.assertNotIn("AUTHORIZATION", content)
        self.finish()

    def test_a_cleanup_that_cannot_remove_a_token_keeps_its_tls_protection(self):
        # The rewrites go, but a token header stays (a lock held for that one
        # write). A request can still reach the cache by a URL that names it, and
        # would carry the token: it must stay verified and never be redirected.
        self.assertEqual(self.flow(), (True, True))
        session = Path(self.env["CACHEW_SESSION_DIR"])
        cache = self.fixture.cache.url
        state = json.loads((session / "state.json").read_text())
        failing = self.fixture.root / "failing-git"
        failing.write_text(
            "#!/bin/sh\n"
            'case "$*" in *--unset-all*.extraHeader*AUTHORIZATION*) exit 255 ;; esac\n'
            f'exec "{state["real_git"]}" "$@"\n'
        )
        failing.chmod(0o700)
        spec = importlib.util.spec_from_file_location("cachew_session", fixtures.ACTION / "session.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        remaining = module.remove_managed(str(failing), session / "global.config", state, self.env)
        content = (session / "global.config").read_text()
        self.assertIn("AUTHORIZATION", content)
        self.assertNotIn(f"url.{cache}/git/", " ".join(remaining))
        self.assertEqual(self.real_git("config", "--global", "--get", f"http.{cache}/.sslVerify"), "true")
        self.assertEqual(self.real_git("config", "--global", "--get", f"http.{cache}/.followRedirects"), "false")
        self.assertIn(f"http.{cache}/.sslVerify", remaining)
        self.finish()

    def test_prepare_refuses_a_path_or_ref_that_has_no_fallback(self):
        # checkout.py does not run for `cachew: false` in a job with a session.
        outside = self.fixture.root / "outside"
        outside.mkdir()
        (self.fixture.workspace / "link").symlink_to(outside, target_is_directory=True)
        for attack in ({"CACHEW_PATH": "link/escape"}, {"CACHEW_REF": "--upload-pack=x"}):
            with self.subTest(attack=attack):
                (self.fixture.root / "output").write_text("")
                result = self.run_step("session", dict(self.env, **attack))
                outputs = fixtures.runner_file(self.fixture.root / "output")
                self.assertEqual(outputs["ready"], "false")
                self.assertEqual(outputs.get("security-violation"), "true")
                self.assertIn("::error title=cachew-checkout: possible security", result.stdout)

    def test_an_untrusted_https_cache_origin_is_reported_as_a_security_violation(self):
        for url, violation in (("https://evil.example", "true"), ("http://10.43.0.100:8080", None)):
            with self.subTest(url=url):
                (self.fixture.root / "output").write_text("")
                result = self.run_step("session", dict(self.env, CACHEW_CACHEW_URL=url))
                outputs = fixtures.runner_file(self.fixture.root / "output")
                self.assertEqual(outputs["ready"], "false")
                self.assertEqual(outputs.get("security-violation"), violation)
                self.assertEqual("::error title=cachew-checkout: possible security" in result.stdout, bool(violation))
                self.assertEqual(list(self.fixture.runtime.glob("cachew-*")), [])

    def test_a_redirected_session_directory_is_reported_as_a_security_violation(self):
        self.assertEqual(self.flow(), (True, True))
        elsewhere = self.fixture.root / "cachew-elsewhere"
        elsewhere.mkdir()
        (self.fixture.root / "output").write_text("")
        result = self.run_step("session", dict(self.env, CACHEW_SESSION_DIR=str(elsewhere)))
        outputs = fixtures.runner_file(self.fixture.root / "output")
        self.assertEqual(outputs["security-violation"], "true")
        self.assertEqual(outputs["ready"], "false")
        self.assertIn("::error title=cachew-checkout: possible security", result.stdout)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.finish()

    def test_loopback_http_without_the_test_variable_leaves_plain_checkout(self):
        env = dict(self.env)
        del env["CACHEW_TEST_ALLOW_LOOPBACK_HTTP"]
        # Both a loopback cache and a loopback upstream are refused in production.
        self.run_prepare_expecting_fallback(env)
        self.run_prepare_expecting_fallback(dict(env, CACHEW_GITHUB_SERVER_URL="https://github.com"))
        # The plain checkout itself is unaffected (actions/checkout has its own rules).
        self.plain_checkout_without_cache(env)

    def test_action_clears_the_test_only_loopback_variable(self):
        action = (fixtures.ACTION / "action.yml").read_text()
        self.assertNotIn("inputs.cachew-test", action)
        self.assertNotIn("${{ env.CACHEW_TEST_ALLOW_LOOPBACK_HTTP", action)
        cleared = action.count("CACHEW_TEST_ALLOW_LOOPBACK_HTTP: ''")
        self.assertEqual(cleared, action.count("CACHEW_TEST_ALLOW_LOOPBACK_HTTP"))
        self.assertEqual(cleared, 3, "session, cachew and configure steps each clear it")

    def test_original_explicit_global_file_is_restored_for_earlier_post_actions(self):
        custom = self.fixture.root / "caller-global.config"
        custom.write_text("[user]\n\tname = Caller global user\n")
        self.env["GIT_CONFIG_GLOBAL"] = str(custom)
        self.assertEqual(self.flow(), (True, True))
        self.assertEqual(self.real_git("config", "--global", "--includes", "--get", "user.name"), "Caller global user")
        self.finish()
        self.assertEqual(custom.read_text(), "[user]\n\tname = Caller global user\n")

    def test_original_empty_global_selection_remains_intentionally_disabled(self):
        self.env["GIT_CONFIG_GLOBAL"] = ""
        self.assertEqual(self.flow(), (True, True))
        self.assertIsNone(self.original_name)
        self.finish()

    def test_fixture_isolates_an_outer_action_session_and_path_shim(self):
        outer = self.fixture.root / "outer-action"
        outer.mkdir()
        (outer / "cache-disabled").touch()
        outer_global = self.fixture.root / "outer-global.config"
        outer_global.write_text("[user]\n\tname = Outer action user\n")
        shim = self.fixture.root / "outer-bin"
        shim.mkdir()
        marker = self.fixture.root / "outer-shim-invoked"
        executable = shim / "git"
        executable.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        executable.chmod(0o700)
        with patch.dict(
            os.environ,
            CACHEW_SESSION_DIR=str(outer),
            CACHEW_REAL_GIT=self.env["CACHEW_REAL_GIT"],
            GIT_CONFIG_GLOBAL=str(outer_global),
            PATH=str(shim) + os.pathsep + os.environ["PATH"],
        ):
            nested = fixtures.CheckoutTests()
            try:
                nested.setUp()
                self.assertNotIn("CACHEW_SESSION_DIR", nested.env)
                self.assertTrue(nested.attempt())
                self.assertFalse(marker.exists())
                self.assertEqual(outer_global.read_text(), "[user]\n\tname = Outer action user\n")
            finally:
                nested.doCleanups()

    def test_job_wide_upstream_header_never_overrides_other_credentials(self):
        # H2: actions/checkout scopes persisted credentials to the checked-out
        # repository. A job-wide github.com Authorization header would replace the
        # credentials of every later upstream request, e.g. a PAT in a clone URL
        # for another private repository (curl drops URL credentials when an
        # Authorization header is configured), or be sent twice.
        self.fixture.repository("private")
        pat = "separate-private-pat"
        self.fixture.upstream.required_auth["private"] = (
            "basic " + base64.b64encode(f"x-access-token:{pat}".encode()).decode()
        )
        primary = "basic " + base64.b64encode(f"x-access-token:{fixtures.TOKEN}".encode()).decode()
        self.fixture.upstream.required_auth["main"] = primary
        self.assertEqual(self.flow(), (True, True))
        host = urllib.parse.urlsplit(self.fixture.upstream.url).netloc
        result = subprocess.run(
            ["git", "clone", f"http://x-access-token:{pat}@{host}/org/private", str(self.fixture.root / "private")],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # The primary checkout keeps working upstream with exactly one header.
        target = str(self.fixture.target)
        self.real_git("-C", target, "push", "origin", "HEAD:refs/heads/pushed-after-activation")
        directory = Path(self.env["CACHEW_SESSION_DIR"])
        state = json.loads((directory / "state.json").read_text())
        load_wrapper().remove_cache_keys(self.env["CACHEW_REAL_GIT"], directory, state, self.env)
        result = subprocess.run(
            ["git", "-C", target, "fetch", "origin"], env=self.env, text=True, capture_output=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.upstream.duplicate_auth, [])
        upstream_auth = [auth for _, url, auth in self.fixture.upstream.requests if "/org/main/" in url]
        self.assertTrue(upstream_auth and all(auth and auth.lower() == primary.lower() for auth in upstream_auth))
        for name in ("global.config", "checkout.config"):
            for key, _ in self.config_entries(directory / name):
                if key.startswith(f"http.{self.fixture.upstream.url}".lower()):
                    self.assertNotEqual(key.rsplit(".", 1)[1], "extraheader", f"{name}: {key}")
        self.finish()

    def run_prepare_expecting_fallback(self, env):
        (self.fixture.root / "env").write_text("")
        (self.fixture.root / "output").write_text("")
        (self.fixture.root / "path").write_text("")
        result = self.run_step("session", env)
        self.assertEqual(fixtures.runner_file(self.fixture.root / "output").get("ready"), "false")
        self.assertEqual((self.fixture.root / "env").read_text(), "", "no job environment change")
        self.assertEqual((self.fixture.root / "path").read_text(), "", "no Git wrapper on PATH")
        self.assertEqual(list(self.fixture.runtime.glob("cachew-*")), [], "no session left behind")
        return result

    def test_missing_python_falls_back_to_plain_checkout(self):
        binaries = self.fixture.root / "without-python"
        binaries.mkdir()
        (binaries / "python3").write_text("#!/bin/sh\nexit 127\n")
        (binaries / "python3").chmod(0o700)
        result = self.run_prepare_expecting_fallback(dict(self.env, PATH=f"{binaries}{os.pathsep}{self.env['PATH']}"))
        self.assertIn("Python 3.10+ unavailable", result.stdout)
        # action.yml skips the cachew and configure steps when the session is not ready.
        self.plain_checkout_without_cache(self.env, cachew=False)

    def test_prepare_errors_fall_back_to_plain_checkout(self):
        # H3: whatever prevents cachew must never fail a checkout that plain
        # actions/checkout performs.
        no_git = self.fixture.root / "no-git-bin"
        no_git.mkdir()
        for tool in ("python3", "bash"):
            (no_git / tool).symlink_to(shutil.which(tool))
        self.run_prepare_expecting_fallback(dict(self.env, PATH=str(no_git)))

        old_git = self.fixture.root / "old-git-bin"
        old_git.mkdir()
        (old_git / "git").write_text(
            '#!/bin/sh\nif [ "$1" = version ]; then echo "git version 2.25.1"; exit 0; fi\nexec '
            + self.env["CACHEW_REAL_GIT"]
            + ' "$@"\n'
        )
        (old_git / "git").chmod(0o700)
        self.run_prepare_expecting_fallback(dict(self.env, PATH=str(old_git) + os.pathsep + self.env["PATH"]))

        broken = self.fixture.home / ".gitconfig"
        original = broken.read_bytes()
        broken.write_text("[user\n\tname = broken\n")
        try:
            self.run_prepare_expecting_fallback(self.env)
        finally:
            broken.write_bytes(original)
        # The plain checkout still succeeds in the untouched environment.
        self.posts = []
        state = self.fixture.root / "plain-checkout-state"
        state.touch()
        env = dict(self.env, GITHUB_STATE=str(state))
        for name, value in {
            "repository": "org/main",
            "ref": "main",
            "token": fixtures.TOKEN,
            "path": "checkout",
            "fetch-depth": "1",
            "submodules": "recursive",
            "clean": "true",
            "persist-credentials": "true",
            "set-safe-directory": "true",
        }.items():
            env["INPUT_" + name.upper()] = value
        self.run_node(fixtures.CHECKOUT_JS, env=env)
        self.assertEqual(self.fixture.cache.requests, [])

    def test_wrapper_shim_only_starts_python_for_network_commands(self):
        self.assertEqual(self.flow(), (True, True))
        shim = Path(self.env["CACHEW_SESSION_DIR"]) / "bin/git"
        marker = self.fixture.root / "python-started"
        fake = self.fixture.root / "fake-python"
        fake.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        fake.chmod(0o700)
        text = shim.read_text()
        python = text.split("exec '", 1)[1].split("'", 1)[0]
        shim.write_text(text.replace(f"'{python}'", f"'{fake}'"))
        for arguments in (["rev-parse", "--is-inside-work-tree"], ["-c", "a.b=c", "-C", ".", "status", "--short"]):
            result = subprocess.run([str(shim), *arguments], cwd=self.fixture.target, env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), arguments)
        for arguments in (["fetch", "origin"], ["-C", ".", "ls-remote", "origin"], ["-c", "a.b=c", "clone", "x"]):
            result = subprocess.run([str(shim), *arguments], cwd=self.fixture.target, env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 99, arguments)
            self.assertTrue(marker.exists(), arguments)
            marker.unlink()
        # With GIT_SSL_NO_VERIFY any command may reach the cache unverified (a
        # lazy fetch, say): the wrapper keeps the token off those requests.
        # So it does where command-scoped configuration could weaken them.
        for arguments, extra in (
            (["status"], {"GIT_SSL_NO_VERIFY": ""}),
            (["status"], {"GIT_CONFIG_COUNT": "0"}),
            (["status"], {"GIT_CONFIG_PARAMETERS": "'a.b=c'"}),
            (["-c", "HTTP.https://x/.sslVerify=false", "status"], {}),
            (["--config-env=http.https://x/.followRedirects=X", "status"], {}),
            (["--config-env", "http.followRedirects=X", "status"], {}),
            # A file the command includes may hold such a setting.
            (["-c", "include.path=/x", "status"], {}),
            (["-c", "IncludeIf.gitdir:/.path=/x", "status"], {}),
            (["--config-env=include.path=X", "status"], {}),
        ):
            env = dict(self.env, X="true", **extra)
            result = subprocess.run([str(shim), *arguments], cwd=self.fixture.target, env=env, capture_output=True)
            self.assertEqual(result.returncode, 99, (arguments, extra, result.stderr))
            self.assertTrue(marker.exists(), (arguments, extra))
            marker.unlink()
        local = dict(cwd=self.fixture.target, env=self.env, capture_output=True)
        # A helper the shim does not know (git lfs) goes to the wrapper when the
        # job's files hold such a setting for any URL.
        result = subprocess.run([str(shim), "count-objects"], **local)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.real_git("config", "--global", "http.https://elsewhere.example/.sslVerify", "false")
        result = subprocess.run([str(shim), "count-objects"], **local)
        self.assertEqual(result.returncode, 99, result.stderr)
        marker.unlink()
        # So does a command the shim knows as local: its lazy fetch, or the
        # git-lfs filter of a checkout, may reach the cache under that setting.
        for arguments in (["status"], ["-C", ".", "show", "--no-patch"]):
            result = subprocess.run([str(shim), *arguments], **local)
            self.assertEqual(result.returncode, 99, (arguments, result.stderr))
            self.assertTrue(marker.exists(), arguments)
            marker.unlink()
        # GIT_CONFIG names the one file `git config` reads, and no other Git
        # command reads it: it hides neither that setting nor an alias.
        legacy = dict(local, env=dict(self.env, GIT_CONFIG=os.devnull))
        result = subprocess.run([str(shim), "status"], **legacy)
        self.assertEqual(result.returncode, 99, result.stderr)
        marker.unlink()
        self.real_git("config", "--global", "--unset", "http.https://elsewhere.example/.sslVerify")
        self.real_git("config", "--global", "alias.up", "fetch")
        result = subprocess.run([str(shim), "up"], **legacy)
        self.assertEqual(result.returncode, 99, result.stderr)
        marker.unlink()
        self.real_git("config", "--global", "--unset", "alias.up")
        self.real_git("config", "--global", "http.https://elsewhere.example/.followRedirects", "true")
        result = subprocess.run([str(shim), "status"], **local)
        self.assertEqual(result.returncode, 99, result.stderr)
        marker.unlink()
        self.real_git("config", "--global", "--unset", "http.https://elsewhere.example/.followRedirects")
        result = subprocess.run([str(shim), "status"], **local)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        shim.write_text(text)
        self.finish()

    def test_failed_cache_attempt_stays_disabled_for_later_actions(self):
        self.fixture.cache.fail_repo = "main"
        self.assertEqual(self.flow(), (False, False))
        before = list(self.fixture.cache.requests)
        self.fixture.cache.fail_repo = None
        self.prepare()
        self.assertFalse(self.fixture.attempt())
        self.assertFalse(self.activate())
        self.assertEqual(self.fixture.cache.requests, before)
        self.finish()


def load_wrapper():
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_wrapper", fixtures.ACTION / "git_wrapper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()


def load_session():
    spec = importlib.util.spec_from_file_location("cachew_session", fixtures.ACTION / "session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromisorTests(unittest.TestCase):
    """pin_promisor_remotes reads remote.<name>.promisor as Git reads a boolean,
    whatever the remote is named."""

    def test_a_remote_named_with_a_space_or_a_dot_is_pinned(self):
        # Git accepts any remote name without a slash; a lazy fetch through an
        # unpinned one would follow the rewrite to the cache with no upstream retry.
        session = load_session()
        real_git = shutil.which("git")
        cache = "https://cachew.example"
        upstream = "https://github.com"
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            home.mkdir()
            global_config = root / "global.config"
            subprocess.run(
                [real_git, "config", "--file", str(global_config), f"url.{cache}/git/github.com/.insteadOf", upstream + "/"],
                check=True,
            )
            environ = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}
            for index, name in enumerate(("my remote", "a.b", "tab\tname")):
                with self.subTest(name=name):
                    work = root / f"repo-{index}"
                    subprocess.run([real_git, "init", "-q", str(work)], check=True, env=environ)
                    for key, value in (("url", upstream + "/org/main"), ("promisor", "true")):
                        subprocess.run([real_git, "-C", str(work), "config", f"remote.{name}.{key}", value], check=True, env=environ)
                    session_dir = root / f"session-{index}"
                    session_dir.mkdir()
                    self.assertTrue(session.pin_promisor_remotes(real_git, ["-C", str(work)], environ, cache, str(session_dir)))
                    url = subprocess.run(
                        [real_git, "-C", str(work), "ls-remote", "--get-url", name], env=environ, capture_output=True, text=True
                    ).stdout.strip()
                    self.assertEqual(url, upstream + "/org/main")

    def test_every_git_spelling_of_true_is_pinned(self):
        session = load_session()
        real_git = shutil.which("git")
        cache = "https://cachew.example"
        upstream = "https://github.com"
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            home.mkdir()
            global_config = root / "global.config"
            subprocess.run(
                [real_git, "config", "--file", str(global_config), f"url.{cache}/git/github.com/.insteadOf", upstream + "/"],
                check=True,
            )
            environ = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}
            for value, pinned in (("true", True), ("yes", True), ("On", True), ("1", True), ("2", True), ("1k", True), (None, True), ("false", False), ("off", False), ("0", False)):
                with self.subTest(value=value):
                    work = root / f"repo-{value}"
                    subprocess.run([real_git, "init", "-q", str(work)], check=True, env=environ)
                    subprocess.run([real_git, "-C", str(work), "remote", "add", "origin", upstream + "/org/main"], check=True, env=environ)
                    config = work / ".git/config"
                    with config.open("a") as stream:
                        stream.write('[remote "origin"]\n\tpromisor' + ("" if value is None else f" = {value}") + "\n")
                    session_dir = root / f"session-{value}"
                    session_dir.mkdir()
                    self.assertTrue(session.pin_promisor_remotes(real_git, ["-C", str(work)], environ, cache, str(session_dir)))
                    includes = subprocess.run(
                        [real_git, "-C", str(work), "config", "--local", "--get-all", "include.path"],
                        env=environ, capture_output=True, text=True,
                    ).stdout
                    self.assertEqual(bool(includes), pinned)
