#!/bin/bash
# SGR Docker Platform – Quick Start Script
# 
# Usage:
#   ./scripts/quickstart.sh dev      # Start development stack
#   ./scripts/quickstart.sh prod     # Start production stack
#   ./scripts/quickstart.sh test     # Run tests
#   ./scripts/quickstart.sh health   # Check health
#   ./scripts/quickstart.sh clean    # Clean up

set -e

BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
RESET='\033[0m'

echo_info() {
    echo -e "${BLUE}ℹ ${1}${RESET}"
}

echo_success() {
    echo -e "${GREEN}✓ ${1}${RESET}"
}

echo_error() {
    echo -e "${RED}✗ ${1}${RESET}"
}

# Check Docker
if ! command -v docker &> /dev/null; then
    echo_error "Docker is not installed. Please install Docker first."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo_error "Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

echo_success "Docker and Docker Compose installed"

# Command
CMD=${1:-help}

case $CMD in
    dev)
        echo_info "Starting development stack..."
        docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d
        echo_success "Development stack started"
        echo ""
        echo_info "Services:"
        echo "  API:  http://localhost:8000"
        echo "  Docs: http://localhost:8000/docs"
        echo "  Grafana: http://localhost:3001  (admin / sgr_grafana_dev)"
        ;;

    prod)
        echo_info "Starting production stack..."
        if [ ! -f .env.prod ]; then
            echo_error ".env.prod not found. Copy .env.prod.example and configure:"
            echo "  cp .env.prod.example .env.prod"
            exit 1
        fi
        docker compose -f docker/docker-compose.prod.yml --env-file .env.prod up -d
        echo_success "Production stack started"
        docker compose -f docker/docker-compose.prod.yml ps
        ;;

    test)
        echo_info "Running tests..."
        docker compose -f docker/docker-compose.yml exec api python -m pytest tests/ -v --tb=short
        echo_success "Tests completed"
        ;;

    crash-test)
        echo_info "Running crash scenario tests..."
        docker compose -f docker/docker-compose.yml exec api python -m pytest tests/docker_crash_tests/ -v -m docker_crash
        echo_success "Crash tests completed"
        ;;

    health)
        echo_info "Checking health endpoints..."
        echo ""
        echo_info "Liveness (process alive?):"
        curl -s http://localhost:8000/health/live | python -m json.tool || echo_error "Endpoint not responding"
        echo ""
        echo_info "Readiness (ready for traffic?):"
        curl -s http://localhost:8000/health/ready | python -m json.tool || echo_error "Endpoint not responding"
        echo ""
        echo_info "Trading Health (safe to trade?):"
        curl -s http://localhost:8000/health/trading | python -m json.tool || echo_error "Endpoint not responding"
        ;;

    stop)
        echo_info "Stopping containers..."
        docker compose -f docker/docker-compose.prod.yml stop 2>/dev/null || true
        docker compose -f docker/docker-compose.yml stop 2>/dev/null || true
        echo_success "Containers stopped"
        ;;

    clean)
        echo_info "Cleaning up containers and volumes..."
        docker compose -f docker/docker-compose.prod.yml down -v 2>/dev/null || true
        docker compose -f docker/docker-compose.yml down -v 2>/dev/null || true
        echo_success "Cleanup complete"
        ;;

    logs)
        echo_info "Showing API logs (Ctrl+C to exit)..."
        docker compose -f docker/docker-compose.yml logs -f api || \
        docker compose -f docker/docker-compose.prod.yml logs -f api
        ;;

    status)
        echo_info "Container status:"
        docker compose -f docker/docker-compose.prod.yml ps 2>/dev/null || \
        docker compose -f docker/docker-compose.yml ps 2>/dev/null || \
        echo_error "No containers running"
        ;;

    *)
        echo_info "SGR Docker Platform – Quick Start"
        echo ""
        echo "Usage: ./scripts/quickstart.sh [COMMAND]"
        echo ""
        echo "Commands:"
        echo "  dev        Start development stack (with hot reload)"
        echo "  prod       Start production stack"
        echo "  test       Run full test suite"
        echo "  crash-test Run crash scenario tests"
        echo "  health     Check health endpoints"
        echo "  status     Show container status"
        echo "  logs       Show API logs"
        echo "  stop       Stop containers"
        echo "  clean      Remove containers & volumes"
        echo ""
        echo "Examples:"
        echo "  ./scripts/quickstart.sh dev      # Start dev environment"
        echo "  ./scripts/quickstart.sh test     # Run tests"
        echo "  ./scripts/quickstart.sh health   # Check health"
        ;;
esac
