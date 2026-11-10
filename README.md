# ShadowDev

One ShadowDev server serves the whole team. Each developer signs in on its web page with Jira
(a browser consent, like connecting an app; no API token) and connects their GitHub and Claude
accounts: as many of each as they use, e.g. an office and a personal Jira, GitHub and Claude.

**Projects live on the web page**, not in files. Anyone adds one by pasting its repo URLs and
Jira project keys; ShadowDev checks them against GitHub and Jira and fills in the rest. Every
developer who can reach that project's Jira site can then join it, choosing **which of their
accounts it runs with** (office GitHub and Claude for work, personal ones for their own projects),
a base branch per repo, and what to automate: **bug fixes only**, or **bug fixes and tasks**
(Task, Story, Sub-task).

| You have | ShadowDev |
|---|---|
| an office and a personal Jira | sign in with one, *Connect another Jira account* for the other; one ShadowDev account |
| several Jira projects on one site | one ShadowDev project with several keys (`MAR, MARS`), or one project each |
| the same key (`MAR`) on two sites | no clash: a project belongs to one Jira site |
| an office and a personal GitHub / Claude | both connected, with labels; each project you join picks one |

When an issue of a type they automate is assigned to them in one of their projects, ShadowDev
works on it as that developer, with the accounts they chose for that project, across all of the
project's repos:

1. moves the ticket to **In Progress** (along the workflow, e.g. Backlog → To Do → In Progress)
2. per repo, takes the developer's base branch, or else the latest `release/*` branch on
   GitHub (only purely numeric names count, compared by version: `release/15` > `release/9`),
   or else the repo's default branch
3. per repo, makes a shallow clone of it with the developer's GitHub token, on a branch named
   after the issue key (e.g. `MAR-123`). All of them sit in one workspace,
   `worktrees/<project id>-MAR-123/{fe,services}`, which is **deleted when the run ends**
4. has a small Claude model (Haiku) rate the issue from its text as simple, standard or complex,
   which picks the model for the rest of the run: Haiku, Sonnet at medium effort, or the Claude
   account's default model. An issue that can't be rated runs with the default
5. has Claude analyse the workspace (read-only) and decide which repo(s) to change. For a bug it
   writes a concise **root cause** (2–5 sentences: what is wrong, where, and why it causes the
   reported behaviour) to the ticket, and sets the **fix version** to the highest unreleased
   `Rel N`, replacing any other. For a task it comments a concise **implementation plan**. A
   placeholder, over-long or garbled answer is sent back to Claude
6. has Claude make the change: it edits files and creates new ones, like Claude Code does
7. for each repo that changed, runs its `setup` (once) and `test` commands in a sandbox. New test
   failures go back to Claude, which tries again (up to `MAX_FIX_ATTEMPTS` attempts in total).
   A repo without a test command is not tested, and its PR says so
8. per changed repo: commits as the developer, pushes with their GitHub token, and opens a PR
   **into the base branch**. The PR links are commented on the ticket, which moves to **In Review**

```
Jira webhook → verify signature (the project's secret) → project: by URL, or by site + key (MAR-123 → MAR)
               assignee's Jira account joined that project, automates this issue type? ── no → ignore
                                         │ yes
                                         ▼
          queue → one of MAX_PARALLEL_RUNS workers (5) → pipeline, as the assignee, with that project's accounts
```

New assignments are picked up three ways: the Jira **webhook** (instant, needs ShadowDev reachable
from Jira), **polling** (`JIRA_POLLING=true` in `.env`: every 15 minutes, for machines Jira can't
reach, like a laptop), and **Run issues now** on the page (always available). Polling asks Jira, as
each developer's Jira account in each project, for unresolved issues assigned to it and updated
since the last poll (the first time, the last 15 minutes); repeats are harmless, as with webhooks.

Up to `MAX_PARALLEL_RUNS` (default 5) runs work at the same time, from any developers and
projects; more wait their turn, **developers taking turns**, so one developer's ten queued issues
don't hold up everyone else. Runs still waiting when the server restarts are queued again. Each
run has its own clones. Runs on the same base commit share one baseline test result. Every step is logged in the server's terminal, including what Claude reads
and edits.

