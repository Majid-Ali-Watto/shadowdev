# ShadowDev's commands. `make` or `make help` lists them.
PY := .venv/bin/python
# The process ID of the server started by `make upd`.
PID := data/shadowdev.pid

.DEFAULT_GOAL := help
.PHONY: help install up start upd down stop dev test lint fix check dry key rotate backup logs

help:  ## list these commands
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

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

rotate:  ## re-encrypt stored tokens with a new key; first set SHADOWDEV_SECRET_KEY=new,old (see README)
	$(PY) -m shadowdev rotate-key

backup:  ## copy the database to data/backups/ (kept 7 days), safe while the server runs
	$(PY) -m shadowdev backup

logs:  ## follow today's server log
	tail -f data/logs/shadowdev.log
