# Running ShadowDev as a service

ShadowDev keeps running by itself on every system, with one command:

| System | Command | What starts it |
|---|---|---|
| Linux, run directly | `make service` | systemd, on boot: [`shadowdev.service`](shadowdev.service) |
| macOS (Docker Desktop) | `make docker-service` | Docker Desktop, opened at login by a launchd agent |
| Windows (Docker Desktop, from WSL2) | `make docker-service` | Docker Desktop, opened at sign-in from the Startup folder |
| Linux with Docker | `make docker-service` | Docker, on boot (`docker.service`) |

[`service.sh`](service.sh) does the work for both commands, and `make service-remove` or
`make docker-service-remove` undoes them. Below, under *Docker*, is what the container settings in
[`docker-compose.yml`](../docker-compose.yml) are for. Installing for the first time: start with
[INSTALL.md](../INSTALL.md).

For development on your own machine you don't need it: `make up` (in the terminal) or `make dev`
(restarted on every change) is enough.

## What it gives you

Started by hand (`make up`, `make upd`), the server stops when you log out or the machine reboots.
Installed as a service, systemd looks after it:

| | |
|---|---|
| Starts on boot | once the network is up |
| Restarts after a crash | 10 seconds later |
| Runs as a dedicated user | `shadowdev`, not you or root (see the main README, *Deployment checklist*) |
| Stops gracefully | `systemctl stop` sends SIGTERM to the server only: runs in progress get up to 15 minutes to finish, queued runs start again with the server. Whatever is left after 16 minutes is killed |
| Private files | `UMask=0077`: everything it creates is readable only by its user |
| Logs | in the journal (`journalctl -u shadowdev`), and as always in `data/logs/` |

## Before installing

On the server, as the `shadowdev` user, ShadowDev must already start by hand:

1. Follow [INSTALL.md](../INSTALL.md), sections 2 and 2A: the Jira sign-in app created, `.env`
   filled in, `make install`.
2. Install that user's toolchains (the `claude` CLI, node, uv) and check that `make up` starts the
   server and the page opens. Then stop it (Ctrl+C).

## Installing

From the checkout, as a user who can `sudo` (or as root):

```bash
make service
```

It installs `/etc/systemd/system/shadowdev.service` from [`shadowdev.service`](shadowdev.service),
filled in for this checkout:

- `User=`: the owner of the checkout, so check it out as the user ShadowDev runs as
- `WorkingDirectory=` and `ExecStart=`: this checkout and its `.venv`
- `Environment=PATH=`: the folders holding that user's `claude`, `node` and `uv`, found through their
  login shell. A service does not read the shell profile, so a tool missing here makes the server
  refuse to start, naming the tool

It then starts the service and says whether it runs. Check it any time with `systemctl status
shadowdev` and `curl http://127.0.0.1:8080/health` (with your `HOST` and `PORT`). After moving the
checkout or installing a toolchain elsewhere, run `make service` again.

## Day to day

| To | Run |
|---|---|
| See whether it runs | `systemctl status shadowdev` |
| Follow its log | `journalctl -u shadowdev -f` |
| Stop it (runs finish first) | `systemctl stop shadowdev` |
| Stop the runs at once, while it is stopping | `systemctl kill --kill-whom=main shadowdev` (a second SIGTERM to the server) |
| Restart it after an update | `git pull`, `make install`, `systemctl restart shadowdev` |
| Not start it on boot any more | `systemctl disable shadowdev` |
| Remove the service | `make service-remove` |

After changing `shadowdev.service`, run `make service` again.

`make backup` works while the service runs. Schedule it, e.g. a daily cron job of the `shadowdev`
user (`cd /home/shadowdev/ShadowDev && make backup`), and copy the backups off the machine with the
key kept separately (see the main README, *Backups and the key*).

## Don't mix it with `make upd`

Use either the service or `make upd`/`make down` on one machine, not both: they would start two
servers on the same port, and the second refuses to start. `make down` only stops a server started
by `make upd`.

## macOS, Windows and Linux with Docker

`make docker-service`, from the ShadowDev folder (on Windows, in the Ubuntu terminal of WSL2), starts
ShadowDev in Docker like `make docker-up` and makes Docker itself start by itself:

- **macOS**: a launchd agent, `~/Library/LaunchAgents/dev.shadowdev.docker.plist`, opens Docker
  Desktop in the background at login.
- **Windows**: `ShadowDev.cmd` in your Startup folder opens Docker Desktop when you sign in.
- **Linux**: `docker.service` is enabled, so Docker starts on boot.

The container's `restart: unless-stopped` does the rest: ShadowDev starts whenever Docker does, and
again after a crash. `make docker-down` stops it until the next `make docker-up`, and
`make docker-service-remove` stops it and removes what `make docker-service` added (on Linux,
`docker.service` stays enabled, since other containers may need it).

Docker Desktop is a desktop app: on macOS and Windows, ShadowDev runs while you are signed in, and
pauses while the computer sleeps. For a team server that is always on, use Linux.

## Docker

[`Dockerfile`](../Dockerfile) and [`docker-compose.yml`](../docker-compose.yml) run ShadowDev in a
Linux container: the way to run it on macOS and Windows, and an alternative to the service on Linux
([INSTALL.md](../INSTALL.md), sections 2B to 2D). `restart: unless-stopped` gives what the service gives: it
starts with Docker and restarts after a crash.

What the container gets, and why:

| Setting | Why |
|---|---|
| A user of its own (`HOST_UID`/`HOST_GID`: yours) | not root; files in the mounted `data/` stay yours. The code in the image is root's, so the server can't change it |
| `.env` mounted read-only as a file | never `env_file`: secrets stay in `Config`, out of the process environment that commands inherit |
| `HOST=0.0.0.0` inside, published on `127.0.0.1:8080` | reachable from this machine only, as without Docker; put a proxy with https in front to share it |
| `seccomp`, `apparmor` and `systempaths` unconfined | what `bwrap` needs to build each run's sandbox inside the container: user namespaces, mounts, and a `/proc` of its own. Docker's default profiles refuse these to everything in a container |
| `cap_drop: ALL`, `no-new-privileges` | in exchange: the container has no Linux capabilities and can't gain any, so code that got out of a run's sandbox is still an unprivileged user in a container |
| `stop_grace_period: 16m`, tini as PID 1 | `docker compose stop` is graceful like `systemctl stop`: runs in progress get up to 15 minutes. A second `docker compose kill -s TERM shadowdev` while it stops stops them at once. tini reaps the processes runs leave behind |
| Volumes for `worktrees/` and the package caches | live run workspaces stay off the host's file sharing, which is slow on macOS and Windows |

Projects' setup and test commands run in this container (in the sandbox), so it has what they
usually need: node 22 with npm, and uv for Python. A project that needs more (another node version
through nvm, Java, a browser) gets an image of its own, `FROM shadowdev` with those added, named in
`docker-compose.yml`. Toolchains under the user's home are seen by the sandbox as on a host
(`SANDBOX_HOME_TOOLS`).

Use either Docker or the service on one machine, not both, for the same reason as below.