**Several issues at once**, from the web page: *separately* gives each issue its own branch and
PR, worked on in parallel. *Together* fixes them in one Claude session on one branch
(`MAR-1_MAR-2`) with one PR per repo; each ticket still gets its own root cause, fix version,
status changes and PR comment. An issue fixed in a group counts as handled afterwards.

## Layout

```
ShadowDev/
├── shadowdev/
│   ├── __main__.py        python -m shadowdev: the server, or dry-run
│   ├── config.py          .env settings
│   ├── app.py             wires repositories and services together
│   ├── server.py          HTTP server: dispatches each request to its route
│   ├── models/            plain dataclasses: User and its accounts, Subscription, Actor, Project/Repo, Issue, Tokens
│   ├── repositories/      SQLite, one class per table group
│   │   ├── database.py    schema, connections, token encryption
│   │   ├── migrations.py  upgrading a v1 database (and its projects/*.toml) in one transaction
│   │   ├── users.py       developers, their Jira/GitHub/Claude accounts (encrypted), subscriptions
│   │   ├── projects.py    projects: Jira site and keys, repos, workflow, webhook secret
│   │   ├── sessions.py    web sessions (hashed)
│   │   └── runs.py        run history shown on the page, accepted webhook events
│   ├── services/          business logic; raise UserError for problems shown to the developer
│   │   ├── accounts.py    sign-in, connect/remove Jira, GitHub and Claude accounts, pause, delete
│   │   ├── projects.py    add/edit/delete projects, join/leave them
│   │   ├── runs.py        run issues by hand, stop a run
│   │   ├── run_queue.py   the pool of parallel runs, developers taking turns
│   │   ├── webhook.py     Jira webhook: signature, replay protection, matching the developer
│   │   ├── privacy.py     Atlassian's personal data reporting: erase closed accounts, refresh changed ones
│   │   └── poller.py      JIRA_POLLING: ask Jira every 15 minutes for new assignments
│   ├── routes/            HTTP only: parse the request, call a service, respond
│   │   ├── request.py     Request helpers, security headers, the signed-in form guard
│   │   ├── auth.py        /login/jira, /jira/connect, OAuth callback, /logout
│   │   ├── settings.py    the developer's page and its account actions
│   │   ├── projects.py    the project editor, joining and leaving
│   │   └── webhook.py     /webhook
│   ├── views/             pages.py (HTML), style.css
│   ├── clients/           atlassian.py (Jira OAuth sign-in), jira.py, github.py, rest.py
│   └── automation/        working on one issue
│       ├── pipeline.py    analyse → change → test → PR
│       ├── prompts.py     what Claude is asked, and checks that its answers are usable
│       ├── verify.py      sandboxed setup/test commands, JUnit results, base-branch baselines
│       ├── agent.py       headless Claude Code session, as the developer
│       ├── rules.py       issue types, release branches, fix versions
│       ├── gitops.py      git as the developer: list branches, clone, commit, push
│       ├── sandbox.py     bubblewrap sandbox for setup/test commands
│       ├── cancel.py      stopping a run
│       └── dryrun.py      python -m shadowdev dry-run: a made-up issue, no Jira or GitHub
├── tests/
├── data/                  database and per-developer Claude config (created; never committed)
└── worktrees/             workspaces of running issues, deleted when each run ends
```

## Architecture

Python standard library only (`http.server`, `sqlite3`, `urllib`), plus `cryptography`. Each layer
only calls the ones below it:

```
routes/  ──▶  services/  ──▶  repositories/  ──▶  SQLite (data/shadowdev.db)
   │              │      └──▶  clients/       ──▶  Jira, GitHub
   ▼              └──────────▶  automation/   ──▶  git, Claude Code, the sandbox
views/        (all layers pass models/ around)
```

| Layer | Owns | Does not |
|---|---|---|
| `routes/` | parsing the request, cookies, CSRF/Origin checks, choosing the response | business rules, SQL |
| `services/` | validating against Jira/GitHub/Claude, decisions, the run queue | read forms or HTTP headers |
| `repositories/` | SQL, encryption of tokens | call Jira/GitHub, make decisions |
| `models/` | plain dataclasses | I/O |
| `views/` | HTML, escaping every outside value | lookups |
| `automation/` | working on one issue: clone, Claude, sandboxed tests, PR | know about HTTP |

