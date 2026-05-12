# TASK

Merge branch `{{BRANCH}}` for issue **#{{ISSUE}}** in repo `{{REPO}}`.

Push the branch to origin and open a pull request. Comment on the issue with the PR link.

# CONTEXT

## Branch status

```bash
git status
git log origin/{{TARGET_BRANCH}}..HEAD --oneline
```

## Issue details

```bash
gh issue view {{ISSUE}} --repo {{REPO}}
```

# EXECUTION

Follow these steps in order. Stop immediately if any step fails.

## 1. Verify branch is clean and tests pass

```bash
git status
```

The working tree must be clean. If there are uncommitted changes, abort with an explanation.

Run the test suite:

```bash
{{TESTS_BLOCK}}
```

If tests fail, abort with an explanation. Do not push a broken branch.

## 2. Push the branch to origin

```bash
git push -u origin {{BRANCH}}
```

## 3. Open a pull request

Collect the commit summary and reviewer notes from the issue comments, then open the PR:

```bash
gh pr create --repo {{REPO}} --head {{BRANCH}} --base {{TARGET_BRANCH}} --fill --body "$(cat <<'PRBODY'
Closes #{{ISSUE}}

## Commit summary

<!-- one-line summary of the key commits on this branch -->

## AC self-report

<!-- for each acceptance criterion item: checked or not, with brief note -->

## Reviewer notes

<!-- paste the reviewer's structured comment from the issue, if any -->
PRBODY
)"
```

Capture the PR URL from the command output.

## 4. Comment on the issue

```bash
gh issue comment {{ISSUE}} --repo {{REPO}} --body "PR #<N> opened, ready for human review. <PR_URL>"
```

Replace `<N>` with the PR number and `<PR_URL>` with the URL from step 3.

# COMPLETION

Output `<promise>COMPLETE</promise>` and exit.

# HARD RULES

- Do NOT run `git merge` to `{{TARGET_BRANCH}}`.
- Do NOT run `git checkout {{TARGET_BRANCH}}`.
- Do NOT run `gh issue close`.
- Do NOT set `--auto-merge`.
- Do NOT squash or rebase commits.
- Do NOT merge the PR yourself — the human merges via GitHub after review.
- GitHub closes the issue automatically when the human merges, via the `Closes #{{ISSUE}}` keyword.
