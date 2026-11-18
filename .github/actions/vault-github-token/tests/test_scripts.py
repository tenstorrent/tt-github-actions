# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Fake credentials only. This curl replacement never opens a network connection.
CURL_MOCK = r"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
url = args[-1]
stage = next(name for name, fragment in [
    ('health', '/sys/health'), ('oidc', '/oidc?'),
    ('login', '/auth/jwt-github/login'), ('token', '/github/token/')
] if fragment in url)
log = Path(os.environ['MOCK_LOG'])
previous = log.read_text().splitlines()
entry = {'stage': stage, 'args': args}
if stage == 'login':
    entry['body'] = json.load(sys.stdin)
with log.open('a') as out:
    out.write(json.dumps(entry) + '\n')
if stage == 'health' and sum(json.loads(line)['stage'] == 'health' for line in previous) < int(os.environ.get('HEALTH_FAILURES', '0')):
    sys.exit(22)
if stage == os.environ.get('FAIL_STAGE'):
    sys.exit(22)
response = {
    'health': {}, 'oidc': {'value': 'fake-jwt-for-test'},
    'login': {'auth': {'client_token': 'fake-vault-token-for-test'}},
    'token': {'data': {'token': os.environ.get('FAKE_TOKEN', 'fake-github-token-for-test')}}
}[stage]
if stage == os.environ.get('BAD_STAGE'):
    response = {}
