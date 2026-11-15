# Admin guide

[README](../README.md) · [Features](../FEATURES.md) · [Install](../INSTALL.md) · [User guide](USER-GUIDE.md) · [Admin](ADMIN.md) · [Safety](SAFETY.md) · [Architecture](ARCHITECTURE.md)

For whoever runs the server. The first run, step by step, is [INSTALL.md](../INSTALL.md).

## Requirements

- **Linux** directly: Python 3.11+, git, [bubblewrap](https://github.com/containers/bubblewrap)
  (`bwrap`), `prlimit` (util-linux), the [Claude Code](https://claude.com/claude-code) CLI, and
  [uv](https://docs.astral.sh/uv/) to install ShadowDev's one dependency, `cryptography`
- **macOS, Windows, or Linux** in Docker: Docker Desktop (on Windows with WSL2), or Docker Engine
  with its compose plugin. The image brings all of the above, with node, npm and uv for the
  projects' commands ([Dockerfile](../Dockerfile), [deploy/README.md](../deploy/README.md), *Docker*)
- No clones and no GitHub access of its own: every clone, push and PR uses the developer's token.
  Developers bring their own Claude login
- What the repos' setup and test commands need: for the marketplace, Node from `.nvmrc` with npm,
  and `uv` (it fetches Python 3.12 itself)

## Setup

[INSTALL.md](../INSTALL.md) is the first-run guide, for each system: the Jira sign-in app, `.env`,
starting the server directly (Linux) or in Docker (macOS, Windows, Linux), checking the install,
the first sign-in, and sharing it with the team. `make` lists every command; each is a short name
for the command shown.

**Jira webhooks** start runs the moment an issue is assigned (without them, developers turn on
*Poll Jira* in Settings). Per project, in its Jira site: **Settings → System → WebHooks → Create a
WebHook**. The project's owner sees the values on their ShadowDev page:

- URL: `https://<your-address>/webhook?project=<id>`
- Secret: the project's webhook secret
- Events: Issue **created** and Issue **updated**. Updated covers an issue that gets assigned later.
- JQL (optional, reduces noise): `project in (MAR) AND issuetype in (Bug, Task, Story, Sub-task)`, with
  the project's own issue types

A per-project secret means a site's admin can only send events for that site's projects. The
shared `https://<your-address>/webhook`, signed with `WEBHOOK_SECRET`, also works (one webhook
per site for all its projects; the site is read from the event).

## Try it without Jira: dry run

Runs the real pipeline on the real repos for a made-up issue: clones of your base branches,
Claude with your token, the sandboxed setup and tests, and the self-heal loop. Jira writes are
printed instead of made, and nothing is committed, pushed or opened. You need to have signed in
and connected GitHub and Claude on the web page. It runs with the accounts you joined the
project with (or your first GitHub and Claude accounts).

```bash
python3 -m shadowdev dry-run --summary "Invoice total ignores the discount" \
    --description "Steps: add a 10% discount... Expected: ... Actual: ..."
```

- `--note "Only the API"` adds a developer's note for Claude, as on the page
- `--type Task` (or `Story`) tries the task flow; the default is `Bug`
- `--description-file bug.txt` reads a longer description from a file
- `--project` and `--as` choose the project (by name) and developer when there are several

In Docker, run it in the container: `docker compose exec shadowdev python -m shadowdev dry-run --summary "..."`.

The change stays in `worktrees/<project id>-<KEY>-0/<repo>` (e.g. `worktrees/1-MAR-0/fe`) for you to inspect;
the command lists the changed files. The next dry run replaces it.

## Projects

Developers add and edit projects on the web page (see the [User guide](USER-GUIDE.md)); nothing to restart. A
Jira project key belongs to one project per Jira site. **Upgrading from v1**: on the first start,
the database is upgraded in one transaction (a copy of the old one is kept as
`data/shadowdev.db.v1-backup`). Developers keep their accounts, tokens, settings and run history,
sign in again once, and each `projects/*.toml` becomes a project owned by its first subscriber,
with its own webhook secret. The old `/webhook` keeps working. After that, `projects/` can be
deleted, and so can `data/shadowdev.db.v1-backup` once all is well (ShadowDev deletes it itself after
a week, or as soon as a developer or one of their accounts is removed).

## Server: `.env`

See [`.env.example`](../.env.example). `JIRA_SITES`: the Jira sites developers may sign in with and
add projects on, comma-separated, e.g. `https://office.atlassian.net,https://home.atlassian.net`
(`JIRA_BASE_URL`, v1's single site, still works). `*` allows any site, which lets anyone with an
Atlassian account use the server and its machine. `ATLASSIAN_CLIENT_ID`,
`ATLASSIAN_CLIENT_SECRET`, `PUBLIC_URL`, `SHADOWDEV_SECRET_KEY` (encrypts developers' tokens; see
*Backups and the key* to change it),
`WEBHOOK_SECRET` (optional: the shared `/webhook`; each project also has its own), `HOST`, `PORT`, and,
optional for emailing developers, `SMTP_HOST`, `SMTP_PORT` (587, STARTTLS; 465 for TLS from the start; a relay
on `localhost` may be unencrypted), `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_FROM`.

Timing and limits are each developer's own, on the **Settings** tab. Times are in minutes, with
decimals for less (0.5 is 30 seconds): Poll Jira and its interval (default off, 15), how often GitHub
(its PRs) and Slack (review requests) are each read (1 each, at least 0.25), how long review comments
wait for more (2), and the Claude and setup/test command timeouts (30 each). Counts: runs at the same
time (5; mind the machine: parallel runs mean parallel `npm ci` and test suites, and the server runs
at most 20 in all) and fix attempts (3).

## Starting and stopping

`make up` runs the server in the terminal, `make upd` in the background (`make logs` follows it);
in Docker, `make docker-up` and `make docker-down` (INSTALL.md, *Everyday commands*).
Stopping it (Ctrl+C, `make down`, `make docker-down`, or `systemctl stop` with the unit below) is graceful: it takes no
new requests or runs, the runs in progress get up to 15 minutes to finish, and runs still queued
start again with the server. Stopping it a second time (Ctrl+C again, `make down` again) stops the
runs in progress at once, as their Stop button does. A Jira webhook sent while the server is down
is delivered again by Jira.

`GET /health` answers `{"status": "ok"}` while the server runs and can read its database, for a
reverse proxy or a monitor. Starting a sign-in and the OAuth callback take at most 30 requests a
minute from one client, and `/webhook` 600; more are answered `429` with `Retry-After`. Behind a
reverse proxy or tunnel on the same machine, the client is the address the proxy puts last in
`X-Forwarded-For`.

## Backups and the key

`make backup` copies the database to `data/backups/`, safe while the server runs. A backup is
readable only by ShadowDev's user, its tokens stay encrypted with `SHADOWDEV_SECRET_KEY`, and
ShadowDev deletes it after 7 days, and at once when a developer is erased, so nothing removed lives
on in a copy. To keep backups longer, copy them elsewhere, together with the key.

To change the key (it leaked, or a person who knew it left), without anyone connecting their
accounts again:

1. `make key` prints a new one. In `.env`, put it **first**, before the old one:
   `SHADOWDEV_SECRET_KEY=<new>,<old>`. The server reads with either and writes with the new one.
2. `make rotate`: every stored token and webhook secret is encrypted again with the new key, in one
   transaction, and the backups (encrypted with the old key) are deleted.
3. Remove the old key from `.env`, and restart the server.

## Jira sign-in app (once)

Developers sign in through an OAuth 2.0 (3LO) app that the company registers once, with the scopes
`read:jira-work`, `write:jira-work` and `read:jira-user` and the callback URL
`PUBLIC_URL/oauth/jira/callback`. [INSTALL.md](../INSTALL.md), step 2, creates it click by click.
Its **Distribution** must be set to **Sharing** for anyone but its owner to sign in; that also lets
one app serve several Jira sites, e.g. the office one and developers' personal ones.

`offline_access` is requested at sign-in, so ShadowDev gets a refresh token and keeps working
without developers signing in again.

**Personal data.** ShadowDev stores Atlassian accounts' IDs, names and emails, so in the app's
**Distribution** settings answer *Does your app store personal data?* with **Yes**. It implements
Atlassian's [personal data reporting API](https://developer.atlassian.com/cloud/jira/platform/user-privacy-developer-guide/):
a background job reports every stored account once per cycle (7 days, or what Atlassian's
`Cycle-Period` header says), 90 per request, at jittered times, resuming after restarts. A
**closed** account is erased: its Jira account, tokens and the subscriptions using it, or, if it was
the developer's only Jira account, the whole developer as on *Delete my ShadowDev account*. For an
**updated** one, its name and email are fetched again. It authenticates with the stored accounts'
own OAuth tokens, trying the next one if one is refused. [PRIVACY.md](../PRIVACY.md) says what is
stored and where; its GitHub URL can be the app's privacy policy URL.

## Test gate: no new failures

A repo's `test` command must write JUnit XML into `$SHADOWDEV_REPORT_DIR` (pytest
`--junitxml`, vitest `--reporter=junit`). Before the first check, ShadowDev runs the same command
on the untouched base branch, in a separate worktree. A change passes when:

- the command exits 0, or
- every report from the baseline is present again, and no test fails that passed on the base branch.

The setup and test commands must leave the code as they found it: if they change a file a commit
would take (a `--fix`, a snapshot update, or a hostile dependency), the run stops and nothing is
pushed, because the PR is opened in the developer's name.

Tests that already fail on the base branch are therefore not held against the change, but they
don't verify it either. A suite that writes no report on the base branch is not checked at
all. For example, on this machine the documents and notifications services can't load without
the system `unixodbc` package. If the base branch fails without writing any report, the run
stops with an error asking you to fix the test command.

## Deployment checklist

ShadowDev holds every developer's tokens, so the machine and account it runs on are part of its
security:

- [ ] A **dedicated server or VM**, used for nothing else: no one's browser, SSH keys or personal
      files on it
- [ ] A **dedicated system user** (e.g. `shadowdev`) that is **not** in the `docker`, `sudo`, `lxd`
      or `adm` groups; ShadowDev never needs root. Its home holds only ShadowDev, the toolchains
      and caches
- [ ] Install the toolchains as that user (nvm/node, uv, the `claude` CLI) and check that their
      paths are in `SANDBOX_HOME_TOOLS` (the defaults cover nvm, uv, cargo, pyenv, volta, bun, pnpm)
- [ ] `.env` readable only by that user (`chmod 600 .env`; `data/` and `worktrees/` are created
      `700`). Keep a copy of `SHADOWDEV_SECRET_KEY` in your password manager or secret store
- [ ] HTTPS in front (a reverse proxy or a named Cloudflare tunnel), with `PUBLIC_URL` set to it
- [ ] Run it as a service so it survives reboots: `make service` (the systemd unit in `deploy/`), or
      `make docker-service` in Docker; see [deploy/README.md](../deploy/README.md). The systemd
      service's logs then go to the journal too
- [ ] Point the reverse proxy's or a monitor's health check at `/health`
- [ ] `make backup` on a schedule (e.g. a daily cron job), copied off the machine with the key kept
      separately
- [ ] Disk encryption, and OS updates (bubblewrap, git, the kernel)
- [ ] On GitHub: branch protection on `release/*` requiring an approving review
- [ ] Developers use fine-grained GitHub tokens limited to the project repos
