# Makefile for Meta Comment & DM Automation Bot

.PHONY: install lint format typecheck test test-all run worker migrate up down clean help

VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UVICORN ?= $(VENV)/bin/uvicorn
RUFF ?= $(VENV)/bin/ruff
MYPY ?= $(VENV)/bin/mypy
PYTEST ?= $(VENV)/bin/pytest
ALEMBIC ?= $(VENV)/bin/alembic

help:
	@echo "Available commands:"
	@echo "  make install    - Install project dependencies in virtualenv"
	@echo "  make lint       - Run ruff linter checks"
	@echo "  make format     - Format codebase with ruff"
	@echo "  make typecheck  - Run strict mypy type checking"
	@echo "  make test       - Run unit tests"
	@echo "  make test-all   - Run all tests including integration tests"
	@echo "  make run        - Start FastAPI ASGI development server"
	@echo "  make worker     - Run background worker process"
	@echo "  make migrate    - Apply database migrations to head"
	@echo "  make up         - Start docker-compose stack (api, worker, postgres, redis)"
	@echo "  make down       - Stop docker-compose stack"

install:
	@if [ -d "$(VENV)" ]; then \
		$(PYTHON) -m pip install -r requirements.txt; \
	else \
		python3 -m venv $(VENV) && $(PYTHON) -m pip install -r requirements.txt; \
	fi

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

typecheck:
	$(MYPY) --strict src tests

test:
	$(PYTEST) -v -m "not integration"

test-all:
	$(PYTEST) -v

run:
	$(UVICORN) meta_bot.api.app:create_app --factory --reload --port 8000

worker:
	$(PYTHON) -m meta_bot.workers.main

migrate:
	@echo "Migrations deferred for now; will be added later."

up:
	docker compose up -d --build

down:
	docker compose down

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
