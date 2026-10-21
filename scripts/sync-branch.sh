#!/usr/bin/env bash
# Rebase the current branch onto the branch it was cut from, then force-push with lease.
# Base = the branch's open same-repo PR base, else `git config branch.<name>.base`, else dev.
# Only for your own short-lived branches; never run on dev, main, pre-release/* or release/*.
set -euo pipefail

branch=$(git symbolic-ref --short HEAD)
case "$branch" in
  dev | main | pre-release/* | release/*) echo "refusing to rebase shared branch '$branch'" >&2; exit 1 ;;
esac

base=$(gh pr list --head "$branch" --state open --json baseRefName,isCrossRepository \
  -q '[.[] | select(.isCrossRepository | not)][0].baseRefName // empty' 2>/dev/null || true)
[ -n "$base" ] || base=$(git config "branch.$branch.base") || base=dev
case "$base" in -*) echo "bad base '$base'" >&2; exit 1 ;; esac
echo "rebasing $branch onto origin/$base"

git fetch origin -- "$base"
git fetch origin -- "$branch" 2>/dev/null || true   # not pushed yet is fine
lease=$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)
# Never drop commits someone else pushed: the remote tip must already be in our branch.
if [ -n "$lease" ] && ! git merge-base --is-ancestor "$lease" HEAD; then
  echo "origin/$branch has commits not in your $branch; pull them first" >&2
  exit 1
fi
git rebase "origin/$base"
git push --force-with-lease="$branch:$lease" origin "$branch"   # empty lease = must not exist yet
