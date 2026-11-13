# Privacy: what ShadowDev stores, and where

ShadowDev runs on the machine of the developer who installed it. **It has no central server and
sends nothing to its authors.** Everything it stores stays in that installation's folder on that
machine. It only talks to Atlassian (Jira), GitHub, Anthropic (Claude) and, if connected, Slack, on that
developer's behalf, with the accounts they connected.

## What is stored

All of it is under the ShadowDev folder, readable only by the user running it (`data/`, `data/logs/`
and `worktrees/` are created with permissions `700`, the database and log files with `600`).

### `data/shadowdev.db` (SQLite database)

| What | Details | Protection |
|---|---|---|
| Atlassian (Jira) accounts | account ID, display name, email address (empty if Atlassian hides it), when they were fetched, whether Atlassian refused the grant | |
| Jira sites | each site's ID, URL and name, and which accounts can reach it | |
| Jira access | OAuth access and refresh tokens | encrypted |
| GitHub accounts | label, login, commit name and email (from the GitHub profile), when the token expires (as GitHub says) | |
| GitHub access | personal access token | encrypted |
| Claude accounts | label, and the name of its config folder | |
| Claude access | `claude setup-token` token or Anthropic API key | encrypted |
| Slack account (optional) | Slack member ID and workspace name | |
| Slack access | the developer's Slack user token | encrypted |
| Pull requests in review | repo, PR number, branch, and the Slack channel and message where review was asked | |
| Projects | name, Jira site and project keys, repos (`owner/name`), descriptions, setup and test commands, Jira statuses, the Slack review channel's ID and reviewers' Slack member IDs | |
| Webhook secrets | one per project | encrypted |
| Project memberships | which accounts each project runs with, mode, base branches, when Jira was last polled | |
| Web sessions | a SHA-256 hash of the session cookie, a CSRF token, an expiry time | the cookie itself is not stored |
| Run history (the latest 200 runs per developer) | issue keys, project name, status, result message (PR links or the error), the note the developer gave the run, the questions it asked them about the issue, the titles and links of its PRs until their review is asked for, and how long it worked, Claude's turns and the cost Claude reported | |
| Run logs | for each of those runs, its last 400 log lines: what Claude read, said and changed, the commands and their results, the developer's display name | shown only to its developer; deleted with the run, and all of a developer's when one of their Jira accounts is removed |
| Recent activity (the latest 200 per developer) | when they signed in, and what they changed: accounts saved or removed (by label or login), projects added, edited, joined or left (by name), pausing, limits | never a token, nor anything about a Jira account |
| Webhook events | a hash of each event accepted, for one hour, so it is not accepted twice | |

"Encrypted" means encrypted with Fernet using `SHADOWDEV_SECRET_KEY` from `.env`. A copy of the
database alone does not reveal those values. Anyone who can read both the database and `.env`
on that machine can decrypt them, so the machine account is what protects them. The key can be
replaced without developers reconnecting their accounts (README, *Backups and the key*). Rows that are
deleted are overwritten in the database file (`PRAGMA secure_delete`).

Issue text (summaries, descriptions) is **not** stored in the database, except what a run's result,
its questions and its PR titles (above) say about the issue. It is read from Jira when a run starts.

### `data/claude/<id>/`

The Claude Code config folder of each connected Claude account. Claude's transcript of a run
(which contains the code it read) is deleted when the run ends.

### `worktrees/`

Clones of the project's repos for a run in progress, its test reports and baseline copies. They
are **deleted when the run ends**, however it ends. A run that Claude's usage limit interrupts
keeps them while it waits for the limit to reset, for at most 5 hours, and then continues. When the
developer switched on **Switch Claude account at its limit**, such a run continues with another of
their own Claude accounts instead: the issue text and code then go to Anthropic under that account,
and the run's transcript is copied to that account's config folder, where it is deleted like any other. A dry run keeps its change for inspection
until the next dry run replaces it.

### `data/backups/` and `data/shadowdev.db.v1-backup`

Copies of the database, with the same data and the same protection: the ones `make backup` makes,
and, only after upgrading from ShadowDev v1, the old database. ShadowDev deletes each at the first
start a week or more after it was made, all of them at once when a developer or one of their Jira
accounts is removed, so nothing erased stays in a copy, and all of them when the key is changed.
A backup copied elsewhere is yours to look after.

### Logs

ShadowDev logs to its terminal, or to wherever it is run from, e.g. the systemd journal, and the
server writes the same to `data/logs/shadowdev.log`, one file per day, **deleted after 5 days**. Logs
include issue keys and summaries, what Claude says and the files it reads and edits, the questions
it asks, test output, and developers' display names and commit names and emails. They never
include tokens. Removing an account does not edit the log files: what they say about it is gone
within those 5 days. The lines of each run are also kept with the run (*Run logs*, above).

## What is sent, and to whom

| To | What | Why |
|---|---|---|
| Atlassian (Jira) | requests as the developer's Jira account: reading assigned issues, comments, fields, status changes | the work itself |
| Atlassian | the stored Atlassian account IDs and when their data was fetched, once a week | [personal data reporting](https://developer.atlassian.com/cloud/jira/platform/user-privacy-developer-guide/), required by Atlassian |
| GitHub | clones, pushes, pull requests, reviews and review replies as the developer's GitHub account | the work itself |
| Slack (optional) | messages posted as the developer in the projects' review channels; those channels' messages are read as them, to find review requests | asking for and answering reviews |
| Anthropic (Claude) | the issue text and the code Claude reads, as the developer's Claude account | Claude does the analysis and the change |

There is no analytics, telemetry or tracking.

## How long, and how to delete

- Data stays until the developer removes it. **Delete my ShadowDev account** on the web page
  removes that developer's accounts, tokens, memberships, sessions and run history, and their Claude
  config folders. Removing a single Jira, GitHub or Claude account removes that account's data.
- When Atlassian reports that an Atlassian account was closed, ShadowDev erases it automatically:
  the account and its tokens, and the developer too if it was their only Jira account. When
  Atlassian reports an account was changed, its name and email are fetched again.
- Deleting the `data/` and `worktrees/` folders removes everything.
- Access granted to ShadowDev is revoked at the source: Atlassian (*Connected apps* in your
  Atlassian account), GitHub (delete the token), Anthropic (revoke the token or API key).
