# Installing ShadowDev

A step-by-step guide to running ShadowDev for the first time, on Linux, macOS or Windows.

**Who does what.** One person (the admin) runs one ShadowDev server for the whole team; that is
what this guide is about. Developers then need only a browser, plus [Claude Code](https://claude.com/claude-code)
on their own computer once, to create their Claude token (step 4).

## 1. Choose how to run it

The safety of every run rests on a Linux sandbox (`bwrap`) and `prlimit`, which exist only on Linux.
On macOS and Windows, ShadowDev runs in a Linux container that brings them along.

| Your computer | How | Guide |
|---|---|---|
| **Linux server** (the team's, permanent) | directly, as a service | [2A](#2a-linux-directly), then [deploy/README.md](deploy/README.md) |
| **Linux desktop or laptop** | directly, or in Docker | [2A](#2a-linux-directly) or [2D](#2d-linux-with-docker) |
| **macOS** (Apple silicon or Intel) | in Docker | [2B](#2b-macos-docker-desktop) |
| **Windows 10/11** | in Docker, from WSL2 | [2C](#2c-windows-docker-desktop-and-wsl2) |

Whichever you pick, do step 2 first, then your section, then steps 3 to 5. Running directly and
running in Docker use the same `.env` and `data/`, so you can switch later without losing anything
(but never run both on one checkout at the same time).

## 2. Before you start (every system)

### Decide the address: `PUBLIC_URL`

This is where developers open ShadowDev, and Atlassian sends them back to it after sign-in.

- **To try it on your own computer:** `http://localhost:8080`. Nothing else is needed.
- **For the team:** an https address, e.g. `https://shadowdev.example.com`, through a reverse proxy
  or a Cloudflare tunnel (step 5). Plain http is refused for anything but localhost, because
  session cookies would travel unencrypted.

You can start with localhost and change it later (in `.env` and in the Jira sign-in app).

### Create the Jira sign-in app (once per company)

Developers sign in with Jira through an OAuth app that you register once. Any Atlassian account can
create it, unless your organization's admin has blocked that.

1. Open [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps/)
   → **Create** → **OAuth 2.0 integration**, and name it, e.g. "ShadowDev".
2. **Permissions** → **Jira API** → **Add**, then **Configure** and add the classic scopes
   `read:jira-work`, `write:jira-work` and `read:jira-user`.
3. **Authorization** → **OAuth 2.0 (3LO)** → callback URL: your `PUBLIC_URL` followed by
   `/oauth/jira/callback`, e.g. `http://localhost:8080/oauth/jira/callback`. It must match exactly.
4. **Distribution** → **Edit** → **Sharing**: fill in the vendor and personal data fields (answer
   *Does your app store personal data?* with **Yes**; [PRIVACY.md](PRIVACY.md) explains what is
   stored). Until you do, only you can sign in. Sharing also lets one app serve several Jira sites.
5. **Settings**: keep the **Client ID** and **Secret** at hand for `.env`.

### Get the code

```bash
git clone <this repository's URL> ShadowDev
cd ShadowDev
```

On Windows, do this inside WSL2 (section 2C), not in PowerShell.

### Write `.env`

```bash
cp .env.example .env
chmod 600 .env                                  # readable only by you: it holds secrets
openssl rand -base64 32 | tr '+/' '-_'          # prints a new SHADOWDEV_SECRET_KEY
```

Open `.env` in any editor and fill in:

| Setting | Value |
|---|---|
| `JIRA_SITES` | your Jira site, e.g. `https://acme.atlassian.net` (several: comma-separated) |
| `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET` | from the Jira sign-in app |
| `PUBLIC_URL` | the address you chose, e.g. `http://localhost:8080` |
| `SHADOWDEV_SECRET_KEY` | the key `openssl` printed |

Leave the rest as it is for now; [.env.example](.env.example) explains each line. **Keep a copy of the
key** in your password manager: it encrypts every developer's tokens, and without it they all have
to connect their accounts again.

## 2A. Linux, directly

For Debian, Ubuntu, Fedora, Arch and the like. Run it as a normal user, never as root.

1. **Install what it runs with.**

   ```bash
   sudo apt install git make bubblewrap util-linux python3        # Debian, Ubuntu
   sudo dnf install git make bubblewrap util-linux python3        # Fedora
   sudo pacman -S git make bubblewrap util-linux python           # Arch
   curl -LsSf https://astral.sh/uv/install.sh | sh                # uv (installs Python packages)
   curl -fsSL https://claude.ai/install.sh | bash                 # the claude CLI
   ```

   Python must be 3.11 or newer (`python3 --version`). Open a new terminal afterwards, so `uv` and
   `claude` are on `PATH`. Also install what your projects' setup and test commands need, e.g.
   Node through [nvm](https://github.com/nvm-sh/nvm) for a JavaScript project.
2. **Install ShadowDev** into `.venv`: `make install`.
3. **Start it**: `make up`. It checks that git, `claude`, `bwrap` and `prlimit` are there and names
   any that is missing. It is running when it prints `listening on http://127.0.0.1:8080`. Ctrl+C
   stops it.
4. Open `PUBLIC_URL` in your browser and continue with [step 3](#3-check-the-install).

`make upd` runs it in the background instead (`make logs` follows it, `make down` stops it), and
`make service` makes it a systemd service that starts on boot: do that on a server
([deploy/README.md](deploy/README.md)).

## 2B. macOS (Docker Desktop)

1. **Install Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/),
   choosing **Apple silicon** or **Intel** (Apple menu → About This Mac says which). Open it once
   and wait until it says it is running. In **Settings → Resources**, give it at least 4 GB of memory
   (runs install packages and run test suites in parallel).
2. **Install git and make**, if `git --version` doesn't answer already: `xcode-select --install`.
3. **Start it**, from the ShadowDev folder in Terminal:

   ```bash
   make docker-up
   ```

   The first time, building the image takes a few minutes. Then `make docker-logs` should show
   `listening on http://0.0.0.0:8080` (Ctrl+C leaves the log; the server keeps running).
4. Open `http://localhost:8080` and continue with [step 3](#3-check-the-install).

To keep it running, starting whenever you log in: `make docker-service` ([deploy/README.md](deploy/README.md)).

## 2C. Windows (Docker Desktop and WSL2)

Everything is typed in the Ubuntu terminal of WSL2, a Linux that runs inside Windows.

1. **Install WSL2 with Ubuntu.** Open PowerShell as administrator and run `wsl --install`, then
   restart Windows. Open **Ubuntu** from the Start menu and choose a user name and password.
2. **Install Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/).
   In its **Settings**: **General** → *Use the WSL 2 based engine* on; **Resources → WSL
   integration** → turn on **Ubuntu**; **Resources** → at least 4 GB of memory.
3. **In the Ubuntu terminal**, install git and make, and check that Docker answers:

   ```bash
   sudo apt update && sudo apt install -y git make openssl
   docker version
   ```

4. **Get the code into your Linux home** (`cd ~` first, then *Get the code* and *Write `.env`* from
   step 2). Not under `/mnt/c/`: the database and the run workspaces are much slower and less
   reliable on the Windows drive. To edit `.env` with a Windows editor, `explorer.exe .` opens the
   folder, or `code .env` with VS Code; `nano .env` works in the terminal.
5. **Start it**: `make docker-up`. Then `make docker-logs` should show `listening on
   http://0.0.0.0:8080`.
6. Open `http://localhost:8080` in your Windows browser (WSL2 forwards it) and continue with
   [step 3](#3-check-the-install).

To keep it running, starting whenever you sign in to Windows: `make docker-service`, in the Ubuntu
terminal ([deploy/README.md](deploy/README.md)).

WSL2 is also a Linux, so section 2A works there too, without Docker. Docker is the tested way.

## 2D. Linux with Docker

Nothing to install but Docker: no Python, `bwrap` or `claude` on the machine itself.

1. **Install Docker Engine and its compose plugin**: your distribution's packages (`docker.io` and
   `docker-compose-v2` on Ubuntu), or [Docker's own](https://docs.docker.com/engine/install/). Check
   with `docker compose version`. To run `docker` without `sudo`, add yourself to the `docker` group
   (`sudo usermod -aG docker $USER`, then log in again). That group is as powerful as root, so on a
   shared server keep it to the admins: the container itself runs as an unprivileged user.
2. **Start it**: `make docker-up`. `make docker-logs` should show `listening on http://0.0.0.0:8080`.
3. Open `PUBLIC_URL` and continue with [step 3](#3-check-the-install).

To keep it running, starting on boot: `make docker-service` ([deploy/README.md](deploy/README.md)).

## 3. Check the install

Optional, but it proves the sandbox works on this machine (about 2 minutes):

| Running | Command |
|---|---|
| directly (2A) | `make check` (lint and tests) |
| in Docker (2B, 2C, 2D) | `make docker-test` |

The end of the output says `OK`. `curl http://localhost:8080/health` answers `{"status": "ok"}`
while the server runs.

## 4. First sign-in (every developer, from any computer)

On the ShadowDev page:

1. **Sign in with Jira**, choose your site on the Atlassian page and press **Accept**.
2. **Accounts → GitHub**: paste a personal access token, with a label such as *office*. A
   fine-grained token for the projects' repositories with **Contents** and **Pull requests** set to
   read and write (add **Checks**, **Commit statuses** and **Actions** read, so failing CI gets
   fixed), or a classic token with the `repo` scope. With organization SSO, authorize it for the
   organization.
3. **Accounts → Claude**: on your own computer, install Claude Code and run `claude setup-token`:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash      # macOS, Linux, WSL
   irm https://claude.ai/install.ps1 | iex             # Windows PowerShell
   claude setup-token
   ```

   Your browser opens: sign in to the Claude account to use and press **Authorize**, then paste
   the `sk-ant-oat…` token it prints into the page. An Anthropic Console API key works too.
4. **Projects**: join a project, or **Add a project** with its Jira project keys and repos (paste
   `owner/name` or the GitHub URL), and choose which GitHub and Claude account it runs with.
5. **Try it without touching Jira or GitHub** (a dry run: the real pipeline on a made-up issue,
   nothing posted or pushed):

   | Running | Command |
   |---|---|
   | directly | `make dry S="Login button does nothing"` |
   | in Docker | `docker compose exec shadowdev python -m shadowdev dry-run --summary "Login button does nothing"` |

6. **Run a real issue**: on the **Run issues** tab, **Run issues now** with an issue assigned to you. Its
   row on the **Recent runs** tab shows its progress, and its PR link when it is done.

From then on, issues assigned to you in your projects are worked on as they come in. The
[User guide](docs/USER-GUIDE.md) explains the rest: notes for Claude, automation switches, Slack reviews.

## 5. Share it with the team

1. **Give it an https address.** On a server, a reverse proxy (Caddy, nginx) or a named
   [Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
   pointing at `http://127.0.0.1:8080`. Set `PUBLIC_URL` in `.env` to that address, and add
   `PUBLIC_URL/oauth/jira/callback` as a callback URL in the Jira sign-in app. Restart ShadowDev.
2. **Jira webhooks**, for runs that start the moment an issue is assigned (otherwise each developer
   turns on *Poll Jira* in Settings). Per project, in Jira: **Settings → System → WebHooks → Create
   a WebHook**, with the URL and secret its owner sees on the ShadowDev page, and the events
   *Issue created* and *Issue updated*.
3. **For a permanent server**, go through the [Deployment checklist](docs/ADMIN.md#deployment-checklist): a dedicated machine and
   user, backups, and `make service` or `make docker-service` so it starts by itself
   ([deploy/README.md](deploy/README.md)).

## Everyday commands

| To | Directly | In Docker |
|---|---|---|
| Start | `make up` (or `make upd`, background) | `make docker-up` |
| Keep it running (starts by itself) | `make service` | `make docker-service` |
| Stop (runs in progress finish first) | Ctrl+C, or `make down` | `make docker-down` |
| Follow the log | `make logs` | `make docker-logs` |
| Back up the database | `make backup` | `docker compose exec shadowdev python -m shadowdev backup` |
| Update to a new version | `git pull`, `make install`, start again | `git pull`, `make docker-up` |
| Run the tests | `make check` | `make docker-test` |
| A shell where ShadowDev runs | your terminal | `make docker-shell` |

`make` alone lists every command. Without make (PowerShell outside WSL2), the Docker commands are
`docker compose up -d --build`, `docker compose stop` and `docker compose logs -f`; create the
`data` folder first, or Docker creates it owned by root.

## When something goes wrong

| What you see | What to do |
|---|---|
| `Missing required environment variable: …` | fill that line in `.env` |
| `SHADOWDEV_SECRET_KEY is not a valid key` | generate it again with `openssl` (step 2) and paste the whole line |
| `bwrap is not installed or not on PATH` (or git, claude, prlimit) | install it (2A, step 1), or use Docker |
| `bwrap: No permissions to create new namespace` | directly: on Debian, `sudo sysctl kernel.unprivileged_userns_clone=1`; on Ubuntu 24.04 and later, keep the `apparmor` profiles package that allows bwrap. In Docker: start it with `make docker-up` or `docker compose`, which give bwrap what it needs; a plain `docker run` doesn't |
| `PUBLIC_URL must be https://` | use https, or `http://localhost:8080` while you try it |
| Atlassian says the redirect URI or callback is wrong | the app's callback URL must be exactly `PUBLIC_URL/oauth/jira/callback` |
| Only you can sign in | the Jira sign-in app's **Distribution** is not set to **Sharing** yet |
| *Address already in use* | something else uses port 8080: stop it, or change `PORT` (directly) or the port in `docker-compose.yml` (Docker) |
| `no .env` from `make docker-up` | step 2, *Write `.env`* |
| `.env` is a folder | Docker created it because the file was missing: remove the empty folder and write the file |
| `Permission denied` on `data/` | it belongs to another user (e.g. root, from an earlier `sudo`): `sudo chown -R $USER data` |
| The page doesn't open | `make logs` or `make docker-logs` says why; check `curl http://localhost:8080/health` |
| A run fails on its setup or test command | its **Log** (under Recent runs) shows the output. In Docker, a project that needs more than node, npm and uv needs its own image (deploy/README.md, *Docker*) |
