# Architecture

[README](../README.md) · [Features](../FEATURES.md) · [Install](../INSTALL.md) · [User guide](USER-GUIDE.md) · [Admin](ADMIN.md) · [Safety](SAFETY.md) · [Architecture](ARCHITECTURE.md)

For contributors: where code lives and where a change goes. Rules for editing are in [CLAUDE.md](../CLAUDE.md).

## Layout

```
ShadowDev/
├── shadowdev/
│   ├── __main__.py        python -m shadowdev: the server (--reload: restarted on changes), dry-run, backup, rotate-key
│   ├── reload.py          --reload: the server in a child process, restarted when ShadowDev's files or .env change
│   ├── config.py          .env settings
│   ├── app.py             wires repositories and services together
│   ├── server.py          HTTP server: dispatches each request to its route; stops gracefully on SIGTERM
│   ├── logfiles.py        data/logs/: a private log file per day, kept 5 days
│   ├── models/            plain dataclasses: User and its accounts and Limits, Subscription and its Settings, Actor, Project/Repo, Issue, Tokens, status rules (release.py), run search (run.py)
│   ├── repositories/      SQLite, one class per table group
│   │   ├── database.py    schema, connections, token encryption, key rotation, backups
│   │   ├── migrations.py  upgrading a v1 database (and its projects/*.toml) in one transaction
│   │   ├── users.py       developers, their Jira/GitHub/Claude accounts (encrypted), subscriptions, activity
│   │   ├── projects.py    projects: Jira site and keys, repos, workflow, webhook secret
│   │   ├── sessions.py    web sessions (hashed)
│   │   ├── runs.py        run history shown on the page (with each run's log and cost), accepted webhook events
│   │   ├── held.py        PRs, Slack replies and Jira/GitHub comments waiting for their developer's working hours
│   │   └── reviews.py     PRs in review: the ones ShadowDev opened, and the ones asked for in Slack
│   ├── services/          business logic; raise UserError for problems shown to the developer
│   │   ├── accounts.py    sign-in, connect/remove Jira, GitHub and Claude accounts, pause, delete
│   │   ├── projects.py    add/edit/delete projects, join/leave them
│   │   ├── runs.py        run issues by hand, stop a run
│   │   ├── run_queue.py   the pool of parallel runs, developers taking turns
│   │   ├── webhook.py     Jira webhook: signature, replay protection, matching the developer
│   │   ├── privacy.py     Atlassian's personal data reporting: erase closed accounts, refresh changed ones
│   │   ├── poller.py      Poll Jira (Settings): ask Jira for new assignments, each developer at their interval
│   │   ├── releases.py    move complete tickets by the project's status rules, tell the developer in Slack what others miss
│   │   ├── outbox.py      once a developer's working hours begin: open the PRs, post the replies and comments that waited
│   │   └── reviews.py     at each developer's interval: review comments and CI on its PRs (GitHub), review requests (Slack)
│   ├── routes/            HTTP only: parse the request, call a service, respond
│   │   ├── request.py     Request helpers, security headers, the signed-in form guard
│   │   ├── ratelimit.py   requests per client and minute, on the routes open to anyone
│   │   ├── auth.py        /login/jira, /jira/connect, OAuth callback, /logout
│   │   ├── settings.py    the developer's page and its account actions
│   │   ├── projects.py    the project editor, joining and leaving
│   │   └── webhook.py     /webhook, /health
│   ├── views/             HTML: one module per page or tab
│   │   ├── components.py  esc(), page(), and the pieces every page shares
│   │   ├── home.py        the developer's page: header, status, tabs, NOTICES
│   │   ├── runs.py        Run issues tab, and Recent runs tab (search, filters, pages)
│   │   ├── projects.py    Projects tab: projects to join, webhook help
│   │   ├── accounts.py    Accounts tab: Jira, GitHub, Claude, Slack
│   │   ├── settings.py    Settings tab: timing and limits, pause, delete account
│   │   ├── activity.py    Activity tab: sign-ins and changes to the account, by pages
│   │   ├── features.py    Features tab: what ShadowDev does (FEATURES.md, alike)
│   │   ├── project_editor.py  add or edit a project
│   │   ├── auth.py        sign in, connecting another Jira account
│   │   ├── assets/        logo.jpg (inlined as a data: URI by components.LOGO)
│   │   └── static/        style.css and the scripts (inlined with the page's CSP nonce); app.js moves
│   │                      between the signed-in pages and sends their forms without reloading
│   ├── clients/           atlassian.py (Jira OAuth sign-in), jira.py, github.py, slack.py, rest.py
│   └── automation/        working on one issue
│       ├── pipeline.py    analyse → change → test → PR
│       ├── review.py      address review comments and failing CI on its PRs; review a colleague's PR
│       ├── prompts.py     what Claude is asked, and checks that its answers are usable
│       ├── guidance.py    the instruction files each repo keeps for AI tools
│       ├── verify.py      sandboxed setup/test commands, JUnit results, base-branch baselines
│       ├── agent.py       headless Claude Code session, as the developer
│       ├── limits.py      the Claude account's usage limits: holding runs, waiting for a reset
│       ├── rules.py       release branches, fix versions, urgency by Jira priority
│       ├── gitops.py      git as the developer: list branches, clone, commit, push
│       ├── sandbox.py     bubblewrap sandbox for setup/test commands
│       ├── cancel.py      stopping a run
│       ├── runlog.py      what a run logged and what Claude reported it cost, kept with the run
│       └── dryrun.py      python -m shadowdev dry-run: a made-up issue, no Jira or GitHub
├── deploy/              the service on each system: service.sh (make service, make docker-service), the systemd unit, the Docker settings (deploy/README.md)
├── tests/
├── Makefile           short names for the commands: `make` lists them
├── Dockerfile             the server in a Linux container, for macOS, Windows and Linux
├── docker-compose.yml     how that container runs: data/ and .env mounted, what the sandbox needs
├── README.md              what ShadowDev is, and where each doc is
├── FEATURES.md            the short version (also the page's Features tab)
├── INSTALL.md             the first run, step by step, on each system
├── docs/                  user guide, admin guide, safety model, architecture (this file)
├── data/                  database, backups, logs and per-developer Claude config (created; never committed)
└── worktrees/             workspaces of running issues, deleted when each run ends
```

