.PHONY: help install dev-up dev-down test lint typecheck fmt check docker-fmt docker-typecheck all docker-build docker-run docker-prod docker-test health

# Colors
BLUE  = \033[0;34m
GREEN = \033[0;32m
RED   = \033[0;31m
RESET = \033[0m

help:
	@echo "$(BLUE)Project SGR – Production Docker Platform$(RESET)"
	@echo ""
	@echo "$(GREEN)Development$(RESET)"
	@echo "  make install          Install dependencies"
	@echo "  make dev-up           Start dev infrastructure"
	@echo "  make dev-down         Stop dev infrastructure"
	@echo "  make api              Start API (hot reload)"
	@echo ""
	@echo "$(GREEN)Testing$(RESET)"
	@echo "  make test             Run full test suite"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests"
	@echo "  make test-crash       Run docker crash tests"
	@echo "  make test-cov         Run tests with coverage"
	@echo ""
	@echo "$(GREEN)Quality$(RESET)"
	@echo "  make lint             Run ruff linter"
	@echo "  make typecheck        Run mypy type checker"
	@echo "  make fmt              Format code"
	@echo "  make check            lint + typecheck + test (CI)"
	@echo ""
	@echo "$(GREEN)Docker$(RESET)"
	@echo "  make docker-build     Build production images"
	@echo "  make docker-dev       Build development images"
	@echo "  make docker-run       Run dev stack"
	@echo "  make docker-prod      Run production stack"
	@echo "  make docker-test      Run tests in container"
	@echo "  make docker-stop      Stop containers"
	@echo "  make docker-clean     Remove containers & volumes"
	@echo ""
	@echo "$(GREEN)Database$(RESET)"
	@echo "  make db-migrate       Run migrations"
	@echo "  make db-revision      Create migration"
	@echo "  make db-downgrade     Rollback migration"
	@echo ""
	@echo "$(GREEN)Monitoring$(RESET)"
	@echo "  make health           Check health endpoints"
	@echo "  make health-ready     Check readiness probe"
	@echo "  make health-trading   Check trading health"
	@echo ""

# ============================================================================
# Installation
# ============================================================================

install:
	pip install -e ".[dev,ml]"

dev-up:
	docker compose -f docker/docker-compose.yml up -d postgres redis prometheus grafana
	@echo "$(GREEN)✓ Infrastructure ready$(RESET)"
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

# ============================================================================
# Testing
# ============================================================================

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-crash:
	pytest tests/docker_crash_tests/ -v -m docker_crash

test-cov:
	pytest tests/ --cov=sgr --cov-report=html --cov-report=term-missing

# ============================================================================
# Quality Checks
# ============================================================================

lint:
	ruff check sgr/ tests/

fmt:
	ruff format sgr/ tests/
	ruff check --fix sgr/ tests/

typecheck:
	mypy sgr/ --strict

check: lint typecheck test
	@echo "$(GREEN)✓ All checks passed$(RESET)"

# ============================================================================
# Docker
# ============================================================================

docker-build:
	docker compose -f docker/docker-compose.prod.yml build --no-cache

docker-dev:
	docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml build

docker-run:
	docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d
	@echo "$(GREEN)✓ Dev stack running$(RESET)"
	@echo "  API:  http://localhost:8000"
	@echo "  Docs: http://localhost:8000/docs"

docker-prod:
	docker compose -f docker/docker-compose.prod.yml --env-file .env.prod up -d
	@echo "$(GREEN)✓ Production stack running$(RESET)"
	docker compose -f docker/docker-compose.prod.yml ps

docker-test:
	docker compose -f docker/docker-compose.yml exec api python -m pytest tests/ -v --tb=short

docker-stop:
	docker compose -f docker/docker-compose.prod.yml stop || true
	docker compose -f docker/docker-compose.yml stop || true

docker-clean:
	docker compose -f docker/docker-compose.prod.yml down -v || true
	docker compose -f docker/docker-compose.yml down -v || true

docker-logs:
	docker compose -f docker/docker-compose.yml logs -f api

docker-fmt:
	docker compose -f docker/docker-compose.yml exec -T api ruff format sgr/ tests/
	docker compose -f docker/docker-compose.yml exec -T api ruff check --fix sgr/ tests/

docker-typecheck:
	docker compose -f docker/docker-compose.yml exec -T api mypy sgr/ --strict

docker-scan:
	@echo "$(BLUE)Scanning Docker images for vulnerabilities...$(RESET)"
	trivy image sgr-api:latest || echo "Image not found, build first: make docker-build"
	trivy image sgr-worker:latest || echo "Image not found"

# ============================================================================
# Database
# ============================================================================

db-migrate:
	alembic upgrade head

db-revision:
	alembic revision --autogenerate -m "$(msg)"

db-downgrade:
	alembic downgrade -1

# ============================================================================
# Health Checks
# ============================================================================

health:
	@echo "$(BLUE)Health Checks:$(RESET)"
	@curl -s http://localhost:8000/health | python -m json.tool || echo "$(RED)✗ API not responding$(RESET)"

health-ready:
	@echo "$(BLUE)Readiness Probe:$(RESET)"
	@curl -s http://localhost:8000/health/ready | python -m json.tool

health-trading:
	@echo "$(BLUE)Trading Health:$(RESET)"
	@curl -s http://localhost:8000/health/trading | python -m json.tool

# ============================================================================
# All
# ============================================================================

all: check
	@echo "$(GREEN)✓ All tasks complete$(RESET)"
