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
# Requires gh, make, npm and node. Exit 3 means the bundle did not move, so the
# PR needs no replacement and can be merged as it is.
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
# on Linux, and git is already a hard requirement here. Every file the build
# emitted rather than the three named above, because a bump that makes rollup
# split out a new chunk has to be held to the determinism check too.
dist_hashes() {
  find "$DIST" -type f | sort | while read -r file; do
    printf '%s  %s\n' "$(git hash-object "$file")" "$file"
  done
}

# git diff cannot see a file the build newly emitted, since an untracked path is
# not a diff; status can, and a new chunk moves the bundle as much as an edit to
# an existing one does.
dist_moved() { [ -n "$(git status --porcelain -- "$DIST")" ]; }

[ "$#" -ge 1 ] || die "Usage: $(basename "$0") PR [BRANCH]"

readonly PR=$1
case $PR in
  '' | *[!0-9]*) die "First argument must be a PR number, not '$PR'." ;;
esac

need gh "Reading the PR"
need make "Rebuilding the bundle"
need npm "Building the web console"
need node "Building the web console"

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "Working tree is dirty. This checks files out and stages them; commit or set aside first."
fi

echo "==> Reading PR #$PR"
# Into a variable, then split with `cut`, rather than `read` from a process
# substitution. Two reasons, both of which cost the diagnostic below. `read`
# fails under set -e when the substitution produced nothing, taking the script
# down at that line before it can say what failed; and tab is IFS *whitespace*,
# so `IFS=$'\t' read` collapses an empty @tsv field and shifts every later field
# left - an empty headRefName lands the sha in head_ref and the base ref in
# head_sha, which passes an emptiness check on head_sha alone. `cut -s` keeps
# empty fields empty and prints nothing for a line holding no tab at all.
fields=$(
  gh pr view "$PR" --json headRefName,headRefOid,baseRefName \
    --jq '[.headRefName, .headRefOid, .baseRefName] | @tsv'
) || die "Could not read PR #$PR from gh."
head_ref=$(printf '%s\n' "$fields" | cut -s -f1)
head_sha=$(printf '%s\n' "$fields" | cut -s -f2)
base_ref=$(printf '%s\n' "$fields" | cut -s -f3)
[ -n "$head_ref" ] && [ -n "$head_sha" ] && [ -n "$base_ref" ] ||
  die "Could not read PR #$PR from gh."
touched=$(gh pr view "$PR" --json files --jq '.files[].path' | sort) ||
  die "Could not read PR #$PR's file list from gh."

# A bump that also edits sources or config is not this script's shape: the
# rebuild would carry those edits into the replacement branch unremarked. A bump
# that touches only the lockfile *is* this shape - that is what Dependabot opens
# for a transitive dependency, which is the `esrap` case web/README.md names -
# so the test is "nothing beyond these two", not "exactly these two".
extra=$(printf '%s\n' "$touched" | grep -vxF -e "$LOCK" -e "$MANIFEST") || extra=
if [ -n "$extra" ]; then
  die "PR #$PR touches more than the two dependency files, so it is not a plain bump:
$(printf '%s\n' "$extra" | sed 's/^/  /')
Rebuild it by hand."
fi
if ! printf '%s\n' "$touched" | grep -qxF "$LOCK"; then
  die "PR #$PR does not touch $LOCK, so there is no resolved-version change here to
rebuild for. Rebuild it by hand."
fi

readonly BRANCH=${2:-build/web-bundle-pr$PR}
if git rev-parse --verify -q "refs/heads/$BRANCH" > /dev/null; then
  die "Branch $BRANCH already exists. Pass another name, or delete it first."
fi

echo "==> Fetching origin/$base_ref and $head_sha"
git fetch -q origin "$base_ref"
git fetch -q origin "$head_ref"

# The two files are taken whole from the PR head onto a branch off the *current*
# base, so if either moved on the base since the PR forked, that checkout reverts
# it: a downgrade of an unrelated package, staged as part of this bump. Compared
# at the fork point rather than by ancestry, because a Dependabot branch goes
# stale against unrelated commits constantly and only these two files matter.
fork_point=$(git merge-base "origin/$base_ref" "$head_sha")
if ! git diff --quiet "$fork_point" "origin/$base_ref" -- "$MANIFEST" "$LOCK"; then
  die "$MANIFEST or $LOCK moved on origin/$base_ref since PR #$PR forked, so taking