`app.py` builds everything once (`build_app(cfg)`) and `server.py` hands each request to the
function in `routes.ROUTES` for its method and path.

A request to the developer's page, e.g. `POST /run`:

```
server.Handler → routes.ROUTES[("POST", "/run")]
  → request.form_action: same Origin, live session, CSRF token, loads the User
  → settings.action: calls RunService.request(user, keys, together, project_id)
       → finds each key's project among the developer's → checks in Jira, as the Jira account they
         joined it with, that it is assigned to them → RunQueue.submit → RunRepository.add
  → redirect to /?ok=queued, or on UserError the page again with the message (400)
```

A webhook: `routes/webhook.py` checks the body size, `WebhookService.handle` verifies the signature
and timestamp, drops replays, matches the developer, and queues it. A `RunQueue` worker then reads
the project and the developer afresh and calls `automation.pipeline.process_issues` with the
`Actor` of their subscription: the Jira, GitHub and Claude accounts they chose for that project.

### Adding things

- **A page action**: a function in the right `services/` class (raise `UserError` for anything the
  developer should read), then a route in `routes/settings.py` with `action("<notice key>", …)`, a
  message for it in `views/pages.NOTICES`, and the form in `views/pages.py`.
- **A new area of the site**: a new `routes/<area>.py` with its own `ROUTES`, merged in
  `routes/__init__.py`. Wrap signed-in POST routes in `form_action`.
- **A column or table**: change `SCHEMA` in `repositories/database.py`, bump `VERSION`, and upgrade
  databases of the previous version in `Database.__init__` (like `migrations.upgrade_from_v1`,
  in one transaction). Encrypt anything secret with `db.encrypt`.
- **A setting**: a field on `Config` in `config.py`, read in `Config.from_env`, and a line in
  `.env.example`.
- **A pipeline step**: `automation/pipeline.py` for the order of steps, `prompts.py` for what Claude
  is asked, `verify.py` for anything that runs commands.

## For developers

Open the ShadowDev page and:

1. **Sign in with Jira**: click the button, and on the Atlassian page choose your site and press
   **Accept**. Issues assigned to you are yours. ShadowDev reads them and comments as you. Access
   renews itself; if you revoke it in your Atlassian account settings, sign in again. Another
   Atlassian account (e.g. your personal one): **Connect another Jira account** on your page.
   Don't sign in with it separately, or it becomes a second ShadowDev account.
2. **GitHub**: one or more accounts, each with a label (e.g. *office*, *personal*), each a
   personal access token. Either fine-grained, for the projects' repositories, with
   *Contents: read and write* and *Pull requests: read and write*; or classic, with the `repo`
   scope (plus `workflow` if changes may touch `.github/workflows`). With organization SSO,
   authorize the token for the organization. Branches are pushed and PRs
   opened as you, and commits are authored with your GitHub name and email.
3. **Claude**: one or more accounts, each with a label. Run `claude setup-token` in a terminal
   (install Claude Code first if needed: `curl -fsSL https://claude.ai/install.sh | bash`). Your
   browser opens; sign in to the Claude account you want and press **Authorize**, then paste the
   `sk-ant-oat…` token it prints. An Anthropic Console API key works too. Each Claude account has
   its own config dir on the server, so personal and office sessions never mix. (There is no
   "Sign in with Claude" button like Jira's: Anthropic offers no OAuth app registration for
   third-party tools.)
4. **Projects**: join the ones on your Jira sites, choosing the GitHub and Claude account for each
   (and the Jira account, if you have two on that site), optionally a base branch per repo, and
   *Bug fixes only* or *Bug fixes and tasks*. Not there yet? **Add a project**:
   - a name, its Jira site and Jira project keys (`MAR, MARS`)
   - its repos: paste `owner/name` or the GitHub URL. The folder name defaults to the repo name
     and the description for Claude to GitHub's. Optionally a `setup` command (`npm ci`) and a
     `test` command that writes JUnit XML into `$SHADOWDEV_REPORT_DIR` (see *Test gate*)
   - optionally the Jira statuses to move issues through, and a root cause custom field

   You own the projects you add: only you can change or delete them, and you join them
   automatically. Your page shows each one's **Jira webhook** URL and secret, for instant runs.