print(json.dumps(response))
"""


class VaultScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "calls"
        self.log.touch()
        self.output = self.root / "output"
        self.env = {
            **os.environ,
            "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(self.output),
            "MOCK_LOG": str(self.log),
            "RUNNER_OS": "Linux",
            "VAULT_URL": "https://vault.example.invalid",
            "VAULT_ROLE": "example--repo--main",
            "VAULT_TOKEN_NAME": "example--repo--read",
            "NETWORK_MODE": "userspace",
            "PROXY_URL": "",
            "VAULT_PROXY": "http://127.0.0.1:1055",
            "TS_CLIENT_ID": "test-client",
            "TS_AUDIENCE": "test-audience",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fake-request-token",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.example.invalid/oidc?test=1",
        }
        self.mock("tailscale", 'exit "${TAILSCALE_STATUS:-1}"')
        self.mock("sleep", "exit 0")
        self.mock("curl", CURL_MOCK, "python3")

    def mock(self, name, body, interpreter="bash"):
        path = self.root / name
        path.write_text(f"#!/usr/bin/env {interpreter}\n{body}\n")
        path.chmod(0o755)

    def run_script(self, name, **overrides):
        self.output.unlink(missing_ok=True)
        self.log.write_text("")
        return subprocess.run(
            ["bash", str(SCRIPTS / f"{name}.sh")],
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_configuration_modes(self):
        for mode, proxy, join in [
            ("userspace", "http://127.0.0.1:1055", "true"),
            ("kernel", "", "true"),
            ("existing", "", "false"),
        ]:
            with self.subTest(mode=mode):
                result = self.run_script("validate", NETWORK_MODE=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.outputs()["proxy"], proxy)
                self.assertEqual(self.outputs()["join"], join)
                self.assertEqual("--tun=userspace-networking" in self.outputs()["tailscaled-args"], mode == "userspace")

    def test_existing_connection_requires_no_tailscale_identity(self):
        for mode in ["userspace", "kernel", "existing"]:
            result = self.run_script(
                "validate", NETWORK_MODE=mode, TAILSCALE_STATUS="0", TS_CLIENT_ID="", TS_AUDIENCE=""
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.outputs()["join"], "false")

    def test_custom_proxy_and_vault(self):
        result = self.run_script(
            "validate", PROXY_URL="http://localhost:20155", VAULT_URL="https://staging.example.invalid/"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["url"], "https://staging.example.invalid")
        self.assertIn("--outbound-http-proxy-listen=localhost:20155", self.outputs()["tailscaled-args"])
        result = self.run_script("validate", NETWORK_MODE="existing", PROXY_URL="http://proxy.example.invalid:8080")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_inputs_fail_before_network(self):
        for overrides in [
            {"VAULT_ROLE": ""},
            {"VAULT_TOKEN_NAME": ""},
            {"VAULT_ROLE": "../other"},
            {"VAULT_TOKEN_NAME": "name token | X; other/path"},
            {"VAULT_TOKEN_NAME": "name\nother"},
            {"TS_CLIENT_ID": ""},
            {"TS_AUDIENCE": ""},
            {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""},
            {"ACTIONS_ID_TOKEN_REQUEST_URL": ""},
            {"RUNNER_OS": "macOS"},
            {"NETWORK_MODE": "auto"},
            {"VAULT_URL": "http://vault.example.invalid"},
            {"VAULT_URL": "https://user:pass@vault.example.invalid"},
            {"VAULT_URL": "https://vault.example.invalid/path"},
            {"PROXY_URL": "http://127.0.0.1:65536"},
            {"PROXY_URL": "http://127.0.0.1:0"},
            {"PROXY_URL": "http://external.invalid:1055"},
            {"NETWORK_MODE": "kernel", "PROXY_URL": "http://127.0.0.1:1055"},
        ]:
            with self.subTest(overrides=overrides):
                result = self.run_script("validate", **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertEqual(self.calls(), [])

    def test_token_flow_direct_and_proxy(self):
        for proxy in ["", "http://127.0.0.1:1055", "http://proxy.example.invalid:8080"]:
            with self.subTest(proxy=proxy):
                result = self.run_script("get-token", VAULT_PROXY=proxy, NO_PROXY="*")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.outputs(), {"token": "fake-github-token-for-test"})
                calls = self.calls()
                self.assertEqual([call["stage"] for call in calls], ["health", "oidc", "login", "token"])
                for call in calls:
                    args = call["args"]
                    self.assertIn("--max-time", args)
                    if call["stage"] == "oidc":
                        self.assertNotIn("--proxy", args)
                        self.assertIn("audience=https%3A%2F%2Fvault.example.invalid", args[-1])
                    else:
                        self.assertEqual(args[args.index("--proxy") + 1], proxy)
                        self.assertEqual(args[args.index("--noproxy") + 1], "")
                self.assertEqual(calls[2]["body"], {"role": "example--repo--main", "jwt": "fake-jwt-for-test"})
                # Credentials may only appear in runner masking commands, never diagnostic output.
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        "::add-mask::fake-jwt-for-test",
                        "::add-mask::fake-vault-token-for-test",
                        "::add-mask::fake-github-token-for-test",
                    ],
                )
                self.assertEqual(result.stderr, "")

    def test_health_retry_and_exhaustion(self):
        for failures, expected_calls, expected_status in [(2, 3, 0), (6, 6, 1)]:
            result = self.run_script("get-token", HEALTH_FAILURES=str(failures))
            self.assertEqual(result.returncode, expected_status, result.stderr)
            self.assertEqual(sum(call["stage"] == "health" for call in self.calls()), expected_calls)
            if expected_status:
                self.assertFalse(self.output.exists())
                self.assertTrue(all(call["stage"] == "health" for call in self.calls()))

    def test_denied_or_malformed_responses_produce_no_token(self):
        for stage in ["oidc", "login", "token"]:
            for failure in ["FAIL_STAGE", "BAD_STAGE"]:
                with self.subTest(stage=stage, failure=failure):
                    result = self.run_script("get-token", **{failure: stage})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(self.output.exists())
                    self.assertEqual(self.calls()[-1]["stage"], stage)

    def test_empty_and_multiline_tokens_are_rejected(self):
        for token in ["", "fake\nnew-output=value", "fake\rnew-output=value"]:
            result = self.run_script("get-token", FAKE_TOKEN=token)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
