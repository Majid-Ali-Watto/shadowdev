# ShadowDev

A team server that works on Jira bugs and tasks as the developer they are assigned to: Claude Code
analyses the issue, changes the project's repos, their tests run in a sandbox, and a PR is opened
with the developer's own GitHub token. A developer has any number of Jira, GitHub and Claude
accounts (e.g. office and personal); projects are rows in the database, each on one Jira site,
and each subscription (developer in a project) says which accounts it runs with. README.md
explains the product, setup and safety model; its **Architecture** section explains the layers.
This file covers what to know before editing.

## Commands

```bash
.venv/bin/python -m unittest            # all tests (~25 s); needs git, bwrap, prlimit on PATH
.venv/bin/python -m unittest tests.test_server.WebTest.test_stop_button   # one test
uv run ruff check                       # lint (config in pyproject.toml); `--fix` sorts imports and the like
.venv/bin/python -m shadowdev           # the server (needs .env, bwrap, claude)
.venv/bin/python -m shadowdev --reload  # the same, restarted whenever shadowdev/ or .env changes (development)
.venv/bin/python -m shadowdev dry-run --summary "..."   # real pipeline, nothing sent to Jira/GitHub
```

## Layout and layering

```
routes/ → services/ → repositories/ → SQLite
                    → clients/ (Jira, GitHub, Atlassian OAuth)
                    → automation/ (pipeline: git, Claude, sandbox)
models/ (dataclasses) and views/ (HTML) are used across layers.
app.py builds everything (build_app); server.py dispatches to routes.ROUTES[(method, path)].
```

