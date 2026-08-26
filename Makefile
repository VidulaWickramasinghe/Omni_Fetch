PYTHON ?= python3
VENV ?= backend/.venv
VENV_PYTHON := $(VENV)/bin/python
VENV_RUFF := $(VENV)/bin/ruff
VENV_UVICORN := $(VENV)/bin/uvicorn

.PHONY: help install install-dev run test test-cov lint format format-check audit compose-up compose-down clean

help:
	@echo "OmniFetch developer commands"
	@echo "  make install       Install runtime dependencies into backend/.venv"
	@echo "  make install-dev   Install runtime and development dependencies"
	@echo "  make run           Run the API on http://127.0.0.1:8000"
	@echo "  make test          Run the test suite"
	@echo "  make test-cov      Run tests with branch coverage"
	@echo "  make lint          Run Ruff checks"
	@echo "  make format        Format Python sources and tests"
	@echo "  make audit         Audit installed Python dependencies"
	@echo "  make compose-up    Build and run the local-only containers"

$(VENV_PYTHON):
	$(PYTHON) -m venv $(VENV)

install: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r backend/requirements.txt

install-dev: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r backend/requirements-dev.txt

run:
	cd backend && ../$(VENV_UVICORN) app.main:app --host 127.0.0.1 --port 8000 --reload

test:
	PYTHONDONTWRITEBYTECODE=1 $(VENV_PYTHON) -m pytest

test-cov:
	PYTHONDONTWRITEBYTECODE=1 $(VENV_PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-report=xml

lint:
	$(VENV_RUFF) check backend/app backend/tests

format:
	$(VENV_RUFF) format backend/app backend/tests
	$(VENV_RUFF) check --fix backend/app backend/tests

format-check:
	$(VENV_RUFF) format --check backend/app backend/tests

audit:
	$(VENV_PYTHON) -m pip_audit -r backend/requirements.txt

compose-up:
	docker compose up --build --detach

compose-down:
	docker compose down

clean:
	$(PYTHON) -c 'from pathlib import Path; import shutil; [shutil.rmtree(p, ignore_errors=True) for p in (Path(".pytest_cache"), Path(".ruff_cache"), Path("htmlcov"))]; [p.unlink(missing_ok=True) for p in (Path(".coverage"), Path("coverage.xml"))]'