Saving checks each step: the GitHub and Claude tokens are tried, the GitHub account must be able
to push to every repo of the project, your Jira account must be able to see its Jira projects,
and base branches must exist. New assignments then run automatically. **Run issues now** starts
issues that were assigned before you joined: enter one or more keys (and the project, when the
same key is in two of yours), and choose whether several are fixed separately or together on one
branch. An optional **note for Claude** says what the tickets don't (where to look, what to
leave alone); it is given to Claude with the issues, and is not posted to Jira or GitHub. A line starting
with a key (`MAR-123: ...`) begins a note for that issue only; the lines before those are for all.
**Recent runs** shows what happened, and the ticket gets comments with the analysis and
the PRs.

- **Stop** (next to a queued or running run in **Recent runs**) stops that run: a queued one never
  starts; a running one stops at once (Claude and the test commands are killed, nothing more is
  pushed or written to Jira, and its code is deleted). The ticket is left as it was, so you can
  see how far the run got. A `git push` already under way finishes.
- **Pause automation** stops ShadowDev from working on your issues and keeps your settings:
  webhooks for your issues are ignored, queued runs are skipped, and runs can't be started. A run
  in progress finishes. **Resume automation** turns it back on.
- **Sign out** only ends your browser session; automation keeps running.
- **Removing an account** stops the runs that used it. Projects that ran with a removed GitHub or
  Claude account wait until you choose another one; projects joined with a removed Jira account
  are left.
- **Delete my ShadowDev account** stops your runs in progress (nothing more is pushed or written
  to Jira, and their code is deleted) and removes your accounts, tokens, settings and run history.
  Projects you own pass to their longest-standing member (or are deleted if nobody else is in
  them). To revoke
  access completely, also remove ShadowDev under *Connected apps* in your Atlassian account, delete
  your GitHub token, and revoke your Claude token.

## For the admin

### Projects

Developers add and edit projects on the web page (see *For developers*); nothing to restart. A
Jira project key belongs to one project per Jira site. **Upgrading from v1**: on the first start,
the database is upgraded in one transaction (a copy of the old one is kept as
`data/shadowdev.db.v1-backup`). Developers keep their accounts, tokens, settings and run history,
sign in again once, and each `projects/*.toml` becomes a project owned by its first subscriber,
with its own webhook secret. The old `/webhook` keeps working. After that, `projects/` can be
deleted, and so can `data/shadowdev.db.v1-backup` once all is well.

### Server: `.env`

