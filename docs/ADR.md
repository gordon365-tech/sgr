# Project SGR – Architecture Decision Records

## ADR-001: Event-Driven Orchestration

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Decoupling, scalability, async-first

### Context
Multi-component trading system needs to coordinate signals → risk → execution → portfolio without tight coupling.

### Decision
Implement event-driven orchestration using Redis pub/sub as event bus:
- Strategy Engine publishes `SignalEvent`
- Trading Orchestrator subscribes, applies risk checks
- Execution Engine executes approved orders
- Portfolio Engine records state

### Consequences
- **Positive:** Loose coupling, easy to add new subscribers, natural async model
- **Negative:** Event ordering guarantees needed, eventual consistency model

### Alternatives Considered
1. Direct function calls (tight coupling, sequential)
2. Message queue (Kafka) – overkill for single-host dev

---

## ADR-002: Backtesting Architecture

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Validation before live trading, fast iteration

### Context
Must validate strategies against historical data before deployment to production.

### Decision
Implement separate backtesting engine with:
- Historical data loader (exchange API + CSV fallback)
- Event-driven simulator (same event model as live system)
- Performance analyzer (Sharpe, Sortino, Calmar ratios)
- Walk-Forward validation + Monte Carlo
- Go/No-Go report for trading approval

### Consequences
- **Positive:** High confidence in strategy viability, detects overfitting
- **Negative:** Historical data quality critical, simulator ≠ live execution perfectly

### Validation Gates
1. Backtesting: Min Sharpe >0.8, MaxDD <20%, PF >1.5
2. Walk-Forward: Consistency score >0.7
3. Monte Carlo: P95 Drawdown <25%, Ruin prob <5%

---

## ADR-003: Multi-Stage Docker Builds

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Image size, build speed, security

### Context
Python project with ML dependencies (PyTorch ~2GB) needs lean production images.

### Decision
- **Builder stage:** Full development environment, compile wheels, install all deps
- **Runtime stage:** Copy only `/usr/local` (deps) + source, remove build tools
- **Result:** ~800MB prod image vs ~2.5GB monolithic

### Consequences
- **Positive:** Faster deploys, less attack surface, repeatable builds
- **Negative:** Slightly longer build time (but cached well)

---

## ADR-004: Kubernetes over Docker Compose

**Status:** Accepted (Production)  
**Date:** 2024-08-20  
**Drivers:** Scalability, orchestration, HA

### Context
System needs HA, auto-scaling, rolling updates in production.

### Decision
- **Dev:** Docker Compose (hot reload, easy local testing)
- **Prod:** Kubernetes (replicas, HPA, network policies, ingress)
- K8s manifests in `/k8s/` with clear separation of concerns

### Consequences
- **Positive:** Industrial-grade deployment, automatic recovery
- **Negative:** Operational complexity, requires k8s knowledge

---

## ADR-005: TimescaleDB for Time-Series

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Query performance, OHLCV compression

### Context
Store millions of OHLCV candles efficiently and query time-ranges fast.

### Decision
Use PostgreSQL with TimescaleDB extension:
- Hypertables for automatic partitioning by time
- Compression: ~90% space savings
- Native time-series functions (time_bucket, etc.)

### Alternatives
- InfluxDB: Simpler but proprietary
- MongoDB: Better scaling but worse for structured queries

---

## ADR-006: Strategy Registry Pattern

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Extensibility, runtime activation

### Context
Need to support multiple strategies, enable/disable at runtime.

### Decision
- Strategies decorated with `@register_strategy("name")`
- Registry singleton holds all registered strategies
- Strategies activated/deactivated via REST API
- Backtesting & live system use same registry

### Consequences
- **Positive:** Easy to add new strategies, no config files
- **Negative:** Requires import-time registration

---

## ADR-007: Circuit Breaker for Exchanges

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Resilience to outages

### Context
Exchange API can be slow or down; need to fail fast and recover.

### Decision
Implement Circuit Breaker pattern per exchange:
- **CLOSED:** Normal operation
- **OPEN:** Exchange failed N times, reject requests for 60s
- **HALF_OPEN:** Test recovery, allow one request

### Consequences
- **Positive:** Prevents cascading failures, auto-recovery
- **Negative:** Briefly loses orders during recovery window

---

## ADR-008: WebSocket Live Dashboard

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Real-time visibility

### Context
Traders need live portfolio, risk, strategy metrics without polling.

### Decision
- FastAPI WebSocket endpoint broadcasts updates on candle arrival
- Frontend subscribes via Next.js + use-websocket
- Pub/sub on Redis for multi-instance support

### Consequences
- **Positive:** Real-time, efficient, standard web tech
- **Negative:** Stateful connections, scale requires Redis

---

## ADR-009: API Key Rotation

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Security best practice

### Context
Exchange API keys or user tokens could be compromised.

### Decision
- Keys automatically invalidate after 90 days
- Rotation endpoint with 7-day grace period (old key still works)
- Audit log all rotations
- Rate limiting on rotation attempts

### Consequences
- **Positive:** Reduces window of compromise
- **Negative:** Clients must update keys regularly

---

## ADR-010: Audit Logging for Sensitive Actions

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Compliance, forensics

### Context
Regulatory/compliance requirement to track who did what.

### Decision
- Audit log table with action, user, timestamp, details
- Logged actions: strategy activation, trades, config changes, logins
- Immutable (append-only), queryable by date range/user

### Consequences
- **Positive:** Forensic trail, compliance ready
- **Negative:** Audit table grows large (implement archival)

---

## ADR-011: GitHub Actions CI/CD

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** Automation, quality gates

### Context
Multiple developers, need automated testing before merge.

### Decision
- Lint + type check + test on every PR (85% coverage gate)
- Build Docker images on main branch only
- Push to GHCR (GitHub Container Registry)
- Integration test with Docker Compose on main

### Consequences
- **Positive:** Prevents regressions, auto-deployment pipeline
- **Negative:** Actions minutes quota, build overhead

---

## ADR-012: Next.js Frontend over Vue/React SPA

**Status:** Accepted  
**Date:** 2024-08-20  
**Drivers:** SSR, SEO, fast development

### Context
Need real-time dashboard + marketing website eventually.

### Decision
- Next.js 14 (App Router) with TypeScript
- Tailwind CSS for styling (SGR dark theme)
- WebSocket via use-websocket library
- Zustand for state management
- Deploy as containerized standalone Next.js server

### Consequences
- **Positive:** SSR-capable, great DX, large ecosystem
- **Negative:** Heavier than plain React, overkill for pure dashboard

---

## References
- [Accelerated Microservices with Messaging](https://www.nginx.com/blog/microservices-at-scale-intro/)
- [Circuit Breaker Pattern](https://martinfowler.com/bliki/CircuitBreaker.html)
- [PostgreSQL TimescaleDB Docs](https://docs.timescaledb.com/)
