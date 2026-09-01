# NÄCHSTE SCHRITTE – EHRLICHE ROADMAP

**Basis:** Commit 5c6b303ad5d5664644b646ba041f551e425dcbb1  
**Realität:** Code geschrieben, aber NICHT integriert/getestet  
**Status:** WIP (Work In Progress)

---

## AKTUELLE SITUATION

### Was existiert
- ✅ Bestehendes SGR Project (funktioniert)
- ⚠️ 43 neue Dateien (untracked, teilweise geschrieben, NICHT integriert)
- ⚠️ 4 modified Dateien (uncommitted Changes)

### Was NICHT existiert
- ❌ Docker Images (nicht gebaut)
- ❌ Getestete Features (Tests nicht ausgeführt)
- ❌ Integrierte Features (in productivem Code nicht genutzt)
- ❌ Git Commits (alle uncommitted)

---

## ROADMAP: WAS MUSS GETAN WERDEN

### PHASE 1: INTEGRATION (1-2 Tage)

**1.1 order_safety.py INTEGRIEREN**
```python
# In sgr/execution/engine.py
from sgr.execution.order_safety import SafeOrderExecutor

# ExecutionEngine.__init__()
self._safe_executor = SafeOrderExecutor(order_repository)

# ExecutionEngine.execute()
result = await self._safe_executor.execute_safely(
    order_request,
    exchange_submit_fn=adapter.place_order
)
```

**1.2 Health Routers REGISTRIEREN**
```python
# In sgr/api/main.py
from sgr.api.routers import health

app.include_router(health.router)  # Bereits existiert, aber:
# Stelle sicher, dass router all 3 Endpoints registriert
```

**1.3 Metrics RECORDET**
```python
# In sgr/execution/engine.py (nach order submission)
from sgr.monitoring.trading_metrics import record_order_submitted
record_order_submitted(exchange, symbol, side, trading_mode)

# In sgr/risk/engine.py (nach rejection)
from sgr.monitoring.trading_metrics import record_risk_rejection
record_risk_rejection(trading_mode, reason)
```

**Verifikation:**
```bash
# Health Endpoints prüfen
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
curl http://localhost:8000/health/trading

# Logs prüfen (keine Exceptions)
docker compose logs api
```

---

### PHASE 2: TESTING (1-2 Tage)

**2.1 Unit Tests für order_safety.py**
```bash
# Schreiben
pytest tests/execution/test_order_safety.py -v

# Tests sollen prüfen:
# - Idempotency Key generation
# - Duplicate detection (memory)
# - Duplicate detection (DB)
# - Unknown state handling
```

**2.2 Integration Tests**
```bash
# Tests für health endpoints
pytest tests/api/test_health_checks.py -v

# Tests für metrics recording
pytest tests/monitoring/test_trading_metrics.py -v
```

**2.3 Crash Scenario Tests (Docker erforderlich)**
```bash
# Nach Docker Setup (Phase 3)
pytest tests/docker_crash_tests/ -v -m docker_crash
```

**Verifikation:**
```bash
pytest tests/ --cov=sgr --cov-report=term-missing
# Coverage sollte >= 85% bleiben
```

---

### PHASE 3: DOCKER BUILD & TEST (1-2 Tage)

**3.1 Docker Images bauen**
```bash
# Production Image
docker build -f docker/Dockerfile.prod -t sgr-api:prod .

# Worker Image
docker build -f docker/Dockerfile.worker -t sgr-worker:prod .

# Verifikation
docker images | grep sgr
```

**3.2 Docker Compose testen**
```bash
# Production Stack starten
docker compose -f docker/docker-compose.prod.yml up -d

# Warten bis healthy
sleep 10

# Health checks
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
curl http://localhost:8000/health/trading

# Services prüfen
docker compose ps
docker compose logs
```

**3.3 Crash Szenarien testen**
```bash
# Container restart
docker compose restart api
curl http://localhost:8000/health/live  # sollte 200 sein

# Worker restart
docker compose restart worker

# Logs prüfen auf Errors
docker compose logs worker
```

**Verifikation:**
```bash
# Alle Services healthy
docker compose ps | grep healthy
```

---

### PHASE 4: GIT COMMIT & CLEANUP (1 Tag)

**4.1 Uncommitted Changes committen**
```bash
# Alles staged
git add .

# Review
git status

# Commit
git commit -m "feat(docker-platform): Docker production setup + order safety

Integration:
- order_safety.py integrated in ExecutionEngine
- health routers registered and tested
- metrics recording active
- worker container ready

Testing:
- unit tests: order_safety, health, metrics
- integration tests: health endpoints, metrics
- docker tests: container health, compose stack

Verified:
- All tests pass (85%+ coverage)
- Docker images build
- Health endpoints respond
- Metrics record correctly
- No regressions

See: FAKTISCHE_ANALYSE_2024-09-01.md for details"
```

