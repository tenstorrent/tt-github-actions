# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Migrate a repository's checkouts to cachew-checkout and verify its pins.

Run from inside the consumer repository (or pass --root). The pin and any
excluded paths come from that repository's .github/cachew-checkout.json:

    {"action_sha": "<full commit of tenstorrent/tt-github-actions>",
     "exclude": ["vendored/tree/"]}

Modes:
  --write        migrate: actions/checkout v7.0.1 steps and pins to any other
                 commit (or to the action's old tenstorrent/.github or tt-metal
                 location) become
                 tenstorrent/tt-github-actions/.github/actions/cachew-checkout@<action_sha>.
                 With --action-sha, the new pin is saved to the config first.
  --check        exit 1 if any file still needs --write, or holds a reference
                 --verify-pins rejects that --write cannot change (a flow-style
                 step, a ref that is not a commit), or a v7.0.1 checkout step it
                 cannot rewrite in place (a tagged, anchored, aliased, escaped
                 or multi-line value, a flow-sequence step). --write reports
                 those too.
  --verify-pins  exit 1 unless every reference pins exactly action_sha.

With no paths, --write and --check inspect every tracked .yml/.yaml under the
repository root's .github/actions or .github/workflows; a nested .github (a
test fixture, a vendored tree) is data GitHub does not run. --verify-pins also reads every
tracked action.yml/action.yaml, wherever a composite action lives, and accepts
only a reference written as a plain or quoted value that starts and ends on
its `uses:` line, since that is the only form whose ref it can read exactly.
A `uses:` value in any other form fails only when its text (or the anchor an
alias names) could spell the action; other actions' steps, data under a
`uses:` key and script text are left alone.

Only checkouts of the exact version the action forwards to (actions/checkout
v7.0.1, by commit or tag) are migrated, so no step changes checkout version. Left
as plain actions/checkout on purpose:
  * `uses:` lines inside a block scalar (a `run: |` script that writes another
    workflow, say): they are text, not steps of this file. A pin to the action
    there is still moved to action_sha, never turned back into actions/checkout;
  * steps with a trailing `# cachew: keep` comment;
  * jobs whose runs-on is a literal GitHub-hosted label (ubuntu-*, windows-*,
    macos-*, in any case) or a list of only such labels, where cachew is a
    no-op; --write turns cachew pins there back into actions/checkout;
  * gh-aw generated lock files (*.lock.yml) and their .md sources, which pin
    their own checkout version and run on GitHub-hosted runners. --verify-pins
    still holds a lock file's pins to this action to action_sha, since GitHub
    runs it: set the pin in the .md source and recompile;
  * paths under a configured "exclude" directory, or that file (e.g. vendored
    trees whose workflows run in another repository). `tools` excludes tools/,
    not tools-extra/.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


ACTION_REPO = "tenstorrent/tt-github-actions/.github/actions/cachew-checkout"
# Where the action lived before it moved to tenstorrent/tt-github-actions.
# --write repins these; --verify-pins rejects them.
LEGACY_REPOS = (
    "tenstorrent/.github/actions/cachew-checkout",
    "tenstorrent/tt-metal/.github/actions/cachew-checkout",
)
# The actions/checkout commit action.yml forwards to (checked by the tests).
FORWARDED_CHECKOUT = "3d3c42e5aac5ba805825da76410c181273ba90b1"
MIGRATABLE_REFS = (FORWARDED_CHECKOUT, "v7.0.1")
CONFIG = Path(".github/cachew-checkout.json")
SHA = re.compile(r"[0-9a-f]{40}")

