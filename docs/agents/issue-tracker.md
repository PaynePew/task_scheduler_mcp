# Issue tracker: GitHub

Issues for this repo live in GitHub Issues. Skills that read or write issues
(`to-issues`, `triage`, `to-prd`, `qa`) use the `gh` CLI.

## Prerequisite: remote not yet configured

As of this file's creation, no GitHub remote is set on this repo. Before any
issue-writing skill can publish, do one of:

- `gh repo create <owner>/<name> --source=. --remote=origin --push`
- Create the repo manually on github.com, then
  `git remote add origin git@github.com:<owner>/<name>.git && git push -u origin master`

Verify with `gh repo view` — it should print the repo metadata, not error.

## Creating issues

```bash
gh issue create --title "<title>" --body "<body>" --label "<label>"
```

Skills may also use `gh issue create --body-file <path>` when the body has
been drafted to a file first.

## Reading issues

```bash
gh issue list --state open --label ready-for-agent
gh issue view <number>
gh issue view <number> --comments
```

Pass an issue number, URL, or `gh`-compatible identifier when a skill asks
for an issue reference.

## Updating issues

```bash
gh issue edit <number> --add-label <label> --remove-label <label>
gh issue comment <number> --body "<comment>"
gh issue close <number> --reason "completed"|"not planned"
```

## Labels

See `docs/agents/triage-labels.md` for the canonical label vocabulary.
Labels must exist in the repo before they can be applied — create them
with `gh label create <name>` if `gh issue create --label` errors.
