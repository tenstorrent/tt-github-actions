# Cachew checkout

**Parity target:** [`actions/checkout` v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)
(commit `3d3c42e5aac5ba805825da76410c181273ba90b1`). The action accepts that
version's inputs, produces its outputs and repository layout, and forwards to
exactly that commit whenever cachew does not serve the checkout. Wherever this
document says the pinned, standard or official checkout, it means that version.

Use this action in place of `actions/checkout` v7.0.1, retaining existing checkout inputs:

```yaml
- name: Checkout
  id: checkout
  uses: tenstorrent/tt-github-actions/.github/actions/cachew-checkout@<full-commit-sha>
  with:
    ref: ${{ inputs.ref || github.sha }}
    path: docker-job
    submodules: recursive
    fetch-depth: 500
    fetch-tags: true
```

The remote reference works before the workspace exists. A local reference
(`./actions/cachew-checkout`, only in this repository) requires a bootstrap
checkout. Every input and the `ref` and `commit` outputs of the pinned
`actions/checkout` v7.0.1 are forwarded, and the cachew path reads its inputs
the same way. Existing checkout conditions, IDs, paths, and timeouts still apply.

Like `actions/checkout` v7, a `pull_request_target` or `workflow_run` workflow
must set `allow-unsafe-pr-checkout: true` to check out fork pull request code.
Without it, cachew does not check that code out and the standard checkout
refuses it.

| Additional input | Default | Meaning |
| --- | --- | --- |
| `cachew` | `auto` | `auto`: self-hosted Linux runners only. `false`: never. `true`: any Linux runner |
| `cachew-url` | `auto` | `auto` probes the trusted origins; or one trusted HTTPS origin |
| `cachew-health-timeout` | `2` | Seconds for each liveness request, maximum 30 |
| `cachew-fetch-timeout` | `120` | Seconds without transfer progress before a cache fetch is abandoned, maximum 600 |

## Where cachew runs

Cachew is only reachable from runners on the CI VLAN. With the default
`cachew: auto`, the runner skips every cachew step unless
`runner.environment == 'self-hosted'` and `runner.os == 'Linux'`. On GitHub-hosted
runners (`ubuntu-latest`, `ubuntu-slim`, larger runners) the action is exactly the
pinned `actions/checkout` plus, on Linux, one step that reads the `cachew`
setting: no probe, no token, no Git wrapper and no configuration change. A job or
workflow `env: CACHEW_CHECKOUT: 'false'` disables cachew for every checkout in its
scope without editing each step. Both are trimmed and read in any case, as
actions/checkout reads its inputs, so `cachew: "false\n"` opts out too. After an earlier checkout in the
job activated cachew, a `cachew: false` checkout still runs the session's prepare
step, so the official checkout runs upstream with its own throwaway global file;
the job-wide rewrite stays in place for later steps.