# A step that runs a copy of the action kept in the consumer repository, where
# it lived before it moved out of tt-metal: never the pinned action.
LOCAL_COPY = re.compile(r"(?i:\./+\.github/+actions/+cachew-checkout/*)")
CHECKOUT = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:-[ \t]+)?(?:[!&][^\s,\[\]{}]*[ \t]+)*(?P<key>['\"]?)uses(?P=key)[ \t]*:[ \t]+)(?P<quote>['\"]?)"
    r"(?P<value>(?i:actions/checkout)@(?:"
    + "|".join(map(re.escape, MIGRATABLE_REFS))
    + r")|(?i:"
    + "|".join(map(re.escape, (ACTION_REPO, *LEGACY_REPOS)))
    + r")@[0-9a-f]{40}|"
    + LOCAL_COPY.pattern
    + r")"
    r"(?P=quote)(?:[ \t]+#(?P<comment>[^\n]*))?[ \t]*$"
)
# The tag comment --write writes after a reference it rewrites, which is
# replaced; the rest of a step's comment is the author's and stays.
# White space before the first tag; separators, white space among them, after
# each, so no space can be read two ways.
TAG_COMMENT = re.compile(r"(?i:[ \t]*(?:(?:v\d+(?:\.\d+)*|cachew checkout)(?![\w.])[ \t;,:-]*)*)")
# The pin itself in a CHECKOUT match: the action, at any location, and its commit.
PIN = re.compile(r"(?i:" + "|".join(map(re.escape, (ACTION_REPO, *LEGACY_REPOS))) + r")@[0-9a-f]{40}")
# A flow mapping's uses: key, matched where flow_mapping says the key starts. A
# quoted key's value may follow its colon at once (`{"uses":"..."}`); after a
# plain key, `uses:x` is one scalar.
FLOW_USES_KEY = re.compile(r"""(?P<q>['"]?)uses(?P=q)[ \t]*:""")
# Any mention of the action with a ref, wherever it appears in a file and however
# its path is spelled: one word that starts at the owner, names the action and
# ends in @<ref>. GitHub resolves owner and repository names in any case, so a
# reference spelled Tenstorrent/.GitHub/... is held to the pin as well.
ANY_PIN = re.compile(r"(?i:tenstorrent[/\\][^\s@'\"#]*cachew-checkout)[^\s@'\"#]*@[^\s'\"#]+")
# The action's directory in each repository it has lived in, in lower case.
ACTION_PATHS = {
    tuple(repo.lower().split("/")[:2]): tuple(repo.lower().split("/")[2:]) for repo in (ACTION_REPO, *LEGACY_REPOS)
}
ACTION_OWNER_REPO = tuple(ACTION_REPO.lower().split("/")[:2])
# Escape sequences of a double-quoted YAML scalar that can spell a character of
# the action's name or its @: \xNN, \uNNNN, \UNNNNNNNN, an escaped line break
# (which joins two lines) and an escaped / or \.
ESCAPE = re.compile(r"\\(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(\r?\n[ \t]*)|([/\\]))")
# A uses: key in block style (`uses:` or `- uses:`) or inside a flow mapping
# (`{uses: ...}`), its key optionally quoted. White space follows the colon,
# except after a quoted key in a flow mapping, where the value may follow at
# once, as in JSON (`{"uses":"..."}`).
USES_KEY = re.compile(
    r"""(?m)(?:^[ \t]*(?:-[ \t]+)?(?:[!&][^\s,\[\]{}]*[ \t]+)*|(?P<flow>[{,])[ \t]*)(?P<q>(?P<quoted>['"])?)uses(?P=q)[ \t]*:"""
    r"""(?(quoted)(?(flow)|(?=[ \t\r\n]|$))|(?=[ \t\r\n]|$))"""
)
PLAIN_CHECKOUT = f"actions/checkout@{FORWARDED_CHECKOUT} # v7.0.1"
# GitHub matches runner labels in any case.
HOSTED_LABEL = re.compile(r"(?i:ubuntu|windows|macos)-[\w.-]+")
TOP_LEVEL = re.compile(r"^(?P<quote>['\"]?)(?P<key>[\w-]+)(?P=quote)[ \t]*:")
# A job's runs-on key, with the value on its line (empty before a block sequence).
RUNS_ON = re.compile(r"(?P<quote>['\"]?)runs-on(?P=quote)[ \t]*:(?:[ \t]+(?P<value>.*))?")
TRAILING_COMMENT = re.compile(r"(?:^|[ \t]+)#.*$")
# Tags and anchors that start a node's text.
LEADING_PROPERTIES = re.compile(r"^(?:[!&][^\s,\[\]{}]*(?:[ \t]+|$))*")


# A line whose value is a block scalar: `key: |`, `- >-`, `key: !!str |2`, with
# an optional comment. Its content is every following line indented more deeply.
BLOCK_SCALAR = re.compile(r"(?:^[ \t]*|[:\-][ \t]+)(?:[!&]\S*[ \t]+)*[|>](?:[+-][1-9]?|[1-9][+-]?)?[ \t]*(?:#.*)?$")
# The start of a line that holds block sequence entries: `- `, `- - `.
ENTRIES = re.compile(r"[ \t]*(?:-[ \t]+)*")
ITEM_SCALAR = re.compile(r"(?:[!&]\S*[ \t]+)*[|>]")
# Where a node starts on a line: after white space and any tags and anchors.
NODE_START = re.compile(r"[ \t]*(?:[!&]\S*[ \t]+)*")
# A plain key and the `: ` after it; the `:` that follows a quoted key.
PLAIN_KEY = re.compile(r"[^\s#'\"{\[|>][^#]*?:(?:[ \t]+|$)")
KEY_END = re.compile(r"[ \t]*:(?:[ \t]+|$)")
# In a flow collection, a quote that starts a node, and a comment.
FLOW_QUOTE = re.compile(r"(?:^|[{\[,:])[ \t]*(?P<quote>['\"])")
COMMENT = re.compile(r"(?:^|[ \t])#")


def lines(text):
    """The lines of a file, each with its line feed. Not str.splitlines, which
    also splits at form feed, U+0085, U+2028 and others that YAML keeps in a line."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


class ConfigError(Exception):
    pass


def load_config(root, path=None):
    """{"action_sha": str, "exclude": [str]} from the consumer's config file."""
    file = Path(path) if path else root / CONFIG
    try:
        data = json.loads(file.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{file} not found; create it with an action_sha (see --help)")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{file}: invalid JSON: {error}")
    except OSError as error:
        raise ConfigError(f"{file}: cannot be read: {error}")
    if not isinstance(data, dict):
        raise ConfigError(f"{file}: must be a JSON object with an action_sha")
    sha = data.get("action_sha", "")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ConfigError(f"{file}: action_sha must be a full 40-character commit SHA, got {sha!r}")
    exclude = data.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(p, str) and p for p in exclude):
        raise ConfigError(f"{file}: exclude must be a list of directories or files")
    return {"action_sha": sha, "exclude": tuple(exclude), "file": file, "data": data}


def save_sha(config, sha):
    config["data"]["action_sha"] = sha
    config["file"].write_text(json.dumps(config["data"], indent=2) + "\n")
    config["action_sha"] = sha


def repository_root(start=None):
    start = start or Path.cwd()
    return Path(subprocess.check_output(["git", "-C", str(start), "rev-parse", "--show-toplevel"], text=True).strip())


