# Safety model

[README](../README.md) · [Features](../FEATURES.md) · [Install](../INSTALL.md) · [User guide](USER-GUIDE.md) · [Admin](ADMIN.md) · [Safety](SAFETY.md) · [Architecture](ARCHITECTURE.md)

What ShadowDev guards, what it treats as hostile, and how.

What ShadowDev guards: every developer's GitHub, Jira and Claude tokens, the company's code, and
the server itself. What it treats as hostile: the text of every Jira issue (anyone who can write
or assign one), the code Claude writes from it, the dependencies `npm ci`/`uv` install, and the
internet in front of the web page and webhook.

**Claude** runs headless with `--restricted --strict-mcp-config` and only the
Read/Glob/Grep/Edit/Write tools, which confines it to its workspace (verified: it refuses paths
outside, also through symlinks) and stops it writing `.git` or settings. It cannot run commands.
On top of that it runs **inside the sandbox** below, with only its workspace and its own config
dir writable, and an environment holding only the developer's Claude token. Its working directory
is the workspace that holds the clones, not a repo, so no repo's Claude settings or hooks load;
the repos' instruction files are pointed out to it as text to read.

**The sandbox** ([bubblewrap](https://github.com/containers/bubblewrap)) runs the repos' setup and
test commands (code Claude wrote) and Claude. It is an allowlist:
- the system is read-only; `/tmp`, `/dev`, `/proc` are private; `/run` is empty, which hides the
  D-Bus and systemd user sockets (no `systemd-run` escape), the docker socket and SSH agents
- the home directory is empty, except the toolchains (read-only) listed in `SANDBOX_HOME_TOOLS` and
  the package caches (writable) listed in `SANDBOX_HOME_CACHES`: no keys, browser profiles or other
  repos. The caches are **per project** (see Caches below)
- of ShadowDev, only the run's own folders are visible: not `.env`, not the token database, not
  other runs' code. A clone's git dir is read-only, at its own path and at the clone's `.git` (for
  tools that use git). ShadowDev never reads a `.git` inside a work tree, so one planted there
  counts for nothing (see Git below)
- the environment is an allowlist (`PATH`, `LANG`, `NVM_*`, …): no secrets
- each file written is capped at 2 GB, output kept in memory is capped, commands time out. The
  sandbox's `/tmp` and home are capped at 1 GB (`SANDBOX_TMP_MB`), where bwrap can size a tmpfs
- each command and Claude is capped at 4 GB of memory and 2048 processes (`SANDBOX_MEMORY_MB`,
  `SANDBOX_TASKS_MAX`), each in a systemd user scope of its own, so the caps count its processes and
  not the server's. Where systemd can't give a scope (Docker, a server without a user manager) the
  server says so once at startup and commands run without those caps; `prlimit --nproc` is not used,
  since it counts per user and would count the server's own threads
- the network stays open (npm, uv, Redis on localhost). With nothing else visible, a run can only
  send out the code of repos its developer can read anyway

**Caches** are shared by a project's runs, never between projects: a command of one project's run
can't write a package cache that another project's commands then execute. They live on the server
in `~/.cache/shadowdev-sandbox/<project id>/` (one folder per cache, such as `.npm`), so the
Docker image keeps them in its `.cache` volume. Members of one project still share its caches, as
they share its repos; a developer who can change the code can already change what runs.

**Git** runs outside the sandbox, so every git command ignores hooks, `core.fsmonitor` and any
global or system git config, and gets an environment without secrets. Each clone's git dir lives
in its run's own `<workspace>.git` folder, beside the workspace (`worktrees/<run>.git/<repo>`), so
no sandboxed command can write it. Every git command names its git dir and work tree explicitly,
so a `.git` planted in a work tree, a folder or a file, is never read. Before each git command the
work tree must still be the folder that was cloned (same device and inode): a sandboxed command that
moves a clone aside and plants a repository in its place stops the run, and nothing is committed
from it. The GitHub token reaches git through a credential helper that reads it from that command's
environment and answers only for `https://github.com`: it never appears in a URL, argv or git
config, and a rewritten URL cannot send it elsewhere.

**Secrets** from `.env` are read into ShadowDev's config, never into its process environment, so
nothing it starts inherits them. Developers' tokens are encrypted in the database with
`SHADOWDEV_SECRET_KEY`, which protects copies of the database (backups); on the server itself the
key sits next to it, so protecting the server account is what protects the tokens (see the
[deployment checklist](ADMIN.md#deployment-checklist)). Each run uses only its assignee's tokens.
What Claude writes for people is checked before it is posted: an answer that holds a token-shaped
string (`sk-ant-`, GitHub's `gh[pousr]_` and `github_pat_`, Slack's `xox[abpr]-`) or the developer's
own Claude token is unusable, and Claude is asked again without it.

**No code is kept on the server.** It holds no clones. Each run clones only the base branch it
needs (shallow), with the developer's own token, so a developer can only work on repos they can
read. When the run ends (PR opened, tests failing, or a crash) its clones, their git dirs, baseline
copies, test reports and Claude's transcript (which holds the code Claude read) are deleted. Only
results remain: Jira comments, PRs, the run list. A dry run keeps its change for inspection until the
next one.

**The web page** uses Jira sign-in (OAuth with a one-time `state`), HttpOnly SameSite=Lax session
cookies (a session lasts 7 days from when it was last used; the page refreshing its run list does
not count as use), Secure (and HSTS) because `PUBLIC_URL` must be https (plain http only for localhost), CSRF
tokens, an Origin check, a strict Content-Security-Policy, a 20 s request timeout, and at most 64
connections handled at once (the live updates of Recent runs hold one each: at most 16, 3 per developer;
a page refused one asks every few seconds instead). Connecting another Jira account is a CSRF-checked POST, and the OAuth
state says what it is for, so another site can't attach an account to yours; an Atlassian account
belongs to one ShadowDev account. Only sites in `JIRA_SITES` can sign in. Tokens are never shown
back. A developer can queue only issues assigned to them (to the Jira account they joined that
project with), and at most 10 runs of one project queued or running at a time.

**Projects** are visible only to developers who can reach their Jira site, and editable only by
their owner, whose GitHub account must be able to push to every repo: the setup/test commands run
on every member's runs, so changing them needs the access that changing the repos' code needs.
Those commands also can't change what is committed (see [Test gate](ADMIN.md#test-gate-no-new-failures)).
A test command that exits 0 but writes none of the JUnit reports the base branch's tests write fails
the change too: a suite that was skipped does not pass.

**Reviews** are pinned to what was read. An approval, or a review with comments, names the head
commit it was made on, so a push after it is not approved with it; and a PR whose branch moved
between reading its diff and cloning it is not reviewed at all: nothing is approved or commented on.

**Webhooks** must carry a valid `X-Hub-Signature` HMAC with the project's own secret (or the shared
one; 401 otherwise), and a timestamp less than an hour old; each event is accepted once, also
across restarts (it is recorded in the database), so a captured one cannot be replayed. The issue is then
fetched again from Jira, as the assignee, before anything runs. A branch for the issue on GitHub
means it was handled, so repeat events do nothing. To run an issue again, close its PR and
delete its branch. An issue whose last run **failed or was stopped** is not started again by a
webhook or by polling (ShadowDev's own comment about the failure updates the issue, which would
otherwise start it again each time): start it again with **Run issues now**.

**What stays the reviewers' job.** Anyone who can write a ticket can steer what Claude writes, and
the PR is opened under the developer's name. The tests ran the change's own code, so their result
is a check, not proof. Protect `release/*` on GitHub so every PR needs an approving review, and
let CI, not ShadowDev, be the authority on tests. Prefer fine-grained GitHub tokens (only the
project repos) over classic `repo` tokens (every repo the developer can reach).
