# Actions

Reusable composite actions for the Tenstorrent org. Reference them from any
repository as `tenstorrent/tt-github-actions/.github/actions/<name>@<ref>`.

| Action | Use it for |
|---|---|
| [`docker-publish`](docker-publish/action.yml) | Building and publishing one image from a Dockerfile |
| [`docker-bake-publish`](docker-bake-publish/action.yml) | Publishing the targets of a `docker buildx bake` file |

Both put every published image through the same pipeline. Nothing reaches a
registry until the gates have passed, and nothing is reachable by tag until all
of the pipeline has passed.

## What the pipeline does

1. **Build into a local OCI layout** on the runner, with max-mode SLSA
   provenance and a BuildKit SBOM attestation. Nothing is pushed.
2. **Gate every image.** Each platform image is checked for the expected OCI
   labels (a missing one is a warning, not a failure), and Trivy must find
   no secret, misconfiguration or license issue at the configured
   severities. Vulnerabilities at those severities are reported but do not
   fail the gate unless `trivy-fail-on-vulnerabilities` is `"true"`. A
   separate crypto scan inventories certificates and keys into a CBOM and
   fails the gate on any private key not explicitly allowed (see
   [Crypto inventory and private keys](#crypto-inventory-and-private-keys)).
   Trivy reports are uploaded as a workflow artifact, pass or fail.
3. **Push by digest.** Only once every image has passed are the layouts pushed
   to the repository of each image's first tag, with no tag.
4. **Enrich the SBOM.** Each platform is rescanned with syft into an SPDX 2.3
   SBOM. The describing package gets its license, source and supplier from
   the image's OCI labels. Packages get the OS, source-package, supplier and
   layer data Trivy needs to scan the SBOM, and package checksums from the
   archive hashes syft reads but does not write for most ecosystems (see
   [Why not Trivy out of the box](#why-not-trivy-out-of-the-box)). The
   `org.opencontainers.image.*` labels that all platforms share are copied
   onto the index as annotations, since GitHub's package page reads the
   description, source and license only from there.
5. **Sign the SBOM** of each platform with `actions/attest` and push the
   Sigstore bundle as an OCI referrer of that platform image.
6. **Copy, then tag.** Every image is copied by digest to any other
   repositories it is tagged in, together with its signed SBOM bundles. Only
   then are the tags applied.

If Trivy fails or errors, nothing is pushed:
the image never leaves the runner. See
[Failure modes and gotchas](#failure-modes-and-gotchas) for what each failure
leaves behind, and for the one way image content can still leave the runner
before the gate: build cache.

## Failure modes and gotchas

### Build cache is uploaded before the gate runs

> [!WARNING]
> `cache-to` bypasses the gate. BuildKit exports cache **while it builds**,
> before the label check and Trivy run. If `cache-to` points anywhere other
> than the runner (`type=registry`, a bare `ghcr.io/...:cache` reference,
> `type=gha`, `type=s3`, `type=azblob`), the layers of an image that then fails
> the gate are already in that cache, and nothing removes them.

The cache is not a tagged, runnable image, but it holds the same layer blobs,
and anyone who can read the cache location can download them. That includes
whatever Trivy's secret scanner found: a credential baked into a layer reaches
the cache before Trivy flags it.

Both actions print a `::warning::` for every such `cache-to`, including ones
set in a bake file. To keep failing images off the registry entirely:

- Use `cache-from` only (reading cache uploads nothing), `type=inline` (cache
  metadata inside the image, which is pushed only after the gate), or
  `type=local` on a self-hosted runner.
- If you need a shared cache, give it its own private repository, never the
  published one, with read access limited to the build, and expire it.

### What each failure leaves behind

| Fails at | Registry | Tags |
|---|---|---|
| Build (including running out of disk for the OCI layout) | Nothing, apart from build cache (above) | None |
| Gating Trivy findings, or Trivy erroring (including a vulnerability database download that fails from Harbor and upstream) | Nothing, apart from build cache | None |
| Any image in a bake run fails the gate | Nothing for any target, including ones that passed | None |
| Push by digest | Possibly some blobs, or whole images that passed the gate | None |
| SBOM enrichment, signing, copying to other repositories | Images that passed the gate, as untagged digests; signed SBOM bundles if signing ran | None |
| Applying tags | Everything | Possibly some (see below) |

The runner's OCI layouts are deleted when the gate step ends, pass or fail.
Trivy reports are uploaded as an artifact either way.

### Other gotchas

- **The tag phase is not atomic.** Gating, pushing, enrichment, signing and
  copying all finish before the first tag, but tags are separate registry
  calls. A registry failure during tagging can leave some tags applied.
- **Signatures cannot be withdrawn.** An SBOM signed with `actions/attest`
  is recorded in GitHub's attestation store and the public Sigstore
  transparency log (public repositories) even if a later step fails.
- **Vulnerabilities do not block by default.** They are listed in the scan
  log, the step summary, a warning annotation per platform and the uploaded
  report, and the image is published anyway. With
  `trivy-fail-on-vulnerabilities: "true"` the same image can pass today and
  fail tomorrow: Trivy's vulnerability database is not pinned, so a rebuild or
  re-run can fail on a newly published CVE with no code change.
- **Non-pushed bake targets are not gated.** `type=docker` and local exports
  build unchanged; only targets that push to a registry go through the gate.
- **Disk.** Every image to be pushed sits on the runner as an OCI layout until
  the gate passes (see [Requirements](#requirements)).

## Requirements

- A Linux runner (x64 or arm64) with Docker, `jq`, `curl` and `git`.
- A BuildKit builder. Run `docker/setup-buildx-action` first; the classic
  `docker` driver cannot produce attestations.
- Free disk on the runner for the built images: each is exported to
  `$RUNNER_TEMP` as an OCI layout (compressed layers, all platforms) and kept
  until it is pushed. With bake, every pushed target in the run is on disk at
  once. A remote or Kubernetes builder streams the whole image to the runner.
- A registry login (`docker/login-action`) for every registry being pushed to.
- Job permissions:

  ```yaml
  permissions:
    contents: read
    packages: write          # pushing to GHCR
    id-token: write          # signing the SBOM
    attestations: write      # storing the attestation
    artifact-metadata: write # only if attest-storage-record is true (default)
  ```

  Attestation in private repositories needs GitHub Enterprise Cloud. Set
  `attest-sbom: "false"` to skip signing.

## docker-publish

```yaml
- uses: docker/setup-buildx-action@<sha>
- uses: docker/login-action@<sha>
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
- id: meta
  uses: docker/metadata-action@<sha>
  with:
    images: ghcr.io/tenstorrent/my-image
- id: publish
  uses: tenstorrent/tt-github-actions/.github/actions/docker-publish@<ref>
  with:
    tags: ${{ steps.meta.outputs.tags }}
    labels: ${{ steps.meta.outputs.labels }}
    platforms: linux/amd64,linux/arm64
```

`tags` must be fully qualified (`ghcr.io/org/image:tag`). The first tag's
repository is where the image is built and scanned; tags in other repositories
receive an identical copy with the same digest.

`image-output-options` is appended to the action's own local OCI output, so it
is checked before the build: one line, and only `compression`,
`compression-level`, `force-compression`, `oci-mediatypes`, `oci-artifact`,
`rewrite-timestamp` and `annotation*` options. Anything else, such as `type`,
`push`, `name`, `dest` or a second output line, could push before the gate and
fails the step.

Outputs: `digest` (the published index), `image` (`<primary-repo>@<digest>`)
and `build-digest` (the index BuildKit built, before enrichment, never tagged).

### Images without an SBOM

`sbom: "false"` skips the SBOM work but keeps the gate. **It is only for
throwaway images**: short-lived CI images that are deleted after the run and
that nobody pulls outside it, such as tt-metal's prebuilt test images. Any
image published to a registry for distribution (releases, images other teams
or users pull) MUST keep the SBOM.

- BuildKit generates no SBOM attestation, syft is not installed, and the image
  is not rescanned or enriched. `attest-sbom` is ignored, since there is
  nothing to sign.
- The label check and the Trivy scan, private key check included, run as
  usual, and nothing is pushed unless they pass.
- The image is published exactly as built: `digest` equals `build-digest`, and
  the index gets no label annotations. Provenance is still attached.

## docker-bake-publish

```yaml
- uses: tenstorrent/tt-github-actions/.github/actions/docker-bake-publish@<ref>
  env:
    UBUNTU_VERSION: "22.04"
    PYTHON_VERSION: "3.10"
  with:
    bake-file: docker-bake.hcl
    targets: release
    set-lines: |
      *.platform=linux/amd64
```

Targets whose output pushes to a registry are rewritten to export a local OCI
layout (keeping exporter options such as compression) and go through the
pipeline; their `tags` are applied at the end. Targets that do
not push (`type=docker`, local exports, `type=image,push=false`) are built
unchanged and are not gated. `type=registry,push=false` is rejected, because
buildx 0.16 keeps it local and newer buildx pushes it. A `type=docker` target
(load or archive) is built without attestations: buildx rejects them for that
exporter unless the daemon uses the containerd image store.

Output: `images`, a JSON array with one `{name, repo, digest, build_digest,
tags}` entry per published target (`[]` when no target pushes).

### Bake variables

The action has no inputs for bake variables. Bake reads a `variable` block's
value from the environment variable of the same name, and `env:` on the step
that uses the action reaches bake, so any variable your bake file declares can
be set as in the example above. Job-level and workflow-level `env:` work too.

```hcl
variable "UBUNTU_VERSION" { default = "22.04" }
```

To change a single target's attribute rather than a variable, use `set-lines`
(for example `app.args.FLAVOR=debug`). `tags` and `output` of pushed targets
are the exception: the action reads them to decide what to publish and then
replaces them, so set those per target, not with a wildcard.

## Labels

Both actions take a `labels` input: newline-separated `key=value` lines, such
as `docker/metadata-action`'s `labels` output, set on every image. They
override `LABEL` in the Dockerfile and labels in a bake file. docker-bake-publish
turns each line into a `*.labels.<key>=<value>` set line ahead of `set-lines`,
so a set line can still change a label for one target.

Both actions add these labels unless a `labels` line already sets the key:

| Label | Value | When |
|---|---|---|
| `com.tenstorrent.git.ref` | `GITHUB_REF`, the ref that ran the workflow (`refs/heads/main`, `refs/pull/123/merge`, `refs/tags/v1.0`) | Always |
| `com.tenstorrent.git.branch` | The pull request's head branch, or the branch that ran the workflow | Not for tags |
| `org.opencontainers.image.revision` | `GITHUB_SHA` | Builds of `main` (`GITHUB_REF` is `refs/heads/main`) only |

The OCI spec has no key for a branch, so the first two use a Tenstorrent
namespace, as the spec asks for keys it does not define. They describe the
workflow run; a job that checks out another ref with `ref:` can set them
itself.

`revision` is left to the caller off `main`: on `pull_request`,
`GITHUB_SHA` is a temporary merge commit, and a checkout with `ref:` builds a
different commit, so the caller states which commit it built. Off `main`, set
`revision` yourself, or the label check warns and nothing in the image ties it
to a commit. With the default checkout, `github.sha` is the commit checked out:

```yaml
labels: org.opencontainers.image.revision=${{ github.sha }}
```

With `ref:`, write `git rev-parse HEAD` to a step output and use that instead.

`source` and `licenses` can come from `labels`, from `LABEL` in the
Dockerfile, or from the bake file. The OCI image spec defines the standard
`org.opencontainers.image.*` keys as its
[pre-defined annotation keys](https://github.com/opencontainers/image-spec/blob/main/annotations.md#pre-defined-annotation-keys),
and its [image config](https://github.com/opencontainers/image-spec/blob/main/config.md#properties)
says labels follow the same rules.

## Inputs shared by both actions

| Input | Default | Meaning |
|---|---|---|
| `labels` | none | OCI labels for every image, `key=value` per line; adds the git ref, the branch, and `revision` on `main` (see [Labels](#labels)) |
| `required-labels` | `org.opencontainers.image.source`, `.revision`, `.licenses` | Label keys every platform image should carry; a missing one is a warning |
| `trivy-severity` | `CRITICAL,HIGH` | Severities Trivy reports; findings at them fail the gate (vulnerabilities only with the next input) |
| `trivy-fail-on-vulnerabilities` | `false` | Let vulnerabilities fail the gate; otherwise they are only reported |
| `harbor-prefix` | none | Harbor pull-through prefix for Trivy's databases (see [Trivy databases and caching](#trivy-databases-and-caching)) |
| `trivy-scanners` | `vuln,misconfig,secret,crypto` | Trivy scanners to run |
| `trivy-allowed-private-keys` | none | Reviewed private keys the gate accepts, by path glob or `sha256:` fingerprint |
| `trivy-ignore-unfixed` | `false` | Ignore vulnerabilities with no fixed version |
| `trivy-ignorefile` | none | Path to a `.trivyignore` of reviewed exceptions |
| `sbom-supplier` | `Organization: Tenstorrent` | SPDX supplier of the describing package |
| `attest-sbom` | `true` | Sign each platform's SBOM and push it as a referrer |
| `attest-storage-record` | `true` | Also create a GitHub artifact storage record |
| `github-token` | `github.token` | Token used to call the attestations API |

See each `action.yml` for the full list.

## Things to know

- **Finding the signed SBOM.** `trivy image --sbom-sources oci` and
  `gh attestation verify` find it when given a platform digest
  (`repo@sha256:...`). A tag resolves to the index, which carries a referrer
  only for single-platform images. Verify with
  `--predicate-type https://spdx.dev/Document`; see
  [Signing](#signing-actionsattest-not-cosign) for why it is not the
  `.../v2.3` type.
- **The full SBOM is also in the registry.** Each platform's
  attestation-manifest in the published index holds the enriched SBOM as an
  in-toto layer with predicate type `https://spdx.dev/Document`, the same
  place BuildKit puts its own. [Cycode](#continuous-monitoring-with-cycode)
  shows how to read it.
- **Large SBOMs are signed without file entries.** `actions/attest` accepts at
  most 16 MiB. An SBOM over that is signed without its file-level entries, and
  its `comment` names the registry blob (`repo@sha256:...`) holding the in-toto
  statement whose predicate is the complete SBOM. Packages, relationships and
  annotations, which are what Trivy reads, are kept. If the SBOM is still too
  large the job fails before anything is signed or tagged.
- **Build args are public.** They are recorded in the provenance attestation.
  Pass secrets with `secrets` or `secret-files`.
- **Tools are pinned, with no fallback.** syft, oras and trivy each come from
  one release tarball whose sha256 is pinned in `install-tools.sh` and checked
  before it is unpacked. There is no alternative source and no reuse of a
  binary already on the runner. The BuildKit SBOM scanner image is pulled by
  digest from the org mirror (see [Tool images](#tool-images)); a scanner
  given as a tag is rejected. Trivy's vulnerability database is the one
  thing that is not pinned: it has to be current to be useful, and Trivy
  fetches it at scan time (see
  [Trivy databases and caching](#trivy-databases-and-caching)).
- **Tools on `PATH`.** The actions install syft, oras and trivy into
  `$RUNNER_TEMP` and add that directory to `GITHUB_PATH`, so later steps of the
  calling job see them too.
- **Trivy exceptions** belong in a `.trivyignore` in the calling repository,
  passed with `trivy-ignorefile`, so each one is code reviewed. Use one for
  a false positive or an accepted risk. Trivy's
  [`.trivyignore`](https://trivy.dev/docs/latest/configuration/filtering/#trivyignore)
  format lists one finding ID per line (a CVE, a misconfiguration ID such as
  `AVD-DS-0002`, or a secret rule ID such as `aws-access-key-id`), with an
  optional `exp:YYYY-MM-DD` expiry so the exception is reviewed again. The
  [`.trivyignore.yaml`](https://trivy.dev/docs/latest/configuration/filtering/#trivyignoreyaml)
  format (experimental) also limits an exception to file paths or package
  URLs and records a `statement` explaining it. The finding ID is in the scan
  log and in the uploaded report. `.trivyignore` does not apply to the
  crypto scanner; private keys it finds are allowed with
  `trivy-allowed-private-keys` instead (see
  [Crypto inventory and private keys](#crypto-inventory-and-private-keys)).

## Crypto inventory and private keys

When `trivy-scanners` includes `crypto` (the default), each platform image
gets a second Trivy scan with
[`--scanners crypto`](https://trivy.dev/docs/latest/guide/scanner/crypto/).
It lists the certificates, private keys and public keys in the image (files
named `*.pem`, `*.der`, `*.crt`, `*.cer`, `*.key`) with their algorithms and
security levels, as a CycloneDX CBOM.

- The CBOM is `cbom-<platform>.cdx.json` in the uploaded Trivy report
  artifact. The step summary shows the number of certificates and private
  keys per platform.
- **Every private key fails the gate**, whatever `trivy-severity` is, unless
  `trivy-allowed-private-keys` allows it. The error names the file, the
  layer and the key's fingerprint. A failed crypto scan also fails the gate,
  since the check could not run. Trivy marks the scanner experimental.
- **Private keys are looked for in every layer.** The CBOM describes the
  final filesystem, where Trivy has applied later layers' deletions, but a
  key copied in one step and deleted in a later one is still in the published
  layer. Each layer is therefore also scanned on its own. Trivy caches layer
  analysis, so this adds about 0.2 s per layer after the full scan. Remove a
  key in the step that adds it, or exclude it from the build context.
- Key algorithms below 112 bits of classical security (NIST's floor, e.g.
  RSA below 2048) get a warning.
- It runs separately because Trivy only accepts `--scanners crypto` with
  `--format cyclonedx`, not with the JSON report the gate reads.
- Expect the base image's CA bundle in it: plain Alpine lists about 150 CA
  certificates. Certificates and public keys never fail the gate.
- Leave `crypto` out of `trivy-scanners` to skip it, which also turns off
  the private key check below.

### Allowing a private key

Some packages ship private keys as test fixtures. After reviewing one, list
it in `trivy-allowed-private-keys`, one entry per line:

```yaml
trivy-allowed-private-keys: |
  # tornado's test suite key, reviewed 2026-11-12
  usr/lib/python3*/site-packages/tornado/test/test.key
  # one exact key, wherever it is copied
  sha256:875698095ff4af948036be76a064adf369185f0cabd7bd6d9dec54a14db61b7c
```

- A path entry is a glob matched against the file's path in the image, with
  no leading slash. `*` also matches `/`.
- A `sha256:` entry is the key's fingerprint from the error message or the
  CBOM. It allows that key at every path, and no other key.

### How this overlaps with the secret scanner

The secret scanner's `private-key` rule (severity HIGH) also finds private
keys, so most keys are reported twice. Each scanner also finds keys the
other misses. Measured with Trivy 0.76.2:

| Key | Secret scanner | Crypto scanner |
|---|---|---|
| PEM key (PKCS#8, PKCS#1, EC, encrypted) in a `.pem` or `.key` file | Yes | Yes |
| Binary DER key (`.der`) | No | Yes |
| PEM key in a file without a key extension | Yes | No |
| OpenSSH private key | Yes | No |
| Key under `tests/`, `usr/share/doc/`, `node_modules/` or `vendor/` | No (skipped by its built-in allow rules) | Yes |
| Key in a layer, deleted by a later layer | Yes for the forms above | Yes (each layer is scanned on its own) |

The secret scanner gates only at the configured severities, so with
`trivy-severity: CRITICAL` the keys only it finds pass. Exceptions differ
too: a secret scanner finding is ignored with the `private-key` rule ID in
`.trivyignore` (which ignores every such finding unless `.trivyignore.yaml`
limits it to paths), a crypto scanner finding with
`trivy-allowed-private-keys`. A key both find needs both.

### SSH host keys

Installing `openssh-server` generates the host keys
(`/etc/ssh/ssh_host_*_key`) at build time. They are OpenSSH private keys, so
the secret scanner fails the gate on them, and every container from the image
would share them. Do not allow them. Delete them in the same `RUN` that
installs the package, so no layer keeps them:

```dockerfile
# Host keys are generated at container start, not baked into the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_*
```

Then generate them when the container starts, before sshd runs. With an
entrypoint script (tt-metal's `dockerfile/entrypoint.sh`):

```bash
ssh-keygen -A          # creates any missing host keys; a no-op when they exist
service ssh start
```

`ssh-keygen -A` needs root. If something other than the entrypoint starts
sshd (for example a Kubernetes `command`, which replaces `ENTRYPOINT`), wrap
sshd itself so the keys are created on every start, as tt-metal's
`dockerfile/exabox_tools/Dockerfile` does:

```dockerfile
RUN dpkg-divert --local --rename --add /usr/sbin/sshd \
    && printf '%s\n' \
        '#!/bin/sh' \
        '[ "$(id -u)" != 0 ] || ssh-keygen -A >&2' \
        'exec /usr/sbin/sshd.distrib "$@"' \
        > /usr/sbin/sshd \
    && chmod 755 /usr/sbin/sshd
```

## Trivy databases and caching

Trivy keeps two kinds of cached data, and they are cached in different places.

**Databases** (vulnerability DB, Java DB, misconfiguration checks bundle) are
OCI artifacts Trivy downloads into its cache directory on the runner
(`~/.cache/trivy` by default). By default they come from `mirror.gcr.io` and
`ghcr.io`. Set `harbor-prefix` (tt-metal's `check-harbor` output, e.g.
`harbor.ci.tenstorrent.net/`) to pull them through the Harbor pull-through
cache instead:

- Harbor is reachable only from the on-prem runners, not from GitHub-hosted
  ones (`ubuntu-latest`, `ubuntu-slim`). A prefix from a health check that
  ran on another runner can therefore be unusable, so the action first
  requests `https://<harbor-host>/v2/` (10 second limit). If Harbor does not
  answer, the prefix is ignored with a warning and everything comes from
  upstream.
- The vulnerability DB is downloaded once before the scans, from
  `<prefix>ghcr.io/aquasecurity/trivy-db:2`. If that fails, it is downloaded
  from upstream with a warning. Trivy itself moves to its next repository only
  on a 429 or 5xx, not when a host is unreachable, so the action does this
  fallback.
- Once Harbor has served the DB, the Java DB comes from
  `<prefix>ghcr.io/aquasecurity/trivy-java-db:1` and the checks bundle from
  `<prefix>ghcr.io/aquasecurity/trivy-checks:2`. Trivy is downloading the Java
  DB lazily here, and again falls through to its defaults only on a 429 or
  5xx, so a scan that errors with Harbor's Java DB is run once more with
  Trivy's default Java DB repositories, with a warning. If the checks bundle
  cannot be pulled, Trivy uses the checks built into the binary.
- With an empty `harbor-prefix`, everything comes from upstream as before.

**The scan cache** holds per-layer analysis results (package lists and so
on), so an unchanged layer is not analysed again. It is a local BoltDB file by
default. Trivy can keep it in Redis instead, which shares it between runners.
Redis holds only the scan cache, never the databases above. The action does
not configure this. Trivy reads a `TRIVY_*` environment variable for every
flag, and `env:` on the step that uses the action reaches Trivy, so a caller
can opt in:

```yaml
- uses: tenstorrent/tt-github-actions/.github/actions/docker-bake-publish@<ref>
  env:
    TRIVY_CACHE_BACKEND: redis://${{ vars.TRIVY_REDIS_USER }}:${{ secrets.TRIVY_REDIS_PASSWORD }}@${{ vars.TRIVY_REDIS_HOST }}:${{ vars.TRIVY_REDIS_PORT }}
    TRIVY_CACHE_TTL: 168h
```

- The URL is parsed by go-redis, so `redis://user:password@host:port/db` and
  `rediss://` (TLS) work. For client certificates use `TRIVY_REDIS_TLS`,
  `TRIVY_REDIS_CA`, `TRIVY_REDIS_CERT` and `TRIVY_REDIS_KEY`.
- Trivy writes to the cache during every scan, so the user needs write
  access. The read-only ccache user does not fit, and ccache's
  `|read-only=true` suffix is not part of a Redis URL.
- Without `TRIVY_CACHE_TTL`, entries never expire.
- Trivy marks the scan cache backend as experimental.

See Trivy's [cache](https://trivy.dev/docs/latest/guide/configuration/cache/)
and [database](https://trivy.dev/docs/latest/guide/configuration/db/) docs.

## Why not Trivy out of the box

Trivy alone could cover most of this: `trivy image` as the gate, `trivy image
--format spdx-json` for the SBOM, and `cosign attest` to sign it. We use Trivy
for what it does best, the gate, and generate the SBOM with syft instead,
because the SBOM is the provenance and traceability record and Trivy's leaves
out things that record needs. The comparisons below were run with Trivy
0.76.2 and syft 1.57.1 on `python:3.12-slim` (linux/amd64).

### SBOM generator: syft, not Trivy

| | syft + `enrich-sbom.jq` | `trivy image --format spdx-json` |
|---|---|---|
| Software not installed by a package manager | Found by content (the Python interpreter, binaries copied onto `FROM scratch`) | Missing |
| CPEs, for NVD-based scanners | 95 of 95 packages | None |
| Files | Every file, with SHA-1, SHA-256 and the package that owns it | One |
| Image license, source and supplier | From the OCI labels | `NOASSERTION` / `NONE`; labels only as notes |
| `licenseConcluded` | Left to a reviewer, as SPDX intends | Copied from the declared license |
| Supplier | Valid SPDX (`Organization: Name (email)`) | `Name <email>`, not valid SPDX |
| Size | 5.7 MB | 0.2 MB |

Running both and merging was rejected: the two tools name the same package
differently (`bin/registry` vs `github.com/docker/distribution/cmd/registry`,
`stdlib@v1.20.8` vs `stdlib@1.20.8`), so the merge would be lossy and
fragile.

### Making the syft SBOM scan like the image

Trivy's SPDX decoder relies on fields syft does not write. Scanning the raw
syft SBOM with `trivy sbom` found 6 of the 210 vulnerabilities a direct
`trivy image` scan finds. [`enrich-sbom.jq`](docker-publish/scripts/enrich-sbom.jq)
adds them, from data syft already collects:

- an operating-system package that contains every OS package, so Trivy picks
  the right vulnerability database (6 → 31);
- `sourceInfo: built package from: <source> <version>`, because distro
  advisories are keyed by source package (31 → 210, matching the image scan);
- suppliers from package maintainers, and `LayerDiffID` / `LayerDigest`
  annotations naming the layer that introduced each package;
- package checksums (apk, npm/yarn/pnpm/bun/deno, Cargo, Composer, Hex,
  conda) from the archive hashes syft reads but writes to SPDX only for Java,
  Go and opam.

syft also runs with every file hashed (`file.metadata.selection: all`, so
`FROM scratch` images get real digests), with the text of licenses it cannot
map to an SPDX ID embedded for review, and with Go binaries' `(devel)` main
module version guessed from the binary's contents. That guess can be wrong,
and it takes precedence over the commit-based pseudo-version.

Archives nested inside archives (wheels, zips) are not scanned. On the
tt-metal `ubuntu-24.04-ci-build` image, scanning them added 19 entries but
only one new package (the `pip` wheel bundled with `ensurepip`). The rest were
second copies of pip and setuptools, Windows launchers inside their wheels,
and test fixtures.

### Format: SPDX 2.3

- Trivy reads SPDX 2.1 to 2.3 and CycloneDX, but not SPDX 3.0, and
  `actions/attest`'s `sbom-path` input rejects SPDX 3.0.
- SPDX 2.3 is what GitHub's own dependency graph exports.
- CycloneDX was built and compared side by side, then dropped. Trivy got
  identical results from it, but nothing we use needs it, GitHub only stores
  and displays it, and a second, unsigned SBOM next to the signed one leaves
  auditors asking which one counts. If a consumer needs CycloneDX (for
  example Dependency-Track, which reads nothing else), it can be generated
  again for any published image.
- SPDX 2.3 requires a SHA-1 for every file (§8.4), so files carry SHA-1 and
  SHA-256. Verify with SHA-256. Symlinks and other non-regular files have no
  contents to hash; syft gives them an all-zero SHA-1 to satisfy the spec.

### Signing: actions/attest, not cosign

`actions/attest` signs with the workflow's GitHub OIDC identity, so there are
no keys to manage, and the attestations appear on GitHub and verify with
`gh attestation verify`. The SBOM is passed as a custom predicate with type
`https://spdx.dev/Document`, because that exact string is the only SPDX type
Trivy recognizes when it looks for SBOMs in the registry. Passing it as
`sbom-path` would make `actions/attest` set `https://spdx.dev/Document/v2.3`,
which Trivy ignores.

## Continuous monitoring with Cycode

The Trivy gate checks an image once, at publish time. To be alerted about
vulnerabilities disclosed later, import each published image's SBOM into
Cycode, which is meant to re-check stored SBOMs (confirm it does; see the last
point below). The actions do not do this themselves; add a step after the
publish step:

```yaml
- id: publish
  uses: tenstorrent/tt-github-actions/.github/actions/docker-publish@<ref>
  with:
    tags: ${{ steps.meta.outputs.tags }}
    labels: ${{ steps.meta.outputs.labels }}
    platforms: linux/amd64,linux/arm64

- name: Import SBOMs into Cycode
  shell: bash
  env:
    IMAGE: ${{ steps.publish.outputs.image }}
    VERSION: ${{ steps.meta.outputs.version }}
    CYCODE_CLIENT_ID: ${{ secrets.CYCODE_CLIENT_ID }}
    CYCODE_CLIENT_SECRET: ${{ secrets.CYCODE_CLIENT_SECRET }}
  run: |
    set -euo pipefail
    pip install --quiet cycode==3.27.1
    repo="${IMAGE%@*}"
    index="$(oras manifest fetch "$IMAGE")"
    jq -c '.manifests[] | select(.annotations["vnd.docker.reference.type"] == "attestation-manifest")
           | {att: .digest, img: .annotations["vnd.docker.reference.digest"]}' <<< "$index" |
    while read -r m; do
      att="$(jq -r .att <<< "$m")"; img="$(jq -r .img <<< "$m")"
      platform="$(jq -r --arg d "$img" '.manifests[] | select(.digest == $d) | .platform
        | [.os, .architecture, .variant] | map(select(. != null)) | join("/")' <<< "$index")"
      layer="$(oras manifest fetch "${repo}@${att}" | jq -r '
        [.layers[] | select(.annotations["in-toto.io/predicate-type"] == "https://spdx.dev/Document")][0].digest')"
      oras blob fetch --output - "${repo}@${layer}" | jq '.predicate' > sbom.spdx.json
      cycode import sbom \
        --name "${repo} ${VERSION} ${platform}" \
        --vendor Tenstorrent \
        --label "platform:${platform}" --label "digest:${img}" \
        sbom.spdx.json
    done
```

- **Credentials.** Create a Cycode service account and store its client ID
  and secret as the `CYCODE_CLIENT_ID` and `CYCODE_CLIENT_SECRET` secrets.
  For a Cycode tenant outside the US, also set `CYCODE_API_URL` and
  `CYCODE_APP_URL` (for example `https://api.eu.cycode.com` and
  `https://app.eu.cycode.com`).
- **What gets imported.** One SBOM per platform image, the full enriched SPDX
  from the registry. `oras` and `jq` are already on `PATH` after the action
  runs, and the registry login is the one the action used. Each run creates
  new imports, so name them by version, not by branch.
- **Signed SBOMs instead.** To import only an SBOM whose signature checks
  out, replace the `oras blob fetch` line with
  `gh attestation verify "oci://${repo}@${img}" --repo "$GITHUB_REPOSITORY" --predicate-type https://spdx.dev/Document --format json | jq '.[0].verificationResult.statement.predicate' > sbom.spdx.json`
  (with `GH_TOKEN` set). An SBOM over 16 MiB was signed without its file
  entries, which Cycode does not need.
- **docker-bake-publish.** Loop over the `images` output and run the same
  steps for each entry's `repo` and `digest`.
- **The image is already published** when this step runs. A failed import
  fails the job but does not unpublish anything.
- **Check before relying on it.** Cycode's import documentation is behind
  its login. The CLI only uploads the file; Cycode does all parsing on its
  servers. Cimon, Cycode's build agent, uploads SPDX to the same import API
  ([release notes](https://docs.cimon.build/overview/release-notes)), so SPDX
  2.3 is expected to work. Confirm in your tenant that Debian and Alpine
  packages get findings: Trivy finds 210 vulnerabilities in the
  `python:3.12-slim` SBOM. Also confirm that an import picks up a CVE
  disclosed after it was imported, without a re-import.

## Maintaining

- Scripts live in [`docker-publish/scripts`](docker-publish/scripts) and are
  shared by both actions; `bake.sh` is in
  [`docker-bake-publish/scripts`](docker-bake-publish/scripts).
- Composite actions cannot share input definitions, so the inputs above are
  declared in both `action.yml` files. `ruby .github/actions/check-consistency.rb`
  fails if the copies drift, and runs in CI together with shellcheck
  ([`actions-lint.yml`](../workflows/actions-lint.yml)).
- Tool versions and checksums are pinned in
  [`install-tools.sh`](docker-publish/scripts/install-tools.sh). Bump a version
  together with both of its checksums.
- Image digests live only in [`tool-images.json`](tool-images.json); the
  consistency check fails if a script or `action.yml` hardcodes one.
- A step must set in its `env:` every variable its scripts read (the scripts
  they run included), or a caller's job-level `env:` would reach them. The
  consistency check fails on any it does not set.

## Tool images

The BuildKit SBOM scanner runs inside every build, so the org uses one copy of
it that this repository manages. [`tool-images.json`](tool-images.json) pins
the upstream image by digest and names its mirror:

| Image | Upstream | Mirror |
|---|---|---|
| `buildkit-syft-scanner` | `docker.io/docker/buildkit-syft-scanner` | `ghcr.io/tenstorrent/buildkit-syft-scanner` |

Both actions default to `<mirror>@<digest>` from that file.
[`mirror-tool-images.sh`](mirror-tool-images.sh) copies the pinned digest from
upstream to the mirror, and the
[`Mirror tool images`](../workflows/mirror-tool-images.yml) workflow
runs it when the pin file changes. A registry-to-registry copy keeps the
digest, so the mirror is byte-identical to upstream and anyone can check it
against Docker's published image. Upstream is contacted when the pin changes,
not on every build.

To move the org to a new scanner version:

1. Look up the new digest of the upstream tag, for example with
   `docker buildx imagetools inspect docker/buildkit-syft-scanner:stable-1`.
2. Change `digest` in `tool-images.json` in a pull request. The mirror workflow
   runs on the pull request and copies the new digest before anything asks
   for it.
3. Merge. Callers get the new scanner when they move to the new commit of
   this repository.

Things to know:

- The mirror package must be public, because every repository's builds pull
  it. A new package starts private; set its visibility once in the package
  settings. The workflow fails until it is publicly pullable.
- Each mirrored digest also gets a `<tag>-<digest prefix>` tag. Nothing pulls
  by it; it keeps registry cleanup from deleting a digest that is in use.
- To pull the scanner through a cache such as Harbor, pass the same digest
  through it with `syft-scanner-image` or `sbom-generator`. The actions do not
  add a prefix themselves.