The trusted origins are listed in [`trusted_caches.json`](trusted_caches.json),
one per cluster, as deployed by tt-flux `apps/cachew`:
`cachew.ci.tenstorrent.net` (Austin), `cachew.yyz2.ci.tenstorrent.net` (Toronto)
and `cachew.dev.tenstorrent.net` (dev). Restricted DNS on the runners resolves all
three names to the local cluster's Service, and each cluster's certificate only
covers its own name, so `auto` uses the first origin whose unauthenticated
`GET /_liveness` returns 200 over verified TLS. Off the VLAN the names do not
resolve. Only these origins ever receive a token, and only after they answered
the probe. Plain HTTP is never accepted for the cache or upstream, not even to
`127.0.0.1`. Only the test suite enables loopback HTTP fixtures, through
`CACHEW_TEST_ALLOW_LOOPBACK_HTTP=1`, which `action.yml` clears on every cachew
step. A non-HTTPS `cachew-url` (including the in-cluster
`http://cachew.cachew.svc.cluster.local`) leaves the plain checkout: no session,
token, wrapper or configuration change. An untrusted HTTPS one fails the action
(see [Refused inputs](#refused-inputs)).

`cachew-checked-out` reports whether cachew produced the complete checkout, in
which case `actions/checkout` did not run. `cachew-configured` reports whether
global caching was enabled for later Git operations when the action finished.

## Checkout and later dependencies

On an enabled runner the action creates an owned job-global Git configuration
under `RUNNER_TEMP` (selected with `GIT_CONFIG_GLOBAL`, Git 2.32 or newer). It
starts from the caller's global settings and included files, without inherited
cachew rewrites. The original files are never modified. If the session cannot be
created (no Git, Git older than 2.32, unreadable configuration), the action falls
back to the plain checkout and leaves the job environment unchanged.

The cache phase performs the whole checkout and leaves the repository the pinned
`actions/checkout` would have created: the same `origin` remote, remote-tracking
branch, pull or tag ref, checked-out branch or detached `HEAD`, shallow depth,
`gc.auto = 0`, submodule configuration, persisted credentials (a
`RUNNER_TEMP/git-credentials-<uuid>.config` file with host and container
`includeIf` entries, plus `insteadOf` rewrites in submodules), the
`safe.directory` entry and the `ref` and `commit` outputs. Two differences
remain. Checkout clones each submodule's default branch, which adds a local
tracking branch and, when shallow, a single-branch refspec; cachew fetches only
the gitlink commit and keeps the default refspec. And checkout writes the
`safe.directory` entry to a temporary global file it deletes when the step
ends; with cachew, whichever checkout ran, the entry stays in the job's global
file for later steps (unless `set-safe-directory: false`).

A shallow checkout of a commit SHA, including the event's `GITHUB_SHA`, never
contacts upstream. A branch, tag or other ref name, the default branch of another
repository, `fetch-depth: 0` or `fetch-tags: true` costs one upstream `ls-remote`
with the token, so branch and tag names always match GitHub even when the
cache's ref list lags; the objects for those exact SHAs then come from cachew.
The main repository is fetched once, then each requested submodule at its exact
gitlink SHA, including nested submodules. Cachew serves commit SHAs and
`refs/pull/*` from its mirror and forwards anything missing upstream. Worktree
and submodule commands run with `protocol.allow=never`, so any missing object
fails the attempt rather than reaching upstream.

The whole cache phase has a 300-second budget (`CACHEW_CHECKOUT_BUDGET`) on top
of the per-fetch stall timeout, so a slow but moving cache falls back to the
official checkout well within the step's `timeout-minutes`. Retries share this budget.

With `filter`, the main repository needs two cache fetches: the complete tip
commit, then the filtered history at the requested depth. The worktree
therefore needs no lazy fetches. `origin` is recorded as the promisor remote with
the same filter, as standard checkout does, and an `insteadOf` the repository
includes pins it upstream, so objects missing later are fetched lazily from upstream
rather than through the job's cache rewrite. The same applies to sparse
checkouts and to partial clones the Git wrapper makes. Each activation also pins
partial clones already under the workspace that the wrapper never saw: an
earlier step's filtered checkout or clone, or one a self-hosted runner kept from
an earlier job. It reads up to 100,000 directories, without following symbolic
links, and warns when the workspace has more. A partial clone outside the
workspace made before caching was on is not pinned. The pin is the URL Git
would use without cachew when it is written, applying the job's own `insteadOf`
for it at that time. It maps the whole remote URL, so it outranks any rewrite the
job adds later for that remote, such as a mirror or a token in a URL: while
caching is on, a later rewrite does not apply to that repository's lazy fetches.
Those upstream lazy fetches authenticate as they do after a standard checkout:
with the persisted credentials, so with `persist-credentials: false` a private
repository's lazy fetches fail. The cache's token never reaches them.
Once caching is off for the job and cachew's rewrite is gone, cachew removes its
pins and writes no more, even for a command that read the configuration before
caching was turned off, so later rewrites apply as they would without cachew. A pin also ends with
the job (see below), so a later job in a workspace the runner kept is not
affected. A job that rewrites
a filtered checkout's remote should do so before the checkout. Submodules are fetched unfiltered, as standard checkout fetches them.

`sparse-checkout` works like standard checkout: it implies `filter: blob:none`,
applies the patterns (`sparse-checkout set`, or `info/sparse-checkout` without
cone mode), and lets the checkout fetch only the blobs it needs. The main
repository is fetched once, filtered, and only `sparse-checkout set` and
`checkout` may fetch, from the selected cache only (a per-command `insteadOf`
plus the cache transport); `origin` stays upstream in the persisted
configuration. Sparse checkouts with submodules use the standard checkout.

A transient cache failure is retried up to twice before falling back: no answer
or a timeout from the liveness probe, a 5xx, or a connection that breaks
mid-fetch. Each retry starts again from an empty target and probes the cache
again, after about 3 seconds and then about 6 seconds, each lengthened by up to
half at random so that runners failing together do not return together. A name
that does not resolve (every runner off the CI VLAN), a redirect, a 4xx or a
certificate error is not retried, and attempts share the 300-second budget.

The pinned official checkout runs only when cachew did not produce the checkout:
cachew skipped (hosted runner, `cachew: false`, no session), an unsupported mode
(LFS, SSH, sparse with submodules), a non-empty target, or any failure of the
cache or of the local steps. A failed attempt first removes everything it created, so checkout
starts from the same empty directory, with its own throwaway global file.
Afterward the action adds:

```text
url.<cachew>/git/github.com/.insteadOf = https://github.com/
http.<cachew>/git/github.com/....extraHeader = the checkout token (the cache's github.com URLs only)
http.<cachew>/.sslVerify               = true
http.<cachew>/.followRedirects         = false
checkout.workers = 0
checkout.thresholdForParallelism = 50
submodule.fetchJobs = 0
gc.auto = 0
```

Tokens are scoped to cache URLs, and requests that can carry one always use
verified TLS and never follow a redirect: the two `http.<cachew>/` settings
above, with `GIT_SSL_NO_VERIFY` dropped from the cache checkout and from the
wrapper's cache attempt. Two things the job can do would get past those settings,
so each keeps reads off the cache:

- `GIT_SSL_NO_VERIFY` set for the checkout step, which is the case when the job
  sets it. Git lets the variable override every `http.sslVerify`, and Git run
  past the wrapper (by its full path, or for a lazy fetch) would keep it. The
  checkout itself still uses the cache; later steps get no cache rewrite.
- A `http.<url>.sslVerify` that is false, or a `http.<url>.followRedirects` that
  is not false, for the cache's host, in any configuration the command reads
  (files, `GIT_CONFIG_COUNT`, `-c`), and for a clone in what it gives the new
  repository before it fetches: its own `-c`/`--config` options and any file
  they include, the `config` file of its template directory (`--template`,
  `GIT_TEMPLATE_DIR`, `init.templateDir`), and every file a conditional include
  names, in the job's files or the template's, whatever its condition, since
  one keyed on the new repository's directory or remote URL applies only once
  the clone has created it. A relative include path is read where Git will
  resolve it: from the new repository's Git directory (`<destination>/.git`,
  the destination of a bare clone, or `--separate-git-dir`) for the clone's
  own options and for files copied from the template. The wrapper reads up to
  30 such files; a clone
  with more goes upstream. Git applies the most specific `http.<url>.*`
  it finds, so the job's can outrank cachew's. The wrapper sends such a clone,
  fetch or `ls-remote` upstream.

The cache checkout asks Git itself, with `git config --get-urlmatch`, what it
would apply to each URL it is about to fetch from the cache: in the new
repository, whose configuration `git init` copies from `GIT_TEMPLATE_DIR`, and
for a sparse checkout's lazy fetches, which read the job's files. Unless the
answer is a verified certificate and no redirect, that checkout uses the
standard checkout instead.

A step that sets `GIT_SSL_NO_VERIFY` only for itself, after the checkout, keeps
its cache rewrite. Whichever way the wrapper then runs a command, as it is or
upstream, the command may still reach the cache on its own: a lazy fetch,
`git lfs`, a remote whose URL names the cache. Under the variable or under such
a setting, the wrapper clears cachew's token headers for the command, so those
requests carry no token; a private repository's read through the cache then
fails with a 401 and no upstream retry.

The shim on `PATH` starts the wrapper only when it has to: for the network
commands and aliases, for any command when `GIT_SSL_NO_VERIFY`,
`GIT_CONFIG_COUNT` or `GIT_CONFIG_PARAMETERS` is set or a `-c`/`--config-env`
option names an `http.*` setting or an include, and for any other command
(`git checkout`, `git show`, `git lfs`) when the job's files hold a weakening
`http.<url>.*` setting. To find that out, the shim runs one `git config` before
each command it would otherwise pass straight to Git. One case stays
uncovered: Git run by its full path.
The action's own `git config` calls ignore the legacy `GIT_CONFIG` variable,
which names one file for `git config` alone to read and write: the job's
commands keep it.
Settings that choose which certificates to trust or
which proxy to use (`GIT_SSL_CAINFO`, `http.sslCAInfo`, `http.proxy`,
`https_proxy`) are the job's own and stay in effect. The cache checkout's Git reads
none of the job's configuration files, so it is given the job's values of those settings for the
server and for the cache (`http.proxy`, `http.noProxy`, `http.sslCAInfo`,
`http.sslCAPath`, `http.sslCert`, `http.sslKey` and their kin, with a
`http.<url>.` value preferred as Git prefers it). The liveness probe passes the same
proxy and certificates to curl, which reads neither Git's configuration nor
`GIT_SSL_CAINFO`; the proxy URL, which may hold a password, goes to curl on
standard input. The `token` input itself is not in the environment of
any process the action starts. During the cache checkout, Git receives it as an
`Authorization` header in command-scoped configuration (`GIT_CONFIG_VALUE_<n>`),
which every process Git starts inherits: the job's own hooks, filter drivers and
`core.fsmonitor` command. The worktree commands (`checkout`, `submodule
update`, `submodule foreach` shells) run without it, except a sparse checkout's,
which may fetch missing blobs from the cache. The token and the header are masked in the log before any Git process starts. The upstream
origin (`github-server-url`) must be `https://<host>[:port]` with a plain DNS
name or IP literal, and a submodule URL exactly `<origin>/<owner>/<name>`;
anything else uses the standard checkout. Upstream credentials stay exactly where
`actions/checkout` puts them (repository-local), so later steps can use other
credentials for other repositories. With `persist-credentials: false` the token
is used only for the checkout itself. Inputs are read as `actions/checkout`
reads them: a `persist-credentials` that is empty after trimming is false, and an
empty `token` uses the standard checkout, which reports it. A checkout that fails or is refused, in
a step the job lets continue, records no token for the cache either. A checkout
whose repository is gone at a later checkout (a step deleted it, or a checkout
at a parent path cleaned it away) no longer gives the cache its token. Steps may write the global
configuration (`git config --global ...`); cachew only adds and removes its own
keys, so those writes survive cache fallback and later checkouts.

The session steps (`session.py`), the cache checkout and the Git wrapper all
run on Python 3.10 or newer from `PATH`. Without it the action falls back to the
plain checkout without changing the job. Once a session exists, later steps use
the same interpreter (`CACHEW_PYTHON`), which the Git wrapper already depends on.
It is the base interpreter's real file, so removing a virtual environment later
in the job, linked or copied, does not remove it.
A later checkout in the job removes cachew's rewrite and headers from the job's
global file, keeping the job's own settings, and gives the official checkout a
flattened throwaway copy of it. If the file cannot be changed (for example it is
locked), caching is turned off for the rest of the job and the copy still leaves
cachew's keys out.

There is no post step: composite actions cannot have one, and a separate
JavaScript action would need its own pin. The session directory lives in
`RUNNER_TEMP`, which the runner empties when the job completes (also for failed
and cancelled jobs) and again before the next job starts, together with the
checkout credentials file in the same directory. A repository checked out from
cachew keeps its `includeIf` entries; once the file is gone they include nothing. Until then later
steps and other actions' post steps keep a working `GIT_CONFIG_GLOBAL`. If the
runner process itself dies, the files remain until that runner's next job starts.

If a later checkout's session cannot be prepared, the standard checkout runs on
a private copy of the job's Git configuration file, and the step after it hands
the job's own file back to later steps. With submodules, that checkout writes an
include of its credentials file to the global file, which would otherwise apply
to every later step.

The session directory and its files are private to the runner user because the
configuration holds cache-scoped tokens. A step that runs Git as another user
with this environment must set its own `GIT_CONFIG_GLOBAL`, or the job should use
`cachew: false`. The same goes for Docker container actions (`docker://` or
Dockerfile actions): the runner passes `GIT_CONFIG_GLOBAL` into them unchanged,
but the host path it names does not exist inside the container, so Git there
ignores the image's own global configuration and cannot write a global one.

## Refused inputs

Most unsupported inputs fall back to the standard checkout. A few only make
sense as an attack and have no fallback: the action posts an error annotation
titled `cachew-checkout: possible security compromise attempt` and fails before
any checkout runs. They are:

- a `github-server-url` (or `cachew-url`) whose host contains shell or Git
  configuration metacharacters, whatever else the URL holds: the standard
  checkout reads only its origin, so a path, query or userinfo does not keep
  that host out of its shell commands;
- an HTTPS `cachew-url` that is not in `trusted_caches.json`, which would have
  received the checkout token;
- a `path` that leaves the workspace (also through a symlink), or a `repository`,
  submodule name or submodule path with `..`, `.git`, a backslash or control
  characters;
- a `ref` that starts with `-` and would be read as a Git option;
- a submodule URL on the upstream host that carries a query, an encoded segment
  or anything else beyond `<owner>/<name>`;
- a `CACHEW_SESSION_DIR` that points outside the action's own session in
  `RUNNER_TEMP`.

The annotation names the check, never the offending value. These checks run only
where cachew runs (self-hosted Linux, or `cachew: true`) and, for every later
checkout, in a job that already has a cachew session, `cachew: false` included;
elsewhere the action is still exactly `actions/checkout`. After a refusal the cache stays off for
anything that still runs in the job.

## Upstream fallback

For later Git reads, a small `git` wrapper on `PATH` sends `clone`, `fetch` and
`ls-remote` through cachew once when their effective URL is the cache. Python
starts for commands that can reach a remote (`clone`, `fetch`, `ls-remote`,
`push`, `pull`, `submodule`, `remote`) and for Git aliases, which are expanded
first and then handled as the command they run; other commands go straight to
Git unless a setting could weaken the cache's requests (see the shim, above). If
the cache attempt fails, the same operation is retried once upstream. Reads of other
remotes (other hosts, token-in-URL or SSH URLs) run unchanged and never affect
the cache. Only failures of the cache endpoint itself (connection, TLS, 3xx/5xx,
a stall longer than `cachew-fetch-timeout`, or the one-hour hard deadline) remove
the job's cache rewrite; answers about one repository or ref (401/403/404, a
missing ref, `ls-remote --exit-code` finding nothing) keep caching on, and so
does a cache attempt whose Git is killed by a signal (the OOM killer, say),
which is retried upstream like any other failure. A failed
cache clone's partial destination is removed before the upstream retry, and a
fetch's `FETCH_HEAD` is put back as it was, so a `fetch --append` retry does not
record the remotes the attempt already fetched a second time. Git runs a
partial clone's lazy fetches itself, past the wrapper, so whether or not the
command succeeded, each promisor remote the cache rewrite reaches is pinned: the
repository's after a clone, fetch, pull or `remote` command, and its submodules'
after a clone or `submodule` command,
as the checkout's `origin` is. The pin maps the remote's URL to the URL Git would
use without cachew. It is written to a file under `pins/` in the session
directory, readable by the owner only, and the repository's `.git/config` gets an
`include.path` naming it. A credential the job's own `insteadOf` adds to that URL
is not copied into the work tree, and the pin ends when the runner empties
`RUNNER_TEMP`: Git ignores an include whose file is gone, so a later job on a
runner that keeps its workspace (self-hosted) gets no pin outranking its own
mirror or credential rewrites. A partial clone made by a
shell alias is not pinned.