- **routes/** do HTTP only: read the request, call one service, respond. Signed-in POST routes
  must be wrapped in `request.form_action` (Origin, session, CSRF). Page actions use
  `settings.action(notice_key, fn)`, so a `UserError` re-renders the page with its message.
- **services/** hold the rules. They take plain values (never a form or headers) and raise
  `services.errors.UserError` for anything the developer should see. Other exceptions are bugs.
- **repositories/** hold all SQL. One `Database` (connections, schema, versions, Fernet), and
  `UserRepository` (developers, their accounts, subscriptions), `ProjectRepository`,
  `SessionRepository`, `RunRepository` (runs, webhook events) on top of it. Open a new connection per
  call (`with closing(self.db.connect()) as conn, conn:`); the web threads and run workers share it.
- **models/**: `User` holds its `JiraAccount`s (with `Site`s), `GitHubAccount`s, `ClaudeAccount`s
  and `Subscription`s; `User.actor(project_id)` resolves the `Actor` a run acts as. The pipeline
  only ever sees an `Actor`, never a `User`.
- **automation/**: `pipeline.py` orders the steps, `prompts.py` is what Claude is asked and checks
  that its answers are usable, `verify.py` runs setup/test commands and compares them with the
  base-branch baseline, `agent.py` runs the `claude` CLI, `sandbox.py` builds the bwrap command,
  `gitops.py` runs git.

Keep to these boundaries when adding code; README "Adding things" lists where each kind of change goes.

## Rules

- **No new dependencies** without asking. It is deliberately stdlib-only (`http.server`, `sqlite3`,
  `urllib`) plus `cryptography`. No web framework, no ORM, no requests.
  Ruff is a development-only dependency (`uv sync` installs it); the server never imports it.
- Lint must pass (`uv run ruff check`). Silence a security finding only on its line, with the reason
  (`# noqa: S… (why)`), never for a whole file.
- **Security invariants. Never weaken these:**
  - Jira issue text, comments and attachments, Claude's output, and repo code are hostile. Attachments
    are only written to `_attachments/` in the workspace (allowlisted types, size-limited, safe
    names) for Claude to read, and are fetched without following redirects. Every command that runs repo code
    goes through `verify.run_command` (bwrap sandbox + `prlimit`); Claude runs sandboxed too.
  - CI output (check summaries, job logs) is hostile like issue text: it comes from running the PR's
    code and its dependencies. Job logs are fetched by following GitHub's redirect without the token.
  - A branch is deleted only when its PR was merged and nothing was pushed since (`pipeline.earlier_work`).
  - PR review comments are instructions only from people with access to the repo
    (`review.REVIEWERS`), and only for the PR's own code. A PR is reviewed, and approved, only when
    it is in a repo of the project whose Slack channel asked, and never the developer's own.
  - Secrets from `.env` live only in `Config`, never in `os.environ`, so child processes can't
    inherit them. Child environments are allowlists (`sandbox.env`, `gitops._env`).
  - Git ignores hooks, fsmonitor and global config (`gitops.SAFE`). The GitHub token reaches git
    only through the credential helper's environment, never in a URL, argv or config.
  - Tokens are stored encrypted (`db.encrypt`), and sessions are stored only as hashes.
  - Every HTML value goes through `views.components.esc`. Notices are fixed strings looked up by key.
  - Pages run no script but `views/static/*.js` inlined by `components.script` with the CSP nonce: no inline
    handlers (`onclick=`), no other script sources.
  - Webhooks need a valid HMAC signature and a fresh timestamp, and are accepted only once.
  - A run's code is deleted when it ends (`pipeline.remove_code`); only a dry run keeps its change.
  - A developer can only act on issues assigned to them (to the Jira account of their
    subscription), re-checked against Jira before running.
  - Only a project's owner edits it, and only while their GitHub account can push to all its repos;
    projects are visible only to developers with a Jira account on the project's site.
  - Setup/test commands must not change what is committed (`verify.check` compares
    `gitops.snapshot` before and after).
  - Atlassian's personal data reporting (`services/privacy.py`) must keep erasing closed accounts:
    any new place that stores Atlassian personal data must be cleared by `erase_closed_jira`.
  - Only sites in `JIRA_SITES` can sign in. Connecting another Jira account starts with a
    CSRF-checked POST, and an Atlassian account belongs to one developer.
- Every automation is a switch in `models.settings.Settings`, per subscription, checked where it acts
  (pipeline, review, services.reviews, webhook, poller) through `actor.settings`. A new automation
  gets a switch, a line in `views/projects.SWITCHES`, and a test with it off.
- Nothing posted to Jira, GitHub or Slack names ShadowDev, Claude, AI or a tool: it is the developer's
  own, in the first person (prompts end with `VOICE`). A run's Jira comments start with one of
  `models/issue.RUN_COMMENT_STARTS` and are recognised by that and their author; a new kind of comment
  gets its start there. `test_nothing_posted_anywhere_names_a_tool` guards this.
- Nothing project-specific in prompts or rules: a project's conventions come from its own files
  (`automation/guidance.py`), its commands, branches and statuses from the project's settings.
- Issue keys become branch and directory names, so validate them with `rules.ISSUE_KEY_RE`.
- Schema changes bump `database.VERSION` and upgrade the previous version in `Database.__init__`,
  in one transaction, like `migrations.upgrade_from_v1` (DDL included: it uses an explicit `BEGIN`).
- Log through `logging.getLogger("shadowdev")`. Every pipeline step is logged with the run's label.

## Tests

- `tests/test_pipeline.py` runs the real pipeline against local bare git repos with Claude, Jira
  and GitHub faked; `test_server.py` drives the real HTTP server; `test_store.py` covers the repositories.
- `tests/helpers.py` builds a config, developers with accounts (`add_developer`), projects
  (`add_project`) and subscriptions (`join`) in a real database.
- Mocks patch names **where they are used**, so moving a function means updating the patch target:
  `services.accounts.{GitHub, Jira, check_token}`, `services.projects.{GitHub, jira_client, gitops}`,
  `services.runs.jira_client`, `services.run_queue.{process_issues, jira_client}`,
  `services.privacy.{report_accounts, access_token}`, `services.poller.jira_client`,
  `automation.pipeline.{ClaudeSession, Slack}`, `automation.review.{ClaudeSession, Slack}`,
  `services.reviews.{GitHub, Slack, address_review, fix_ci, review_pr}`.
- `verify.BASELINES` caches baseline results across runs; tests clear it in `setUp`.
- `tests/__init__.py` points `tempfile` at one folder that is deleted when the tests end; keep using
  `tempfile.mkdtemp()` in tests, never a fixed path.
- Don't name a module under `shadowdev/` `test*.py`: unittest discovery would import it as a test.
- Run the full suite after any change. It is fast and covers the security checks.

## Don't touch

- `.env` and `data/` hold real secrets: the token database and per-developer Claude config. Don't
  read, print or commit them. `worktrees/` holds live run workspaces.
- Projects live in the database. `projects/*.toml` (v1) is read only by
  `migrations.upgrade_from_v1` when a v1 database is upgraded; this checkout's was imported and removed.
- `../market-place-fe` and `../market-place-services` are the repos ShadowDev works *on*, not part of it.
