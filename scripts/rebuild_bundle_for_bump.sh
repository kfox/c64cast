#!/usr/bin/env bash
# Rebuild c64cast/web/dist for a Dependabot npm PR that cannot land on its own.
# See web/README.md, "When Dependabot bumps a web dependency".
#
# Usage: scripts/rebuild_bundle_for_bump.sh PR [BRANCH]
#
# Takes the PR's web/package.json + web/package-lock.json onto a branch off its
# base, rebuilds the bundle, and stages both halves. Prints what moved and
# writes no commit: the message carries claims whoever signs it should have
# checked.
#
# Requires gh, npm and node. Exit 3 means the bundle did not move, so the PR
# needs no replacement and can be merged as it is.
set -euo pipefail

cd "$(dirname "$0")/.." > /dev/null

readonly DIST=c64cast/web/dist
readonly LOCK=web/package-lock.json
readonly MANIFEST=web/package.json
readonly ASSETS=("$DIST/index.html" "$DIST/assets/app.css" "$DIST/assets/app.js")

die() { printf '%s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" > /dev/null 2>&1 || die "$2 needs $1, which is not on PATH."
}

# git hash-object rather than sha256sum: that is shasum on macOS and sha256sum
# on Linux, and git is already a hard requirement here.
dist_hashes() { git hash-object "${ASSETS[@]}"; }

[ "$#" -ge 1 ] || die "Usage: $(basename "$0") PR [BRANCH]"

readonly PR=$1
case $PR in
  '' | *[!0-9]*) die "First argument must be a PR number, not '$PR'." ;;
esac

need gh "Reading the PR"
need npm "Building the web console"
need node "Building the web console"

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "Working tree is dirty. This checks files out and stages them; commit or set aside first."
fi

echo "==> Reading PR #$PR"
IFS=$'\t' read -r head_ref head_sha base_ref < <(
  gh pr view "$PR" --json headRefName,headRefOid,baseRefName \
    --jq '[.headRefName, .headRefOid, .baseRefName] | @tsv'
)
[ -n "${head_sha:-}" ] || die "Could not read PR #$PR from gh."
touched=$(gh pr view "$PR" --json files --jq '.files[].path' | sort)

# A bump that also edits sources or config is not this script's shape: the
# rebuild would carry those edits into the replacement branch unremarked.
expected=$(printf '%s\n%s\n' "$LOCK" "$MANIFEST" | sort)
if [ "$touched" != "$expected" ]; then
  die "PR #$PR touches more than the two dependency files, so it is not a plain bump:
$(printf '%s\n' "$touched" | sed 's/^/  /')
Rebuild it by hand."
fi

readonly BRANCH=${2:-build/web-bundle-pr$PR}
if git rev-parse --verify -q "refs/heads/$BRANCH" > /dev/null; then
  die "Branch $BRANCH already exists. Pass another name, or delete it first."
fi

echo "==> Fetching origin/$base_ref and $head_sha"
git fetch -q origin "$base_ref"
git fetch -q origin "$head_ref"

starting_ref=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)
echo "==> Branching $BRANCH off origin/$base_ref"
git checkout -q -b "$BRANCH" "origin/$base_ref"

abandon() {
  git checkout -q --force "$starting_ref"
  git branch -q -D "$BRANCH"
}

# The baseline run is the point of this script. Without it a local toolchain
# difference - a Node whose minifier disagrees with CI's, a stale install -
# lands in the replacement branch attributed to the bump, and the commit then
# claims the bump did something it did not.
echo "==> Baseline: rebuilding at origin/$base_ref, to check this machine reproduces the committed bundle"
make web > /dev/null
if ! git diff --quiet -- "$DIST"; then
  abandon
  die "make web at origin/$base_ref does not reproduce the committed bundle on this machine,
so any diff after the bump would be this machine's rather than the toolchain's.
Compare node --version against .node-version before going further."
fi
echo "    reproduces it byte for byte"

echo "==> Applying the bump from $head_ref"
git checkout "$head_sha" -- "$MANIFEST" "$LOCK"

echo "==> Rebuilding"
make web > /dev/null

if git diff --quiet -- "$DIST"; then
  abandon
  echo
  echo "The bundle does not move, so PR #$PR needs no replacement and can be merged as it is."
  exit 3
fi
first_build=$(dist_hashes)

echo "==> Confirming the build is deterministic"
make web > /dev/null
[ "$first_build" = "$(dist_hashes)" ] ||
  die "Two consecutive builds disagree, so this bundle is not reproducible. Do not commit it."
echo "    two consecutive builds agree"

git add "$MANIFEST" "$LOCK" "$DIST"
moved=$(git diff --cached --name-only -- "$DIST")

echo
echo "Staged on $BRANCH. Measured facts for the commit message:"
echo
echo "  packages that moved:"
git diff --cached -- "$LOCK" |
  sed -n 's#^\([+-]\).*/-/\(.*\)\.tgz".*#    \1 \2#p' | sort -u
echo "  bundle files that moved:"
printf '%s\n' "$moved" | sed 's/^/    /'
echo "  bundle files that held still:"
for asset in "${ASSETS[@]}"; do
  printf '%s\n' "$moved" | grep -qxF "$asset" || echo "    $asset"
done
echo
echo "Both halves are staged together because web/README.md requires the bundle be"
echo "rebuilt in the same commit as its source. Write the message, then: git commit"
