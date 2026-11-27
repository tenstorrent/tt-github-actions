# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The composite's runner gate: cachew must be a strict no-op off the CI VLAN.

GitHub-hosted runners cannot reach cachew. On them the action must not probe,
send a token, install the Git wrapper or change any Git configuration, so every
cachew step has to be skipped. On Linux only the step that reads the cachew
setting runs first; the harness runs its script for real.
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ACTION = Path(__file__).resolve().parents[1] / "action.yml"
CONTEXT = re.compile(r"\b(runner|env|inputs|steps)\.([\w.-]+)")


def steps():
    text = ACTION.read_text()
    body = text.split("\n  steps:\n", 1)[1]
    result = []
    for block in re.split(r"\n    - ", "\n" + body)[1:]:
        lines = block.splitlines()
        step = {"name": lines[0].removeprefix("name: ").strip(), "raw": block}
        for index, line in enumerate(lines):
            match = re.match(r"\s*(id|if|uses|run):\s*(.*)$", line)
            if not match or match[1] in step:
                continue
            key, value = match[1], match[2].strip()
            if value in (">-", ">", "|"):
                continued = []
                for following in lines[index + 1 :]:
                    if not following.startswith("        "):
                        break
                    continued.append(following[8:] if value == "|" else following.strip())
                value = ("\n" if value == "|" else " ").join(continued)
            step[key] = value
        result.append(step)
    return result


def script(step):
    """A step's run script as the runner receives it."""
    run = step["run"]
    return run[1:-1].replace("''", "'") if run.startswith("'") else run


def value(expression, context):
    """Evaluate the subset of GitHub expression syntax used in action.yml."""

    def lookup(match):
        value = context.get(f"{match[1]}.{match[2]}", "")
        return repr(str(value).lower())

    python = CONTEXT.sub(lookup, expression.removeprefix("${{").removesuffix("}}"))
    python = re.sub(r"'([^']*)'", lambda m: repr(m[1].lower()), python)
    python = python.replace("&&", " and ").replace("||", " or ").replace("always()", "True")
    return eval(python, {"__builtins__": {}}, {})  # noqa: S307 - fixed, test-only grammar


def evaluate(expression, context):
    return bool(value(expression, context))


def output(name):
    text = ACTION.read_text().split("\noutputs:\n", 1)[1].split("\nruns:\n", 1)[0]
    return re.search(rf"^  {name}:\n(?:    .*\n)*?    value: (.*)$", text, re.M)[1]


def runner(environment, os="Linux", cachew="auto", kill_switch=None):
    context = {"runner.environment": environment, "runner.os": os, "inputs.cachew": cachew}
    if kill_switch is not None:
        context["env.CACHEW_CHECKOUT"] = kill_switch
    return context


def mode(context):
    """The mode step's output, from running its script as the runner would."""
    step = next(step for step in steps() if step.get("id") == "mode")
    with tempfile.NamedTemporaryFile() as output:
        env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": output.name, "CACHEW_INPUT": context.get("inputs.cachew", "")}
        if "env.CACHEW_CHECKOUT" in context:
            env["CACHEW_CHECKOUT"] = context["env.CACHEW_CHECKOUT"]
        subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], env=env, check=True)
        return dict(line.split("=", 1) for line in Path(output.name).read_text().splitlines())["cachew"]


JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class RunnerGateTests(unittest.TestCase):
    def setUp(self):
        self.steps = steps()
        self.session = next(step for step in self.steps if step.get("id") == "session")
        self.checkout = next(step for step in self.steps if step.get("id") == "checkout")

    def active_steps(self, context, cloned=False):
        """Steps the runner would start for a given runner context and cachew outcome."""
        local = dict(context)
        started = []
        for step in self.steps:
            condition = step.get("if")
            self.assertIsNotNone(condition, f"step {step['name']!r} must be gated")
            if evaluate(condition, local):
                started.append(step["id"])
                # A skipped step publishes no outputs.
                if step["id"] == "mode":
                    local["steps.mode.outputs.cachew"] = mode(local)
                elif step["id"] == "session":
                    local["steps.session.outputs.ready"] = "true"
                elif step["id"] == "cachew":
                    local["steps.cachew.outputs.checked-out"] = str(cloned).lower()
        return started

    def test_github_hosted_runners_only_run_the_official_checkout(self):
        for environment in ("github-hosted", ""):
            with self.subTest(environment=environment):
                self.assertEqual(self.active_steps(runner(environment)), ["mode", "checkout"])
                self.assertEqual(self.active_steps(runner(environment), cloned=True), ["mode", "checkout"])

    def test_self_hosted_linux_runs_cachew_by_default(self):
        self.assertEqual(
            self.active_steps(runner("self-hosted")), ["mode", "session", "cachew", "checkout", "configure"]
        )

    def test_a_security_violation_fails_the_job_before_any_checkout(self):
        stop = next(step for step in self.steps if step.get("id") == "violation")
        self.assertIn("exit 1", script(stop))
        self.assertLess(self.steps.index(stop), self.steps.index(self.checkout))
        for source in ("session", "cachew"):
            with self.subTest(source=source):
                context = dict(runner("self-hosted"), **{f"steps.{source}.outputs.security-violation": "true"})
                self.assertTrue(evaluate(stop["if"], context))
        self.assertFalse(evaluate(stop["if"], runner("self-hosted")))
        self.assertFalse(evaluate(stop["if"], runner("github-hosted")))

    def test_official_checkout_runs_only_when_cachew_did_not_check_out(self):
        self.assertEqual(self.active_steps(runner("self-hosted"), cloned=True), ["mode", "session", "cachew", "configure"])

    def test_opt_out_and_unsupported_platforms_are_no_ops(self):
        for context in (
            runner("self-hosted", cachew="false"),
            # Read as actions/checkout reads inputs: trimmed, any case.
            runner("self-hosted", cachew=" False\n"),
            runner("self-hosted", kill_switch="false"),
            runner("self-hosted", kill_switch="FALSE \n"),
            runner("github-hosted", cachew="true", kill_switch="false"),
        ):
            with self.subTest(context=context):
                self.assertEqual(self.active_steps(context), ["mode", "checkout"])
        for os_name in ("Windows", "macOS"):
            with self.subTest(os=os_name):
                self.assertEqual(self.active_steps(runner("self-hosted", os=os_name)), ["checkout"])

    def test_opt_out_after_an_activated_session_still_isolates_the_checkout(self):
        # An earlier checkout activated the session: prepare must take the job-wide
        # rewrite out of this checkout's way, but no cache attempt runs.
        for context in (
            runner("self-hosted", cachew="false"),
            runner("self-hosted", kill_switch="false"),
        ):
            with self.subTest(context=context):
                context["env.CACHEW_SESSION_DIR"] = "/runner/_work/_temp/cachew-session"
                self.assertEqual(self.active_steps(context), ["mode", "session", "checkout", "configure"])
        self.assertEqual(self.active_steps(runner("self-hosted", os="macOS")), ["checkout"])

    def test_only_a_checkout_that_succeeded_records_its_token_for_the_cache(self):
        configure = next(step for step in self.steps if step.get("id") == "configure")
        expression = re.search(r"CACHEW_CHECKED_OUT: (\$\{\{.*\}\})", configure["raw"])[1]
        for context, expected in (
            ({"steps.cachew.outputs.checked-out": "true", "steps.checkout.outcome": "skipped"}, True),
            ({"steps.cachew.outputs.checked-out": "false", "steps.checkout.outcome": "success"}, True),
            ({"steps.checkout.outcome": "success"}, True),
            ({"steps.cachew.outputs.checked-out": "false", "steps.checkout.outcome": "failure"}, False),
            ({"steps.checkout.outcome": "failure"}, False),
            ({"steps.cachew.outputs.checked-out": "false", "steps.checkout.outcome": "skipped"}, False),
        ):
            with self.subTest(context=context):
                self.assertIs(evaluate(expression, context), expected)

    def test_an_opted_out_checkout_never_hands_its_token_to_the_cache(self):
        configure = next(step for step in self.steps if step.get("id") == "configure")
        expression = re.search(r"CACHEW_TOKEN: (\$\{\{.*\}\})", configure["raw"])[1]
        for context, expected in (
            ({"inputs.cachew": "auto"}, "secret"),
            ({"inputs.cachew": "false\n"}, False),
            ({"inputs.cachew": "auto", "env.CACHEW_CHECKOUT": " false"}, False),
            # Every character JavaScript's trim strips, as actions/checkout reads inputs.
            *(({"inputs.cachew": f"{space}False{space}"}, False) for space in JS_WHITESPACE),
            *(({"inputs.cachew": "auto", "env.CACHEW_CHECKOUT": f"false{space}"}, False) for space in JS_WHITESPACE),
            # U+200B is not whitespace to JavaScript: not an opt-out.
            ({"inputs.cachew": "false\u200b"}, "secret"),
        ):
            with self.subTest(context=context):
                token = {"inputs.token": "secret", "steps.mode.outputs.cachew": mode(context)}
                self.assertEqual(value(expression, token) or False, expected)

    def test_explicit_opt_in_on_other_linux_runners(self):
        self.assertEqual(
            self.active_steps(runner("github-hosted", cachew="true\n")),
            ["mode", "session", "cachew", "checkout", "configure"],
        )

    def test_official_checkout_is_unmodified(self):
        self.assertEqual(self.checkout["if"], "steps.cachew.outputs.checked-out != 'true'")
        self.assertNotIn("\n      env:", self.checkout["raw"])
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", self.checkout["uses"])

    def test_outputs_default_to_false_when_cachew_is_skipped(self):
        self.assertEqual(output("cachew-configured"), "${{ steps.configure.outputs.configured || 'false' }}")
        self.assertEqual(output("cachew-checked-out"), "${{ steps.cachew.outputs.checked-out || 'false' }}")

    def test_ref_and_commit_come_from_whichever_step_checked_out(self):
        for name in ("ref", "commit"):
            cachew = {
                "steps.cachew.outputs.checked-out": "true",
                f"steps.cachew.outputs.{name}": "from-cachew",
            }
            fallback = {
                "steps.cachew.outputs.checked-out": "false",
                f"steps.cachew.outputs.{name}": "unused",
                f"steps.checkout.outputs.{name}": "from-checkout",
            }
            with self.subTest(name=name):
                self.assertEqual(value(output(name), cachew), "from-cachew")
                self.assertEqual(value(output(name), fallback), "from-checkout")
                self.assertEqual(value(output(name), {f"steps.checkout.outputs.{name}": "hosted"}), "hosted")
        # SHA input: checkout reports an empty ref, and so does cachew.
        self.assertEqual(value(output("ref"), {"steps.cachew.outputs.checked-out": "true"}), "")

    def test_action_needs_no_pin_besides_the_consumers_pin(self):
        # Everything runs from the action's own directory; the only remote
        # action is the pinned official checkout.
        text = ACTION.read_text()
        self.assertNotIn("tenstorrent/tt-metal/", text)
        self.assertEqual(re.findall(r"^\s*uses:\s*(\S+)", text, re.M), [self.checkout["uses"].split()[0]])
        self.assertIn('"$GITHUB_ACTION_PATH/session.py" prepare', self.session["run"])
        self.assertFalse((ACTION.parent / "session").exists())

    def test_ssh_key_is_not_exposed_to_the_cachew_checkout(self):
        cachew = next(step for step in self.steps if step.get("id") == "cachew")
        self.assertNotIn("inputs.ssh-key }}", cachew["raw"])


if __name__ == "__main__":
    unittest.main()