See [`.env.example`](.env.example). `JIRA_SITES`: the Jira sites developers may sign in with and
add projects on, comma-separated, e.g. `https://office.atlassian.net,https://home.atlassian.net`
(`JIRA_BASE_URL`, v1's single site, still works). `*` allows any site, which lets anyone with an
Atlassian account use the server and its machine. `ATLASSIAN_CLIENT_ID`,
`ATLASSIAN_CLIENT_SECRET`, `PUBLIC_URL`, `SHADOWDEV_SECRET_KEY` (encrypts developers' tokens),
`WEBHOOK_SECRET` (optional: the shared `/webhook`; each project also has its own), `JIRA_POLLING`
(`true`: poll Jira every 15 minutes; off by default), `HOST`, `PORT`,
`MAX_FIX_ATTEMPTS`, `MAX_PARALLEL_RUNS` (default 5; mind the machine: parallel runs mean parallel
`npm ci` and test suites).

### Jira sign-in app (once)

Developers sign in through an OAuth 2.0 (3LO) app that the company registers once. Any Atlassian
account can create it, unless your organization's admin has blocked that.

1. Go to [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps/)
   → **Create** → **OAuth 2.0 integration**, named e.g. "ShadowDev".
2. **Permissions** → **Jira API** → **Add**, then **Configure** and add the classic scopes
   `read:jira-work`, `write:jira-work` and `read:jira-user`.
3. **Authorization** → **OAuth 2.0 (3LO)** → callback URL: `PUBLIC_URL/oauth/jira/callback`, e.g.
   `http://localhost:8080/oauth/jira/callback`, or `https://shadowdev.example.com/oauth/jira/callback`.
   It must match `PUBLIC_URL` exactly.
4. **Distribution** → **Edit** → **Sharing**, and fill in the vendor and personal data fields.
   Until then, only the app's owner can sign in. Sharing also lets one app serve several Jira
   sites, e.g. the office one and developers' personal ones.
5. **Settings**: copy the **Client ID** and **Secret** into `.env` as `ATLASSIAN_CLIENT_ID` and
   `ATLASSIAN_CLIENT_SECRET`.

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
own OAuth tokens, trying the next one if one is refused. [PRIVACY.md](PRIVACY.md) says what is
stored and where; its GitHub URL can be the app's privacy policy URL.

### Test gate: no new failures

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

## Safety model

What ShadowDev guards: every developer's GitHub, Jira and Claude tokens, the company's code, and
the server itself. What it treats as hostile: the text of every Jira issue (anyone who can write
or assign one), the code Claude writes from it, the dependencies `npm ci`/`uv` install, and the
internet in front of the web page and webhook.

**Claude** runs headless with `--restricted --strict-mcp-config` and only the
Read/Glob/Grep/Edit/Write tools, which confines it to its workspace (verified: it refuses paths
outside, also through symlinks) and stops it writing `.git` or settings. It cannot run commands.
On top of that it runs **inside the sandbox** below, with only its workspace and its own config
dir writable, and an environment holding only the developer's Claude token.

**The sandbox** ([bubblewrap](https://github.com/containers/bubblewrap)) runs the repos' setup and
test commands (code Claude wrote) and Claude. It is an allowlist:
- the system is read-only; `/tmp`, `/dev`, `/proc` are private; `/run` is empty, which hides the
  D-Bus and systemd user sockets (no `systemd-run` escape), the docker socket and SSH agents
- the home directory is empty, except the toolchains (read-only) and package caches (writable)
  listed in `SANDBOX_HOME_TOOLS` / `SANDBOX_HOME_CACHES`: no keys, browser profiles or other repos
- of ShadowDev, only the run's own folders are visible: not `.env`, not the token database, not
  other runs' code. Each clone's `.git` is read-only, so no hooks or git config can be planted
- the environment is an allowlist (`PATH`, `LANG`, `NVM_*`, …): no secrets
- each file written is capped at 2 GB, output kept in memory is capped, commands time out
- the network stays open (npm, uv, Redis on localhost). With nothing else visible, a run can only
  send out the code of repos its developer can read anyway

**Git** runs outside the sandbox, so every git command ignores hooks, `core.fsmonitor` and any
global or system git config, and gets an environment without secrets. The GitHub token reaches
git through a credential helper that reads it from that command's environment and answers only
for `https://github.com`: it never appears in a URL, argv or git config, and a rewritten URL
cannot send it elsewhere.

**Secrets** from `.env` are read into ShadowDev's config, never into its process environment, so
nothing it starts inherits them. Developers' tokens are encrypted in the database with
`SHADOWDEV_SECRET_KEY`, which protects copies of the database (backups); on the server itself the
key sits next to it, so protecting the server account is what protects the tokens (see the
deployment checklist). Each run uses only its assignee's tokens.

**No code is kept on the server.** It holds no clones. Each run clones only the base branch it
needs (shallow), with the developer's own token, so a developer can only work on repos they can
read. When the run ends (PR opened, tests failing, or a crash) its clones, baseline copies, test
reports and Claude's transcript (which holds the code Claude read) are deleted. Only results
remain: Jira comments, PRs, the run list. A dry run keeps its change for inspection until the
next one.

**The web page** uses Jira sign-in (OAuth with a one-time `state`), HttpOnly SameSite=Lax session
cookies, Secure (and HSTS) because `PUBLIC_URL` must be https (plain http only for localhost), CSRF
tokens, an Origin check, a strict Content-Security-Policy, a 20 s request timeout, and at most 64
connections handled at once. Connecting another Jira account is a CSRF-checked POST, and the OAuth
state says what it is for, so another site can't attach an account to yours; an Atlassian account
belongs to one ShadowDev account. Only sites in `JIRA_SITES` can sign in. Tokens are never shown
back. A developer can queue only issues assigned to them (to the Jira account they joined that
project with), and at most 10 runs at a time.

**Projects** are visible only to developers who can reach their Jira site, and editable only by
their owner, whose GitHub account must be able to push to every repo: the setup/test commands run
on every member's runs, so changing them needs the access that changing the repos' code needs.
Those commands also can't change what is committed (see *Test gate*).

**Webhooks** must carry a valid `X-Hub-Signature` HMAC with the project's own secret (or the shared
one; 401 otherwise), and a timestamp less than an hour old; each event is accepted once, also
across restarts (it is recorded in the database), so a captured one cannot be replayed. The issue is then
fetched again from Jira, as the assignee, before anything runs. A branch for the issue on GitHub
means it was handled, so repeat events do nothing. To run an issue again, close its PR and
delete its branch.

**What stays the reviewers' job.** Anyone who can write a ticket can steer what Claude writes, and
the PR is opened under the developer's name. The tests ran the change's own code, so their result
is a check, not proof. Protect `release/*` on GitHub so every PR needs an approving review, and
let CI, not ShadowDev, be the authority on tests. Prefer fine-grained GitHub tokens (only the
project repos) over classic `repo` tokens (every repo the developer can reach).

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
- [ ] Run it as a service (e.g. a systemd unit with `User=shadowdev` and `Restart=on-failure`) so
      it survives reboots; its logs then go to the journal
- [ ] Disk encryption, and OS updates (bubblewrap, git, the kernel)
- [ ] On GitHub: branch protection on `release/*` requiring an approving review
- [ ] Developers use fine-grained GitHub tokens limited to the project repos

## Requirements

- Linux, Python 3.11+, and `cryptography` (`pip install -e .`, or `uv run shadowdev`)
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), `prlimit` (util-linux), git, and
  the [Claude Code](https://claude.com/claude-code) CLI on `PATH`. Developers bring their own Claude login
- No clones and no GitHub access of its own: every clone, push and PR uses the developer's token
- What the repos' commands need: for the marketplace, Node from `.nvmrc` with npm, and `uv`
  (it fetches Python 3.12 itself)

## Setup

```bash
cp .env.example .env     # fill it in: JIRA_SITES, a new SHADOWDEV_SECRET_KEY, and the Jira sign-in app
pip install -e .         # once; installs cryptography
python3 -m shadowdev     # web page at PUBLIC_URL, Jira webhook at /webhook
```

To share it, expose the server, e.g. with a named Cloudflare tunnel, set `PUBLIC_URL` to its
https address, and add `PUBLIC_URL/oauth/jira/callback` as a callback URL in the Jira sign-in app.

Then, per project, in its Jira site: **Settings → System → WebHooks → Create a WebHook**. The
project's owner sees the values on their ShadowDev page:

- URL: `https://<your-tunnel>/webhook?project=<id>`
- Secret: the project's webhook secret
- Events: Issue **created** and Issue **updated**. Updated covers an issue that gets assigned later.
- JQL (optional, reduces noise): `project in (MAR) AND issuetype in (Bug, Task, Story, Sub-task)`

A per-project secret means a site's admin can only send events for that site's projects. The
shared `https://<your-tunnel>/webhook`, signed with `WEBHOOK_SECRET`, also works (one webhook
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

The change stays in `worktrees/<project id>-<KEY>-0/<repo>` (e.g. `worktrees/1-MAR-0/fe`) for you to inspect;
the command lists the changed files. The next dry run replaces it.

## Tests

```bash
python3 -m unittest -v
```

- `tests/test_pipeline.py` runs the whole pipeline against real local git repos, with the commands
  really running in the sandbox, and Jira, GitHub and Claude faked. It covers bugs and tasks, the
  developer's base branch and identity, the base-branch baseline, the self-heal retry, the give-up
  path, push permissions, repeat webhooks, untested repos, the default-branch fallback, and that
  commands can't see ShadowDev's secrets or change the code that is committed.
- `tests/test_server.py` drives the real HTTP server through Jira OAuth sign-in, connecting
  personal and office accounts, adding, joining and editing projects, and covers the state check,
  CSRF and Origin checks, webhook routing to projects, sites and developers, per-project webhook
  secrets, the run queue (parallel runs, turns, restarts, stopping, pausing, limits), and config.
- `tests/test_atlassian.py` covers the OAuth URLs and token exchange, and refreshing (once, with
  rotation) and revoked grants.
- `tests/test_store.py` covers the repositories: token encryption, several accounts per developer,
  projects per site, ownership passing on, sessions, account deletion, and the v1 upgrade.
- `tests/test_rules.py` covers release branches, fix versions, and issue types.
