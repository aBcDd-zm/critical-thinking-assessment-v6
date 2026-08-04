SHELL := /bin/zsh
PYTHON := backend/.venv/bin/python
PIP := backend/.venv/bin/pip

.PHONY: setup setup-backend setup-frontend migrate test test-backend test-frontend test-e2e test-e2e-stack build start health stop check-stopped local-info admin-password-hash

setup: setup-backend setup-frontend

setup-backend:
	python3 -m venv backend/.venv
	$(PIP) install --upgrade pip
	$(PIP) install -r backend/requirements.txt

setup-frontend:
	cd frontend && npm install

migrate:
	cd backend && .venv/bin/alembic upgrade head

test: test-backend test-frontend build test-e2e test-e2e-stack

test-backend:
	cd backend && .venv/bin/python -m pytest -q

test-frontend:
	cd frontend && npm run test && npm run typecheck

test-e2e:
	cd frontend && npm run test:e2e

test-e2e-stack:
	cd frontend && npm run test:e2e:stack

build:
	cd frontend && npm run build

start:
	./scripts/start-local.sh

health:
	./scripts/check-local.sh running

stop:
	./scripts/stop-local.sh

check-stopped:
	./scripts/check-local.sh stopped

local-info:
	@print "思衡 V6 仅限本地实验：后端 8060，前端 5176；联调临时端口 8061 / 5177。"

admin-password-hash:
	cd backend && .venv/bin/python scripts/generate_admin_password_hash.py