Native recursive clones, submodule and `remote` commands, initialized-submodule
fetches, pushes and pulls use upstream directly. So does a shell alias (`!...`):
Git runs its commands itself, past the wrapper. A push through the wrapper never
goes to cachew, and runs without any of cachew's rewrites, so the job's own
`insteadOf` (a token in a URL) and `pushInsteadOf` (to SSH) apply as they would
without cachew. Past it (a tool that runs Git by its full path, or the submodule
pushes of `git push --recurse-submodules`), a `pushInsteadOf` for the upstream
server, written with the cache rewrite, stops Git applying the rewrite to a
remote's URL when it pushes. That guard also keeps Git from applying the job's
own `insteadOf` to those push URLs, and outranks a `pushInsteadOf` for the same
URLs that the job writes after it. Git does not apply `pushInsteadOf` to an explicit
`remote.<name>.pushurl`, though, and does apply the cache rewrite to it, so such
a push goes to the read-only cache instead of upstream. Run those pushes through `git` on
`PATH`, or give the remote no separate push URL.

Commands sent upstream directly, and every network command once caching is off,
run with a private copy of the job's global file without the cache rewrite. A
`git config --global` write made by their child processes (a `git submodule
foreach` script, a hook) lands in that copy and is lost. Write global settings
from the step itself, or use `--local` inside such commands.

