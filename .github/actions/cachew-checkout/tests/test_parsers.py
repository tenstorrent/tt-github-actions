# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The action's text parsers, called directly: no Git, no server, no network.

Every regular expression in checkout.py and session.py sits in a function
tested here, and each case below pins one character class member, quantifier
bound or anchor: a test fails if any of them changes. The end-to-end tests
reach the same functions, but take minutes, not milliseconds.
"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ACTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACTION))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ACTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checkout = load("checkout")
session = load("session")
Bypass, SecurityViolation = checkout.Bypass, checkout.SecurityViolation
LOOPBACK = {"CACHEW_TEST_ALLOW_LOOPBACK_HTTP": "1"}
IPV6_LONGEST = "ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255"


class OriginTests(unittest.TestCase):
    def test_accepted_origins(self):
        for value, expected in (
            ("https://a", "https://a"),
            ("https://ab", "https://ab"),
            ("https://Ab", "https://ab"),
            ("https://9b", "https://9b"),
            ("https://aBb", "https://abb"),
            ("https://a9b", "https://a9b"),
            ("https://a.b", "https://a.b"),
            ("https://a-b", "https://a-b"),
            ("https://aB", "https://ab"),
            ("https://a9", "https://a9"),
            ("https://GitHub.COM/", "https://github.com"),
            ("https://" + "a" * 253, "https://" + "a" * 253),
            ("https://[::]", "https://[::]"),
            ("https://[::1]", "https://[::1]"),
            ("https://[::A]", "https://[::a]"),
            ("https://[::ffff:1.2.3.4]", "https://[::ffff:1.2.3.4]"),
            (f"https://[{IPV6_LONGEST}]", f"https://[{IPV6_LONGEST}]"),
            ("https://a:1", "https://a:1"),
            ("https://a:8443", "https://a:8443"),
            ("https://a:65535", "https://a:65535"),
            ("https://a:443", "https://a"),
            ("https://a:0443", "https://a"),
            ("https://[::1]:443", "https://[::1]"),
            # Tabs and line breaks are dropped and the ends trimmed, as every parser does.
            ("https://a\tb", "https://ab"),
            ("https://a\nb", "https://ab"),
            ("https://a\rb", "https://ab"),
            ("\x00 https://a", "https://a"),
        ):
            with self.subTest(value=value):
                self.assertEqual(checkout.origin(value, {}), expected)
        self.assertEqual(checkout.origin("http://127.0.0.1:8080", LOOPBACK), "http://127.0.0.1:8080")
        self.assertEqual(checkout.origin("http://localhost:80", LOOPBACK), "http://localhost")

    def test_unsupported_origins_fall_back(self):
        for value in (
            "https://a.",
            "https://-a",
            "https://a-",
            "https://.a",
            "https://" + "a" * 254,
            "https://a:",
            "https://a:123456",
            "https://a:65536",
            "https://[:]",
            f"https://[0{IPV6_LONGEST}]",
            "https://a/x",
            "https://a?q",
            "https://a#f",
            "https://user@a",
            "http://a",
            "http://127.0.0.1",
            "ftp://a",
            "https://",
            "https:///",
            # Without a scheme there is no host to read: a delimiter before the
            # first colon ends what could be one.
            "x/y://[;id]",
            "x?y://[;id]",
            "x#y://[;id]",
            "x\\y://[;id]",
            # A query, fragment or path ends the host, so what follows is not read as host.
            "https://a?q=$(id)",
            "https://a#$(id)",
            "https://a/$(id)",
            "https://u$(id)@a",
        ):
            with self.subTest(value=value):
                with self.assertRaises(Bypass) as raised:
                    checkout.origin(value, {})
                self.assertNotIsInstance(raised.exception, SecurityViolation)

    def test_metacharacter_hosts_are_attacks(self):
        for value in (
            "https://a$(id)",
            "https://a;b",
            "https://a b",
            "https://a`id`",
            "https://a'b",
            "https://user@a;b",
            "https://a$b/path",
            "https://a$b?q",
            "https://a$b#f",
            "http://a$b",
            "https://[::1;id]",
            "https://[::1]`id`",
            "https://x]$(id)",
            "https://a\u2100b;id",
            "https://a$(id)\\.github.com",
            "https:a$(id)",
            "https:\\\\a$(id)",
            # Python rejects these, so only the host as written is read: after
            # the first colon, even with nothing before it.
            "https://[::1;id]:1",
            "://[;id]",
            "https:[;id]:1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(SecurityViolation):
                    checkout.origin(value, {})

    def test_hosts_are_read_up_to_the_delimiters_python_does_not_use(self):
        # Python reads a backslash as part of the host, and here rejects the URL;
        # Git and WHATWG parsers end the host there, at a host with no
        # metacharacters. The URL is unusable, but it is not an attack.
        with self.assertRaises(Bypass) as raised:
            checkout.origin("https://[::1]\\x", {})
        self.assertNotIsInstance(raised.exception, SecurityViolation)


class UntrustedHttpsTests(unittest.TestCase):
    def setUp(self):
        self.trusted = checkout.TRUSTED_CACHES[0].split("://", 1)[1]

    def test_trusted_hosts_in_any_spelling(self):
        for value in (
            f"https://{self.trusted}",
            f"HTTPS://{self.trusted.upper()}",
            f"https:{self.trusted}",
            f"https:\\\\{self.trusted}",
            f"https://user@{self.trusted}:443/x",
            f"https://{self.trusted}.",
            f"https://{self.trusted}/evil",
            f"https://{self.trusted}?evil",
            f"https://{self.trusted}#evil",
            f"https://{self.trusted}\\evil",
            f" https://{self.trusted}\t",
            f"https://{self.trusted[:3]}\n{self.trusted[3:]}",
            "http://evil.example",
            # No host: the token could not go anywhere.
            "https://",
            "https:///",
        ):
            with self.subTest(value=value):
                self.assertFalse(checkout.untrusted_https(value))

    def test_other_https_hosts(self):
        for value in (
            "https://evil.example",
            "HTTPS://Evil.Example",
            "https:evil.example",
            "https:///evil.example",
            "https:\\\\evil.example",
            "https://[::1]:8443",
            f"https://{self.trusted}.evil.example",
        ):
            with self.subTest(value=value):
                self.assertTrue(checkout.untrusted_https(value))


class InputTests(unittest.TestCase):
    def test_repository_names(self):
        for value in ("o/n", "O/N", "o1/n1", "o_x/n_y", "o.x/n.y", "o-x/n-y"):
            with self.subTest(value=value):
                self.assertEqual(checkout.repository_name(value), value)
        for value in ("o", "o/n/x", "o n/x", "o/n x", "\u00f6/n", "o/\u0660", "o:x/n", "o/n%2e", "o/n?"):
            with self.subTest(value=value):
                with self.assertRaises(Bypass):
                    checkout.repository_name(value)

    def test_git_versions(self):
        for output, expected in (
            ("git version 2.43.0\n", (2, 43)),
            ("git version 2.9.5", (2, 9)),
            ("git version 10.1", (10, 1)),
            ("git version 12.34.5.windows.1", (12, 34)),
            ("garbage", None),
            ("2", None),
        ):
            with self.subTest(output=output):
                self.assertEqual(checkout.git_version(output), expected)

    def test_partial_clone_filters(self):
        for value in ("", "blob:none", "tree:0", "blob:limit=1k", "combine:blob:none+tree:0", "sparse:oid=a.b-c%3A_d"):
            with self.subTest(value=value):
                self.assertEqual(checkout.partial_clone_filter(value), value)
        for value in ("blob none", "blob:none;x", "bl\u00f6b:none", "blob:none\n", "a/b", "a,b"):
            with self.subTest(value=value):
                with self.assertRaises(Bypass):
                    checkout.partial_clone_filter(value)

    def test_fetch_depths(self):
        for value, expected in (("0", 0), ("1", 1), ("10", 10), ("+5", 5), ("007", 7)):
            with self.subTest(value=value):
                self.assertEqual(checkout.fetch_depth(value), expected)
        for value in ("", "+", "-1", "1e1", "0x10", "1_0", "\u0661", "1 ", "++1"):
            with self.subTest(value=value):
                with self.assertRaises(Bypass):
                    checkout.fetch_depth(value)

    def test_sha_refs_after_an_ascii_trim(self):
        sha, sha256 = "a" * 40, "B" * 64
        for value, expected in (
            (sha, sha),
            (sha256, sha256),
            ("0123456789abcdefABCDEF0123456789abcdefAB", "0123456789abcdefABCDEF0123456789abcdefAB"),
            (f" \t\n\v\f\r{sha} \t\n\v\f\r", sha),
            (f"\t{sha}", sha),
            (f"\n{sha}", sha),
            (f"\v{sha}", sha),
            (f"\f{sha}", sha),
            (f"\r{sha}", sha),
            (f" {sha}", sha),
            (f"{sha}\t", sha),
            (f"{sha}\n", sha),
            (f"{sha}\v", sha),
            (f"{sha}\f", sha),
            (f"{sha}\r", sha),
            (f"{sha} ", sha),
            # JavaScript's trim would remove these; actions/checkout's SHA test does not.
            (f"\ufeff{sha}", None),
            (f"{sha}\u00a0", None),
            ("0123456789" * 4, "0123456789" * 4),
            ("abcdef" * 6 + "abcd", "abcdef" * 6 + "abcd"),
            ("ABCDEF" * 6 + "ABCD", "ABCDEF" * 6 + "ABCD"),
            ("0123456789" * 6 + "0123", "0123456789" * 6 + "0123"),
            ("abcdef" * 10 + "abcd", "abcdef" * 10 + "abcd"),
            ("ABCDEF" * 10 + "ABCD", "ABCDEF" * 10 + "ABCD"),
            ("a" * 39, None),
            ("a" * 41, None),
            ("a" * 63, None),
            ("a" * 20 + " " + "a" * 20, None),
            ("a" * 20 + "\n" + "a" * 20, None),
            ("g" * 40, None),
            ("main", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(checkout.sha_ref(value), expected)

    def test_each_javascript_whitespace_character_is_trimmed(self):
        spaces = (
            "\t\n\v\f\r \u00a0\u1680"
            + "".join(map(chr, range(0x2000, 0x200B)))
            + "\u2028\u2029\u202f\u205f\u3000\ufeff"
        )
        for space in spaces:
            with self.subTest(space=hex(ord(space))):
                self.assertEqual(checkout.checkout_input({"X": f"{space}x{space}"}, "X"), "x")
        for other in "\x1c\x1d\x1e\x1f\x85\u180e\u200b\x00":
            with self.subTest(other=hex(ord(other))):
                self.assertEqual(checkout.checkout_input({"X": f"{other}x{other}"}, "X"), f"{other}x{other}")

    def test_inputs_are_trimmed_as_javascript_trims(self):
        for value, expected in (
            ("\t\n\v\f\r \u00a0\u1680\u2000\u2005\u200a\u2028\u2029\u202f\u205f\u3000\ufeffx\ufeff \t", "x"),
            ("a b", "a b"),
            ("a\u3000b", "a\u3000b"),
            ("\x1cx\x85", "\x1cx\x85"),
            ("\u200bx", "\u200bx"),
        ):
            with self.subTest(value=value):
                self.assertEqual(checkout.checkout_input({"X": value}, "X"), expected)
        self.assertEqual(checkout.checkout_input({"X": " \u3000"}, "X", "d"), "d")


class GitOutputTests(unittest.TestCase):
    def test_ref_listing(self):
        a, b = "a" * 40, "0123456789abcdefABCDEF0123456789abcdefAB"
        output = (
            f"ref: refs/heads/main\tHEAD\n{a}\tHEAD\n{b}\trefs/heads/x\u2028y\n"
            f"{'c' * 39}\trefs/heads/short\n{'c' * 41}\trefs/heads/long\n{'g' * 40}\trefs/heads/hex\n"
        )
        self.assertEqual(
            checkout.ref_listing(output),
            ({"HEAD": a, "refs/heads/x\u2028y": b}, {"HEAD": "refs/heads/main"}),
        )

    def test_submodule_config_files(self):
        listing = (
            "Entering 'a'\n"
            "file:/w/.git/modules/a/config\tremote.origin.url\n"
            "Entering 'b c'\n"
            "file:/w/b c/.git/config\tremote.origin.url\n"
            "file:\tremote.origin.url\n"
            "xfile:/evil\tremote.origin.url\n"
            "file:/w/d/config\tremote.originXurl\n"
            "file:/w/e/config\tremote.origin.url.x\n"
        )
        self.assertEqual(
            checkout.submodule_config_files(listing),
            ["/w/.git/modules/a/config", "/w/b c/.git/config", "/w/e/config"],
        )

    def test_transport_errors(self):
        transient = (
            b"fatal: unable to access: Failed to connect to cache port 443",
            b"Connection refused",
            b"connection reset by peer",
            b"Operation timed out",
            b"SSL_connect: SSL_ERROR_SYSCALL",
            b"OpenSSL SSL_read: unexpected eof",
            b"gnutls_handshake() failed",
            b"error: RPC failed; curl 18",
            b"fatal: early EOF",
            b"fatal: the remote end hung up unexpectedly",
            b"Operation too slow. Less than 1000 bytes/sec",
            b"The requested URL returned error: 500",
            b"The requested URL returned error: 503",
        )
        permanent = (
            b"Could not resolve host: cache",
            b"The requested URL returned error: 301",
            b"SSL certificate problem: unable to get local issuer certificate",
            b"server certificate verification failed",
            b"gnutls: The server certificate is NOT trusted",
            b"certificate verify failed",
        )
        answers = (
            b"The requested URL returned error: 404\nerror: RPC failed\nfatal: the remote end hung up",
            b"error: RPC failed; HTTP 403 curl 22\nfatal: early EOF",
            b"http 401 early eof",
            b"fatal: repository not found",
            b"returned error: 200 early",
        )
        for errors in transient:
            with self.subTest(errors=errors):
                self.assertTrue(checkout.transport_error(errors))
                self.assertTrue(checkout.transient_error(errors))
        for errors in permanent:
            with self.subTest(errors=errors):
                self.assertTrue(checkout.transport_error(errors))
                self.assertFalse(checkout.transient_error(errors))
        for errors in answers:
            with self.subTest(errors=errors):
                self.assertFalse(checkout.transport_error(errors))

    def test_only_stage_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as root:
            for names, removed in (
                ([checkout.STAGE_PREFIX + "abc_1234"], True),
                ([checkout.STAGE_PREFIX + "abcd1234", checkout.STAGE_PREFIX + "z9_z9_z9"], True),
                ([checkout.STAGE_PREFIX + "abc_123"], False),
                ([checkout.STAGE_PREFIX + "abc_12345"], False),
                ([checkout.STAGE_PREFIX + "ABCD1234"], False),
                ([checkout.STAGE_PREFIX + "abcd-234"], False),
                (["x" + checkout.STAGE_PREFIX[1:] + "abcd1234"], False),
                ([checkout.STAGE_PREFIX + "abcd1234", "other"], False),
            ):
                with self.subTest(names=names):
                    target = Path(root) / "target"
                    target.mkdir()
                    for name in names:
                        (target / name).mkdir()
                    checkout.remove_stale_stages(target)
                    self.assertEqual(sorted(p.name for p in target.iterdir()), [] if removed else sorted(names))
                    for path in target.iterdir():
                        path.rmdir()
                    target.rmdir()

    def test_fork_pull_request_refs(self):
        with tempfile.TemporaryDirectory() as root:
            event = Path(root) / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "repository": {"id": 1},
                        "pull_request": {"head": {"repo": {"id": 2, "full_name": "fork/r"}, "sha": "b" * 40}},
                    }
                )
            )
            environ = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event)}
            for ref, refused in (
                ("refs/pull/12/head", True),
                ("refs/pull/1/merge", True),
                ("refs/pull/x/head", False),
                ("refs/pull//head", False),
                ("refs/pull/1/headx", False),
                ("refs/pull/1/other", False),
                ("xrefs/pull/1/head", False),
                ("refs/heads/main", False),
            ):
                with self.subTest(ref=ref):
                    self.assertEqual(checkout.unsafe_pr_checkout(environ, "base/r", ref, ""), refused)


