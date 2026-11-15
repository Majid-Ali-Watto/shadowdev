# ShadowDev's commands. `make` or `make help` lists them.
PY := .venv/bin/python
# The process ID of the server started by `make upd`.
PID := data/shadowdev.pid

.DEFAULT_GOAL := help
.PHONY: help install up start upd down stop dev test lint fix check dry key rotate backup logs service service-remove \
	docker-up docker-down docker-logs docker-test docker-shell docker-service docker-service-remove

help:  ## list these commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

install:  ## create .venv with the dependencies (and ruff)
	uv sync

up:  ## start the server (needs .env, bwrap, claude)
	$(PY) -m shadowdev

start: up  ## the same as up

upd:  ## start the server detached (in the background); make logs follows it, make down stops it
	@mkdir -p -m 700 data
	@if [ -f $(PID) ] && kill -0 "$$(cat $(PID))" 2>/dev/null; then echo "already running (pid $$(cat $(PID)))"; exit 1; fi
	@setsid nohup $(PY) -m shadowdev >/dev/null 2>&1 & echo $$! > $(PID)
	@sleep 2; if kill -0 "$$(cat $(PID))" 2>/dev/null; then echo "started (pid $$(cat $(PID)))"; \
	else rm -f $(PID); echo "the server did not start; run 'make up' to see why"; exit 1; fi

down:  ## stop the detached server: runs in progress finish first (up to 15 min); run it again to stop them now
	@if [ ! -f $(PID) ] || ! kill -0 "$$(cat $(PID))" 2>/dev/null; then echo "no detached server is running"; rm -f $(PID); exit 0; fi; \
	pid=$$(cat $(PID)); kill -TERM "$$pid"; echo "stopping (pid $$pid): runs in progress finish first; Ctrl+C here and 'make down' again stops them now"; \
	while kill -0 "$$pid" 2>/dev/null; do sleep 1; done; rm -f $(PID); echo "stopped"

stop: down  ## the same as down

dev:  ## start the server, restarted whenever shadowdev/ or .env changes
	$(PY) -m shadowdev --reload

test:  ## run the tests; one of them: make test T=tests.test_server.WebTest.test_stop_button
	$(PY) -m unittest $(T)

lint:  ## check the code with ruff
	uv run ruff check

fix:  ## let ruff fix what it can (import order and the like)
	uv run ruff check --fix

check: lint test  ## lint, then the tests

dry:  ## run the pipeline on a made-up issue, nothing sent: make dry S="Login button does nothing"
	@test -n "$(S)" || { echo 'usage: make dry S="the issue title"'; exit 2; }
	$(PY) -m shadowdev dry-run --summary "$(S)"

key:  ## print a new SHADOWDEV_SECRET_KEY for .env
	@$(PY) -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

rotate:  ## re-encrypt stored tokens with a new key; first set SHADOWDEV_SECRET_KEY=new,old (see docs/ADMIN.md)
	$(PY) -m shadowdev rotate-key

backup:  ## copy the database to data/backups/ (kept 7 days), safe while the server runs
	$(PY) -m shadowdev backup

logs:  ## follow today's server log
	tail -f data/logs/shadowdev.log

service:  ## Linux: install the systemd service (starts on boot, restarts after a crash); asks for sudo
	@bash deploy/service.sh install native

service-remove:  ## stop and remove the systemd service (runs in progress finish first)
	@bash deploy/service.sh remove native

# Docker: the same server on Linux, macOS and Windows (deploy/README.md, Docker). The commands above
# that use $(PY) run inside it as: docker compose exec shadowdev python -m shadowdev <command>.
COMPOSE := HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose
# What bwrap needs inside a container, as docker-compose.yml gives it, for docker-test.
SANDBOX_OPTS := --cap-drop ALL --security-opt no-new-privileges --security-opt seccomp=unconfined \
	--security-opt apparmor=unconfined --security-opt systempaths=unconfined

docker-up:  ## build and start ShadowDev in Docker, detached (needs .env; data/ is created)
	@test -f .env || { echo "no .env: copy .env.example to .env and fill it in (INSTALL.md, step 2)"; exit 1; }
	@mkdir -p -m 700 data
	$(COMPOSE) up -d --build
	@echo "started: open the PUBLIC_URL of .env; make docker-logs follows the log"

docker-down:  ## stop it in Docker: runs in progress finish first (up to 15 min)
	$(COMPOSE) stop
	$(COMPOSE) down

docker-logs:  ## follow its log in Docker
	$(COMPOSE) logs -f

docker-test:  ## run the tests in the Docker image, with the sandbox as it runs there
	$(COMPOSE) build
	docker run --rm $(SANDBOX_OPTS) shadowdev python -m unittest $(T)

docker-service:  ## macOS, Windows (WSL2), Linux: start it in Docker, and again whenever Docker starts (at login or boot)
	@bash deploy/service.sh install docker

docker-service-remove:  ## undo docker-service, and stop it (runs in progress finish first)
	@bash deploy/service.sh remove docker

docker-shell:  ## a shell in the running container (dry runs, backup, rotate: python -m shadowdev ...)
	$(COMPOSE) exec shadowdev bash