## Tests

```sh
CHECKOUT_ACTION_JS=/absolute/path/to/checkout/dist/index.js \
  CACHEW_TEST_ALLOW_LOOPBACK_HTTP=1 \
  python3 -m unittest discover -s .github/actions/cachew-checkout/tests -v
```

To run the same tests on several processes (4 by default; `-j` sets the number):

```sh
CHECKOUT_ACTION_JS=/absolute/path/to/checkout/dist/index.js \
  CACHEW_TEST_ALLOW_LOOPBACK_HTTP=1 \
  python3 .github/actions/cachew-checkout/tests/run_parallel.py -j 4
```

`CHECKOUT_ACTION_JS` is the `dist/index.js` of the `actions/checkout` commit
this action forwards to; the tests compare cachew's checkouts with it. Tests
that need CMake are skipped without it. The `Test cachew-checkout` workflow
runs the suite on pull requests that touch this directory. The fixtures cannot
reach the production boundary (cachew's certificate, restricted DNS, the real
cache), so before consumers pin a commit, run that workflow by hand on it with
`live-smoke` (and a self-hosted runner label on the CI VLAN). It checks out this
repository through cachew, full and filtered, from the action at that exact
commit, and checks the trusted origin, verified TLS without redirects, no token
in the work trees, an upstream push URL, reads through the wrapper, that a
missing blob of the filtered checkout is fetched lazily from upstream only, with
the persisted credentials, and that an untrusted `cachew-url` is refused.

