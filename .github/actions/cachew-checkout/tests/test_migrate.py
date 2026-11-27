# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The consumer migration helper: which checkouts move to the pinned action."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ACTION = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("migrate", ACTION / "migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)

SHA = "a" * 40
PLAIN = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
# A job's steps: key, with its steps at six spaces.
JOB = "jobs:\n  j:\n    runs-on: self-hosted\n    steps:\n"
PINNED = f"tenstorrent/tt-github-actions/.github/actions/cachew-checkout@{SHA} # cachew checkout"
STALE = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout@" + "0" * 40 + " # cachew checkout"
LEGACY = "tenstorrent/tt-metal/.github/actions/cachew-checkout@" + "1" * 40 + " # cachew checkout"
# Where the action lived between tt-metal and tenstorrent/tt-github-actions.
DOT_GITHUB = "tenstorrent/.github/actions/cachew-checkout@" + "2" * 40 + " # cachew checkout"


class RewriteTests(unittest.TestCase):
    def test_a_step_key_after_a_block_scalar_in_the_same_step_is_a_step(self):
        # The scalar belongs to the key after `- `, so a sibling key ends it.
        before = f"""jobs:
  self:
    runs-on: [self-hosted]
    steps:
      - if: >-
          github.event_name == 'push'
        uses: {PLAIN}
      - run: |
          echo
        name: x
      - uses: {PLAIN}
"""
        self.assertEqual(migrate.rewrite(before, SHA), before.replace(PLAIN, PINNED))

    def test_only_a_line_feed_ends_a_line(self):
        # str.splitlines also splits at U+2028, U+0085 and form feed, which would
        # end a script early and migrate the text after it.
        for character in ("\u2028", "\x85", "\f", "\x1c"):
            before = f"""jobs:
  self:
    runs-on: [self-hosted]
    steps:
      - run: |
          cat <<EOF
          a{character}b
          - uses: {PLAIN}
          EOF
"""
            self.assertEqual(migrate.rewrite(before, SHA), before, repr(character))

    def test_a_hash_without_a_space_before_it_is_part_of_the_ref(self):
        before = JOB + "      - uses: actions/checkout@v7.0.1#x\n      - uses: actions/checkout@v7.0.1 #x\n"
        after = migrate.rewrite(before, SHA)
        self.assertIn("uses: actions/checkout@v7.0.1#x\n", after)
        self.assertEqual(after.count(PINNED), 1)

    def test_uses_without_a_space_after_its_colon_is_text_not_a_key(self):
        # YAML reads `- uses:x` as the string "uses:x", which runs nothing.
        for value in (PLAIN, STALE, "./.github/actions/cachew-checkout", f"'{STALE}'"):
            before = f"{JOB}      - uses:{value}\n"
            with self.subTest(value=value):
                self.assertEqual(migrate.rewrite(before, SHA), before)

    def test_a_flow_step_is_reported_once_by_the_reader_that_can_read_it(self):
        # flow_steps names a flow step whose value uses_value reads exactly;
        # hidden_checkouts names one it cannot, so no step is reported twice.
        checkout = "actions/checkout@v7.0.1"
        expected = {
            f"{{uses: {checkout}}}": ([5], []),
            f"{{name: a,\n       uses: {checkout}}}": ([6], []),
            f'{{"uses"  :"{checkout}" # c\n      }}': ([5], []),
            f"{{uses: # c\n        {checkout}}}": ([], [5]),
            f"{{uses:\n        {checkout}}}": ([], [5]),
            f'{{uses: "actions/checkou\\x74@v7.0.1"}}': ([], [5]),
            f"{{uses:{checkout}}}": ([], []),
            f"{{uses: {checkout}x}}": ([], []),
        }
        for step, (flow, hidden) in expected.items():
            text = f"jobs:\n  j:\n    runs-on: self-hosted\n    steps:\n    - {step}\n"
            with self.subTest(step=step):
                self.assertEqual((migrate.flow_steps(text), sorted(migrate.hidden_checkouts(text))), (flow, hidden))

    def test_steps_are_found_where_github_runs_them_and_nowhere_else(self):
        # (--write changes the file, lines --write leaves for the report). From
        # review of PR 22: an escaped JSON-style value, a tagged value next to a
        # nested input, steps: under a matrix, and an alias in a flow sequence.
        checkout = "actions/checkout@v7.0.1"
        hosted = f"jobs:\n  h:\n    runs-on: ubuntu-latest\n    steps:\n      - &co {{uses: {checkout}}}\n"
        expected = {
            f'{JOB}      - {{"uses":"actions/checkout@v7.\\x30.1"}}\n': (False, [5]),
            f'{JOB}      - {{"uses":"{checkout}"}}\n': (False, [5]),
            f"{JOB}      - {{uses: !!str ./local, with: {{uses: {checkout}}}}}\n": (False, []),
            f"{JOB}      - {{uses: ./local, with: {{uses: {checkout}}}}}\n": (False, []),
            f"{JOB}      - {{uses: !!str {checkout}, with: {{a: b}}}}\n": (False, [5]),
            # steps: in data: a matrix entry or an input.
            "jobs:\n  j:\n    runs-on: self-hosted\n    strategy:\n      matrix:\n        include:\n"
            f"          - steps: [{{uses: {checkout}}}]\n    steps:\n      - run: echo\n": (False, []),
            "jobs:\n  j:\n    runs-on: self-hosted\n    strategy:\n      matrix:\n        include:\n"
            f"          - steps:\n              - uses: {checkout}\n    steps:\n      - run: echo\n": (False, []),
            f"jobs:\n  j:\n    runs-on: self-hosted\n    with:\n      steps:\n        - uses: {checkout}\n": (False, []),
            f"steps:\n  - uses: {checkout}\n": (False, []),
            # A composite action's steps run.
            f"runs:\n  using: composite\n  steps:\n    - uses: {checkout}\n": (True, []),
            # A hosted job's checkout step, aliased into a self-hosted job.
            f"{hosted}  s:\n    runs-on: self-hosted\n    steps: [*co]\n": (False, [8]),
            f"{hosted}  s:\n    runs-on: self-hosted\n    steps: [{{run: x}}, *co]\n": (False, [8]),
            f"{hosted}  s:\n    runs-on: self-hosted\n    steps: [*co] # cachew: keep\n": (False, []),
            f"{hosted}  s:\n    runs-on: ubuntu-latest\n    steps: [*co]\n": (False, []),
            # An anchor is its node only, not the checkout beside it on its line.
            f"jobs:\n  h:\n    runs-on: ubuntu-latest\n    steps: [&script {{run: echo}}, {{uses: {checkout}}}]\n"
            "  s:\n    runs-on: self-hosted\n    steps: [*script]\n": (False, []),
            f"jobs:\n  h:\n    runs-on: ubuntu-latest\n    steps: [&script {{run: echo}}, {{uses: {checkout}}}]\n"
            "  s:\n    runs-on: self-hosted\n    steps:\n      - *script\n": (False, []),
            f"jobs:\n  h:\n    runs-on: ubuntu-latest\n    steps: [{{run: echo}}, &co {{uses: {checkout}}}]\n"
            "  s:\n    runs-on: self-hosted\n    steps:\n      - *co\n": (False, [8]),
            f"jobs:\n  h:\n    runs-on: ubuntu-latest\n    steps: [{{run: echo}}, &co {{uses: {checkout}}}]\n"
            "  s:\n    runs-on: self-hosted\n    steps: [*co]\n": (False, [7]),
        }
        for text, (changed, left) in expected.items():
            with self.subTest(text=text):
                after = migrate.rewrite(text, SHA)
                self.assertEqual((after != text, sorted(set(migrate.flow_steps(text)) | set(migrate.hidden_checkouts(after)))), (changed, left))

    def test_each_verify_pins_check_reports_what_only_it_sees(self):
        # --verify-pins layers several readers; each input here reaches one of
        # them only, so a reader that stops seeing it changes the messages.
        action, other = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout", "0" * 40
        expected = {
            # A mention outside any uses: value, in any case or with \ separators.
            f"# Tenstorrent/tt-github-actions/.GitHub/Actions/Cachew-Checkout@{SHA}": [
                f"w.yml:5 mentions Tenstorrent/tt-github-actions/.GitHub/Actions/Cachew-Checkout@{SHA} outside a uses: value it can verify"
            ],
            f"- run: echo tenstorrent\\tt-github-actions/.github\\actions\\cachew-checkout@{SHA}": [
                f"w.yml:5 mentions tenstorrent\\tt-github-actions/.github\\actions\\cachew-checkout@{SHA} outside a uses: value it can verify"
            ],
            # A quoted uses: key, or one after a comma, is still a uses: value.
            f"- 'uses': {action}@{other}": [f"w.yml:5 pins {other}, expected action_sha {SHA}; run --write"],
            f'- "uses": {action}@{other}': [f"w.yml:5 pins {other}, expected action_sha {SHA}; run --write"],
            f"- {{name: x,uses: {action}@{other}}}": [f"w.yml:5 pins {other}, expected action_sha {SHA}; run --write"],
            # \ separates path segments; it is no escape in a plain scalar.
            "- uses: tenstorrent\\tt-github-actions/.github\\actions\\cachew-checkout@main": [
                f"w.yml:5 pins main, expected action_sha {SHA}; run --write"
            ],
            # An @ inside the path is not the action.
            "- run: echo tenstorrent/tt-github-actions/.github/x@y/cachew-checkout@v1": [],
        }
        for step, messages in expected.items():
            text = f"jobs:\n  j:\n    runs-on: self-hosted\n    steps:\n      {step}\n"
            with self.subTest(step=step):
                self.assertEqual(migrate.file_problems("w.yml", text, SHA), messages)

    def test_a_quoted_or_anchored_job_key_still_ends_the_job_before_it(self):
        # Unseen, the hosted job's runs-on counted for the self-hosted job too,
        # and --write turned that job's pin back into plain checkout.
        for key in ('"lint":', "lint: &lint", "'lint':  # comment"):
            before = f"""jobs:
  self:
    runs-on: [self-hosted, linux]
    steps:
      - uses: {PINNED}
      - uses: {PLAIN}
  {key}
    runs-on: ubuntu-latest
    steps:
      - uses: {PLAIN}
"""
            after = before.replace(f"- uses: {PLAIN}", f"- uses: {PINNED}", 1)
            with self.subTest(key=key):
                self.assertEqual(migrate.rewrite(before, SHA), after)

    def test_a_hosted_job_is_recognized_however_its_runs_on_is_written(self):
        def workflow(runs_on, indent="  ", jobs="jobs", step=PLAIN):
            body = f"build:\n{indent}{runs_on}\n{indent}steps:\n{indent}{indent}- uses: {step}\n"
            return f"on: push\n{jobs}:\n" + "".join(indent + line + "\n" for line in body.splitlines())

        hosted = {
            "flow sequence": workflow("runs-on: [ubuntu-latest]"),
            "spaced flow sequence": workflow("runs-on: [ 'ubuntu-latest' ]  # hosted"),
            "block sequence": workflow("runs-on:\n    - ubuntu-latest  # hosted"),
            "block sequence at the key's indentation": workflow("runs-on:\n  - macos-14"),
            "four-space indentation": workflow("runs-on: ubuntu-latest", indent="    "),
            "another case": workflow("runs-on: Ubuntu-Latest"),
            "quoted keys": workflow('"runs-on": windows-2022', jobs='"jobs"'),
            "an anchored label": workflow("runs-on: &os ubuntu-latest"),
            "a tagged label": workflow("runs-on: !!str ubuntu-latest"),
            "an anchored flow sequence": workflow("runs-on: &os [ubuntu-latest]"),
            "an anchored block sequence": workflow("runs-on: &os\n    - ubuntu-latest"),
            "a label on the next line": workflow("runs-on:\n    ubuntu-latest  # hosted"),
            "a flow sequence on the next line": workflow("runs-on:\n    [ubuntu-latest]"),
        }
        for name, before in hosted.items():
            with self.subTest(hosted=name):
                self.assertEqual(migrate.rewrite(before, SHA), before)
                self.assertEqual(migrate.rewrite(before.replace(PLAIN, STALE), SHA), before)
        self_hosted = {
            "a hosted label among others": workflow("runs-on: [self-hosted, ubuntu-22.04]"),
            "a block sequence with another label": workflow("runs-on:\n    - ubuntu-22.04\n    - gpu"),
            "an expression": workflow("runs-on: ${{ matrix.os }}"),
            "an empty sequence": workflow("runs-on: []"),
            "a label over two lines": workflow("runs-on:\n    ubuntu-latest\n    gpu"),
            "a runner group": workflow("runs-on:\n    group: large\n    labels: ubuntu-latest"),
            "a deeper runs-on key": workflow("runs-on: tt-large\n  env:\n    runs-on: ubuntu-latest"),
        }
        for name, before in self_hosted.items():
            with self.subTest(self_hosted=name):
                self.assertEqual(migrate.rewrite(before, SHA), before.replace(PLAIN, PINNED))

    def test_self_hosted_jobs_move_to_the_pinned_action_and_hosted_jobs_keep_checkout(self):
        before = f"""on: push
jobs:
  build:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {PLAIN}
  matrix:
    runs-on: >-
      ${{{{ inputs.label }}}}
    steps:
      - uses: {STALE}
  hosted:
    runs-on: ubuntu-22.04
    steps:
      - uses: {PLAIN}
      - uses: {STALE}
  arm:
    runs-on: "ubuntu-24.04-arm"
    steps:
      - uses: {PINNED}
  kept:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {PLAIN} cachew: keep
"""
        expected = f"""on: push
jobs:
  build:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {PINNED}
  matrix:
    runs-on: >-
      ${{{{ inputs.label }}}}
    steps:
      - uses: {PINNED}
  hosted:
    runs-on: ubuntu-22.04
    steps:
      - uses: {PLAIN}
      - uses: {PLAIN}
  arm:
    runs-on: "ubuntu-24.04-arm"
    steps:
      - uses: {PLAIN}
  kept:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {PLAIN} cachew: keep
"""
        self.assertEqual(migrate.rewrite(before, SHA), expected)

    def test_uses_lines_inside_a_quoted_or_detached_scalar_are_text_not_steps(self):
        line = "uses: actions/checkout@v7.0.1"
        step = f"      - {line}\n"
        migrated = f"      - uses: {migrate.ACTION_REPO}@{SHA} # cachew checkout\n"
        head = "jobs:\n  build:\n    runs-on: [self-hosted]\n    steps:\n"
        for name, script in {
            "double quoted": f'      - run: "echo\n          {line}\n          done"\n',
            "single quoted": f"      - run: 'echo ''\n          {line}\n          done'\n",
            "escaped quote and line break": f'      - run: "a \\"\\\n          {line}\n          "\n',
            "quoted on the next line": f'      - run:\n          "a\n          {line}\n          "\n',
            "quoted key": f'      - "run": "a\n          {line}\n          "\n',
            "flow mapping": f'      - {{name: x, run: "a\n          {line}\n          "}}\n',
            "indicator on the next line": f"      - run:\n          |\n          {line}\n",
            "indicator under a sequence entry": (
                f"      - run:\n          - x\n          -\n            >-\n            {line}\n"
            ),
        }.items():
            with self.subTest(name):
                text = head + step + script + step
                self.assertEqual(migrate.rewrite(text, SHA), head + migrated + script + migrated)
        # Quotes inside a plain scalar open nothing.
        for plain in ('      - run: echo "unclosed\n', "      - name: it's\n", '      - run: a "b" \'c\n'):
            with self.subTest(plain):
                self.assertEqual(migrate.rewrite(head + plain + step, SHA), head + plain + migrated)

    def test_uses_lines_inside_a_script_are_text_not_steps(self):
        def workflow(step, embedded_pin, hosted_pin):
            return (
                "jobs:\n  build:\n    runs-on: [self-hosted]\n    steps:\n"
                f"      - uses: {step}\n"
                "      - name: Write another workflow\n"
                "        run: |\n"
                "          cat > generated.yml <<'EOF'\n"
                "          jobs:\n"
                "            lint:\n"
                "              runs-on: ubuntu-latest\n"
                "              steps:\n"
                f"                - uses: {PLAIN}\n"
                "\n"
                f"                - uses: {embedded_pin}\n"
                "          EOF\n"
                "      - run: >-\n"
                f"          echo uses: {PLAIN}\n"
                f"      - uses: {step}\n"
                "  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: |  # keep\n"
                f"          - uses: {hosted_pin}\n"
                f"      - uses: {PLAIN}\n"
            )

        self.assertEqual(migrate.rewrite(workflow(PLAIN, STALE, STALE), SHA), workflow(PINNED, PINNED, PINNED))

    def test_a_pin_in_text_moves_without_touching_the_rest_of_its_line(self):
        # In a script or a quoted scalar a uses: line is text: only the pin
        # changes. Its quotes stay, and so does its comment, which inside a
        # quoted scalar is part of the value.
        old = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout@" + "0" * 40
        legacy = "tenstorrent/tt-metal/.github/actions/cachew-checkout@" + "1" * 40
        new = f"{migrate.ACTION_REPO}@{SHA}"
        head = "jobs:\n  build:\n    runs-on: [self-hosted]\n    steps:\n"
        for text in (
            "      - run: |\n          cat > wf.yml <<'EOF'\n          - uses: '{pin}' # pinned\n          EOF\n",
            '      - run: "cat > wf.yml <<EOF\n\n          uses: \'{pin}\'\n\n          EOF"\n',
            "      - run: 'cat > wf.yml <<EOF\n\n          uses: \"{pin}\" # kept\n\n          EOF'\n",
            '      - run: "cat > wf.yml <<EOF\n\n          uses: {pin}\n\n          EOF"\n',
        ):
            for pin in (old, legacy, DOT_GITHUB.split(" #")[0]):
                with self.subTest(text=text, pin=pin):
                    before = head + text.format(pin=pin)
                    self.assertEqual(migrate.rewrite(before, SHA), head + text.format(pin=new))

    def test_uses_keys_outside_a_steps_sequence_are_data_not_steps(self):
        # Only an item of a steps: sequence is a step. A uses: key elsewhere (an
        # input, an environment variable, a matrix entry, a data file) is data
        # the workflow reads: its checkout is left alone, and a pin to this
        # action there still moves to the current commit, as in a script.
        def workflow(value, pin):
            return (
                f"uses: {value}\n"
                "jobs:\n  build:\n    runs-on: [self-hosted]\n"
                "    strategy:\n      matrix:\n        include:\n"
                f"          - uses: {value}\n"
                f"            pin: x\n"
                f"          - uses: {pin}\n"
                "    env:\n"
                f"      uses: {value}\n"
                "    steps:\n"
                "      - uses: ./local\n"
                "        with:\n"
                f"          uses: {value}\n"
                "\n"
                "          # a comment\n"
                f"          also: {value}\n"
                "      - name: step\n"
                "        # the step's own key\n"
                f"        uses: {step}\n"
                "    outputs:\n"
                "      list:\n"
                f"      - uses: {value}\n"
            )

        step = PLAIN
        before = workflow(PLAIN, STALE)
        step = PINNED
        self.assertEqual(migrate.rewrite(before, SHA), workflow(PLAIN, PINNED))

    def test_a_quoted_uses_key_is_a_step_key(self):
        # YAML reads "uses", 'uses' and `"uses" :` as the key uses.
        before = (
            "jobs:\n  build:\n    runs-on: [self-hosted]\n    steps:\n"
            f'      - "uses": {PLAIN}\n'
            f"      - name: x\n        'uses' : {PLAIN}\n"
            f'      - "uses": "actions/checkout@v7.0.1"\n'
            "  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - 'uses': {STALE}\n"
        )
        after = (
            "jobs:\n  build:\n    runs-on: [self-hosted]\n    steps:\n"
            f'      - "uses": {PINNED}\n'
            f"      - name: x\n        'uses' : {PINNED}\n"
            f'      - "uses": {PINNED}\n'
            "  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - 'uses': {PLAIN}\n"
        )
        self.assertEqual(migrate.rewrite(before, SHA), after)

    def test_a_steps_sequence_at_its_keys_indentation_holds_steps(self):
        before = f"jobs:\n  build:\n    runs-on: [self-hosted]\n    'steps':  # comment\n    - name: x\n      uses: {PLAIN}\n    - uses: {PLAIN}\n"
        self.assertEqual(migrate.rewrite(before, SHA), before.replace(PLAIN, PINNED))

    def test_the_exact_version_is_migrated_however_its_owner_and_name_are_cased(self):
        # GitHub resolves Actions/Checkout as actions/checkout.
        before = "runs:\n  steps:\n    - uses: Actions/Checkout@v7.0.1\n    - uses: actions/checkout@v7.0.10\n"
        after = f"runs:\n  steps:\n    - uses: {PINNED}\n    - uses: actions/checkout@v7.0.10\n"
        self.assertEqual(migrate.rewrite(before, SHA), after)

    def test_composite_actions_are_migrated_unless_kept(self):
        before = f"runs:\n  using: composite\n  steps:\n    - uses: {PLAIN}\n    - uses: {PLAIN} cachew: keep\n"
        expected = f"runs:\n  using: composite\n  steps:\n    - uses: {PINNED}\n    - uses: {PLAIN} cachew: keep\n"
        self.assertEqual(migrate.rewrite(before, SHA), expected)

    def test_pins_to_the_old_tt_metal_location_move_here(self):
        before = f"""jobs:
  build:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {LEGACY}
  hosted:
    runs-on: ubuntu-22.04
    steps:
      - uses: {LEGACY}
"""
        expected = f"""jobs:
  build:
    runs-on: tt-ubuntu-2204-large-stable
    steps:
      - uses: {PINNED}
  hosted:
    runs-on: ubuntu-22.04
    steps:
      - uses: {PLAIN}
"""
        self.assertEqual(migrate.rewrite(before, SHA), expected)

    def test_pins_to_the_old_dot_github_location_move_here(self):
        before = JOB + f"      - uses: {DOT_GITHUB}\n      - uses: 'Tenstorrent/.GitHub/actions/cachew-checkout@{'2' * 40}'\n"
        expected = JOB + f"      - uses: {PINNED}\n      - uses: {PINNED}\n"
        self.assertEqual(migrate.rewrite(before, SHA), expected)

    def test_other_checkout_versions_are_left_alone(self):
        # Floating tags too: v7 may point past the v7.0.1 the action forwards to.
        text = JOB + "      - uses: actions/checkout@v4\n      - uses: actions/checkout@main\n      - uses: actions/checkout@v7\n"
        self.assertEqual(migrate.rewrite(text, SHA), text)

    def test_rewrite_refuses_a_short_sha(self):
        with self.assertRaises(ValueError):
            migrate.rewrite("", "abc123")

    def test_forwarded_checkout_matches_the_action(self):
        pins = re.findall(r"uses: actions/checkout@([0-9a-f]{40})", (ACTION / "action.yml").read_text())
        self.assertEqual(set(pins), {migrate.FORWARDED_CHECKOUT})