def under(relative, prefixes):
    """Whether a repository-relative path is in one of the excluded directories
    (or is one of the excluded files): `tools` excludes tools/, not tools-extra/."""
    return any(relative == prefix.rstrip("/") or relative.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def excluded(path, prefixes=()):
    relative = path.as_posix()
    if under(relative, prefixes) or generated(path):
        return True
    return path.suffix.lower() == ".md"


def generated(path):
    """A gh-aw lock file, which gh-aw writes from its .md source."""
    return path.name.lower().endswith(".lock.yml")


def scalar(text):
    """A label as written: without its comment and its quotes."""
    text = LEADING_PROPERTIES.sub("", TRAILING_COMMENT.sub("", text).strip())
    return text[1:-1] if len(text) > 1 and text[0] in "'\"" and text[-1] == text[0] else text


def hosted_jobs(text):
    """Character ranges of the workflow jobs whose runs-on is one or more literal
    GitHub-hosted labels: a scalar, a flow sequence (`[ubuntu-latest]`) or a block
    sequence of them. Jobs and their keys are found by their indentation, whatever
    its width; any key at the jobs' indentation starts a job, however it is
    written (quoted, anchored), since a job that is not seen would take the next
    job's runs-on."""
    spans, start, labels, in_jobs, offset = [], None, None, False, 0
    job_indent = key_indent = None
    listing = False

    def close(end):
        if start is not None and labels and all(HOSTED_LABEL.fullmatch(label) for label in labels):
            spans.append((start, end))

    for line in lines(text):
        content = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        top = TOP_LEVEL.match(line)
        if top:
            close(offset)
            start, labels, in_jobs, job_indent = None, None, top["key"] == "jobs", None
        elif not in_jobs or not content or content.startswith("#") or not indent:
            pass
        elif job_indent is None or indent == job_indent:
            close(offset)
            start, labels, job_indent, key_indent, listing = offset, None, indent, None, False
        elif start is not None and indent > job_indent:
            if key_indent is None:
                key_indent = indent
            if listing and indent >= key_indent and (content == "-" or content.startswith("- ")):
                labels.append(scalar(content[1:]))
            elif listing and indent > key_indent:
                # The value on the lines after an empty runs-on:, a scalar or a
                # flow sequence; a second line makes it a multi-line scalar.
                if labels == []:
                    labels = runner_labels(LEADING_PROPERTIES.sub("", TRAILING_COMMENT.sub("", content).strip()))
                else:
                    labels, listing = None, False
            elif indent == key_indent:
                listing = False
                runs_on = RUNS_ON.fullmatch(content)
                if runs_on:
                    value = LEADING_PROPERTIES.sub("", TRAILING_COMMENT.sub("", runs_on["value"] or "").strip())
                    if not value:
                        labels, listing = [], True
                    else:
                        labels = runner_labels(value)
        offset += len(line)
    close(offset)
    for start, end, mapping in flow_jobs(text):
        labels = None
        for key, value in flow_entries(text, mapping):
            if re.fullmatch(r"""(['"]?)runs-on\1""", key):
                labels = runner_labels(LEADING_PROPERTIES.sub("", TRAILING_COMMENT.sub("", value).strip()))
        if labels and all(HOSTED_LABEL.fullmatch(label) for label in labels):
            spans.append((start, end))
    return spans


def runner_labels(value):
    """The labels of a runs-on value on one line: a scalar or a flow sequence."""
    if value.startswith("[") and value.endswith("]"):
        return [scalar(item) for item in value[1:-1].split(",")]
    return [scalar(value)]


# What may come between a key's colon and its value: white space, comments,
# tags and anchors, over any number of lines. Each character can be read only
# one way (one white space character at a time, a comment to its line's end, a
# property to its last character), so a near miss cannot backtrack
# exponentially, as `(?:[ \t]+)*` does.
VALUE_GAP = r"(?:[ \t\r\n]|(?<=[ \t\n])#[^\n]*(?![^\n])|[!&][^\s,\[\]{}]*(?![^\s,\[\]{}]))*"
# From a key's colon to the `{` of a flow mapping value.
MAPPING_START = re.compile(VALUE_GAP + r"\{")


def flow_jobs(text):
    """(start, end, mapping) for each workflow job written as a flow mapping,
    `build: {runs-on: ..., steps: [...]}`, in a block or flow `jobs:` mapping:
    start is where its key starts (another job may share its line), end the
    index after its `}`, mapping where its `{` is."""
    found = []
    offset = 0
    for line in lines(text):
        top = TOP_LEVEL.match(line)
        if top and top["key"] == "jobs":
            colon = offset + top.end()
            start = MAPPING_START.match(text, colon)
            if start:
                # jobs: {build: {...}, ...}
                parent = flow_mapping(text, start.end() - 1)
                keys = parent[1] if parent else []
            else:
                # Block jobs: keys at the indentation of the first one.
                keys, indent = [], None
                for row_offset, row, row_indent in content_rows(text[colon:], block_scalars(text[colon:])):
                    if not row_indent:
                        break
                    indent = row_indent if indent is None else indent
                    if row_indent == indent:
                        keys.append(colon + row_offset + row_indent)
            for key in keys:
                name = re.compile(r"""(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[^\s:#,{}\[\]]+)[ \t]*:""").match(text, key)
                value = name and MAPPING_START.match(text, name.end())
                mapping = value and flow_mapping(text, value.end() - 1)
                if mapping:
                    found.append((key, mapping[0], value.end() - 1))
        offset += len(line)
    return found


def flow_entries(text, at):
    """(key, value) texts of the flow mapping whose `{` is text[at], each value
    without the `,` or `}` after it."""
    mapping = flow_mapping(text, at)
    if not mapping:
        return []
    end, keys = mapping
    entries = []
    for number, key in enumerate(keys):
        stop = keys[number + 1] if number + 1 < len(keys) else end
        name = re.compile(r"""(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[^\s:#,{}\[\]]+)""").match(text, key)
        if not name:
            continue
        rest = text[name.end() : stop]
        colon = re.match(r"[ \t]*:", rest)
        value = rest[colon.end() :] if colon else ""
        value = without_comments(value).strip()
        entries.append((name[0], re.sub(r"[,}]$", "", value).strip()))
    return entries


def without_comments(text):
    """Flow YAML text without its comments: a # after white space, outside
    quoted scalars, to the end of its line."""
    out, quote, i = [], None, 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                out.append(text[i : i + 2])
                i += 2
                continue
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def quote_end(text, at, quote):
    """The index after the quote that closes a quoted scalar whose content starts
    at `at`, or -1 if the scalar goes on past this line."""
    while at < len(text):
        if text[at] == "\\" and quote == '"':
            at += 2
        elif text[at] != quote:
            at += 1
        elif quote == "'" and text[at + 1 : at + 2] == "'":
            at += 2
        else:
            return at + 1
    return -1


def open_quote(text, at=0, state=None):
    """(quote, flow) for the quoted scalar still open at the end of a line, or
    None. `state` is that of a scalar already open where the line starts.

    A quote opens a scalar only where a node starts: after the `- ` entries, after
    a key's `: `, and in a flow collection after `{`, `[`, `,` or `:`. Anywhere
    else it is a character of a plain scalar."""
    quote, flow = state or (None, False)
    key_seen = False
    while True:
        if quote:
            at = quote_end(text, at, quote)
            if at < 0:
                return quote, flow
            quote = None
            if not flow:
                # A quoted key (`"run": ...`) is followed by its value.
                after = KEY_END.match(text, at)
                if not after or key_seen:
                    return None
                key_seen, at = True, after.end()
                continue
        if flow:
            # To the next quote that starts a node of the flow collection.
            opener = FLOW_QUOTE.search(text, at)
            if not opener or COMMENT.search(text, at, opener.start()):
                return None
            quote, at = opener["quote"], opener.end()
            continue
        at = NODE_START.match(text, at).end()
        if text[at : at + 1] in ('"', "'"):
            quote, at = text[at], at + 1
        elif text[at : at + 1] in ("{", "["):
            flow = True
        else:
            # A plain key, then its value; a plain value holds no quoted scalar.
            after = PLAIN_KEY.match(text, at)
            if not after or key_seen:
                return None
            key_seen, at = True, after.end()


def block_scalars(text):
    """Character ranges of scalar content that is not on its key's line: block
    scalars and the later lines of quoted scalars. They hold scripts and other
    embedded text, where a `uses:` line is not a step."""
    spans, parent, start, offset, quoted, previous = [], None, None, 0, None, -1
    for line in lines(text):
        content = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        bare = line.rstrip("\r\n")
        if quoted:
            spans.append((offset, offset + len(line)))
            quoted = open_quote(bare, 0, quoted)
            offset += len(line)
            continue
        if parent is not None and content and indent <= parent:
            spans.append((start, offset))
            parent = None
        if parent is None and content and not content.startswith("#"):
            entries = ENTRIES.match(line)[0]
            if BLOCK_SCALAR.search(bare):
                # The scalar's content is indented past the node that holds it: the
                # key after `- ` (so a sibling key of that step ends it), the `-`
                # itself when the entry is the scalar, or the line before when the
                # indicator stands alone on its line.
                item = ITEM_SCALAR.match(line, len(entries))
                if item and "-" not in entries:
                    parent = previous
                else:
                    parent = max(indent, len(entries.rstrip()) - 1 if item else len(entries))
                start = offset + len(line)
            else:
                quoted = open_quote(bare, len(entries))
            previous = len(entries)
        offset += len(line)
    if parent is not None:
        spans.append((start, offset))
    return spans


# A steps: key that opens a block sequence, optionally quoted, anchored or commented.
STEPS_KEY = re.compile(r"""[ \t]*(?:-[ \t]+)?(?P<q>['"]?)steps(?P=q)[ \t]*:[ \t]*(?:[!&]\S*[ \t]*)*(?:#.*)?""")


def step_lines(text, embedded):
    """Offsets of the lines whose block `uses:` key belongs to a step: a key of a
    mapping that is an item of a block sequence under a `steps:` key. A uses:
    key anywhere else (an input, a variable, a matrix entry, a data file) is
    data, not a step. Lines of block and multi-line quoted scalars, blank lines
    and comments are passed over."""
    rows = content_rows(text, embedded)
    steps = set()
    for index, (offset, line, indent) in enumerate(rows):
        entries = ENTRIES.match(line)[0]
        # Tags and anchors may come before the key: `- &s uses: ...`.
        node = NODE_START.match(line, len(entries)).end()
        if re.match(r"""(['"]?)uses\1[ \t]*:(?=[ \t]|$)""", line[node:]) and step_item(rows, index):
            steps.add(offset)
    return steps


# A sequence entry with no node on its line but properties or a comment: its
# mapping's keys start on the next line.
LONE_ENTRY = re.compile(r"[ \t]*-(?:[ \t]+[!&][^\s,\[\]{}]*)*[ \t]*(?:[ \t]#.*)?")


def step_item(rows, index):
    """Whether the key that starts rows[index] (after any `- `) is a key of a
    mapping that is an item of a block sequence under a `steps:` key."""
    line = rows[index][1]
    entries = ENTRIES.match(line)[0]
    if entries.count("-") > 1:
        return False
    key = len(entries)
    at = index
    if "-" not in entries:
        # The item that holds this key: the nearest line indented less, which
        # must be a `- ` entry whose first key is at this key's column, or a
        # `-` with nothing after it but properties or a comment.
        at = next((i for i in range(index - 1, -1, -1) if rows[i][2] < key), None)
        if at is None:
            return False
        item = ENTRIES.match(rows[at][1])[0]
        lone = LONE_ENTRY.fullmatch(rows[at][1])
        if not lone and (item.count("-") != 1 or len(item) != key):
            return False
        if lone and rows[at + 1][2] != key:
            return False
    return steps_item(rows, at)


def content_rows(text, embedded):
    """(offset, line, indent) for each line that holds YAML nodes: not blank, not
    a comment, not inside a block or multi-line quoted scalar."""
    rows, offset = [], 0
    for line in lines(text):
        content = line.strip()
        inside = any(start <= offset < end for start, end in embedded)
        if content and not content.startswith("#") and not inside:
            rows.append((offset, line.rstrip("\r\n"), len(line) - len(line.lstrip(" "))))
        offset += len(line)
    return rows


def steps_key(rows, index):
    """Whether rows[index] is a `steps:` key whose steps GitHub runs: a key of a
    job under the top-level jobs: key, or of a composite action's top-level runs:
    key. A steps: key anywhere else (a matrix entry, an input) is data."""
    line, indent = rows[index][1], rows[index][2]
    if not STEPS_LINE.match(line) or ENTRIES.match(line)[0].count("-"):
        return False

    def owner(at):
        column = rows[at][2]
        return next((i for i in range(at - 1, -1, -1) if rows[i][2] < column), None)

    parent = owner(index)
    if parent is None or ENTRIES.match(rows[parent][1])[0].count("-"):
        return False
    top = TOP_LEVEL.match(rows[parent][1])
    if rows[parent][2] == 0:
        return bool(top) and top["key"] == "runs"
    grandparent = owner(parent)
    top = grandparent is not None and rows[grandparent][2] == 0 and TOP_LEVEL.match(rows[grandparent][1])
    return bool(top) and top["key"] == "jobs"


def steps_item(rows, at):
    """Whether the `- ` entry on rows[at] is an item of a sequence under a
    `steps:` key GitHub runs (steps_key)."""
    dash = rows[at][2]
    # The sequence's key: the nearest earlier line that is not an item of it.
    parent = next(
        (i for i in range(at - 1, -1, -1) if rows[i][2] < dash or (rows[i][2] == dash and not rows[i][1].lstrip().startswith("-"))),
        None,
    )
    return parent is not None and bool(STEPS_KEY.fullmatch(rows[parent][1])) and steps_key(rows, parent)


def flow_mapping(text, at, children=None):
    """(end, keys) for the flow mapping whose `{` is text[at]: the index after
    its `}` and where each of its own keys starts, leaving out the keys of nested
    collections and anything in quoted or plain scalars. None if it does not end.
    Also reads a flow sequence at text[at]; children, when given, collects where
    each collection or alias directly inside it starts."""
    depth, keys, node, key, quoted, i = 0, [], True, False, False, at
    while i < len(text):
        c = text[i]
        if c in "!&" and node:
            # A tag or anchor: the node, a key or quoted scalar, still follows.
            i = re.compile(r"[^\s,\[\]{}]*").match(text, i + 1).end()
            continue
        if c in "'\"" and node:
            if key and depth == 1:
                keys.append(i)
            i = quote_end(text, i + 1, c)
            if i < 0:
                return None
            # After a quoted key, as in JSON, a value may follow its : directly.
            node = key = False
            quoted = True
            continue
        if c == ":" and quoted:
            node, key, quoted = True, False, False
            i += 1
            continue
        if c not in " \t\r\n":
            quoted = False
        if c in "{[":
            depth += 1
            if depth == 2 and children is not None:
                children.append(i)
            node, key = True, c == "{" and depth == 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1, keys
            node = key = False
        elif c == ",":
            node, key = True, depth == 1
        elif c == ":" and text[i + 1 : i + 2] in ("", " ", "\t", "\r", "\n", ",", "}", "]"):
            node, key = True, False
        elif c == "#" and text[i - 1 : i] in (" ", "\t", "\n"):
            i = text.find("\n", i)
            if i < 0:
                return None
        elif c not in " \t\r\n" and node:
            # A plain scalar: its quotes, braces and colons up to an indicator are text.
            if key and depth == 1:
                keys.append(i)
            if c == "*" and depth == 1 and children is not None:
                children.append(i)
            node = key = False
        i += 1
    return None


def rewrite(text, action_sha):
    if not SHA.fullmatch(action_sha):
        raise ValueError(f"action_sha must be a full commit SHA, got {action_sha!r}")
    action = f"{ACTION_REPO}@{action_sha}"
    hosted = hosted_jobs(text)
    embedded = block_scalars(text)
    steps = step_lines(text, embedded)

    def replace(match):
        if "cachew: keep" in match[0]:
            return match[0]
        pinned = not match["value"].lower().startswith("actions/checkout@")
        if match.start() not in steps:
            # Text a script writes or prints, or data that is not a step of this
            # file: a checkout there is not migrated. A pin to the action is still
            # moved to the current commit, as --verify-pins holds every mention to it,
            # and nothing else on the line changes: inside a quoted scalar its
            # quotes and its comment are part of the value.
            return PIN.sub(action, match[0], count=1) if pinned else match[0]
        own = TAG_COMMENT.sub("", match["comment"] or "", count=1).strip()
        own = f"; {own}" if own else ""
        if any(start <= match.start() < end for start, end in hosted):
            return f"{match['prefix']}{PLAIN_CHECKOUT}{own}" if pinned else match[0]
        return f"{match['prefix']}{action} # cachew checkout{own}"

    return CHECKOUT.sub(replace, text)


def line_number(text, at):
    return text.count("\n", 0, at) + 1


def flow_steps(text, hosted=None):
    """Line numbers of checkout steps written as flow mappings, which --write
    leaves alone: a uses: key of a flow mapping that is an item of a steps:
    sequence, in a job that is not GitHub-hosted, without `# cachew: keep` on
    the mapping's last line. Only a value uses_value reads exactly: one it
    cannot read is hidden_checkouts' to report. hosted, when given, replaces
    the hosted jobs' ranges."""
    return [line_number(text, at) for at in flow_step_keys(text, hosted)]


def flow_step_keys(text, hosted=None):
    """Where each uses: key flow_steps reports starts."""
    hosted = hosted_jobs(text) if hosted is None else hosted
    rows = content_rows(text, block_scalars(text))
    found = []
    for index, (offset, line, indent) in enumerate(rows):
        # An item may carry a tag or anchor before its mapping: `- &a {...}`.
        entries = re.match(ENTRIES.pattern + PROPERTIES, line)[0]
        if ENTRIES.match(line)[0].count("-") != 1 or not line[len(entries) :].startswith("{") or not steps_item(rows, index):
            continue
        if any(start <= offset < end for start, end in hosted):
            continue
        mapping = flow_mapping(text, offset + len(entries))
        if not mapping:
            continue
        end, keys = mapping
        line_end = text.find("\n", end)
        if "cachew: keep" in text[end : len(text) if line_end < 0 else line_end]:
            continue
        for key in keys:
            name = FLOW_USES_KEY.match(text, key)
            if not name or not (name["q"] or re.match(r"[ \t\r\n]|$", text[name.end() : name.end() + 1])):
                continue
            if any(not problem and MIGRATABLE.fullmatch(value) for _, _, value, problem in uses_value(text, name, flow=True)):
                found.append(key)
    return found


# A checkout --write migrates, as a whole word of decoded YAML text: not
# actions/checkout@v7.0.10, not myactions/checkout@v7.0.1.
MIGRATABLE = re.compile(
    r"(?<![\w./\\-])(?i:actions/checkout)@(?:" + "|".join(map(re.escape, MIGRATABLE_REFS)) + r""")(?=$|[\s'",}\]])"""
)
# Properties a YAML node may carry before its content: tags and anchors.
PROPERTIES = r"(?:[!&][^\s,\[\]{}]*[ \t\r\n]+)*"


def migratable(text):
    """Whether YAML text could spell a checkout --write migrates, once its
    comments are dropped and its escapes decoded. Line breaks stay white space,
    as YAML folds them: a ref split over two lines is not this one."""
    without_comments = "\n".join(COMMENT.split(line, maxsplit=1)[0] for line in text.split("\n"))
    return bool(MIGRATABLE.search(unescaped(without_comments)))


def open_bracket(text, at):
    """The innermost flow collection still open at text[at], or -1."""
    depth = 0
    for i in range(at - 1, -1, -1):
        depth += {"}": 1, "]": 1, "{": -1, "[": -1}.get(text[i], 0)
        if depth < 0:
            return i
    return -1


def hidden_checkouts(text, hosted=None, aliases=True):
    """Line numbers of checkout steps --write cannot rewrite in place and the
    flow-mapping report (flow_steps) does not name: a step's uses: value that
    is tagged, anchored, an alias, escaped, a block scalar or on a later line,
    any uses: key of a step in a flow sequence (`steps: [{uses: ...}]`), an
    explicit `? uses` key, and a step or steps alias whose anchor --write left
    unchanged (in a hosted job). Not in GitHub-hosted jobs, scripts or data,
    or with `# cachew: keep`. hosted, when given, replaces the hosted jobs'
    ranges; aliases=False leaves aliases out."""
    return sorted({line_number(text, at) for at in hidden_checkout_keys(text, hosted, aliases)})


def hidden_checkout_keys(text, hosted=None, aliases=True):
    """Where each step hidden_checkouts reports starts: its uses: key, `?`, or
    alias."""
    hosted = hosted_jobs(text) if hosted is None else hosted
    embedded = block_scalars(text)
    steps = step_lines(text, embedded)
    rows = content_rows(text, embedded)
    found = []
    for key in USES_KEY.finditer(text):
        at = key.start("q")
        if any(start <= at < end for start, end in embedded + hosted):
            continue
        for start, end, value, problem in uses_value(text, key):
            extent = value_extent(text, key) if problem else end
            fragment = text[start:extent]
            if not migratable(fragment + (anchored(text, fragment) if problem else "")):
                continue
            line_start = text.rfind("\n", 0, at) + 1
            if key["flow"]:
                # Only an unreadable value in a `- {...}` step: flow_steps
                # reports a readable one, and sequence_steps those of `steps: [...]`.
                mapping = key.start("flow") if key["flow"] == "{" else open_bracket(text, key.start("flow"))
                item_start = text.rfind("\n", 0, mapping) + 1
                entries = re.fullmatch(ENTRIES.pattern + PROPERTIES, text[item_start:mapping])
                row = next((i for i, (offset, _, _) in enumerate(rows) if offset == item_start), None)
                item = entries and entries[0].count("-") == 1 and row is not None and steps_item(rows, row)
                if not (item and problem):
                    continue
            elif line_start not in steps or not problem:
                continue
            line_end = text.find("\n", extent)
            if "cachew: keep" in text[line_start : len(text) if line_end < 0 else line_end]:
                continue
            found.append(at)
    checkouts = None
    for index, (offset, line, indent) in enumerate(rows):
        if any(start <= offset < end for start, end in hosted):
            continue
        explicit = EXPLICIT_USES.fullmatch(line)
        alias = ALIAS_ITEM.fullmatch(line) or STEPS_ALIAS.fullmatch(line)
        if explicit and step_item(rows, index):
            # `? uses` and its `: value` on the lines after it, up to the next
            # key of this mapping.
            column = len(ENTRIES.match(line)[0])
            following = next((o for o, l, i in rows[index + 1 :] if i < column or (i == column and not l.lstrip().startswith(":"))), len(text))
            fragment = text[offset + len(line) : following]
            if migratable(fragment + anchored(text, fragment.strip().lstrip(":").strip())) and "cachew: keep" not in fragment:
                found.append(offset + line.index("?"))
        elif aliases and alias and "cachew: keep" not in line and (steps_key(rows, index) or steps_item(rows, index)):
            # A step, or all of a job's steps, another job anchors: an alias
            # --write cannot rewrite here, whose anchor it left (a hosted job's).
            # Reported when the anchor holds a checkout step that would be
            # migrated or reported outside a hosted job.
            if checkouts is None:
                checkouts = step_checkouts(text, steps)
            if any(start <= at < end for start, end in anchor_spans(text, alias["alias"]) for at in checkouts):
                found.append(offset + alias.start("alias"))

    def resolved():
        nonlocal checkouts
        if checkouts is None:
            checkouts = step_checkouts(text, steps)
        return checkouts

    return found + sequence_steps(text, rows, hosted, resolved if aliases else None)


def step_checkouts(text, steps):
    """Where each checkout step --write would migrate or report in any job,
    hosted or not, starts: the steps an alias could run. Offsets, not line
    numbers, so an anchor holds only the steps inside its own node, not one
    beside it on its line."""
    found = [
        match.start("value")
        for match in CHECKOUT.finditer(text)
        if match.start() in steps and match["value"].lower().startswith("actions/checkout@") and "cachew: keep" not in match[0]
    ]
    return set(found + flow_step_keys(text, hosted=[]) + hidden_checkout_keys(text, hosted=[], aliases=False))


# An explicit `? uses` key, its value on a later `: ` line.
EXPLICIT_USES = re.compile(r"""[ \t]*(?:-[ \t]+)?\?[ \t]+(?:[!&][^\s,\[\]{}]*[ \t]+)*(['"]?)uses\1[ \t]*(?:[ \t]#.*)?""")
# A sequence entry that is an alias, and a steps: key whose value is one.
ALIAS_ITEM = re.compile(r"[ \t]*-[ \t]+(?P<alias>(?:![^\s]*[ \t]+)*\*[^\s,\[\]{}]+)[ \t]*(?:[ \t]#.*)?")
STEPS_ALIAS = re.compile(r"""[ \t]*(?:-[ \t]+)?(['"]?)steps\1[ \t]*:[ \t]+(?P<alias>(?:![^\s]*[ \t]+)*\*[^\s,\[\]{}]+)[ \t]*(?:[ \t]#.*)?""")


# A block `steps:` key that starts its line, up to its colon.
STEPS_LINE = re.compile(r"""[ \t]*(?:-[ \t]+)?(['"]?)steps\1[ \t]*:(?=[ \t]|$)""")
# From a key's colon to the `[` of a flow sequence value: white space, comments,
# tags and anchors, over any number of lines.
SEQUENCE_START = re.compile(VALUE_GAP + r"\[")


def sequence_steps(text, rows, hosted, checkouts=None):
    """Where the uses: keys that name a migratable checkout start, in steps
    written as a flow sequence: `steps: [{uses: ...}]`, with or without a tag
    or anchor, the sequence on the key's line or a later one. Read forward from
    real `steps:` keys, quote-aware, so brackets in quoted scalars and script
    text that only looks like a steps sequence are not taken for one. With
    checkouts (a function returning step_checkouts' offsets), an alias item is
    reported when its anchor holds one of them, as for a block alias item."""
    found = []
    sequences = set()
    for index, (offset, line, _) in enumerate(rows):
        match = STEPS_LINE.match(line)
        if not match or not steps_key(rows, index) or any(start <= offset < end for start, end in hosted):
            continue
        start = SEQUENCE_START.match(text, offset + match.end())
        if start:
            sequences.add(start.end() - 1)
    for job, _, mapping in flow_jobs(text):
        if any(start <= job < end for start, end in hosted):
            continue
        for key in flow_mapping(text, mapping)[1]:
            name = re.compile(r"""(['"]?)steps\1[ \t]*:""").match(text, key)
            start = name and SEQUENCE_START.match(text, name.end())
            if start:
                sequences.add(start.end() - 1)
    for at in sorted(sequences):
        children = []
        sequence = flow_mapping(text, at, children)
        if not sequence:
            continue
        line_end = text.find("\n", sequence[0])
        if "cachew: keep" in text[sequence[0] : len(text) if line_end < 0 else line_end]:
            continue
        for child in children:
            if text[child] == "*":
                alias = re.compile(r"\*[^\s,\[\]{}]+").match(text, child)
                line_end = text.find("\n", child)
                if checkouts is None or "cachew: keep" in text[child : len(text) if line_end < 0 else line_end]:
                    continue
                steps = checkouts()
                if any(start <= at < end for start, end in anchor_spans(text, alias[0]) for at in steps):
                    found.append(child)
                continue
            mapping = text[child] == "{" and flow_mapping(text, child)
            if not mapping:
                continue
            end, keys = mapping
            # An item's own trailing comment, as for a `- {...}` step.
            line_end = text.find("\n", end)
            if "cachew: keep" in text[end : len(text) if line_end < 0 else line_end]:
                continue
            for number, key in enumerate(keys):
                name = re.match(r"""(['"]?)uses\1[ \t]*:""", text[key:end])
                if not name or (not name[1] and not re.match(r"[ \t\r\n,}]|$", text[key + name.end() : end])):
                    continue
                value_end = keys[number + 1] if number + 1 < len(keys) else end
                fragment = text[key + name.end() : value_end]
                if migratable(fragment + anchored(text, fragment.strip())):
                    found.append(key)
    return found


def migrate_file(path, action_sha, write=False, root=None, exclude=()):
    """(changed, flow, left) for one file: whether --write changes it, the line
    numbers of the flow-style checkout steps it leaves alone, and what
    --verify-pins would still reject once it is written."""
    root = root or repository_root()
    path = Path(path)
    absolute = path if path.is_absolute() else root / path
    relative = absolute.resolve().relative_to(root.resolve())
    if excluded(relative, exclude):
        return False, [], []
    before = absolute.read_text()
    after = rewrite(before, action_sha)
    if write and before != after:
        # Keep a file's CRLF line endings, so the diff is only the reference.
        raw = absolute.read_bytes()
        crlf = raw.count(b"\r\n") == raw.count(b"\n") > 0
        with open(absolute, "w", newline="\r\n" if crlf else None) as stream:
            stream.write(after)
    # What --write leaves for --verify-pins to reject: a flow-style step, a ref
    # that is not a commit, a value it cannot read exactly.
    left = file_problems(
        relative.as_posix(), after, action_sha, f"--write cannot change it; write uses: {ACTION_REPO}@<sha> on a block-style step"
    )
    left += [
        f"{relative.as_posix()}:{line}: a checkout step --write cannot rewrite in place; write its uses: as one plain"
        " value on a block-style step, or add # cachew: keep"
        for line in hidden_checkouts(after)
    ]
    return before != after, flow_steps(before), left


def tracked_consumers(root, exclude=()):
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    for value in paths:
        path = Path(value)
        if not value or under(value, exclude):
            continue
        # Only the root's: GitHub runs no workflow from a nested .github, and one
        # there (a test fixture, a vendored tree) is data to leave unchanged.
        parts = path.parts
        if len(parts) < 3 or parts[0] != ".github" or parts[1] not in ("actions", "workflows"):
            continue
        if path.suffix.lower() in (".yml", ".yaml"):
            yield path


def pinned_files(root, exclude=()):
    """Every file whose uses: lines GitHub runs: the migrated workflow and action
    directories, and composite actions (action.yml/action.yaml) anywhere."""
    paths = set(tracked_consumers(root, exclude))
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    for value in tracked:
        if value and not under(value, exclude) and Path(value).name in ("action.yml", "action.yaml"):
            paths.add(Path(value))
    return sorted(paths)


def uses_values(text):
    """(start, end, value, problem) for each uses: value in YAML text. value is
    decoded when it is a plain, single-quoted, or escape-free double-quoted scalar
    on the key's own line; otherwise problem says why it cannot be read exactly,
    and start:end is all the text the value could take in (see value_extent)."""
    for key in USES_KEY.finditer(text):
        for start, end, value, problem in uses_value(text, key):
            yield (start, value_extent(text, key), value, problem) if problem else (start, end, value, problem)


def value_extent(text, key):
    """The end of the text a uses: value could take in: its own line and every
    following line indented more deeply than the key (in a flow mapping, than
    the line where its flow collection starts), as YAML requires of a block or
    flow scalar's continuation, blank and comment lines included: a comment line
    ends no value, at any indentation. In a flow mapping, the value also ends
    where the mapping's next key starts, or at its end: the keys of a nested
    mapping (`with: {uses: ...}`) are not this value's."""
    limit = len(text)
    if key["flow"]:
        mapping = key.start("flow") if key["flow"] == "{" else open_bracket(text, key.start("flow"))
        read = flow_mapping(text, mapping) if mapping >= 0 else None
        if read:
            later = [at for at in read[1] if at > key.start("q")]
            limit = later[0] if later else read[0] - 1
    holder = key.start("q")
    if key["flow"]:
        # The line where the innermost open flow collection starts.
        depth = 0
        for holder in range(key.start(), -1, -1):
            depth += {"}": 1, "]": 1, "{": -1, "[": -1}.get(text[holder], 0)
            if depth < 0:
                break
    line_start = text.rfind("\n", 0, holder) + 1
    line = text[line_start:holder]
    column = len(line) - len(line.lstrip(" ")) if key["flow"] else holder - line_start
    end = text.find("\n", key.end())
    while end >= 0:
        following = text.find("\n", end + 1)
        line = text[end + 1 : len(text) if following < 0 else following]
        content = line.strip(" \t\r")
        if content and not content.startswith("#") and len(line) - len(line.lstrip(" ")) <= column:
            return min(end, limit)
        end = following
    return limit


def could_name_action(text):
    """Whether YAML text could spell this action, once its escapes are decoded
    and the line breaks and spaces a scalar may fold or join are dropped."""
    return "cachew-checkout" in re.sub(r"\s", "", unescaped(text)).lower()


def anchored(text, value):
    """The text of every anchor an alias value (`*name`, after any tags) could
    take: a flow collection's anchor up to its closing bracket; a scalar's in a
    flow collection up to its , ] or }; any other's from the anchor on, with the
    lines after it indented more deeply than its line."""
    return "".join(text[start:end] for start, end in anchor_spans(text, value))


# From an anchor to the start of the flow collection it names.
FLOW_NODE = re.compile(VALUE_GAP + r"[\[{]")
# From an anchor to the end of the scalar it names in a flow collection.
FLOW_SCALAR = re.compile(r"""[ \t\r\n]*(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[^,\[\]{}]*)""")


def anchor_spans(text, value):
    """(start, end) of each anchor anchored() takes."""
    alias = re.match(r"(?:![^\s]*[ \t]+)*\*([^\s,\[\]{}]+)", value)
    if not alias:
        return []
    found = []
    for definition in re.finditer(r"&" + re.escape(alias[1]) + r"(?=[\s,\[\]{}]|$)", text):
        collection = FLOW_NODE.match(text, definition.end())
        read = collection and flow_mapping(text, collection.end() - 1)
        if read:
            found.append((definition.start(), read[0]))
            continue
        if open_bracket(text, definition.start()) >= 0:
            found.append((definition.start(), FLOW_SCALAR.match(text, definition.end()).end()))
            continue
        line_start = text.rfind("\n", 0, definition.start()) + 1
        line = text[line_start : definition.start()]
        column = len(line) - len(line.lstrip(" "))
        end = text.find("\n", definition.end())
        while end >= 0:
            following = text.find("\n", end + 1)
            line = text[end + 1 : len(text) if following < 0 else following]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= column:
                break
            end = following
        found.append((definition.start(), len(text) if end < 0 else end))
    return found


def uses_value(text, key, flow=None):
    """uses_values for one uses: key. flow, when given, says whether the key is
    in a flow mapping; otherwise the key's flow group does."""
    flow = bool(key["flow"]) if flow is None else flow
    start = key.end()
    while start < len(text) and text[start] in " \t":
        start += 1
    line_end = text.find("\n", start)
    line_end = len(text) if line_end < 0 else line_end
    line = text[start:line_end].rstrip("\r")
    if not line or line.startswith("#"):
        yield start, start, None, "its value is not on the uses: line"
    elif line[0] in "|>&*!{[":
        yield start, start, None, "its value is not a plain or quoted scalar"
    elif line[0] == "'":
        match = re.match(r"'((?:[^']|'')*)'", line)
        if not match:
            yield start, start, None, "its quoted value does not end on the uses: line"
        else:
            yield start, start + match.end(), match[1].replace("''", "'"), None
    elif line[0] == '"':
        match = re.match(r'"((?:[^"\\]|\\.)*)"', line)
        if not match:
            yield start, start, None, "its quoted value does not end on the uses: line"
        elif "\\" in match[1]:
            yield start, start, None, "its quoted value contains escape sequences"
        else:
            yield start, start + match.end(), match[1], None
    else:
        # A plain scalar ends at a comment (` #`) and, in a flow mapping, at , or }.
        # Only spaces and tabs around it are not part of it: str.rstrip() would also
        # drop Unicode spaces (U+00A0, U+2028, ...), which Git allows in branch names.
        end = re.search(r"[ \t]#" + (r"|[,}]" if flow else ""), line)
        value = line[: end.start() if end else len(line)].rstrip(" \t")
        if not end and plain_continues(text, line_end, key, flow):
            yield start, start, None, "its plain value goes on past the uses: line"
        else:
            yield start, start + len(value), value, None


def plain_continues(text, line_end, key, flow):
    """Whether a plain scalar that runs to the end of its uses: line goes on over
    the next line that is not blank, as YAML reads it: a more deeply indented line
    in a block mapping, any line in a flow mapping, unless that line is a comment
    or, in a flow mapping, starts with , } or ]. YAML joins the lines with a space,
    so the value GitHub runs is not the one on the uses: line."""
    key_column = key.start("q") - (text.rfind("\n", 0, key.start("q")) + 1)
    for line in text[line_end + 1 :].split("\n"):
        content = line.strip(" \t\r")
        if not content:
            continue
        if content.startswith("#"):
            return False
        if flow:
            return content[0] not in ",}]"
        return len(line) - len(line.lstrip(" ")) > key_column
    return False


def action_reference(value):
    """How GitHub reads a decoded uses: value that names the action: (legacy,
    subdirectory, ref, problem), or None for any other value. The runner splits
    the part before the @ on / and \\ and drops empty segments, so
    owner//repo\\actions/cachew-checkout is the action too. The ref is everything
    after the @, so a branch, short SHA or padded SHA is compared as written, and
    so is a SHA followed by more characters (`<sha>#x`, `<sha>'x`): Git allows
    those in branch names, so they are mutable refs that only look like the pin."""
    path, at, ref = value.partition("@")
    segments = [segment.lower() for segment in re.split(r"[/\\]", path) if segment]
    expected = ACTION_PATHS.get(tuple(segments[:2]))
    if not at or expected is None:
        return None
    rest = tuple(segments[2:])
    if "." in rest or ".." in rest:
        # They resolve on the runner's disk, to the action's directory or any other.
        return False, False, ref, "its path has . or .. segments"
    if rest[: len(expected)] != expected:
        return None
    return tuple(segments[:2]) != ACTION_OWNER_REPO, len(rest) > len(expected), ref, None


def unescaped(text):
    def decode(match):
        code = match[1] or match[2] or match[3]
        if code:
            return chr(int(code, 16)) if int(code, 16) < 0x110000 else match[0]
        return "" if match[4] else match[5]

    return ESCAPE.sub(decode, text)


def file_problems(path, text, action_sha, fix=None):
    """What --verify-pins rejects in one file's text, one message each. fix, when
    given, replaces the advice for a reference to another commit or location."""
    problems = []
    read = []
    for start, end, value, problem in uses_values(text):
        line = text.count("\n", 0, start) + 1
        if problem:
            if not could_name_action(text[start:end] + anchored(text, text[start:end])):
                continue
            problems.append(f"{path}:{line}: a uses: entry cannot be verified: {problem}; write it as one plain line")
            continue
        if LOCAL_COPY.fullmatch(value):
            problems.append(f"{path}:{line} runs {value}, a copy of the action in this repository; {fix or 'run --write'}")
            continue
        reference = action_reference(value)
        if not reference:
            continue
        legacy, subdirectory, ref, problem = reference
        read.append((start, end))
        if problem:
            problems.append(f"{path}:{line} references {value}: {problem}; write {ACTION_REPO}@<sha>")
        elif legacy:
            problems.append(f"{path}:{line} references {value}; the action moved to {ACTION_REPO} ({fix or 'run --write'})")
        elif subdirectory:
            problems.append(f"{path}:{line} references {value}; reference the action itself, not a subdirectory")
        elif ref != action_sha:
            if not fix:
                fix = "set the pin in its .md source and recompile it with gh aw compile" if generated(Path(path)) else "run --write"
            problems.append(f"{path}:{line} pins {ref}, expected action_sha {action_sha}; {fix}")
    # A mention the uses: values above do not account for (a comment, an
    # anchor, a multi-line value) could still be what GitHub runs.
    for match in ANY_PIN.finditer(text):
        if not any(start <= match.start() < end for start, end in read):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{path}:{line} mentions {match[0]} outside a uses: value it can verify")
    # A double-quoted scalar can spell the action with escape sequences
    # (\x65 for e), under a key the reader above does not take for uses:.
    if len(ANY_PIN.findall(unescaped(text))) != len(ANY_PIN.findall(text)):
        problems.append(f"{path} names the action through escape sequences; write it as one plain line")
    return problems


def verify_pins(root, action_sha, exclude=()):
    """Every reference pins exactly action_sha in tenstorrent/tt-github-actions:
    no other commit, no reference to an old location, no subdirectory.
    Whether action_sha exists there is for the caller's CI to check."""
    problems = []
    # Excluded prefixes are left out; gh-aw lock files are not, since GitHub
    # runs them here although --write leaves them to gh-aw.
    for path in pinned_files(root, exclude):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            problems.append(f"{path}: cannot be read ({error})")
            continue
        problems += file_problems(path.as_posix(), text, action_sha)
    for problem in problems:
        print(problem)
    print("Pins verified." if not problems else f"{len(problems)} pin problem(s).")
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify-pins", action="store_true")
    parser.add_argument("--root", type=Path, help="consumer repository (default: the Git repository containing the current directory)")
    parser.add_argument("--config", type=Path, help=f"config file (default: <root>/{CONFIG})")
    parser.add_argument("--action-sha", help="pin to use instead of the config's; saved to the config with --write")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    root = repository_root(args.root)
    try:
        config = load_config(root, args.config)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    if args.action_sha:
        if not SHA.fullmatch(args.action_sha):
            print(f"--action-sha must be a full 40-character commit SHA, got {args.action_sha!r}", file=sys.stderr)
            return 2
        if args.write:
            save_sha(config, args.action_sha)
        else:
            config["action_sha"] = args.action_sha
    sha, exclude = config["action_sha"], config["exclude"]
    if args.verify_pins:
        return verify_pins(root, sha, exclude)
    # A path argument is relative to the current directory, as in any command.
    candidates = [Path.cwd() / path for path in args.paths] or tracked_consumers(root, exclude)
    changed, flow, left, failed = [], [], [], False
    for path in candidates:
        try:
            migrated, lines, problems = migrate_file(path, sha, args.write, root, exclude)
            if migrated:
                changed.append(path)
            flow += [f"{path}:{line}" for line in lines]
            left += problems
        except (OSError, ValueError) as error:
            # One unreadable file (not UTF-8, deleted, outside the repository)
            # must not stop the run midway with the others unvisited.
            failed = True
            print(f"Cannot migrate {path}: {error}", file=sys.stderr)
    for path in changed:
        print(f"{'Migrated' if args.write else 'Needs cachew migration:'} {path}")
    print(f"{len(changed)} file(s) {'updated' if args.write else 'need migration'}.")
    for place in flow:
        print(f"{place}: a checkout step written as a flow mapping is not migrated; write it as a block mapping, or add # cachew: keep")
    for problem in left:
        print(problem)
    return 1 if failed or flow or left or (changed and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