## Pins and releases

The parity target is fixed in three places, which the tests keep in step: the
`uses:` of the upstream step in `action.yml`, `FORWARDED_CHECKOUT` in
`migrate.py`, and the reference bundle the `Test cachew-checkout` workflow
checks out. Moving to a newer `actions/checkout` means changing all three,
re-checking the cachew path against the new version's inputs, outputs and
refusals (such as `allow-unsafe-pr-checkout`), and updating the version named
at the top of this document.

Everything runs from this action's own directory, so a consumer's pin to this
action is the only pin. Pin a full commit SHA of this repository. Everything
except `README.md` and `tests/` reaches consumers only through their pin: the
action's files run from the pinned commit, and consumers fetch `migrate.py` at
that commit too. To release a change, merge it here, then re-pin consumers to a
commit that contains it.

## Migrating a repository

[`migrate.py`](migrate.py) moves a repository's checkouts to this action and
keeps every reference on one pin. It needs only Python 3 and Git, and works on
the repository it is run in. The pin and any excluded paths live in that
repository's `.github/cachew-checkout.json`:

```json
{
  "action_sha": "<full commit SHA of tenstorrent/tt-github-actions>",
  "exclude": ["vendored/tree/"]
}
```

```sh
python3 migrate.py --write                     # migrate to action_sha
python3 migrate.py --write --action-sha <sha>  # re-pin everything, and save <sha>
python3 migrate.py --check                     # exit 1 if anything needs --write or a hand fix
python3 migrate.py --verify-pins               # exit 1 unless every reference pins action_sha
```