class SubmoduleUrlTests(unittest.TestCase):
    def test_submodule_repositories(self):
        superproject = "https://github.com/o/a"
        for value, expected in (
            ("../b", "o/b"),
            ("../../p/q", "p/q"),
            ("https://github.com/o_1/b-2", "o_1/b-2"),
            ("https://github.com/o/b.git", "o/b.git"),
            ("git@github.com:o/b", "o/b"),
        ):
            with self.subTest(value=value):
                self.assertEqual(checkout.submodule_repository(value, superproject, "https://github.com"), expected)
        for value in ("./c", "https://github.com/o", "https://other.example/o/b", "https://github.com/o/b/c"):
            with self.subTest(value=value):
                with self.assertRaises(Bypass) as raised:
                    checkout.submodule_repository(value, superproject, "https://github.com")
                self.assertNotIsInstance(raised.exception, SecurityViolation)
        with self.assertRaises(Bypass) as raised:
            checkout.submodule_repository("https://github.com/", superproject, "https://github.com")
        self.assertNotIsInstance(raised.exception, SecurityViolation)
        with self.assertRaises(Bypass):
            checkout.submodule_repository("git@github.com:o/b", "https://ghe.example/o/a", "https://ghe.example")
        for value in ("https://github.com/o/b?x", "https://github.com/o/b%2e", "https://github.com/o/b#f", "../b;x"):
            with self.subTest(value=value):
                with self.assertRaises(SecurityViolation):
                    checkout.submodule_repository(value, superproject, "https://github.com")