## Layers

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

## Adding things

- **A page action**: a function in the right `services/` class (raise `UserError` for anything the
  developer should read), then a route in `routes/settings.py` with `action("<notice key>", …)`, a
  message for it, with the tab its form is on, in `views/home.NOTICES`, and the form in that tab's
  `views/<tab>.py`.
- **A new area of the site**: a new `routes/<area>.py` with its own `ROUTES`, merged in
  `routes/__init__.py`. Wrap signed-in POST routes in `form_action`.
- **A column or table**: change `SCHEMA` in `repositories/database.py`, bump `VERSION`, and upgrade
  databases of the previous version in `Database.__init__` (like `migrations.upgrade_from_v1`,
  in one transaction). Encrypt anything secret with `db.encrypt`.
- **A setting**: a field on `Config` in `config.py`, read in `Config.from_env`, and a line in
  `.env.example`.
- **A pipeline step**: `automation/pipeline.py` for the order of steps, `prompts.py` for what Claude
  is asked, `verify.py` for anything that runs commands.

## Tests

```bash
make test                # .venv/bin/python -m unittest
make test T=tests.test_server.WebTest.test_stop_button   # one test
```

- `tests/test_pipeline.py` runs the whole pipeline against real local git repos, with the commands
  really running in the sandbox, and Jira, GitHub and Claude faked. It covers bugs and tasks, the
  developer's base branch and identity, the base-branch baseline, the self-heal retry, the give-up
  path, push permissions, repeat webhooks, untested repos, the default-branch fallback, and that
  commands can't see ShadowDev's secrets or change the code that is committed.
- `tests/test_server.py` drives the real HTTP server through Jira OAuth sign-in, connecting
  personal and office accounts, adding, joining and editing projects, and covers the state check,
  CSRF and Origin checks, webhook routing to projects, sites and developers, per-project webhook
  secrets, the run queue (parallel runs, turns, restarts, stopping, pausing, limits, graceful
  shutdown), run logs and costs, rate limits, /health, token warnings, recent activity, and config.
- `tests/test_atlassian.py` covers the OAuth URLs and token exchange, and refreshing (once, with
  rotation) and revoked grants.
- `tests/test_store.py` covers the repositories: token encryption, several accounts per developer,
  projects per site, ownership passing on, sessions, account deletion, the v1 upgrade, key
  rotation, backups, and erasing run logs with a Jira account.
- `tests/test_rules.py` covers release branches, fix versions, urgency and issue types.
- `tests/test_releases.py` covers the status rules: their checks (root cause on bugs only), moving
  complete tickets, the Slack message for incomplete ones, the schedule, and **Check now**.