`--check` and `--write` also fail, naming the line, on a reference to this
action that `--write` cannot change but `--verify-pins` would reject: a
flow-style step (`- {uses: ...}`), a branch or tag instead of a commit, or a
`uses:` value it cannot read exactly. They also fail on an `actions/checkout`
v7.0.1 step whose `uses:` value `--write` cannot rewrite in place: tagged
(`!!str`), anchored, an alias, escaped, a block scalar or on a later line, or a
step of a flow sequence (`steps: [{uses: ...}]`). Fix those by hand, or add
`# cachew: keep`.

`--write` replaces the version tag comment after a rewritten reference
(`# v7.0.1`, `# cachew checkout`) and keeps the rest of the step's comment:
`# v7.0.1 needs tags` becomes `# cachew checkout; needs tags`.

`--write` replaces `actions/checkout` v7.0.1 steps (the version this action
forwards to), pins to any other commit of this action, and pins to its old
`tenstorrent/.github/actions/cachew-checkout` and
`tenstorrent/tt-metal/.github/actions/cachew-checkout` locations, and steps that
run a copy kept in the repository (`uses: ./.github/actions/cachew-checkout`),
which every mode reports until they are moved. It leaves
plain `actions/checkout` where cachew can never serve the checkout:

- steps with a trailing `# cachew: keep` comment (a bootstrap checkout, or a
  composite action whose callers run on GitHub-hosted runners or in an
  already populated workspace);
