# User guide

[README](../README.md) · [Features](../FEATURES.md) · [Install](../INSTALL.md) · [User guide](USER-GUIDE.md) · [Admin](ADMIN.md) · [Safety](SAFETY.md) · [Architecture](ARCHITECTURE.md)

For developers: connecting accounts, joining projects, and what a run does. The short version is [FEATURES.md](../FEATURES.md).

## Getting started

Open the ShadowDev page and:

1. **Sign in with Jira**: click the button, and on the Atlassian page choose your site and press
   **Accept**. Issues assigned to you are yours. ShadowDev reads them and comments as you. Access
   renews itself; if you revoke it in your Atlassian account settings, sign in again. Another
   Atlassian account (e.g. your personal one): **Connect another Jira account** on your page.
   Don't sign in with it separately, or it becomes a second ShadowDev account.
2. **GitHub**: one or more accounts, each with a label (e.g. *office*, *personal*), each a
   personal access token. Either fine-grained, for the projects' repositories, with
   *Contents: read and write* and *Pull requests: read and write* (plus *Checks*, *Commit
   statuses* and *Actions: read*, for ShadowDev to fix failing CI on its PRs); or classic, with the `repo`
   scope, which covers all of that, CI included (plus `workflow` if changes may touch `.github/workflows`). With organization SSO,
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
     `test` command that writes JUnit XML into `$SHADOWDEV_REPORT_DIR` (see [Test gate](ADMIN.md#test-gate-no-new-failures))
   - optionally the Jira statuses to move issues through, and a root cause custom field

   You own the projects you add: only you can change or delete them, and you join them
   automatically. Your page shows each one's **Jira webhook** URL and secret, for instant runs.
   Leaving a project you own hands it to its longest-standing other member, who then sees the webhook
   secret and can change it; if nobody else is in it, delete it instead.

Saving checks each step: the GitHub and Claude tokens are tried, the GitHub account must be able
to push to every repo of the project, your Jira account must be able to see its Jira projects,
and base branches must exist. New assignments then run automatically. **Run issues now** starts
issues that were assigned before you joined: enter one or more keys, or ranges (`MAR-1 to MAR-10`,
also `MAR-1..MAR-10` or `MAR-1 to 10`; of a range, the issues not assigned to you, not found, or of
a type you don't automate are skipped and listed as one skipped run). Leave issues out with
`MAR-1 to MAR-10 except MAR-3, MAR-7` (also `skip`, `without`, `not`; everything after the word is
left out, ranges too), or by marking them anywhere: `MAR-1 to MAR-10, !MAR-3, -MAR-7`. Leaving out
a key that isn't in the list is an error, so a typo doesn't go unnoticed (and the project, when the
same key is in two of yours), and choose whether several are fixed separately or together on one
branch. An optional **note for Claude** says what the tickets don't (where to look, what to
leave alone); it is given to Claude with the issues, and is not posted to Jira or GitHub. A line starting
with a key (`MAR-123: ...`) begins a note for that issue only; the lines before those are for all.
**Overview**, where your page opens once you are set up, is your dashboard (the sections are in the
sidebar on the left; on a phone, in a row along the top):

- **Needs you**: runs that ask you questions (with **Answer**), runs that failed (with their **Log**),
  and accounts to mend (lost Jira access, a GitHub token that expires within a week). The sidebar's
  Overview shows how many. A run counts until the issue runs again, for 7 days
- **Numbers**: runs working now and queued, your PRs in review and how many need attention, runs done
  in the last 7 days (and failed, and asking you), and Claude's cost and time in them
- **Running now**: each running run's progress through its phases, and what queued ones wait for
- **Claude usage**: per Claude account, how much of its 5-hour and weekly limits is used, when each
  resets, and whether it is at its limit (amber from 90%)
- **PRs in review**: the PRs your runs opened, until they close, and how each stands as last read
  from GitHub: waiting for review, comments to answer, changes requested, CI failing, conflicts, or
  approved; *fixing* or *queued* while a run works on it
- **Waiting for your working hours**: per project, the PRs, comments and Slack replies held, and
  when your hours begin

It updates itself along with Recent runs.

**Live logs** follows your runs as they work, like a terminal: what Claude reads, says and changes,
and the commands and their results, each line marked with its run. It shows only your own runs
(the server's own log, with everyone's, stays in the server's terminal: `make logs`). **Pause** stops
it, **Clear** empties it; scrolled up, it stops following until you scroll back down. A finished
run's whole log stays under Recent runs.

Deleting, removing, stopping, leaving, saving changes and signing out ask first, in a dialog that
says what follows.

**Recent runs** shows what happened, with the model and your note. It updates itself as runs
change (the server tells the page), and a running run shows a progress bar of its phases (checking,
cloning, analysing, changing, testing, publishing) with the step it is on, and a link to its log so far;
and the ticket gets comments with the analysis and the PRs. Your latest 200 finished runs are kept.
Search them by issue key, message or note, narrow them under **Filters** by status, project, and
type (Jira issues or pull requests), and page through them 20 at a time; the search and filters are
in the address, so a filtered list can be bookmarked or shared.
Tick finished runs (or the box in the header, for all) and press **Delete selected** to remove them from the list;
queued and running ones are stopped first, and tickets and PRs are never touched.

## How a run works

Accounts and projects, for common setups:

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
   which picks the model for the rest of the run: Haiku or Sonnet, both at high effort, or the Claude
   account's default model at its own recommended effort. An issue that can't be rated runs with the default. When the model
   changes nothing, or its change fails the tests, the next stronger one continues the same
   conversation. Claude is given the issue's summary and description, people's latest comments,
   and its attached screenshots, logs and documents (up to 10 files of 10 MB per issue, kept
   beside the clones in the workspace, so they are never committed)
5. has Claude analyse the workspace (read-only) and decide which repo(s) to change. For a bug it
   writes a concise **root cause** (2–5 sentences: what is wrong, where, and why it causes the
   reported behaviour) to the ticket (its root cause field, or a comment when the project has
   none), and, in a project that uses fix versions, sets the **fix version** by the project's
   choice: the next release (the lowest unreleased numbered version, the default), the highest,
   or the one numbered like the PR's base branch (`release/15` → `Rel 15`), replacing any other.
   For a task it comments a concise **implementation plan**. What counts as a bug or a task is
   the project's: its owner names the Jira issue types of each (by default *Bug*, and *Task,
   Story, Sub-task*), e.g. *Bug, Incident* for an operations team. A
   placeholder, over-long or garbled answer is sent back to Claude. What the issue leaves open
   goes one of three ways:
   - what the code, the repo's instructions or its conventions settle, Claude **decides** itself;
     the PR lists those decisions as **Assumptions**, for the reviewers to check
   - what only the people who asked for it can say (expected behaviour, missing steps) is asked
     of the **reporter**, in a ticket comment mentioning them, about behaviour, not code. The run
     ends as **waiting** and starts again once someone comments (or when you run it with a note,
     which answers for them). At most two rounds per issue; after that Claude works with what it has
   - a **costly technical decision** the code can't settle (a public API, stored data, two valid
     designs, contradicting instructions) is asked of **you**, never on the ticket: in the run's
     message on your page, and as the project's *Tell me* switches say (Slack, email). The run ends as **question**, and
     waits for you: its **Answer** button fills in **Run issues now** with the issues, the questions,
     and a note line per issue for your answers. The next run gives Claude the questions with your
     note, and doesn't stop to ask you again. Nothing of the stopped run is kept but the questions:
     it stops during the read-only analysis, before any change, and its clones and Claude session
     are deleted like any run's, so the next run starts afresh

   Either way the ticket goes back to the status it had
6. has Claude make the change: it edits files and creates new ones, like Claude Code does. For a
   bug, that includes a **regression test** that reproduces it
7. for each repo that changed, runs its `setup` (once) and `test` commands in a sandbox. New test
   failures go back to Claude, which tries again (up to the developer's **Fix attempts**, 3 by default, in Settings).
   A repo without a test command is not tested, and its PR says so. For a bug, the test files
   Claude names as its regression tests are then run on the untouched base branch, without the
   fix, where they must fail. If they pass there too, they go back to Claude once; the PR says
   which test proves the fix, or that none does
8. per changed repo: commits as the developer, pushes with their GitHub token, and opens a PR
   **into the base branch**. The PR links are commented on the ticket as a table (repo folder name, base branch, PR link), and
   the ticket moves to **In Review**

A run that **fails before anything is pushed** moves the ticket back to the status it had. If a
branch was pushed but its **PR could not be opened**, the ticket stays **In Progress**, and the
run and a ticket comment give the link to open the PR by hand.

An issue that already has its ShadowDev branch is skipped, unless it was **sent back**: it is
neither in review nor done, and someone commented after the comment listing its PRs (saying what
is still wrong). Then it is worked on again, and Claude is told about the earlier change. A repo
whose PR is still open continues on its branch, and the push updates that PR (with a comment
saying what changed). A repo whose PR was merged, with nothing pushed since, has its merged
branch deleted and starts afresh from the base branch with a new PR. Any other branch is
continued, and a PR opened for it.

```
Jira webhook → verify signature (the project's secret) → project: by URL, or by site + key (MAR-123 → MAR)
               assignee's Jira account joined that project, automates this issue type? ── no → ignore
                                         │ yes
                                         ▼
          queue → a worker (the developer's Runs at the same time, 5) → pipeline, as the assignee, with that project's accounts
```

**What starts by itself.** Webhooks and polling start only issues assigned to you that meet the
project's *Start by itself only when* conditions (under *Automation*): in one of its statuses (by
default *To Do, Open, Backlog, Selected for Development, Reopened*; empty for any), and in an **active sprint** (started, not
ended; on by default). An issue in no sprint, in a future sprint or in an ended one waits. A
project whose owner unticked **Uses sprints** (Kanban, an operations team) has no sprint condition:
its issues start on their status alone. Starting
a sprint changes none of its issues, so no webhook comes for it: polling asks for the issues in open
sprints too, and starts those never run before. With webhooks alone, an issue already in a sprint
when the sprint starts waits for its next update or for **Run issues now**. Why an issue was not
started is written to the server's log. **Run issues now** starts any issue of yours, whatever its
status or sprint.

**Working hours.** Per project, under *Automation*, a developer can set working hours (from, until,
the days, and a time zone; an end before the start runs overnight). Runs work at any time, started
by hand or by themselves; outside the hours, only what other people see waits:

- a run's **PRs**: it pushes its branch, and its PRs are opened once the hours begin, with what
  follows them (the Slack review request, the PR links on its tickets and their review status).
  Until then the run says *its PRs, and the review request, wait for your working hours: …*, and
  its tickets stay In Progress. A later run of the same branch replaces what an earlier one left.
  What waits is kept in the database (`held`), so a restart loses nothing, and is sent once
  (services/outbox.py, every minute);
- **replies to reviewers in Slack** (*Review comments addressed. Kindly review this PR again*) wait
  the same way, and **reminders** are due once the hours begin;
- **comments on Jira tickets** (root cause or plan, questions to the reporter, failures) and **on
  GitHub PRs** (answers to review comments and resolving their threads, CI and conflict notes) are
  made once the hours begin, in the order they were held. The work itself (commits, pushes, the root
  cause field, the fix version, the ticket's status) happens at once;
- a **review you are asked for** in Slack is still done, and posted on GitHub as ever, but answered in
  the thread only with its reactions (👀, then how it went): no message.

Direct messages and emails to the developer (decisions a run needs, tickets not complete yet) are
never held. *Only push the branch* involves nobody else, so nothing of it waits.

New assignments are picked up three ways: the Jira **webhook** (instant, needs ShadowDev reachable
from Jira), **polling** (**Poll Jira** on the developer's Settings tab: every 15 minutes by default, for machines
Jira can't reach, like a laptop), and **Run issues now** on the page (always available). Polling asks Jira, as
each developer's Jira account in each project, for unresolved issues assigned to it and updated
since the last poll (the first time, the last polling interval); repeats are harmless, as with webhooks.

Finished tickets are moved along by the project's **status rules** (its owner sets them under *Jira
workflow and fields*, e.g. `Ready for Release -> Done: root cause, fix version, parent, original
estimate, logged time`; there are none until they do, since workflows differ). A rule moves a ticket assigned to the developer only once it passes
the rule's checks; root cause is checked on bugs only (the root cause field, or a comment giving it),
and a rule without checks moves tickets as they are. Each developer switches this on per project
(**Move complete tickets**, off by default), checked every 4, 8 (default), 12 or 24 hours, or
whenever they press **Check now** under the project's *Status rules*, which also shows what the last
check moved and what each ticket still misses. A ticket that misses something stays in its status,
and the developer is told, as the project's *Tell me* switches say (Slack, email), each such ticket and what
it misses: on every **Check now**, and on a scheduled check when something new is missing.

Each developer's runs work up to their **Runs at the same time** (Settings, default 5) at once, and
at most 20 in all on the server; more wait their turn, **developers taking turns**, so one developer's ten queued issues
don't hold up everyone else. Each project has its own queue too: its own **Runs at the same time**
(default 5) and a **Priority** (high, normal or low; under *Automation*). When all of a developer's
runs are taken, a free one goes to their waiting project with the highest priority, and projects of
the same priority take turns. Within a project, the most urgent issue by its Jira **priority** goes
first (Highest, Blocker, Critical; then High, Major; Medium; Low, Minor; Lowest, Trivial; a name of
its own counts as Medium), and of the same priority the oldest. A developer can join any number of projects, each with any of their
Jira, GitHub and Claude accounts; each project is its own queue. Runs still waiting when the server restarts are queued again. Each
run has its own clones. Runs on the same base commit share one baseline test result. Every step is logged in the server's terminal, including what Claude reads
and edits.

**Claude's usage limits.** A Claude subscription has a 5-hour and a weekly limit, and the `claude`
CLI reports how much of each is used with every request. ShadowDev keeps a run from being cut off
by them, so no work is paid for twice:

- once **97% of the 5-hour limit** is used, no new run starts: runs stay **queued**, saying why and
  when the limit resets, and start by themselves after the reset
- once **90% of the weekly limit** is used, runs start only when the week resets within 5 hours,
  and then only as many as fit in what is left (a run is taken to need 2% of the week, or as much
  as the largest run seen since the server started); otherwise they stay queued until the weekly reset
- a run **in progress** that a limit cuts off is not failed when the limit resets within 5 hours:
  its code and its Claude conversation are kept, it waits (its line under **Recent runs** says
  until when), and Claude then continues in the same conversation from where it stopped. A limit
  that resets later fails the run as before, and its code is deleted

**Switching Claude accounts.** With several Claude accounts connected, a project's **Switch Claude
account at its limit** (under *Automation*, off by default) lets its runs go on with another of
your accounts instead of waiting: a new run starts on the first of your accounts that is not at a
limit, and a run in progress moves there at once, taking its conversation along, so Claude
continues where it stopped. Only when all of them are at their limits does a run wait, as above.
The project's card says which account is **active** now, and the Accounts tab shows each account's
usage as of its last request. Off, a project only ever uses the account you chose for it: leave it
off where the code must not go to another account (office code to a personal subscription).

Queued and waiting runs can be stopped like any other. Usage is known from ShadowDev's own
requests, so what else the account is used for shows only with the next one; it is kept in memory,
so a run waiting in progress is lost if the server restarts (queued runs are queued again). A
Console API key reports no limits and is never held.

**What to change, and how review is asked for**, on **Run issues now**:

- **Repos to change**: a checkbox for each of the project's repos (the picked project's; before one
  is picked, each project's under its name). Tick the ones the fix may change: with several repos at
  least one, and a project of one repo has it ticked already. When Claude finds the fix needs another
  repo too, nothing is changed: the run ends as **question** and you are told (*Tell me*). Run it
  again with that repo ticked too, or as it is to keep to your choice; the PR then lists what was
  left out under **Assumptions**. With *Ask me technical questions* off you are not asked, and it
  keeps to your choice. Ticking every repo lets Claude decide, as do runs that start by themselves
  (webhook, polling).
- **Review request in Slack**: *One message for all issues* (the default) or *A message for each
  issue*. One message is posted once the last of the issues run separately has finished, with a
  section per repo (its folder name, with its reviewers) listing each issue and its PR; issues that
  failed or wait for an answer are left out. A single issue, issues fixed together, and runs that
  start by themselves ask for their own review, as before.

**Several issues at once**, from the web page: *separately, in parallel* gives each issue its own
branch and PR, worked on at the same time. *Separately, one after another* does the same in the
order given: each issue starts once the one before it has ended (however it ended), after the
**Minutes between issues** you give (0 to 240; 0 starts it at once). An issue waiting for its turn
is listed as queued, saying what it waits for, can be stopped, and the sequence goes on after a
server restart. *Together* fixes them in one Claude session on one branch
(`MAR-1_MAR-2`; three or more consecutive issues as a range,
`MAR-1-to-MAR-10`, so the name stays short and still says which issues it covers) with one PR per repo; each ticket still gets its own root cause, fix version,
status changes and PR comment. An issue fixed in a group counts as handled afterwards.

## What a run took

Under **Recent runs**, a finished run's details say how long it worked, Claude's turns and the cost
Claude reported (for a subscription, what that use would cost on the API), and link to its **Log**:
what the server logged while it worked (what Claude read, said and changed, the commands and their
results). The log is yours alone, and is deleted with the run. When Jira access of one of your
accounts was lost or a GitHub token expires within a week, **Overview** says so under *Needs you*.
The **Activity** tab lists sign-ins and changes to your accounts and projects.

## It is you, everywhere

Everything ShadowDev writes in Jira, GitHub and Slack is posted with your accounts, in your words:
first person, no tool named (Claude is told the same about what it writes: root causes, PR
descriptions, review comments, replies). Commits are authored with your GitHub name and email. To
know its own Jira comments again (to leave them out of what Claude reads, to see that it asked
something, or that a ticket came back), it looks for comments by your Jira account that start the
way its comments do (`models/issue.RUN_COMMENT_STARTS`); the same words from anyone else are
theirs. You remain the author of all of it, so you answer for it like for your own work.

## What is automated: per project, under *Automation*

Each project you join has its own switches, so one server can do everything for a work project
and, say, only reviews for a personal one. All but *Move complete tickets* and *Switch Claude account at its limit* are on by default.
Slack is only read and posted to where both are set up: you connected Slack, and the project has a Slack channel.

| | Off means |
|---|---|
| **Claude model** | *Chosen per issue* (Haiku, Sonnet or the account's default, by how hard it is), or always one model |
| **Runs at the same time** / **Priority** | of this project's runs (1 to 20, default 5), and which of your projects gets a free run first (default normal); see above |
| **After a change passes the tests** | *Open a pull request*, *a draft pull request* (no Slack review request), or *only push the branch* (you open the PR; the ticket stays In Progress and links to where to open it) |
| Start by itself | new assignments wait for **Run issues now**: webhooks and polling ignore them |
| Start by itself only when (statuses, in an active sprint: shown when the project uses sprints) | see *What starts by itself* above |
| Working hours (**empty** by default: any time) | nothing waits; see *Working hours* above |
| Ask the reporter | what only the reporter could say is decided by Claude, and listed in the PR's assumptions |
| Ask me technical questions | Claude makes the technical decisions too, and lists them in the PR's assumptions |
| Prove bug fixes | no regression test is asked for or run without the fix |
| Work again on reopened issues | an issue with its branch is always skipped |
| Switch Claude account at its limit (**off** by default) | runs wait for the reset of the project's own Claude account |
| Tell me: decisions a run needs / tickets not complete yet | each its own way: *In Slack* (the default), *By email*, *Slack and email*, or *Only on this page*. Slack needs Slack connected. Email needs `SMTP_HOST` on the server, and goes to the address under *Email me at*, else the email of the project's Jira account, else its GitHub account's (not GitHub's noreply address), else the one you signed in with. *Try it* sends a sample Slack message or email (made-up tickets with the project's keys), 3 of each per project, each a different kind, 30 seconds apart; the email ones count again for a new address. They follow the saved settings, so save first |
| Move the ticket | its status is never changed |
| Comment on the ticket | no plan (nor root cause, in a project without a root cause field), PR links, failures or questions (and so no questions, and no reopening: it is found by the PR links comment) |
| Fill the root cause field (shown when the project has one) | a bug's root cause field is left alone |
| Set the fix version (shown when the project uses fix versions) | the fix version is left alone |
| Move complete tickets (**off** by default) | tickets are moved by the status rules only when you press **Check now** |
| Ask for review in Slack | no review request is posted |
| Fix review comments / Fix failing CI | its PRs are left to you |
| Fix merge conflicts | a PR that conflicts with its base branch is left to you. On: the latest base is merged into the PR's branch (a merge, never a rebase or force push), Claude resolves only the conflicted files, the tests run, and the merge is pushed with a comment on the PR; at most once per pair of commits and 3 times per PR. Checked after review comments and before failing CI |
| Remind reviewers (and after how long: 1 to 24 hours, default 3) | a PR nobody has reviewed is not asked about again. On: once that long has passed since its review was asked for in Slack, with no review or comment from anyone but you, its reviewers are asked again in that thread, at most once a day and 3 times; PRs asked for together get one reply |
| Review when asked in Slack | mentions in the project's channel are not read |
| Review PRs of these repos (one per repo) | that repo's PRs are not reviewed for you, even when you are mentioned with them |
| Approve as me | a PR that needs no changes gets a comment saying so, and you approve it |
| Approve without review: *From branch* and *Into branch* (**empty** by default) | every PR you are asked about is reviewed |

## The projects' own instructions for AI

Projects keep instructions for AI tools, and each keeps different ones, so ShadowDev assumes none.
In every clone it finds the files committed for that (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, Cursor, Windsurf and Cline rules, path-scoped instructions such
as `.github/instructions/*.instructions.md`, nested `CLAUDE.md`s, and Claude's agents, commands and
skills, with their descriptions), and tells Claude to read and follow them for how code is written
there. ShadowDev's own instructions still decide the workflow: where a repo's instructions ask for
commands, branches, Jira or approvals, the pipeline does those steps. A review reads them from the
PR's base branch, so a PR cannot change how it is judged. They are text: a repo's
`.claude/settings*.json` (permissions, hooks) is never used, and Claude Code doesn't load it.

## Slack and reviews (optional)

Connect **Slack** on your page with your own user token: create a Slack app for yourself at
api.slack.com/apps, give it the *User Token Scopes* `chat:write`, `reactions:write`,
`channels:history` and `groups:history`, install it, and paste its *User OAuth Token* (`xoxp-…`). ShadowDev then posts and
reads in Slack as you. A project's owner sets its **Slack channel for reviews** (the channel's ID)
and each repo's **reviewers** (Slack member IDs, e.g. two for the frontend repo and two for the
backend one) in the project editor. Then, checked once a minute, with nothing having to reach
ShadowDev from outside:

- **Review requests.** When a run opens its PRs, it posts in the channel, as you:
  `Kindly review these PRs: MAR-123: …` with each PR and its repo's reviewers mentioned, ending with
  `Thanks`.
- **Review comments fix themselves.** When reviewers with access to the repo comment on a PR
  ShadowDev opened (and two minutes pass without another comment), a run makes the changes they
  ask for on the PR's branch, runs the tests as for any change, pushes, answers each thread and
  **resolves** the ones it addressed, and replies in the Slack thread asking the reviewers to look
  again. If the tests fail, nothing is pushed and the PR gets a comment saying so. A question is
  answered and left open. This needs no Slack.
- **Failing CI fixes itself.** When the CI checks of a PR ShadowDev opened fail (once every check
  on its latest commit has finished), a run reads the failed checks' output (the end of each GitHub
  Actions job's log, or the check's own summary), has Claude fix what in the PR makes them fail on
  its branch, runs the tests as for any change, pushes, and comments on the PR. A failure that is
  not the PR's (a flaky test, the CI machine) is left, and the PR says so. Review comments waiting
  on the PR go first. This needs no Slack.
