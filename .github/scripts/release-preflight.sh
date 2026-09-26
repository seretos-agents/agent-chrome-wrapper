#!/usr/bin/env bash
# release-preflight.sh <plugin> <version>
#
# Fail-fast pre-flight for release.yml's `stamp` job: checks every
# precondition needed to generate release notes from `main` history via the
# `src/<TAG>` marker scheme (see AGENTS.md and .adev/44-1/plan.md), before
# any side effect (build, push, tag, release, dispatch). Pure `git` -- never
# writes to `origin`.
#
# On success, echoes (and, if $GITHUB_OUTPUT is set, appends):
#   prev_tag=<the resolved predecessor tag, or empty for a first release>
#   main_sha=<the current HEAD sha -- where assemble will place src/<TAG>>
#
# Exit codes: 0 success, 1 a precondition failed, 2 <version> is not valid
# SemVer (propagated from prev-release-tag.sh).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: release-preflight.sh <plugin> <version>" >&2
  exit 2
fi

PLUGIN="$1"
VERSION="$2"
TAG="${PLUGIN}--v${VERSION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Returns 0 iff a line "<sha>\trefs/tags/<want>" (no "^{}" dereference
# suffix) is present in $REMOTE. Exact literal match via `case`, never a
# regex, so version strings containing "." never behave like wildcards.
tag_exists_in_remote() {
  local want="$1" line
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in
      *'^{}') continue ;;
    esac
    case "$line" in
      *$'\t'"refs/tags/${want}") return 0 ;;
    esac
  done <<<"$REMOTE"
  return 1
}

# --- Step 1: resolve the predecessor tag. Also validates VERSION's SemVer
# grammar -- an invalid version exits 2 here, before any other check runs.
set +e
PREV_TAG="$("$BASH" "$SCRIPT_DIR/prev-release-tag.sh" "$PLUGIN" "$VERSION")"
prev_rc=$?
set -e
if [ "$prev_rc" -ne 0 ]; then
  exit "$prev_rc"
fi

# --- Step 2: the "GITHUB_TOKEN can push src/<TAG> at the dispatched commit"
# premise holds only when the dispatched commit is main's own tip.
if [ -n "${GITHUB_REF:-}" ] && [ "$GITHUB_REF" != "refs/heads/main" ]; then
  echo "::error::release-preflight.sh must run against refs/heads/main, got '${GITHUB_REF}'." >&2
  exit 1
fi

# --- Step 3: query origin's tags. A failure here (unreachable remote) is
# fatal -- never silently treated as "no tags exist yet, this is a first
# release".
set +e
REMOTE="$(git ls-remote --tags origin 2>&1)"
remote_rc=$?
set -e
if [ "$remote_rc" -ne 0 ]; then
  echo "::error::git ls-remote --tags origin failed:" >&2
  echo "$REMOTE" >&2
  exit 1
fi

if tag_exists_in_remote "$TAG"; then
  echo "::error::Tag ${TAG} already exists. Delete it first or pick a new version." >&2
  exit 1
fi

if tag_exists_in_remote "src/${TAG}"; then
  echo "::error::Tag src/${TAG} already exists. Use a new version." >&2
  exit 1
fi

# --- Step 4: a checkout that fetched history but not every `<plugin>--v*`
# tag `origin` has (e.g. missing `fetch-depth: 0`) must never be mistaken for
# "first release" just because prev-release-tag.sh saw no local tags.
missing_locally=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  case "$line" in
    *'^{}') continue ;;
  esac
  ref="${line#*$'\t'}"
  case "$ref" in
    "refs/tags/${PLUGIN}--v"*) ;;
    *) continue ;;
  esac
  remote_tag="${ref#refs/tags/}"
  if ! git rev-parse -q --verify "refs/tags/${remote_tag}" >/dev/null 2>&1; then
    missing_locally+=("$remote_tag")
  fi
done <<<"$REMOTE"

if [ "${#missing_locally[@]}" -gt 0 ]; then
  echo "::error::the checkout is missing tag(s) that exist on origin: ${missing_locally[*]}" >&2
  echo "::error::checkout lacks tags -- needs \`fetch-depth: 0\`" >&2
  exit 1
fi

# --- Step 5: the resolved predecessor's src/<PREV_TAG> marker must already
# exist -- bootstrapped once, by a human with their own credentials, outside
# Actions (GITHUB_TOKEN must never create a ref at a historical commit).
if [ -n "$PREV_TAG" ]; then
  if ! tag_exists_in_remote "src/${PREV_TAG}"; then
    echo "::error::missing predecessor marker src/${PREV_TAG}. Bootstrap it once:" >&2
    echo "git tag src/${PREV_TAG} <head_sha of ${PREV_TAG}'s release.yml run>" >&2
    echo "git push origin src/${PREV_TAG}" >&2
    exit 1
  fi
fi

# --- Step 6: outputs.
MAIN_SHA="$(git rev-parse HEAD)"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "prev_tag=${PREV_TAG}"
    echo "main_sha=${MAIN_SHA}"
  } >>"$GITHUB_OUTPUT"
fi
echo "prev_tag=${PREV_TAG}"
echo "main_sha=${MAIN_SHA}"
