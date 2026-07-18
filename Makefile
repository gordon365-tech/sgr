.PHONY: help install dev-up dev-down test lint typecheck fmt check docker-fmt docker-typecheck all

# Colors
BLUE  = \033[0;34m
GREEN = \033[0;32m
RESET = \033[0m

help:
	@echo "$(BLUE)Project SGR$(RESET)"
	@echo ""
	@echo "  $(GREEN)make install$(RESET)        Install dependencies"
	@echo "  $(GREEN)make dev-up$(RESET)         Start dev infrastructure (Postgres, Redis, Grafana)"
	@echo "  $(GREEN)make dev-down$(RESET)       Stop dev infrastructure"
	@echo "  $(GREEN)make test$(RESET)           Run full test suite"
	@echo "  $(GREEN)make lint$(RESET)           Run ruff linter"
	@echo "  $(GREEN)make typecheck$(RESET)      Run mypy type checker"
	@echo "  $(GREEN)make fmt$(RESET)            Format code"
	@echo "  $(GREEN)make check$(RESET)          lint + typecheck + test (CI equivalent)"
	@echo "  $(GREEN)make docker-fmt$(RESET)     Format code in container"
	@echo "  $(GREEN)make docker-typecheck$(RESET) Typecheck in container"
	@echo ""

install:
	pip install -e ".[dev,ml]"

dev-up:
	docker compose -f docker/docker-compose.yml up -d postgres redis prometheus grafana
	@echo "$(GREEN)Infrastructure ready$(RESET)"
	@echo "  Postgres:   localhost:5432"
	@echo "  Redis:      localhost:6379"
	@echo "  Prometheus: localhost:9090"
	@echo "  Grafana:    localhost:3001  (admin / sgr_grafana_dev)"

dev-down:
	docker compose -f docker/docker-compose.yml down

dev-down-volumes:
	docker compose -f docker/docker-compose.yml down -v

api:
	uvicorn sgr.api.main:app --host 0.0.0.0 --port 8000 --reload

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-cov:
	pytest tests/ --cov=sgr --cov-report=html --cov-report=term-missing

lint:
	ruff check sgr/ tests/

fmt:
	ruff format sgr/ tests/
	ruff check --fix sgr/ tests/

typecheck:
	mypy sgr/ --strict

check: lint typecheck test
	@echo "$(GREEN)All checks passed$(RESET)"

# ---------------------------------------------------------------------------
# Docker Commands
# ---------------------------------------------------------------------------

# Führt Ruff-Formatierung und automatische Fixes im Container aus
docker-fmt:
	docker compose -f docker/docker-compose.yml exec -T api ruff format sgr/ tests/
	docker compose -f docker/docker-compose.yml exec -T api ruff check --fix sgr/ tests/

# Führt den strict Typecheck im Container aus
docker-typecheck:
	docker compose -f docker/docker-compose.yml exec -T api mypy sgr/ --strict

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
db-migrate:
	alembic upgrade head

db-revision:
	alembic revision --autogenerate -m "$(msg)"

db-downgrade:
	alembic downgrade -1