**4.2 Documentation bereinigen**
```bash
# Entfernen
rm DOCKER_IMPLEMENTATION_REPORT.md  # Obsolet
rm FINAL_CHECKLIST.md  # Obsolet
rm ABSCHLUSSBERICHT_DE.md  # Obsolet

# Behalten
docs/DEPLOYMENT.md  # aktualisieren
docs/ADR-13-DOCKER-PRODUCTION-PLATFORM.md  # aktualisieren
FAKTISCHE_ANALYSE_2024-09-01.md  # Realität dokumentieren
```

---

## ZEITSCHÄTZUNG

| Phase | Task | Zeit | Risiko |
|-------|------|------|--------|
| 1 | Integration | 1-2 Tage | **Mittel** (API-Änderungen) |
| 2 | Testing | 1-2 Tage | **Niedrig** (Standard-Tests) |
| 3 | Docker Test | 1-2 Tage | **Mittel** (Docker-Komplexität) |
| 4 | Commit & Cleanup | 1 Tag | **Niedrig** |
| **TOTAL** | | **4-7 Tage** | |

---

## RISIKEN & MITIGATIONEN

### Risiko 1: Integration bricht bestehende Tests
**Mitigation:**
- Tests VOR Integration ausführen (Baseline)
- Nach Integration: `pytest tests/ -v` muss 100% grün sein
- Bei Fehler: sofort revert und debug

### Risiko 2: Docker Image zu groß oder baut nicht
**Mitigation:**
- Multi-stage Dockerfile befolgen
- Layer caching prüfen
- `docker build --progress=plain` für Debugging

### Risiko 3: Health Checks geben false positives
**Mitigation:**
- Jeder Check soll fail-safe sein (lieber false-negative)
- Tests mit alle Komponenten aus
- Tests mit einzelnen Komponenten aus

### Risiko 4: Order Safety wird nicht benutzt
**Mitigation:**
- ExecutionEngine Unit Tests MÜSSEN order_safety aufrufen
- Coverage Report muss order_safety zeigen
- Integration Test muss Duplicate Detection prüfen

---

## SUCCESS CRITERIA (PRE-COMMIT)

| Kriterium | Status | Verifikation |
|-----------|--------|-------------|
| Tests >= 85% | ? | `pytest --cov` |
| No Regressions | ? | `pytest tests/` grün |
| Docker Build | ? | `docker build .` erfolgreich |
| Health Endpoints | ? | `curl /health/*` antwortet |
| Metrics Recording | ? | `prometheus scrape` zeigt Metriken |
| order_safety integriert | ? | `grep SafeOrderExecutor execution/engine.py` |
| No uncommitted Changes | ? | `git status` clean |

---

## COMMANDS CHECKLISTE

```bash
# Phase 1: Integration
# [ ] order_safety in ExecutionEngine aufgerufen
# [ ] health router registriert
# [ ] metrics recordet

# Phase 2: Testing
[ ] pytest tests/execution/test_order_safety.py -v
[ ] pytest tests/api/test_health_checks.py -v
[ ] pytest tests/monitoring/test_trading_metrics.py -v
[ ] pytest tests/ --cov=sgr --cov-report=term-missing
[ ] Coverage >= 85%

# Phase 3: Docker
[ ] docker build -f docker/Dockerfile.prod -t sgr-api:prod .
[ ] docker compose -f docker/docker-compose.prod.yml up -d
[ ] curl http://localhost:8000/health/live
[ ] curl http://localhost:8000/health/ready
[ ] docker compose ps | grep healthy
[ ] docker compose logs api | grep ERROR
[ ] pytest tests/docker_crash_tests/ -v -m docker_crash

# Phase 4: Git
[ ] git add .
[ ] git status (all staged)
[ ] git commit -m "..."
[ ] git log -1
```

---

## NÄCHSTER HALTEPUNKT

**Nach Integration & Testing:**

1. **DECISION POINT: Ist alles grün?**
   - JA → Proceed to Phase 4 (Commit)
   - NEIN → Debug & Retry Phase 1/2/3

2. **VOR MERGE zu main:**
   - [ ] Code Review (2 Personen)
   - [ ] Run on Staging
   - [ ] Load Test (100+ RPS)
   - [ ] Security Review

3. **NACH MERGE:**
   - [ ] Production Deployment
   - [ ] Monitor Metrics
   - [ ] Verify no issues

---

## REALISTISCHE PROGNOSE

**Mit 1-2 Engineern, 4-5 Tage konzentriertes Arbeiten:**

✅ **Wird erreicht sein:**
- Order Safety integriert & getestet
- Health Checks funktionieren
- Docker Images gebaut & getestet
- Alles committed & merged

❌ **Wird NICHT erreicht sein (noch nicht):**
- Kubernetes Deployment (Phase 2)
- AWS Terraform (Phase 3)
- Full Production Rollout
- Live Trading Enablement

---

## FAZIT

**Aktueller Code:** ⚠️ Gut geschrieben, aber NICHT produktiv nutzbar  
**Nächste 4-5 Tage:** Integration, Testing, Verification  
**Danach:** Production-ready sein

**Wichtig:** Nicht "es existiert" mit "es funktioniert" verwechseln.

---

**Erstellt:** 2024-09-01  
**Basis:** Faktische Analyse Commit 5c6b303  
**Status:** Actionable Roadmap für die Integration