- **Reviews for you.** When someone mentions you in the channel, or in the thread of one of its
  last 20 messages, with a link to a PR of the project (not your own, not from a fork), ShadowDev
  replies **Reviewing PR 830**, reads the diff and
  the code, and either posts a review with comments as you and replies **Comments added on PR 830**, or
  **approves the PR as you** and replies **Approved PR 830 ✅**. A message that asks several people
  about different PRs (`@a @b <fe PRs> @c @d <services PRs>`) gives each the PRs listed after their
  mention. The message that asked gets 👀 while its PRs are reviewed, then ✅ when all were approved,
  📝 when some got comments, or ⚠️ when a review failed. A PR is left alone when it is already
  approved (by anyone, you too), when another reviewer with access to the repo has commented on it
  or asked for changes, or when someone asked about it with you reacted 👀 on the message before
  you did: they are reviewing it. Asked later in the thread of a message that asked others ("@you can
  you review these?", no links), you review that message's PRs, unless one of those it asked has
  reacted 👀. Such a reply counts only when it mentions you and asks in words (can you review, please
  review, take a look, PTAL), and not when it says not to ("no need", "don't"). When someone in that thread mentions you
  again or writes "review again", it reviews again. Each message counts once: only the ones posted
  after the last one read are acted on, from when you connected Slack on.

- **Approval only.** Some PRs carry changes that were already reviewed, e.g. a release branch going
  into `main`. Under a project's *Automation → Approve without review*, give the PR's own branch
  (**From branch**, e.g. `release/*, hotfix/*`) and the branch it goes into (**Into branch**, e.g.
  `main`): names or patterns, separated by commas. A PR you are asked about (to review or to
  approve) whose two branches both match is **approved as you without being read**: nothing is
  said in Slack, and the message that asked gets ✅ once its PRs are approved. Everything else
  about reviews still holds: only the project's repos you review, never your own PR or one from a
  fork, not one already approved or being reviewed by someone else, and only with *Approve as me*
  on. Anyone who can push a branch with a matching name gets its PR approved this way, so choose
  branches that are protected on GitHub. Saving checks that each name or pattern matches a branch
  of the project's repos on GitHub. These approvals ask Claude nothing, so the Claude account's
  usage limits never hold them.

Each PR gets at most 5 rounds of fixes, 3 of CI fixes and 5 of reviews. These runs show under **Recent runs**
like any other, named after the PR (`acme/fe#12`), and can be stopped there.

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