class RepositoryTests(unittest.TestCase):
    """migrate.py run against a throwaway consumer repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.files = {
            ".github/workflows/ci.yml": f"jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n      - uses: {PLAIN}\n",
            ".github/actions/setup/action.yml": f"runs:\n  using: composite\n  steps:\n    - uses: {LEGACY}\n",
            "vendored/.github/workflows/ci.yml": f"jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n      - uses: {PLAIN}\n",
            ".github/workflows/agent.lock.yml": f"jobs:\n  a:\n    runs-on: self-hosted\n    steps:\n      - uses: {PLAIN}\n",
            "docs/example.yml": f"- uses: {PLAIN}\n",
        }
        for name, text in self.files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)
        self.write_config({"action_sha": SHA, "exclude": ["vendored/"]})
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, data):
        (self.root / ".github/cachew-checkout.json").write_text(json.dumps(data))

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = migrate.main(["--root", str(self.root), *args])
        return code, out.getvalue()

    def read(self, name):
        return (self.root / name).read_text()

    def test_nested_github_directories_are_data_not_workflows(self):
        # GitHub runs only the repository root's .github/workflows. A fixture
        # repository under tests/ is data: --write must leave its bytes alone.
        name = "tests/fixtures/repo/.github/workflows/ci.yml"
        (self.root / name).parent.mkdir(parents=True)
        (self.root / name).write_text(self.files[".github/workflows/ci.yml"])
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        self.assertEqual(self.run_main("--write")[0], 0)
        self.assertEqual(self.read(name), self.files[".github/workflows/ci.yml"])
        self.assertEqual(self.run_main("--check")[0], 0)
        self.assertEqual(self.run_main("--verify-pins")[0], 0)

    def test_an_unreadable_value_of_another_action_named_cachew_passes(self):
        # Only text that could spell cachew-checkout fails: another action whose
        # name merely contains cachew, written as a folded scalar, is not this one.
        self.assertEqual(self.run_main("--write")[0], 0)
        name = ".github/workflows/tools.yml"
        (self.root / name).write_text(
            "jobs:\n  b:\n    runs-on: self-hosted\n    steps:\n      - uses: >-\n          myorg/cachew-tools@v1\n"
        )
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        self.assertEqual(self.run_main("--check"), (0, "0 file(s) need migration.\n"))
        self.assertEqual(self.run_main("--verify-pins")[0], 0)

    def test_write_keeps_crlf_line_endings(self):
        name = ".github/workflows/ci.yml"
        crlf = self.files[name].replace("\n", "\r\n")
        (self.root / name).write_bytes(crlf.encode())
        self.assertEqual(self.run_main("--write")[0], 0)
        written = (self.root / name).read_bytes().decode()
        self.assertIn(PINNED, written)
        self.assertEqual(written.count("\n"), written.count("\r\n"))
        self.assertEqual(written.count("\n"), crlf.count("\n"))
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_check_write_and_verify(self):
        code, out = self.run_main("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("2 file(s) need migration", out)
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1)
        self.assertIn("the action moved to", out)

        self.assertEqual(self.run_main("--write")[0], 0)
        self.assertIn(PINNED, self.read(".github/workflows/ci.yml"))
        self.assertIn(PINNED, self.read(".github/actions/setup/action.yml"))
        # Excluded prefix, gh-aw lock file and files outside .github/{actions,workflows}.
        for name in ("vendored/.github/workflows/ci.yml", ".github/workflows/agent.lock.yml", "docs/example.yml"):
            self.assertEqual(self.read(name), self.files[name], name)

        self.assertEqual(self.run_main("--check")[0], 0)
        self.assertEqual(self.run_main("--verify-pins")[0], 0)

    def test_check_fails_on_every_reference_write_cannot_fix(self):
        # --check passing must mean --verify-pins passes for these files: a
        # cachew reference --write leaves alone (a flow-style step, a ref that is
        # not a commit, the old tt-metal location) is reported, not skipped.
        workflow = self.root / ".github/workflows/ci.yml"
        (self.root / ".github/actions/setup/action.yml").write_text(f"runs:\n  using: composite\n  steps:\n    - uses: {PINNED}\n")
        head = "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
        legacy = LEGACY.split(" #")[0]
        stale = STALE.split(" #")[0]
        for step in (
            f"      - {{uses: {legacy}}}\n",
            f"      - {{name: x, uses: {stale}}}\n",
            "      - uses: tenstorrent/tt-metal/.github/actions/cachew-checkout@main\n",
            f"      - {{uses: {DOT_GITHUB.split(' #')[0]}}}\n",
            "      - uses: tenstorrent/.github/actions/cachew-checkout@main\n",
            "      - uses: tenstorrent/tt-github-actions/.github/actions/cachew-checkout@v1\n",
            "      - uses: \"tenstorrent/tt-github-actions/.github/actions/cachew-checkout@main\" # pinned later\n",
        ):
            with self.subTest(step=step):
                workflow.write_text(head + step)
                self.assertEqual(self.run_main("--verify-pins")[0], 1)
                for mode in ("--check", "--write"):
                    code, out = self.run_main(mode)
                    self.assertEqual(code, 1, out)
                    self.assertIn(".github/workflows/ci.yml:5", out)
        workflow.write_text(head + f"      - uses: {PINNED}\n      - {{uses: {PINNED.split(' #')[0]}}}\n")
        for mode in ("--check", "--write", "--verify-pins"):
            code, out = self.run_main(mode)
            self.assertEqual(code, 0, out)

    def test_steps_that_run_an_in_repo_copy_of_the_action_are_migrated(self):
        # Before the move, consumers ran the action from their own repository: a
        # local copy left there is never the pin, so every mode sees it.
        workflow = self.root / ".github/workflows/ci.yml"
        (self.root / ".github/actions/setup/action.yml").write_text(f"runs:\n  using: composite\n  steps:\n    - uses: {PINNED}\n")
        head = "jobs:\n  build:\n    runs-on: {runner}\n    steps:\n"
        for local in ("./.github/actions/cachew-checkout", "./.github/actions/cachew-checkout/", "'./.github/actions/cachew-checkout'"):
            for runner, expected in (("self-hosted", PINNED), ("ubuntu-latest", PLAIN)):
                with self.subTest(local=local, runner=runner):
                    workflow.write_text(head.format(runner=runner) + f"      - uses: {local}\n        with:\n          fetch-depth: 0\n")
                    for mode in ("--check", "--verify-pins"):
                        code, out = self.run_main(mode)
                        self.assertEqual(code, 1, out)
                        self.assertIn(".github/workflows/ci.yml", out)
                    self.assertEqual(self.run_main("--write")[0], 0)
                    self.assertEqual(
                        workflow.read_text(),
                        head.format(runner=runner) + f"      - uses: {expected}\n        with:\n          fetch-depth: 0\n",
                    )
                    for mode in ("--check", "--verify-pins"):
                        code, out = self.run_main(mode)
                        self.assertEqual(code, 0, out)

    def test_flow_style_checkout_steps_are_reported_not_skipped(self):
        # --write does not migrate a step written as a flow mapping; it and
        # --check name each one, and fail, so none is left behind unnoticed.
        workflow = self.root / ".github/workflows/ci.yml"
        checkout = PLAIN.split(" #")[0]
        head = "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
        for step in (
            f"      - {{uses: {checkout}, with: {{fetch-depth: 0}}}}\n",
            f"      - {{name: x, 'uses': \"{checkout}\"}}\n",
            f"      - {{name: x, uses: {checkout},\n          with: {{fetch-depth: 0}}}}\n",
            f"      - {{name: x,\n          uses: {checkout}}}\n",
        ):
            with self.subTest(step=step):
                workflow.write_text(head + step)
                line = 5 + step.count("\n", 0, step.index("uses"))
                for mode in ("--check", "--write"):
                    code, out = self.run_main(mode)
                    self.assertEqual(code, 1, out)
                    self.assertIn(f".github/workflows/ci.yml:{line}", out)
                    self.assertIn("flow", out)
                self.assertEqual(workflow.read_text(), head + step)
        # A kept step, a hosted job and a script are not steps to migrate here.
        for text in (
            head + f"      - {{uses: {checkout}}} # cachew: keep\n",
            head.replace("self-hosted", "ubuntu-latest") + f"      - {{uses: {checkout}}}\n",
            head + f"      - run: |\n          - {{uses: {checkout}}}\n",
            # Not items of a steps: sequence, or not flow mappings at all.
            "jobs:\n  build:\n    runs-on: self-hosted\n    strategy:\n      matrix:\n        include:\n"
            f"          - {{uses: {checkout}}}\n    steps:\n      - run: echo\n",
            head + f"      - run: echo \"{{uses: {checkout}}}\"\n",
            head + f"      - run: echo {{uses: {checkout}}}\n",
            head + f"      - {{run: echo, with: {{uses: {checkout}}}}}\n",
            head + f"      - {{run: 'echo {{uses: {checkout}}}'}}\n",
        ):
            with self.subTest(text=text):
                workflow.write_text(text)
                for mode in ("--write", "--check"):
                    code, out = self.run_main(mode)
                    self.assertNotIn("ci.yml:", out)

    def test_checkouts_in_forms_write_cannot_read_are_reported_not_skipped(self):
        # GitHub runs these steps' checkout, but --write cannot rewrite their
        # value in place: --check and --write must name each one and fail.
        workflow = self.root / ".github/workflows/ci.yml"
        checkout = PLAIN.split(" #")[0]
        escaped = checkout.replace("/", "\\x2f", 1)
        head = "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
        cases = (
            (head + f"      - uses: !!str {checkout}\n", 5),
            (head + f"      - uses: &c {checkout}\n", 5),
            (head + f"      - name: &c {checkout}\n        run: echo\n      - uses: *c\n", 7),
            (head + f"      - uses:\n          {checkout}\n", 5),
            (head + f"      - uses: >-\n          {checkout}\n", 5),
            (head + f'      - uses: "{escaped}"\n', 5),
            (head + f"      - {{uses: !!str {checkout}}}\n", 5),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps: [{{uses: {checkout}}}]\n", 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps:\n      [{{run: echo}},\n       {{uses: {checkout}}}]\n", 6),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps: &s [{{uses: {checkout}}}]\n", 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps: !!seq [{{uses: {checkout}}}]\n", 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f'    steps: [{{name: "Checkout [source", uses: {checkout}}}]\n', 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f'    steps: [{{name: !!str "Checkout [source", uses: {checkout}}}]\n', 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f'    steps: [{{name: &n "Checkout [source", uses: {checkout}}}]\n', 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f'    steps: [{{"uses":"{checkout}"}}]\n', 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f'    "steps": [{{"name":"x","uses":"{checkout}"}}]\n', 4),
            ("jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps:\n      !!seq\n      [{{uses: {checkout}}}]\n", 6),
            (head + f'      - {{"uses":"{checkout}"}}\n', 5),
            (head + f"      - &a {{uses: {checkout}}}\n", 5),
            (head + f"      - !!map {{uses: !!str {checkout}}}\n", 5),
            # An explicit key.
            (head + f"      - ? uses\n        : {checkout}\n", 5),
            (head + f"      - name: x\n        ? uses\n        : {checkout}\n", 6),
            # A step a hosted job anchors, run again through an alias here.
            (
                "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
                f"      - &co\n        uses: {checkout}\n"
                "  build:\n    runs-on: self-hosted\n    steps:\n      - *co\n",
                10,
            ),
            (
                "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps: &st\n"
                f"      - uses: {checkout}\n"
                "  build:\n    runs-on: self-hosted\n    steps: *st\n",
                8,
            ),
            # A job written as a flow mapping.
            (f"jobs:\n  build: {{runs-on: self-hosted, steps: [{{uses: {checkout}}}]}}\n", 2),
            (f"jobs:\n  build: {{runs-on: [self-hosted, gpu],\n    steps: [{{uses: {checkout}}}]}}\n", 3),
            (f"jobs: {{build: {{runs-on: self-hosted, steps: [{{uses: {checkout}}}]}}}}\n", 1),
            # A hosted job beside it on the same line covers only itself.
            (
                "jobs: {hosted: {runs-on: ubuntu-latest, steps: [{run: echo}]},"
                f" build: {{runs-on: self-hosted, steps: [{{uses: {checkout}}}]}}}}\n",
                1,
            ),
            (
                f"jobs: {{build: {{runs-on: self-hosted, steps: [{{uses: {checkout}}}]}},"
                " hosted: {runs-on: ubuntu-latest, steps: [{run: echo}]}}\n",
                1,
            ),
        )
        for text, line in cases:
            with self.subTest(text=text):
                workflow.write_text(text)
                for mode in ("--check", "--write"):
                    code, out = self.run_main(mode)
                    self.assertEqual(code, 1, out)
                    self.assertIn(f".github/workflows/ci.yml:{line}", out)
                self.assertEqual(workflow.read_text(), text)
        # Kept, hosted, script text and data are still left alone.
        for text in (
            head + f"      - uses: !!str {checkout} # cachew: keep\n",
            head.replace("self-hosted", "ubuntu-latest") + f"      - uses: !!str {checkout}\n",
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n" f"    steps: [{{uses: {checkout}}}]\n",
            f"jobs:\n  build: {{runs-on: ubuntu-latest, steps: [{{uses: {checkout}}}]}}\n",
            f"jobs: {{\n  build: {{\n    runs-on: ubuntu-latest,\n    steps: [\n      {{uses: {checkout}}}\n    ]\n  }}\n}}\n",
            # A comment after a flow job's runs-on is not part of its label.
            "jobs:\n  build: {\n    runs-on: ubuntu-latest, # hosted runner\n"
            f"    steps: [{{uses: {checkout}}}]\n  }}\n",
            "jobs:\n  build: {\n    runs-on: [ubuntu-latest], # hosted runner\n"
            f"    steps: [{{uses: {checkout}}}]\n  }}\n",
            # An aliased step whose anchor runs only a script, or whose checkout
            # is data under another key, and aliases kept by their own comment.
            "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - &script\n        run: echo {checkout}\n"
            "  build:\n    runs-on: self-hosted\n    steps:\n      - *script\n",
            "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - &step\n        uses: ./local\n        with:\n          uses: {checkout}\n"
            "  build:\n    runs-on: self-hosted\n    steps:\n      - *step\n",
            "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - &co\n        uses: {checkout}\n"
            "  build:\n    runs-on: self-hosted\n    steps:\n      - *co # cachew: keep\n",
            "jobs:\n  hosted:\n    runs-on: ubuntu-latest\n    steps: &st\n"
            f"      - uses: {checkout}\n"
            "  build:\n    runs-on: self-hosted\n    steps: *st # cachew: keep\n",
            # Aliased steps whose anchor --write migrates, or a hosted job's.
            "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
            f"      - &co\n        uses: {checkout} # cachew: keep\n"
            "  hosted:\n    runs-on: ubuntu-latest\n    steps:\n      - *co\n",
            head + f"      - ? uses\n        : {checkout} # cachew: keep\n",
            head + f"      - run: |\n          uses: !!str {checkout}\n",
            head + f"      - uses: ./local\n        with:\n          uses: !!str {checkout}\n",
            head + f"      - {{run: echo, with: {{uses: !!str {checkout}}}}}\n",
            "jobs:\n  build:\n    runs-on: self-hosted\n" f"    env: [{{uses: {checkout}}}]\n    steps:\n      - run: echo\n",
            # Another version, a ref that only starts like this one, or the
            # checkout named only in a comment.
            head + f"      - uses: !!str {checkout.replace('3d3c42e5', '00000000')}\n",
            head + "      - uses: !!str actions/checkout@v7.0.10\n",
            head + f"      - uses: !!str {checkout}0\n",
            head + "      - uses: !!str myactions/checkout@v7.0.1\n",
            head + f"      - uses: !!str ./local # was {checkout}\n",
            head + f"      - uses: # was {checkout}\n          ./local\n",
            # Script text that only looks like a steps sequence, and a uses:
            # inside a quoted scalar of a step.
            head + f"      - run: 'echo steps: [{{uses: {checkout}}}]'\n",
            head + f'      - run: "echo steps: [{{uses: {checkout}}}]"\n',
            "jobs:\n  build:\n    runs-on: self-hosted\n" f'    steps: [{{run: "echo uses: {checkout}"}}]\n',
            "jobs:\n  build:\n    runs-on: self-hosted\n" f"    steps: [{{uses: {checkout}}}] # cachew: keep\n",
            # An item of a multi-line sequence kept by its own trailing comment.
            "jobs:\n  build:\n    runs-on: self-hosted\n"
            f"    steps: [\n      {{uses: {checkout}}}, # cachew: keep\n      {{run: echo}}\n    ]\n",
            "jobs:\n  build:\n    runs-on: self-hosted\n"
            f"    steps: [\n      {{run: echo}},\n      {{uses: {checkout}}} # cachew: keep\n    ]\n",
        ):
            with self.subTest(text=text):
                workflow.write_text(text)
                for mode in ("--write", "--check"):
                    code, out = self.run_main(mode)
                    self.assertNotIn("ci.yml:", out)

    def test_a_step_item_whose_keys_start_on_a_later_line_or_after_properties_migrates(self):
        head = "jobs:\n  build:\n    runs-on: self-hosted\n    steps:\n"
        for item in (
            "      -\n        uses: {}\n",
            "      - # checkout\n        uses: {}\n",
            "      - &s\n        uses: {}\n",
            "      - !!map\n        name: x\n        uses: {}\n",
            "      - &s uses: {}\n",
            "      - !!str uses: {}\n",
            "      - name: &n x\n        uses: {}\n",
        ):
            before = head + item.format(PLAIN)
            with self.subTest(item=item):
                self.assertEqual(migrate.rewrite(before, SHA), head + item.format(PINNED))
        # Under another key of the item, it is data.
        data = head + f"      -\n        with:\n          uses: {PLAIN}\n"
        self.assertEqual(migrate.rewrite(data, SHA), data)

    def test_a_steps_own_comment_survives_migration(self):
        # The version tag comment is replaced; anything else the author wrote
        # after it stays on the line.
        checkout = PLAIN.split(" #")[0]
        pin = PINNED.split(" #")[0]
        head = "jobs:\n  build:\n    runs-on: {}\n    steps:\n"
        for runner, before, after in (
            ("self-hosted", f"{checkout} # v7.0.1 needs full history", f"{pin} # cachew checkout; needs full history"),
            ("self-hosted", f"{checkout} # needs full history", f"{pin} # cachew checkout; needs full history"),
            ("self-hosted", f"{checkout} # v7.0.1", f"{pin} # cachew checkout"),
            ("ubuntu-latest", f"{pin} # cachew checkout; needs tags", f"{PLAIN}; needs tags"),
            ("ubuntu-latest", f"{pin} # cachew checkout", PLAIN),
        ):
            with self.subTest(before=before):
                text = head.format(runner) + f"      - uses: {before}\n"
                self.assertEqual(migrate.rewrite(text, SHA), head.format(runner) + f"      - uses: {after}\n")

    def test_an_exclude_prefix_is_a_whole_directory(self):
        # "tools" excludes tools/, not a sibling tools-extra/ whose action GitHub runs.
        self.write_config({"action_sha": SHA, "exclude": ["tools"]})
        for name in ("tools/x/action.yml", "tools-extra/x/action.yml"):
            (self.root / name).parent.mkdir(parents=True)
            (self.root / name).write_text(f"runs:\n  using: composite\n  steps:\n    - uses: {STALE}\n")
        self.assertEqual(self.run_main("--write")[0], 0)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("tools-extra/x/action.yml", out)
        self.assertNotIn("tools/x/action.yml", out)

    def test_a_config_that_is_not_an_object_is_reported_in_every_mode(self):
        for text in ("[]", "5", "null", '"x"', "\udcff"):
            for mode in ("--check", "--write", "--verify-pins"):
                with self.subTest(text=text, mode=mode):
                    (self.root / ".github/cachew-checkout.json").write_text(text, errors="surrogateescape")
                    code, out = self.run_main(mode)
                    self.assertEqual(code, 2, out)
                    self.assertIn("cachew-checkout.json", out)

    def test_action_sha_with_write_repins_and_saves_the_config(self):
        self.run_main("--write")
        new = "b" * 40
        self.assertEqual(self.run_main("--write", "--action-sha", new)[0], 0)
        self.assertIn(f"cachew-checkout@{new}", self.read(".github/workflows/ci.yml"))
        self.assertEqual(json.loads(self.read(".github/cachew-checkout.json"))["action_sha"], new)
        self.assertEqual(self.run_main("--verify-pins")[0], 0)

    def test_verify_pins_reports_a_pin_other_than_the_config(self):
        self.run_main("--write")
        code, out = self.run_main("--verify-pins", "--action-sha", "c" * 40)
        self.assertEqual(code, 1)
        self.assertIn("expected action_sha", out)
        # Without --write, --action-sha does not touch the config.
        self.assertEqual(json.loads(self.read(".github/cachew-checkout.json"))["action_sha"], SHA)

    def test_verify_pins_compares_the_whole_ref(self):
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        good = workflow.read_text()
        for ref in ("main", SHA[:7], SHA + "0"):
            workflow.write_text(good.replace(f"@{SHA}", f"@{ref}"))
            code, out = self.run_main("--verify-pins")
            self.assertEqual(code, 1, ref)
            self.assertIn(ref, out)

    def test_verify_pins_reads_the_whole_uses_value(self):
        # Git allows #, ' and " in branch names, so a SHA followed by any of them
        # is a mutable ref, and the parts of a value YAML joins must be read whole.
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        good = workflow.read_text()
        action = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout"
        line = f"- uses: {PINNED}"
        for bad in (
            f"- uses: {action}@{SHA}#x",
            f"- uses: \"{action}@{SHA}#x\"",
            f"- uses: \"{action}@{SHA}'x\"",
            f"- uses: '{action}@{SHA}\"x'",
            f"- {{uses: {action}@main, with: {{a: b}}}}",
            f"- uses: \"tenstorrent/tt-github-actions/.github/actions/cachew-checkou\\x74@main\"",
            f"- uses: >-\n          {action}@main",
            # Split, so that only the alias leads to it.
            "- name: &pin >-\n          tenstorrent/tt-github-actions/.github/actions/\n          cachew-checkout@main\n      - uses: *pin",
            f"- uses: \"{action}@\\\n          main\"",
            # A plain scalar goes on over more deeply indented lines, blank lines
            # between them included; YAML joins them with a space.
            f"- uses: {action}@{SHA}\n          x",
            f"- uses: {action}@{SHA}\n\n          x",
            f"- uses: {action}@{SHA}\n  \t\n          \tx",
            f"- {{uses: {action}@{SHA}\n          x}}",
            # YAML ends a plain scalar's line at spaces and tabs only; these are
            # part of the ref, and Git allows them in branch names.
            *(f"- uses: {action}@{SHA}{space}" for space in ("\u00a0", "\u3000", "\u2028", "\x85")),
        ):
            with self.subTest(bad=bad):
                workflow.write_text(good.replace(line, bad))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 1, out)
        for fine in (
            f"- uses: '{action}@{SHA}'",
            f'- uses: "{action}@{SHA}"',
            f"- {{uses: {action}@{SHA}}}",
            # A comment ends a plain scalar, so lines after it do not continue it.
            f"- uses: {action}@{SHA} # pinned\n          # x",
            f"- uses: {action}@{SHA}\n          # x",
        ):
            with self.subTest(fine=fine):
                workflow.write_text(good.replace(line, fine))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 0, out)

    def test_unreadable_uses_values_fail_only_when_they_could_name_the_action(self):
        # A uses: value the reader cannot take exactly is a problem only when it
        # could spell this action: an unrelated folded step, data under a uses:
        # key and script text are left alone, in every mode.
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        good = workflow.read_text()
        line = f"- uses: {PINNED}"
        action = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout"
        for unrelated in (
            f"{line}\n      - uses: >-\n          actions/setup-python@v5",
            f"{line}\n      - uses: actions/setup-python@v5\n          # pinned\n        env:\n          uses: |\n            a\n            b",
            f"{line}\n      - run: |\n          cat <<EOF\n          uses: {{key: value}}\n          uses:\n          uses: >\n          EOF",
            f"{line}\n      - uses: actions/setup-python@\n          v5",
            f"{line}\n      - name: &py actions/setup-python@v5\n      - uses: *py",
        ):
            with self.subTest(unrelated=unrelated):
                workflow.write_text(good.replace(line, unrelated))
                for mode in ("--check", "--write", "--verify-pins"):
                    code, out = self.run_main(mode)
                    self.assertEqual(code, 0, f"{mode}: {out}")
        # Still caught: the action hidden in a value read over several lines,
        # in a block-valued key, in script text, or behind escapes.
        for hidden in (
            f"{line}\n      - uses: >-\n          tenstorrent/tt-github-actions/.github/actions/\n          cachew-checkout@main",
            f"{line}\n      - name: x\n        env:\n          uses: |\n            {action}@main",
            f"{line}\n      - run: |\n          uses: {{{action}@main}}",
            f"{line}\n      - uses: >-\n          tenstorrent/tt-github-actions/.github/actions/\\x63achew-checkout@main",
            f"{line}\n      - uses: [\n          tenstorrent/tt-github-actions/.github/actions/cach\n          ew-checkout@main]",
        ):
            with self.subTest(hidden=hidden):
                workflow.write_text(good.replace(line, hidden))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 1, out)

    def test_verify_pins_reads_the_action_path_as_the_runner_does(self):
        # The runner splits owner/repo/path on / and \\ and drops empty segments,
        # and . or .. segments resolve on its disk.
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        good = workflow.read_text()
        line = f"- uses: {PINNED}"
        spellings = (
            "tenstorrent/tt-github-actions/.github//actions/cachew-checkout",
            "tenstorrent//tt-github-actions/.github/actions/cachew-checkout",
            "tenstorrent/tt-github-actions/.github/./actions/cachew-checkout",
            "tenstorrent/tt-github-actions/.github/actions/../actions/cachew-checkout",
            "tenstorrent/tt-github-actions/.github\\actions\\cachew-checkout",
            "tenstorrent\\tt-github-actions/.github\\actions\\cachew-checkout",
            "tenstorrent/tt-metal//.github/actions/cachew-checkout",
        )
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                workflow.write_text(good.replace(line, f"- uses: {spelling}@main"))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 1, out)
        # The same action at the pin passes, and so does another action of the repository.
        for fine in (f"tenstorrent/tt-github-actions/.github//actions/cachew-checkout@{SHA}", "tenstorrent/tt-github-actions/.github/actions/other@main"):
            with self.subTest(fine=fine):
                workflow.write_text(good.replace(line, f"- uses: {fine}"))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 0, out)

    def test_verify_pins_sees_the_action_spelled_with_escapes_under_any_key(self):
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        good = workflow.read_text()
        line = f"- uses: {PINNED}"
        value = '"tenstorrent/tt-github-actions/.github/actions/cachew-ch\\x65ckout@main"'
        for bad in (
            f'- "us\\x65s": {value}',
            f"- ? uses\n        : {value}",
            f"- !!str uses: {value}",
            f"- &k uses: {value}",
            f'- "us\\x65s": "tenstorrent/tt-github-actions/.github/actions/cachew-checkout\\u0040main"',
            f'- "us\\x65s": "tenstorrent/tt-github-actions/.github/actions/cachew-\\\n          checkout@main"',
        ):
            with self.subTest(bad=bad):
                workflow.write_text(good.replace(line, bad))
                code, out = self.run_main("--verify-pins")
                self.assertEqual(code, 1, out)
        # Escapes that spell nothing of the action are no concern of the gate.
        workflow.write_text(good + '      - run: "printf \'a\\x41\\tb\'"\n')
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 0, out)

    def test_verify_pins_reads_composite_actions_anywhere(self):
        self.run_main("--write")
        action = self.root / "tools/ci/action.yml"
        action.parent.mkdir(parents=True)
        action.write_text("runs:\n  using: composite\n  steps:\n    - uses: tenstorrent/tt-github-actions/.github/actions/cachew-checkout@main\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("main", out)

    def test_verify_pins_sees_references_in_any_owner_and_repository_case(self):
        # GitHub resolves owner/repo case-insensitively, so this is the same action on a branch.
        self.run_main("--write")
        workflow = self.root / ".github/workflows/ci.yml"
        workflow.write_text(
            workflow.read_text().replace(
                f"tenstorrent/tt-github-actions/.github/actions/cachew-checkout@{SHA}", "Tenstorrent/tt-github-actions/.GitHub/actions/cachew-checkout@main"
            )
        )
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("main", out)

    def test_a_lock_file_is_left_alone_but_held_to_the_pin(self):
        # GitHub runs gh-aw lock files: --write leaves them to gh-aw, but a pin
        # in one is still checked, since it runs in this repository's jobs.
        lock = self.root / ".github/workflows/agent.lock.yml"
        lock.write_text(lock.read_text().replace(PLAIN, f"tenstorrent/tt-github-actions/.github/actions/cachew-checkout@{'d' * 40}"))
        self.assertEqual(self.run_main("--write", "--action-sha", "b" * 40)[0], 0)
        self.assertIn("@" + "d" * 40, lock.read_text())
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("agent.lock.yml", out)
        self.assertIn("recompile", out)
        lock.write_text(lock.read_text().replace("d" * 40, "b" * 40))
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 0, out)

    def test_an_excluded_prefix_is_not_held_to_the_pin(self):
        vendored = self.root / "vendored/.github/workflows/ci.yml"
        vendored.write_text(vendored.read_text().replace(PLAIN, f"tenstorrent/tt-github-actions/.github/actions/cachew-checkout@{'d' * 40}"))
        self.assertEqual(self.run_main("--write")[0], 0)
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 0, out)

    def test_verify_pins_reads_workflow_suffixes_in_any_case(self):
        upper = self.root / ".github/workflows/upper.YML"
        upper.write_text(f"jobs:\n  a:\n    runs-on: self-hosted\n    steps:\n      - uses: tenstorrent/tt-github-actions/.github/actions/cachew-checkout@main\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        self.run_main("--write")
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("upper.YML", out)

    def test_a_path_argument_is_read_from_the_current_directory(self):
        nested = self.root / "sub"
        nested.mkdir()
        (nested / "wf.yml").write_text(f"{JOB}      - uses: {PLAIN}\n")
        (self.root / "wf.yml").write_text(f"{JOB}      - uses: {PLAIN}\n")
        with contextlib.chdir(nested):
            code, out = self.run_main("--write", "wf.yml")
        self.assertEqual(code, 0, out)
        self.assertIn(PINNED, (nested / "wf.yml").read_text())
        self.assertIn(PLAIN, self.read("wf.yml"))

    def test_a_file_that_cannot_be_read_is_reported_and_the_rest_are_migrated(self):
        workflows = self.root / ".github/workflows"
        (workflows / "aaa-binary.yml").write_bytes(b"\xff\xfe uses\n")
        (workflows / "zzz.yml").write_text(f"{JOB}      - uses: {PLAIN}\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        code, out = self.run_main("--write")
        self.assertEqual(code, 1, out)
        self.assertIn("aaa-binary.yml", out)
        self.assertNotIn("Traceback", out)
        self.assertIn(PINNED, (workflows / "zzz.yml").read_text())
        code, out = self.run_main("--verify-pins")
        self.assertEqual(code, 1, out)
        self.assertIn("aaa-binary.yml", out)

    def test_bad_config_is_reported(self):
        for data, message in (({"action_sha": "main"}, "full 40-character"), ({"action_sha": SHA, "exclude": "x"}, "list of directories or files")):
            self.write_config(data)
            code, out = self.run_main("--check")
            self.assertEqual(code, 2)
            self.assertIn(message, out)
        (self.root / ".github/cachew-checkout.json").unlink()
        code, out = self.run_main("--check")
        self.assertEqual(code, 2)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
