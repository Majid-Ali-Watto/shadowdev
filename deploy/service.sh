#!/usr/bin/env bash
# Installs ShadowDev as a service that starts by itself (deploy/README.md). Called by make:
#   make service / service-remove                 Linux, run directly: the systemd unit shadowdev.service
#   make docker-service / docker-service-remove   macOS, Windows (WSL2), Linux with Docker: Docker starts at
#                                                 login or boot, and compose's restart policy starts ShadowDev
# It never reads .env; it only checks that it is there.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNIT=/etc/systemd/system/shadowdev.service
AGENT="$HOME/Library/LaunchAgents/dev.shadowdev.docker.plist"  # macOS: opens Docker Desktop at login
STARTUP_FILE=ShadowDev.cmd                                     # Windows: the same, from the Startup folder

die() { echo "$*" >&2; exit 1; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

platform() {
  case "$(uname -s)" in
    Darwin) echo macos ;;
    Linux) if grep -qi microsoft /proc/version 2>/dev/null; then echo windows; else echo linux; fi ;;
    *) die "unsupported system: $(uname -s). On Windows, run this in the Ubuntu terminal of WSL2 (INSTALL.md, 2C)" ;;
  esac
}

# --- Linux, directly: systemd -----------------------------------------------------------------------

# The folders holding claude, node and uv for the user the service runs as, from their login shell
# (a service doesn't read the shell profile, so the unit's PATH must name them).
tool_path() {
  local owner=$1 shell found dirs="" d
  shell=$(getent passwd "$owner" | cut -d: -f7)
  [ -x "$shell" ] || shell=/bin/bash
  local probe='command -v claude; command -v node; command -v uv'
  if [ "$(id -un)" = "$owner" ]; then
    found=$("$shell" -l -i -c "$probe" </dev/null 2>/dev/null || true)
  else
    found=$(sudo -u "$owner" -H "$shell" -l -i -c "$probe" </dev/null 2>/dev/null || true)
  fi
  echo "$found" | grep -q '/claude$' || die "the claude CLI is not on $owner's PATH: install it as $owner (INSTALL.md, 2A)"
  for d in $(echo "$found" | grep '^/' | xargs -n1 dirname | awk '!seen[$0]++'); do dirs="$dirs$d:"; done
  echo "${dirs}/usr/local/bin:/usr/bin:/bin"
}

native_install() {
  [ "$(platform)" = linux ] || die "make service is for Linux run directly. Here, use: make docker-service"
  command -v systemctl >/dev/null || die "systemd is not running here: start ShadowDev with make upd instead"
  [ -f "$ROOT/.env" ] || die "no .env: write it first (INSTALL.md, step 2)"
  [ -x "$ROOT/.venv/bin/python" ] || die "no .venv: run make install first"
  if [ -f "$ROOT/data/shadowdev.pid" ] && kill -0 "$(cat "$ROOT/data/shadowdev.pid")" 2>/dev/null; then
    die "a server started by make upd is running: stop it with make down first"
  fi
  local owner path tmp
  owner=$(stat -c %U "$ROOT")
  [ "$owner" != root ] || die "$ROOT belongs to root: check it out as the user ShadowDev runs as (deploy/README.md)"
  path=$(tool_path "$owner")
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  sed -e "s|^User=.*|User=$owner|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
      -e "s|^ExecStart=.*|ExecStart=$ROOT/.venv/bin/python -m shadowdev|" \
      -e "s|^Environment=PATH=.*|Environment=PATH=$path|" \
      "$ROOT/deploy/shadowdev.service" > "$tmp"
  echo "installing $UNIT: user $owner, in $ROOT"
  echo "  PATH=$path"
  as_root install -m 644 "$tmp" "$UNIT"
  as_root systemctl daemon-reload
  as_root systemctl enable --now shadowdev
  sleep 3
  if systemctl is-active --quiet shadowdev; then
    echo "running, and starts on boot. systemctl status shadowdev, journalctl -u shadowdev -f (deploy/README.md)"
  else
    journalctl -u shadowdev -n 20 --no-pager 2>/dev/null || true
    die "it did not start: the log above says why"
  fi
}

native_remove() {
  [ -f "$UNIT" ] || { echo "the service is not installed"; return; }
  echo "stopping: runs in progress finish first (up to 15 minutes)"
  as_root systemctl disable --now shadowdev
  as_root rm -f "$UNIT"
  as_root systemctl daemon-reload
  echo "removed: start it by hand with make up or make upd"
}

# --- Docker: Docker starts by itself, and restart: unless-stopped starts ShadowDev with it ------------

windows_startup_dir() {
  local appdata
  appdata=$(cmd.exe /c 'echo %APPDATA%' 2>/dev/null | tr -d '\r') || true
  [ -n "$appdata" ] || die "can't reach Windows from WSL (cmd.exe): is WSL interop on?"
  echo "$(wslpath "$appdata")/Microsoft/Windows/Start Menu/Programs/Startup"
}

docker_install() {
  local os
  os=$(platform)
  command -v docker >/dev/null || die "docker is not installed (INSTALL.md, 2B to 2D)"
  case "$os" in
    macos)
      mkdir -p "$(dirname "$AGENT")"
      cat > "$AGENT" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Opens Docker Desktop in the background at login; ShadowDev's container starts with it. -->
<plist version="1.0"><dict>
  <key>Label</key><string>dev.shadowdev.docker</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-g</string><string>-a</string><string>Docker</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
      launchctl bootout "gui/$(id -u)/dev.shadowdev.docker" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$AGENT"
      echo "Docker Desktop now opens at login ($AGENT)"
      ;;
    windows)
      local exe="/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe" dir
      [ -f "$exe" ] || die "Docker Desktop is not in C:\\Program Files\\Docker: turn on Settings → General → Start Docker Desktop when you sign in instead"
      dir=$(windows_startup_dir)
      printf '@rem Opens Docker Desktop at sign-in; ShadowDev'"'"'s container starts with it (deploy/README.md).\r\n@start "" "%s"\r\n' \
        "$(wslpath -w "$exe")" > "$dir/$STARTUP_FILE"
      echo "Docker Desktop now opens when you sign in to Windows ($STARTUP_FILE in your Startup folder)"
      ;;
    linux)
      as_root systemctl enable docker.service >/dev/null
      echo "Docker now starts on boot (docker.service)"
      ;;
  esac
  make -C "$ROOT" --no-print-directory docker-up
  echo "ShadowDev starts whenever Docker does (restart: unless-stopped); make docker-service-remove undoes this"
}

docker_remove() {
  case "$(platform)" in
    macos)
      launchctl bootout "gui/$(id -u)/dev.shadowdev.docker" 2>/dev/null || true
      rm -f "$AGENT"
      ;;
    windows) rm -f "$(windows_startup_dir)/$STARTUP_FILE" ;;
    linux) echo "docker.service stays enabled: other containers may need it (sudo systemctl disable docker turns it off)" ;;
  esac
  echo "stopping: runs in progress finish first (up to 15 minutes)"
  make -C "$ROOT" --no-print-directory docker-down
  echo "removed: start it by hand with make docker-up"
}

case "${1:-} ${2:-}" in
  "install native") native_install ;;
  "remove native") native_remove ;;
  "install docker") docker_install ;;
  "remove docker") docker_remove ;;
  *) die "usage: deploy/service.sh install|remove native|docker (or make service, make docker-service)" ;;
esac
