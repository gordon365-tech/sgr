"""
SGR E2E Lifecycle Scenarios
============================
Szenario-basierter End-to-End-Test-Runner fuer den vollstaendigen
Trading-Lifecycle (Signal -> Risk -> Portfolio -> Execution -> Exchange
Adapter -> Order -> Fill -> Position -> Monitoring -> Exit -> PnL).

Bewusste Architekturentscheidung (siehe conftest.py Docstring): baut
NICHTS neu, was bereits existiert - nutzt exakt dieselbe Engine-
Verdrahtung wie tests/integration/test_orchestrator_pipeline.py
(TradingOrchestrator + StrategyEngine + RiskEngine + ExecutionEngine +
PortfolioEngine + FeatureStore), ersetzt darin lediglich den dortigen
MockExchangeAdapter durch einen echten BinanceAdapter (Paper/Testnet,
futures_mode=True) - siehe conftest.py fuer die vollstaendige Isolations-
Begruendung (eigener Test-Tenant, eigene Redis-DB, keine Beruehrung von
Gordon/Sumo).
"""
