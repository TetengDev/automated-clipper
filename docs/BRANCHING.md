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

`branch-policy` runs from the default branch's (`dev`) copy of the workflow (`pull_request_target`), so a PR cannot change the rule it is judged by. It is a guardrail for the promotion path, not a security boundary. It also re-runs when a PR's base is changed. It only lets `release/vX.Y.Z` or `hotfix/vX.Y.Z` into `main`, and only `pre-release/vX.Y.Z` (same version) or `hotfix/vX.Y.Z` into `release/vX.Y.Z`. Fork branches are rejected.

Every PR needs **1 approval**, and a new push dismisses earlier approvals. PRs are authored by the maintainer's account, and GitHub does not let an author approve their own PR. So the maintainer merges through the ruleset's **repo-admin bypass**, which works only on PRs, never on direct pushes; that merge is the human approval. **The bypass skips everything:** the approval, every required check (`audit` and `branch-policy` included), and the promotion path. GitHub will not stop a red merge, so check the checks list and the review record before you bypass. "Agents never merge" is a working rule, not a technical control; agents using the maintainer's credentials could. To enforce it, give agents a separate non-admin account whose PRs you then approve normally.

## Vulnerabilities do not block dev

- The `audit` job (`pip-audit` and `npm audit --omit=dev`) runs on every PR. It is a **required** check only on `release/**` and `main`. On a dev PR it can show red, but the PR can still be merged.
- Dependabot opens one bundled version-update PR against `dev` each week (`.github/dependabot.yml`). You can merge it into dev or leave it open.
- At release time, apply all dependency fixes in one go on the pre-release branch:
  - Retarget the open bundle PR, and any security PRs, to the pre-release branch: `gh pr edit <N> --base pre-release/vX.Y.Z`.
  - Or fix them by hand in a `fix/deps-vX.Y.Z` PR into the pre-release branch.
  - The PR into `release/vX.Y.Z` cannot merge until `audit` is green.

## Cutting a release

First merge any open "chore: sync main into dev" PR, so the new candidate starts from the last release.

```bash
git fetch origin
git push origin origin/dev:refs/heads/pre-release/v0.1.0   # freeze the candidate
# dependency/vuln fixes and stabilization: PRs into pre-release/v0.1.0
git push origin origin/main:refs/heads/release/v0.1.0      # release branch starts at main
gh pr create --base release/v0.1.0 --head pre-release/v0.1.0 --title "release: v0.1.0 candidate"
# after it merges:
gh pr create --base main --head release/v0.1.0 --title "release: v0.1.0"
```

When the PR into `main` merges, the `Post-release` workflow runs on the push to main and:
1. Tags `v0.1.0` and creates the GitHub release with generated notes.
2. Opens a **"chore: sync main into dev"** PR. Merge it with a **merge commit**. That brings the release's dependency fixes back into dev without rewriting dev.
   - If its checks don't show up, close and reopen the PR to run CI.
   - If it was squash-merged by mistake, dev never gets main's commits and every later sync PR repeats old releases. To recover, merge `origin/main` into a branch cut from dev and open a PR with a merge commit.
   - The workflow runs on the push to `main` and opens the PR with the `SYNC_PR_TOKEN` secret. It is stored in the **`release-sync` environment**, which only `main` may use, so no PR branch can read it. Use a fine-grained PAT for this repo with *Contents: read*, *Pull requests: write* and an expiry. Because a PAT opens the PR, CI runs on it. Without the secret it falls back to `GITHUB_TOKEN`, which fails unless Actions may create PRs. If so, open it by hand with `gh pr create --base dev --head main`. Keep "Allow GitHub Actions to create and approve pull requests" **off**, so no workflow can approve a PR.
   - The workflow is safe to re-run: it skips a tag that already exists.

Hotfix: cut `hotfix/v0.1.1` from `main`, then open a PR into `main`. It is tagged and synced in the same way. If a `release/*` branch is in flight, also open a PR from the hotfix into it; `branch-policy` allows that. Otherwise that release ships without the fix.

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
- It stops if the remote branch has commits you don't have locally, and pushes with an explicit `--force-with-lease`, so it never drops someone else's push.
- GitHub's **Update branch** button also works (choose *rebase*).
