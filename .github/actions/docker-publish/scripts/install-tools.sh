#!/bin/bash
# Install syft (SBOM generation), oras (OCI blob/manifest plumbing) and trivy
# (vulnerability scanning) from pinned, sha256-verified release tarballs.
#
# Installs into $RUNNER_TEMP rather than /usr/local/bin: self-hosted runners do
# not all grant passwordless sudo. The directory is added to $GITHUB_PATH, which
# puts these binaries on PATH for every later step of the caller's job, not
# just this action's.
#
# Every tool comes from exactly one place, a release tarball whose sha256 is
# pinned below, and is checked before it is unpacked. There is no alternative
# source, no fallback, and no reuse of a binary already on the runner: each
# run installs from the verified tarball again.
#
# Pinned versions/checksums come from each project's *_checksums.txt release
# asset. Bump the version and BOTH checksums together; never float to latest.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SYFT_VERSION="1.57.1"
SYFT_SHA256_AMD64="bf52519eb57f675d47c07d43213cb6dff6449f3d6d1147ff6701ed91f44eef0c"
SYFT_SHA256_ARM64="aa3d3e07344f590bcce8d31043b752daa76c8a56c2aa34ceeee63c6168152582"

ORAS_VERSION="1.3.6"
ORAS_SHA256_AMD64="66fb0ad7206f3f6fa77ed5b552049d364ccf4b50b1684554f731f76302b52ca0"
ORAS_SHA256_ARM64="b84269e78dfc2d5329a4aab373da3200de95e6c138338cf6c66c10c0a3f248c1"

TRIVY_VERSION="0.76.2"
TRIVY_SHA256_AMD64="bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf"
TRIVY_SHA256_ARM64="ab9d51f86e6a5fb936f605faff6b593ac51bc378fff4d3e8b9d58358b9c367c3"

# Only with INSTALL_ATTEST=true. Node checksums are from the release's
# SHASUMS256.txt; actions/attest is pinned by commit (v4.2.2), which git verifies.
NODE_VERSION="24.22.2"
NODE_SHA256_AMD64="361718c444e49af2ca5a110c83ba6a8901143f23ab285ebbef0341f391f482a9"
NODE_SHA256_ARM64="95c264150aceb7f3437a29fd6984924f5f8d1511ea3cbd6c508b83f6c555dae2"
ATTEST_COMMIT="1e69f48acb82d1966a394da916b4c1698aa569d6"

[ "${RUNNER_OS:-$(uname -s)}" = Linux ] \
  || die "docker-publish needs a Linux runner; got ${RUNNER_OS:-$(uname -s)}. The pinned syft/oras/trivy/node downloads are Linux builds."

case "${RUNNER_ARCH:-$(uname -m)}" in
  X64 | x86_64) ARCH=amd64 ;;
  ARM64 | aarch64 | arm64) ARCH=arm64 ;;
  *) die "Unsupported runner architecture: ${RUNNER_ARCH:-$(uname -m)}" ;;
esac

BIN_DIR="${RUNNER_TEMP:-/tmp}/docker-publish-bin"
mkdir -p "$BIN_DIR"

# Usage: install_pinned_binary <url> <expected_sha256> <binary_name>
install_pinned_binary() {
  local url="$1" expected="$2" name="$3" tmp actual
  tmp="$(mktemp -d)"
  curl -sSfL --retry 3 "$url" -o "${tmp}/${name}.tar.gz"
  actual="$(sha256_of "${tmp}/${name}.tar.gz")"
  if [ "$actual" != "$expected" ]; then
    rm -rf "$tmp"
    die "sha256 mismatch for ${name} from ${url} (expected ${expected}, got ${actual})"
  fi
  tar -xzf "${tmp}/${name}.tar.gz" -C "$tmp" "$name"
  install "${tmp}/${name}" "${BIN_DIR}/${name}"
  rm -rf "$tmp"
  log "installed ${name}: $("${BIN_DIR}/${name}" version 2>/dev/null | head -1 || true)"
}

if [ "$ARCH" = amd64 ]; then
  syft_sha="$SYFT_SHA256_AMD64" oras_sha="$ORAS_SHA256_AMD64" trivy_sha="$TRIVY_SHA256_AMD64" trivy_arch="64bit"
else
  syft_sha="$SYFT_SHA256_ARM64" oras_sha="$ORAS_SHA256_ARM64" trivy_sha="$TRIVY_SHA256_ARM64" trivy_arch="ARM64"
fi

# Only the SBOM enrichment uses syft; INSTALL_SYFT=false skips it.
if [ "${INSTALL_SYFT:-true}" = true ]; then
  install_pinned_binary \
    "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_${ARCH}.tar.gz" \
    "$syft_sha" syft
fi
install_pinned_binary \
  "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_${ARCH}.tar.gz" \
  "$oras_sha" oras
install_pinned_binary \
  "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${trivy_arch}.tar.gz" \
  "$trivy_sha" trivy

# GITHUB_PATH applies to every later step of the caller's job, not only this
# action's: callers' own steps after this action also see these binaries.
echo "$BIN_DIR" >> "${GITHUB_PATH:-/dev/null}"

# actions/attest can't be a `uses:` step here: it takes one subject per call and
# the number of platform images is only known after the build. Its bundled
# dist/ is run directly by attest-sbom.sh, once per subject. Node stays off
# PATH so the caller's Node version is untouched.
if [ "${INSTALL_ATTEST:-false}" = true ]; then
  ATTEST_ROOT="${RUNNER_TEMP:-/tmp}/docker-publish-attest"
  node_arch="$([ "$ARCH" = amd64 ] && echo x64 || echo arm64)"
  node_sha="$([ "$ARCH" = amd64 ] && echo "$NODE_SHA256_AMD64" || echo "$NODE_SHA256_ARM64")"
  rm -rf "$ATTEST_ROOT" && mkdir -p "$ATTEST_ROOT/node"
  curl -sSfL --retry 3 "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz" -o "$ATTEST_ROOT/node.tar.gz"
  actual="$(sha256_of "$ATTEST_ROOT/node.tar.gz")"
  [ "$actual" = "$node_sha" ] || die "sha256 mismatch for node ${NODE_VERSION} (expected ${node_sha}, got ${actual})"
  tar -xzf "$ATTEST_ROOT/node.tar.gz" -C "$ATTEST_ROOT/node" --strip-components 1
  rm "$ATTEST_ROOT/node.tar.gz"
  log "installed node: $("$ATTEST_ROOT/node/bin/node" --version)"

  git init -q "$ATTEST_ROOT/attest"
  git -C "$ATTEST_ROOT/attest" fetch -q --depth 1 https://github.com/actions/attest.git "$ATTEST_COMMIT"
  git -C "$ATTEST_ROOT/attest" -c advice.detachedHead=false checkout -q FETCH_HEAD
  [ "$(git -C "$ATTEST_ROOT/attest" rev-parse HEAD)" = "$ATTEST_COMMIT" ] || die "actions/attest checkout is not ${ATTEST_COMMIT}"
  log "installed actions/attest@${ATTEST_COMMIT}"
fi