class SessionParserTests(unittest.TestCase):
    def test_git_booleans(self):
        for value in (None, "true", "YES", "On", " on ", "1", "10", "1k", "-1", "+2g", "junk", "0x", "k", "0kk"):
            with self.subTest(value=value):
                self.assertTrue(session.git_true(value))
        for value in ("false", "No", "OFF", "", " ", "0", " 0 ", "+0", "-0", "00", "0k", "0M", "0g"):
            with self.subTest(value=value):
                self.assertFalse(session.git_true(value))

    def test_conditional_includes(self):
        for key in (
            "includeIf.gitdir:/x/.path",
            "INCLUDEIF.onbranch:main.path",
            "includeif..path",
            "includeif.a.b.path",
        ):
            with self.subTest(key=key):
                self.assertTrue(session.conditional_include(key))
        for key in ("include.path", "includeif.path", "includeif.x.pathx", "xincludeif.a.path", "includeifx.a.path"):
            with self.subTest(key=key):
                self.assertFalse(session.conditional_include(key))

    def test_relative_conditions_are_anchored(self):
        environ = {"HOME": "/home/u"}
        for key, expected in (
            ("includeIf.gitdir:./x/.path", "includeIf.gitdir:/a/x/.path"),
            ("includeIf.gitdir/i:./x/.path", "includeIf.gitdir/i:/a/x/.path"),
            ("INCLUDEIF.GITDIR:~/y.path", "INCLUDEIF.GITDIR:/home/u/y.path"),
            ("includeIf.gitdir:/./x.path", "includeIf.gitdir:/./x.path"),
            ("includeIf.gitdir:x/./y.path", "includeIf.gitdir:x/./y.path"),
            ("includeIf.onbranch:./x.path", "includeIf.onbranch:./x.path"),
            ("includeIf.gitdirx:./x.path", "includeIf.gitdirx:./x.path"),
            ("includeIf.onbranch:includeif.gitdir:./x.path", "includeIf.onbranch:includeif.gitdir:./x.path"),
        ):
            with self.subTest(key=key):
                self.assertEqual(session.anchored_condition(key, "/a/b.config", environ), expected)


if __name__ == "__main__":
    unittest.main()
