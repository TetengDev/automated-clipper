#!/usr/bin/env bash
# Rebase the current branch onto the branch it was cut from, then force-push with lease.
# Base = the branch's open PR base, else `git config branch.<name>.base`, else dev.
# Only for your own short-lived branches; never run on dev, main, pre-release/* or release/*.
set -euo pipefail

branch=$(git symbolic-ref --short HEAD)
case "$branch" in
  dev | main | pre-release/* | release/*) echo "refusing to rebase shared branch '$branch'" >&2; exit 1 ;;
esac

base=$(gh pr list --head "$branch" --state open --json baseRefName -q '.[0].baseRefName // empty' 2>/dev/null || true)
[ -n "$base" ] || base=$(git config "branch.$branch.base") || base=dev
echo "rebasing $branch onto origin/$base"

git fetch origin "$base"
git fetch origin "$branch" 2>/dev/null || true   # not pushed yet is fine
# Explicit lease: an IDE background fetch cannot silently widen it. Empty = must not exist.
lease=$(git rev-parse -q --verify "origin/$branch" || true)
git rebase "origin/$base"
git push --force-with-lease="$branch:$lease" origin "$branch"
