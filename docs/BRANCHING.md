# Branching and releases

```
feature/*, fix/*, chore/*  --squash PR-->  dev
dev  --cut-->  pre-release/vX.Y.Z  (+ dependency/vulnerability fixes, by PR)
pre-release/vX.Y.Z  --merge PR-->  release/vX.Y.Z  (cut from main)
release/vX.Y.Z  --merge PR-->  main  ==> tag vX.Y.Z + GitHub release
main  --auto "sync main into dev" PR, merge commit-->  dev
hotfix/vX.Y.Z  (cut from main)  --merge PR-->  main  ==> tag + sync to dev
```

## Branches

| Branch | Purpose | Protection (rulesets) | Merge method |
|---|---|---|---|
| `dev` (default) | Integration branch. Every story PR targets it. | PR required; `test`, `node`, `docker` must pass; no force push; no deletion | squash for features, merge commit for the main sync PR |
| `pre-release/vX.Y.Z` | Release candidate: dev at the cut, plus all pending dependency and vulnerability fixes | PR required; `test`, `node`, `docker`; no force push | squash or merge |
| `release/vX.Y.Z` | Frozen, audited snapshot that goes to production | PR required; `test`, `node`, `docker`, `audit`, `branch-policy`; no force push; no deletion | merge commit |
| `main` | Production history. Only releases and hotfixes. | PR required; `test`, `node`, `docker`, `audit`, `branch-policy`; no force push; no deletion | merge commit |
| `v*` tags | Release tags | no update, no deletion | n/a |

`branch-policy` only lets `release/vX.Y.Z` or `hotfix/vX.Y.Z` into `main`, and only `pre-release/vX.Y.Z` (same version) or `hotfix/vX.Y.Z` into `release/vX.Y.Z`. Fork branches are rejected.

No approvals are required, because a solo maintainer cannot approve their own PR. The gate is the PR, its checks, the review record, and a human pressing merge.

## Vulnerabilities do not block dev

- The `audit` job (`pip-audit` and `npm audit --omit=dev`) runs on every PR. It is a **required** check only on `release/**` and `main`. On a dev PR it can show red, but the PR can still be merged.
- Dependabot opens one bundled version-update PR against `dev` each week (`.github/dependabot.yml`). You can merge it into dev or leave it open.
- At release time, apply all dependency fixes in one go on the pre-release branch:
  - Retarget the open bundle PR, and any security PRs, to the pre-release branch: `gh pr edit <N> --base pre-release/vX.Y.Z`.
  - Or fix them by hand in a `fix/deps-vX.Y.Z` PR into the pre-release branch.
  - The PR into `release/vX.Y.Z` cannot merge until `audit` is green.

## Cutting a release

```bash
git fetch origin
git push origin origin/dev:refs/heads/pre-release/v0.1.0   # freeze the candidate
# dependency/vuln fixes and stabilization: PRs into pre-release/v0.1.0
git push origin origin/main:refs/heads/release/v0.1.0      # release branch starts at main
gh pr create --base release/v0.1.0 --head pre-release/v0.1.0 --title "release: v0.1.0 candidate"
# after it merges:
gh pr create --base main --head release/v0.1.0 --title "release: v0.1.0"
```

When the PR into `main` merges, the `Post-release` workflow:
1. Tags `v0.1.0` and creates the GitHub release with generated notes.
2. Opens a **"chore: sync main into dev"** PR. Merge it with a **merge commit**. That brings the release's dependency fixes back into dev without rewriting dev.

Hotfix: cut `hotfix/v0.1.1` from `main`, then open a PR into `main`. It is tagged and synced in the same way.

## Why dev is merged into, never rebased

`dev` is shared. Rebasing it means force-pushing, which rewrites the commits every open feature branch was cut from, and those branches would then need `rebase --onto` surgery. Dev's history stays clean anyway:
- each feature is one squash commit;
- each release adds exactly one sync merge commit, which marks where the release landed.

## Keeping your branch up to date

Rebase *your own* branch onto the branch it was cut from, not onto `main`:

```bash
scripts/sync-branch.sh
```

- The base comes from the open PR. Failing that, it uses `git config branch.<name>.base`, and failing that, `dev`.
- Set the base for a branch with no PR: `git config branch.my-fix.base pre-release/v0.1.0`.
- The script refuses to run on `dev`, `main`, `pre-release/*` and `release/*`.
- It pushes with `--force-with-lease`, so it never overwrites someone else's push.
- GitHub's **Update branch** button also works (choose *rebase*).
