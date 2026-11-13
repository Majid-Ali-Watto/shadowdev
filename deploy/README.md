# Running ShadowDev as a service

This folder holds [`shadowdev.service`](shadowdev.service), a systemd unit that runs ShadowDev as a
permanent background service on a Linux server. ShadowDev itself never reads it; nothing changes
until you install it.

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

1. Follow *Setup* in the main [README](../README.md): `make install`, `.env` filled in, the Jira
   sign-in app created.
2. Install that user's toolchains (the `claude` CLI, node, uv) and check that `make up` starts the
   server and the page opens. Then stop it (Ctrl+C).

## Installing

1. Edit `shadowdev.service` to match the server:
   - `User=`: the user ShadowDev runs as
   - `WorkingDirectory=` and `ExecStart=`: where ShadowDev is checked out
   - `Environment=PATH=`: the folders holding that user's `claude`, `node` and `uv`. A service does
     not read the user's shell profile, so a tool missing here makes the server refuse to start,
     naming the tool
2. As root:

   ```bash
   cp deploy/shadowdev.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now shadowdev
   ```

3. Check that it is running: `systemctl status shadowdev`, and `curl http://127.0.0.1:8080/health`
   answers `{"status": "ok"}` (with your `HOST` and `PORT`).

## Day to day

| To | Run |
|---|---|
| See whether it runs | `systemctl status shadowdev` |
| Follow its log | `journalctl -u shadowdev -f` |
| Stop it (runs finish first) | `systemctl stop shadowdev` |
| Stop the runs at once, while it is stopping | `systemctl kill --kill-whom=main shadowdev` (a second SIGTERM to the server) |
| Restart it after an update | `git pull`, `make install`, `systemctl restart shadowdev` |
| Not start it on boot any more | `systemctl disable shadowdev` |

After changing `shadowdev.service`, copy it again and run `systemctl daemon-reload`.

`make backup` works while the service runs. Schedule it, e.g. a daily cron job of the `shadowdev`
user (`cd /home/shadowdev/ShadowDev && make backup`), and copy the backups off the machine with the
key kept separately (see the main README, *Backups and the key*).

## Don't mix it with `make upd`

Use either the service or `make upd`/`make down` on one machine, not both: they would start two
servers on the same port, and the second refuses to start. `make down` only stops a server started
by `make upd`.
