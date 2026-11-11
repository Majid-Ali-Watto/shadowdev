# Privacy: what ShadowDev stores, and where

ShadowDev runs on the machine of the developer who installed it. **It has no central server and
sends nothing to its authors.** Everything it stores stays in that installation's folder on that
machine. It only talks to Atlassian (Jira), GitHub, Anthropic (Claude) and, if connected, Slack, on that
developer's behalf, with the accounts they connected.

## What is stored

All of it is under the ShadowDev folder, readable only by the user running it (`data/` and
`worktrees/` are created with permissions `700`, the database file with `600`).

### `data/shadowdev.db` (SQLite database)

| What | Details | Protection |
|---|---|---|
| Atlassian (Jira) accounts | account ID, display name, email address (empty if Atlassian hides it), when they were fetched | |
| Jira sites | each site's ID, URL and name, and which accounts can reach it | |
| Jira access | OAuth access and refresh tokens | encrypted |
| GitHub accounts | label, login, commit name and email (from the GitHub profile) | |
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
| Run history | issue keys, project name, status, result message (PR links or the error) | |
| Webhook events | a hash of each event accepted, for one hour, so it is not accepted twice | |

"Encrypted" means encrypted with Fernet using `SHADOWDEV_SECRET_KEY` from `.env`. A copy of the
database alone does not reveal those values. Anyone who can read both the database and `.env`
on that machine can decrypt them, so the machine account is what protects them.

Issue text (summaries, descriptions) is **not** stored. It is read from Jira when a run starts and
kept in memory only.

### `data/claude/<id>/`

The Claude Code config folder of each connected Claude account. Claude's transcript of a run
(which contains the code it read) is deleted when the run ends.

### `worktrees/`

Clones of the project's repos for a run in progress, its test reports and baseline copies. They
are **deleted when the run ends**, however it ends. A dry run keeps its change for inspection
until the next dry run replaces it.

### `data/shadowdev.db.v1-backup`

Only after upgrading from ShadowDev v1: a copy of the old database, with the same kind of data.
Delete it once the upgrade is confirmed.

### Logs

ShadowDev logs to its terminal, or to wherever it is run from, e.g. the systemd journal. Logs
include issue keys and summaries, the files Claude reads and edits, test output, and developer
display names. ShadowDev does not keep log files of its own.

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