the PR's copies would revert that. Rebase it first (comment '@dependabot rebase'
on the PR), then rerun."
fi

starting_ref=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)
echo "==> Branching $BRANCH off origin/$base_ref"
git checkout -q -b "$BRANCH" "origin/$base_ref"

abandon() {
  git checkout -q --force "$starting_ref" || return 0
  git branch -q -D "$BRANCH" || return 0
}

# Everything below here can fail - most plausibly `make web`, since the `npm
# test` it ends with is one of the things a bump breaks - and every such failure
# would otherwise strand the checkout on this throwaway branch with a rebuilt
# bundle in it, so the next run refuses twice over: dirty tree, branch exists.
# Unwind by default; `keep_branch` flips once there is a staged tree to hand on.
keep_branch=no
trap '[ "$keep_branch" = yes ] || abandon' EXIT

# The baseline run is the point of this script. Without it a local toolchain
# difference - a Node whose minifier disagrees with CI's, a stale install -
# lands in the replacement branch attributed to the bump, and the commit then
# claims the bump did something it did not.
echo "==> Baseline: rebuilding at origin/$base_ref, to check this machine reproduces the committed bundle"
make web > /dev/null
if dist_moved; then
  die "make web at origin/$base_ref does not reproduce the committed bundle on this machine,
so any diff after the bump would be this machine's rather than the toolchain's.
Compare node --version against .node-version before going further."
fi
echo "    reproduces it byte for byte"

echo "==> Applying the bump from $head_ref"
git checkout "$head_sha" -- "$MANIFEST" "$LOCK"

echo "==> Rebuilding"
make web > /dev/null

if ! dist_moved; then
  echo
  echo "The bundle does not move, so PR #$PR needs no replacement and can be merged as it is."
  exit 3
fi
first_build=$(dist_hashes)

echo "==> Confirming the build is deterministic"
make web > /dev/null
[ "$first_build" = "$(dist_hashes)" ] ||
  die "Two consecutive builds disagree, so this bundle is not reproducible. Nothing was
committed and $BRANCH has been deleted; fix the toolchain before rerunning."
echo "    two consecutive builds agree"

keep_branch=yes
git add "$MANIFEST" "$LOCK" "$DIST"
moved=$(git diff --cached --name-only -- "$DIST")

echo
echo "Staged on $BRANCH. Measured facts for the commit message:"
echo
# A resolved URL is <registry>/<name>/-/<unscoped-name>-<version>.tgz, and the
# name carries its scope one segment further left. Printing the tarball basename
# alone drops that scope and gives two different packages the same label:
# `@tailwindcss/vite` reads as `vite-4.3.3` beside the real `vite` at 8.3.0, and
# `@types/node` as `node-26.6.1`. These lines go into a commit message.
echo "  packages that moved:"
git diff --cached -- "$LOCK" |
  sed -n 's#^\([+-]\).*"resolved": "\([^"]*\)\.tgz".*#\1 \2#p' |
  while read -r sign url; do
    base=${url##*/-/}
    [ "$base" != "$url" ] || continue   # not a <registry>/<name>/-/<file> URL
    path=${url%/-/*}
    unscoped=${path##*/}
    name=$unscoped
    scope=${path%/*}
    scope=${scope##*/}
    case $scope in @*) name="$scope/$unscoped" ;; esac
    printf '    %s %s@%s\n' "$sign" "$name" "${base#"$unscoped"-}"
  done | sort -u
echo "  bundle files that moved:"
printf '%s\n' "$moved" | sed 's/^/    /'
echo "  bundle files that held still:"
for asset in "${ASSETS[@]}"; do
  printf '%s\n' "$moved" | grep -qxF "$asset" || echo "    $asset"
done
echo
echo "Both halves are staged together because web/README.md requires the bundle be"
echo "rebuilt in the same commit as its source. Write the message, then: git commit"
