#!/usr/bin/env bash
# Rebase the current branch onto the branch it was cut from, then force-push with lease.
# Base = the open PR's base, else `git config branch.<name>.base`, else dev.
# Only for your own short-lived branches; never run on dev, main, pre-release/* or release/*.
set -euo pipefail

branch=$(git symbolic-ref --short HEAD)
case "$branch" in
  dev | main | pre-release/* | release/*) echo "refusing to rebase shared branch '$branch'" >&2; exit 1 ;;
esac

base=$(gh pr view --json baseRefName -q .baseRefName 2>/dev/null) \
  || base=$(git config "branch.$branch.base") \
  || base=dev

git fetch origin "$base"
git rebase "origin/$base"
git push --force-with-lease origin "$branch"
echo "rebased $branch onto origin/$base"
