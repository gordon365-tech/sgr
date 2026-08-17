# Patch anwenden: TradingOrchestrator (Signal → Risk → Execution → Portfolio)

## Was hier drin ist

`phase-orchestrator.patch` — ein `git diff`-Patch, erzeugt aus einem frischen Klon
von `https://github.com/gordongraff81-tech/sgr` (main, Commit `5ab631a`).

**Wichtig:** Dieser Patch geht vom *tatsächlichen* GitHub-main-Stand aus, nicht vom
Phase-7A-Stand aus dem alten Bericht (der existiert nachweislich nirgends mehr).
Falls dein lokales `C:\Projects\sgr` von `origin/main` abweicht, prüfe das vor dem
Anwenden mit `git status` / `git diff origin/main`.

## Anwenden

```powershell
cd C:\Projects\sgr
git status                     # sollte "clean" und auf main sein
git apply --check phase-orchestrator.patch   # Trockenlauf, meldet Konflikte
git apply phase-orchestrator.patch           # tatsächlich anwenden
```

Falls `git apply --check` Konflikte meldet: das heißt, dein lokaler Stand weicht
vom geklonten `main` ab (z. B. eigene uncommitted Änderungen). In dem Fall bitte
`git diff` posten, dann passe ich den Patch an.

## Danach sofort verifizieren (nicht überspringen)

```powershell
pip install -e . 
pip install pytest pytest-asyncio pytest-cov pytest-mock ruff mypy

python -m pytest -q --no-cov
# Erwartet: 323 passed, 12 failed (vorbestehend, siehe unten), 9 skipped

ruff check sgr/orchestrator/ sgr/api/ sgr/risk/engine.py sgr/core/types.py
mypy sgr/orchestrator/engine.py --ignore-missing-imports
```

## Danach sofort committen UND pushen (in einem Schritt, nicht getrennt)

```powershell
git add -A
git commit -m "feat(orchestrator): wire Signal->Risk->Execution->Portfolio pipeline"
git push origin main
```

Bitte danach `git log --oneline -3` und `git status` posten, damit ich verifizieren
kann, dass es wirklich auf GitHub angekommen ist — das war genau der Punkt, an dem
es letztes Mal verloren gegangen ist.

## Die 12 vorbestehenden Testfehler (nicht Teil dieser Änderung)

`test_ml_engine.py` (7×, sklearn/numpy-Versionsproblem), `test_saas.py` (3×,
bcrypt-Versionsproblem), `test_strategy_portfolio.py` (1×), `test_api.py` (1×,
Test-Isolation). Verifiziert per `git stash`-Baseline-Vergleich vor jeder Änderung
in dieser Session — identisch vorher wie nachher.