- jobs whose `runs-on` is a literal GitHub-hosted label (`ubuntu-*`,
  `windows-*`, `macos-*`, in any case), or a list of only such labels, where
  cachew is a no-op; cachew pins there are turned back into
  `actions/checkout`. Labels behind expressions or
  reusable-workflow inputs cannot be seen;
- `uses:` lines that are not steps: the content of a block scalar (`run: |`),
  the later lines of a quoted scalar that spans lines, and any `uses:` key that
  is not in an item of a `steps:` sequence (a step input, an environment
  variable, a matrix entry, a data file). A pin to this action there is still
  moved to `action_sha`, since `--verify-pins` holds every mention to it;
- gh-aw generated `*.lock.yml` files and their `.md` sources. GitHub runs a
  lock file, so `--verify-pins` still holds its pins to this action to
  `action_sha`: set the pin in the `.md` source and recompile with
  `gh aw compile`;
- paths under an `exclude` entry, a directory (`tools` excludes `tools/`, not
  `tools-extra/`) or a single file.
- files under a `.github` directory other than the repository root's (a
  test fixture or vendored repository): GitHub does not run them.

A checkout step written as a flow mapping (`- {uses: actions/checkout@...}`)
is not rewritten: `--write` and `--check` name each one and exit 1. Write it
as a block mapping, or add `# cachew: keep`. A pin to this action inside a
script or quoted text moves to `action_sha` with nothing else on its line
changed.

Other `actions/checkout` versions are never touched, so no step changes
checkout version. `--verify-pins` accepts only a plain or quoted `uses:` value
that starts and ends on the key's line; it rejects block scalars, aliases,
escapes and values YAML continues on later lines, since GitHub could read
those as another ref. It rejects such a value only when its text, or the
anchor an alias names, could spell this action, so other actions' steps,
data under a `uses:` key and script text pass. It does not check that the pin exists here;
a consumer's CI can do that by fetching it, as tt-metal's
`test-cachew-checkout` workflow does. Run the copy of `migrate.py` at the
consumer's pin (tt-metal's `.github/scripts/utils/cachew_checkout.py`
downloads and runs it), so the helper always matches the action in use.

The remote `uses:` downloads this repository's archive at the pinned commit
(well under 1 MB) the first time a job uses it, even where cachew is skipped.
From tenstorrent/tt-metal it was a few hundred MB.

This action was moved here from `tenstorrent/.github/actions/cachew-checkout`,
where it had moved from `tenstorrent/tt-metal/.github/actions/cachew-checkout`
(tt-metal `c3fd978`, which includes tt-metal#65453's low-speed fallback retry).
Older pins at either location keep working, since they name commits; run
`migrate.py --write --action-sha <sha>` with a commit of this repository to
move them here.
