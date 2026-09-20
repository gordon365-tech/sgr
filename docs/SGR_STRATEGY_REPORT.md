# SGR Strategiebericht — Autonomous Adaptive Trading System

**Status:** Lebendes Architekturdokument
**Version:** 2.0 (Futures-Grid-Erweiterung)
**Datum:** 2026-09-20
**Vorgänger:** Kein vorheriges Dokument mit dieser Terminologie war im Repository vorhanden — dieses Dokument konsolidiert die bestehende, im Code bereits umgesetzte Architektur und erweitert sie um Futures Grid als zentrale Produktklasse.

---

## 0. Leseanleitung und Grundprinzip

SGR ist **kein gewöhnlicher Trading-Bot** und **kein gewöhnlicher Grid-Bot**. SGR ist eine **exchange-agnostische, autonome Trading-Intelligence-Plattform**, die selbst beurteilt,

- welches Marktregime gerade vorliegt (**Market State Engine**),
- ob eine Strategieklasse in diesem Regime nachweisbar einen Vorteil (Edge) hat (**Edge Engine**),
- wie viel Kapital diese Strategie verdient (**Capital Allocation Engine**),
- wie sich Strategie-Parameter über Zeit weiterentwickeln (**Strategy Evolution Engine**),
- und wie eine genehmigte Entscheidung sicher, idempotent und risikogeprüft ausgeführt wird (**Execution Intelligence**).

Futures Grid Trading ist ab dieser Version eine **erstklassige, zentrale Strategieklasse** innerhalb genau dieser Architektur — keine isolierte Pionex-Sonderlösung, kein separates System. SGR entscheidet selbst, **ob** ein Grid sinnvoll ist, **welche Richtung** (Long/Short/kein Grid), **welche Range**, **wie viel Kapital**, **wann angepasst** werden muss und **wann keine nachgewiesene Edge mehr besteht** — auf Basis prüfbarer Strategy Features und Market-Regime-Signale, nicht auf Basis fester Gewinnversprechen.

**Ausdrücklich keine Zusicherung:** Dieses Dokument und der zugehörige Code versprechen keine Gewinne, keine garantierte Performance und keine automatische regulatorische Zulässigkeit von Futures/Futures-Grid-Produkten für irgendeine Jurisdiktion — insbesondere nicht für deutsche Retailkunden (siehe Abschnitt 9, Compliance Layer).

---

## 1. Ist-Zustand (vor dieser Erweiterung)

Der bestehende, produktiv gewachsene Code implementiert bereits — unter anderen Namen — wesentliche Teile der Architekturvision:

| Architekturkonzept (Vision) | Bestehende Implementierung |
|---|---|
| Market State Engine | `sgr/market_data/feature_engineering.py`, `sgr/strategy/regime_classifier.py`, `sgr/strategy/regime_profile.py` |
| Edge Engine | `sgr/backtesting/performance.py` (PerformanceAnalyzer), `sgr/strategy/strategy_scoring.py`, `sgr/backtesting/validation.py` (Walk-Forward/Monte-Carlo) |
| Capital Allocation | **Nicht vorhanden** — Positionsgröße wird pro Signal durch `sgr/risk/position_sizer.py` bestimmt, aber es gab keine strategieübergreifende Kapitalzuteilung |
| Strategy Evolution | `sgr/strategy/parameter_optimizer.py` (Parameter-Suche), `sgr/strategy/symbol_validation_runner.py` (Pro-Symbol-Validierung) |
| Execution Intelligence | `sgr/execution/engine.py`, `sgr/execution/order_safety.py`, `sgr/execution/preflight.py`, `sgr/execution/quantization.py`, `sgr/risk/kill_switch.py` |
| Exchange Abstraction | `sgr/exchanges/base.py` (Protocol), `sgr/exchanges/ccxt_base.py`, `sgr/exchanges/factory.py` — bislang **nur Exchange**, keine ProductType-Dimension |
| Compliance Layer | **Nicht vorhanden** |
| Futures Grid | **Nicht vorhanden** als Strategieklasse; Pionex-Adapter existierte nur für Spot |

**Wichtiger Architektur-Befund aus dieser Erweiterung:** ccxt (verifiziert von Version 4.1.50 bis 4.5.81, dem aktuell in `pyproject.toml` zulässigen Bereich) führt **keine `"pionex"`-Exchange-ID mehr**. Der bestehende, rein ccxt-basierte `PionexAdapter` konnte sich dadurch weder in PAPER noch in LIVE tatsächlich verbinden — ein latenter, durch vollständig gemockte Tests nicht aufgedeckter Defekt, der bereits **vor** dieser Erweiterung bestand. Der Fix (siehe Abschnitt 8) ist reiner Bugfix-Charakter, kein Verhaltensverlust für Binance.

Binance-Spot- und Binance-Futures-Integration, die bestehende Strategy Engine, Risk Engine, Execution Engine, Portfolio Engine, Tenant-Isolation, Order Safety, Reconciliation, Kill Switch, Backtesting und Walk-Forward-Validierung sind durch diese Erweiterung **nicht verändert** worden (0 Regressionen, siehe Abschnitt 11 „Testergebnisse“ im Implementierungsbericht).

---

## 2. Architekturvision: drei orthogonale Ebenen

SGR unterscheidet ab dieser Version grundsätzlich zwischen drei unabhängigen Ebenen, die **nicht hart miteinander verdrahtet** sind:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. EXCHANGE          Binance · Pionex · (zukünftig weitere)     │
├─────────────────────────────────────────────────────────────────┤
│  2. PRODUCT TYPE       Spot · Perpetual · Futures Grid ·          │
│                        Spot Grid · (zukünftig weitere)            │
├─────────────────────────────────────────────────────────────────┤
│  3. STRATEGY           Trend Following · Mean Reversion ·         │
│                        Futures Grid Long/Short/Adaptive ·         │
│                        (zukünftig weitere)                        │
└─────────────────────────────────────────────────────────────────┘
```

Jede Strategie deklariert explizit, welche `ProductType`s und welche `ExchangeID`s sie unterstützt (`supported_product_types`, `supported_exchanges`). Vor jeder Order prüft die Execution-Schicht über die **Exchange/Product Capability Layer** (`sgr/exchanges/capabilities.py`), ob die konkrete Kombination technisch existiert — unabhängig davon, was eine Strategie annehmen möchte.

```python
@dataclass(frozen=True)
class ExchangeCapability:
    exchange: ExchangeID
    product_type: ProductType
    supports_long: bool
    supports_short: bool
    supports_leverage: bool
    supports_grid: bool
    supports_futures_grid: bool
    supports_reduce_only: bool
    supports_hedge_mode: bool
    supports_one_way_mode: bool
    supports_funding: bool
    supports_conditional_orders: bool
    supports_position_mode: bool
    max_leverage: Decimal | None
```

Registrierte Kombinationen: `BINANCE/SPOT`, `BINANCE/PERPETUAL`, `BINANCE/FUTURES_GRID`, `PIONEX/SPOT`, `PIONEX/PERPETUAL`, `PIONEX/FUTURES_GRID`. Futures Grid ist damit **technisch auf beiden Exchanges möglich** — die strategische Bevorzugung von Pionex (siehe Abschnitt 10) ist eine **Konfigurations-/Capital-Allocation-Entscheidung**, keine Code-Sperre.

---

## 3. Market State Engine — erweitert um Grid Suitability

Die bestehende Feature-Berechnung (`FeatureEngineer`, `IndicatorValues`, `FuturesFeatures`) liefert bereits `adx_14`, `atr_pct`, `bb_width`, `di_plus`/`di_minus`, `funding_rate_annualized`, `volume_ratio`. Neu hinzugekommen ist `sgr/market_data/grid_suitability.py`:

```python
@dataclass(frozen=True)
class GridSuitabilityScore:
    range_quality: float        # ADX niedrig + kompakte Bollinger-Bänder
    volatility_quality: float   # weder zu ruhig noch zu wild (Sweet Spot)
    liquidity_score: float      # geschätztes Quote-Volumen
    funding_cost_score: float   # Funding-Rate vs. Limit
    trend_risk: float           # invertiert zu range_quality
    breakout_risk: float        # DI+/DI--Divergenz als Proxy
    composite: float            # gewichtetes, konservatives Gesamtmaß
    is_suitable: bool
    suggested_direction: GridDirection   # LONG | SHORT | NEUTRAL
    reasons: list[str]                   # nachvollziehbare Begründung je Komponente
```

**Ausdrücklich kein Buy/Sell-Signal-Ersatz** (siehe Aufgabenstellung): der Score entscheidet nur, ob Futures Grid als Strategieklasse überhaupt in Frage kommt. Regeln sind als prüfbare, in Tests verifizierte Heuristiken implementiert (`tests/unit/test_grid_suitability.py`, 11 Tests), nicht als feste Gewinnversprechen:

- Seitwärtsmarkt (niedriger ADX, kompakte Bollinger-Bänder) → höherer `range_quality` → Grid-Kandidat.
- Starker gerichteter Trend (hoher ADX, DI-Divergenz) → `is_suitable=False`, `suggested_direction=NEUTRAL`.
- Extreme Volatilität (ATR% > konfigurierbares Limit) → `volatility_quality=0.0`.
- Illiquider Markt (geschätztes Volumen unter Schwelle) → `liquidity_score` niedrig, blockiert `is_suitable`.
- Ungewöhnliche Funding-Rate → `funding_cost_score=0.0`, blockiert `is_suitable`.
- Zu geringe erwartete Edge → `composite` unter Schwelle → kein Kapital (siehe Edge Engine, Abschnitt 4).

---

## 4. Edge Engine — getrennte Bewertung für Grid-Strategien

**Kernregel (aus der Aufgabenstellung übernommen):** *„Eine Futures Grid Strategie darf nicht nur nach absolutem PnL bewertet werden.“* `sgr/strategy/grid_edge.py` implementiert deshalb `GridEdgeMetrics` mit allen geforderten Kennzahlen:

```
net_pnl · gross_pnl · fees · funding · slippage · win_rate · profit_factor ·
max_drawdown_pct · sharpe · sortino · grid_efficiency · capital_utilization ·
exposure_time · average_grid_capture · edge_stability · regime_compatibility
```

`grid_efficiency = net_pnl / gross_pnl` macht sichtbar, wie viel des Bruttogewinns tatsächlich nach Fees/Funding/Slippage übrig bleibt. `edge_stability` prüft, ob der Ertrag gleichmäßig über die Zeit entsteht oder sich auf ein einzelnes Segment konzentriert (Überanpassungs-Indikator). `is_edge_confirmed()` ist das Gate für Kapitalfreigabe und verlangt **gleichzeitig**: genug abgeschlossene Zyklen, positiven Netto-PnL, ausreichende Grid-Effizienz, ausreichenden Sharpe **und** ausreichende Stabilität — ein hoher Bruttogewinn mit winziger Nettomarge besteht die Prüfung nicht (verifiziert in `tests/strategy/test_grid_edge.py::test_low_grid_efficiency_blocks_despite_positive_pnl`).

---

## 5. Capital Allocation Engine (neu, v1 — regelbasiert)

`sgr/strategy/capital_allocation.py` ist die **erste Version** einer strategieübergreifenden Kapitalzuteilung. Sie ist bewusst **kein** ML-/RL-Allokator und **keine** automatische Live-Rebalancing-Engine, sondern ein transparenter, regelbasierter Scorer:

```python
class CapitalAllocationEngine:
    def allocate(
        self, candidates: list[AllocationCandidate], total_capital: Decimal,
        min_score_threshold: float = 0.1,
    ) -> list[AllocationResult]: ...
```

Jeder Kandidat (direktionale Strategie **oder** Grid-Strategie) liefert einen einheitlichen, vergleichbaren `edge_score()`:

- Direktional: `max(sharpe, 0) * drawdown_penalty`
- Grid: `max(sharpe, 0) * grid_efficiency * (0.5 + 0.5 * edge_stability)`

**Keine feste Bevorzugung aufgrund des Produktnamens** (siehe Aufgabenstellung) — verifiziert in `test_no_product_name_bias_grid_and_directional_compete_on_score_alone`: ein schwaches Grid erhält nachweislich **weniger** Kapital als eine starke direktionale Strategie. Ein `max_single_strategy_fraction`-Cap verhindert Konzentrationsrisiko auf eine einzelne Variante. Nicht zugeteiltes Restkapital wird **nicht** automatisch umverteilt (transparente Kapitalreserve statt stiller Umverteilung).

**Offener Punkt:** Diese Engine ist als Analyse-/Vorschlags-Werkzeug konzipiert — sie ist **nicht** live in den Orchestrator-Hauptzyklus verdrahtet (siehe Abschnitt 12).

---

## 6. Strategy Evolution — Futures Grid als Teil des Genome

`sgr/core/grid_types.py::FuturesGridParameters` ist das Strategy Genome für Futures Grid — ein `@dataclass(frozen=True)` mit genau den in der Aufgabenstellung geforderten Feldern:

```python
grid_lower_price · grid_upper_price · grid_count · grid_spacing · grid_mode ·
long_or_short · leverage · margin_mode · position_size · max_notional ·
take_profit · stop_loss · maximum_holding_time · funding_cost_limit ·
volatility_limit · liquidity_limit · maximum_grid_exposure ·
maximum_open_grid_orders
```

`compute_levels()` unterstützt sowohl arithmetische als auch geometrische Preis-Level-Verteilung (`GridSpacingMode`). Jede Mutation der Strategy Evolution Engine (Grid Count, Range, Spacing, Long/Short, Leverage, Volatility-/Funding-/Trend-Filter, Breakout Protection, Adaptive Range, Dynamic Grid Density) erzeugt eine neue, immutable `FuturesGridParameters`-Instanz und **muss** danach die vollständige Validierungspipeline erneut durchlaufen (Abschnitt 7) — keine Mutation erhält direkten Kapitalzugang.

Drei konkrete Strategien (`sgr/strategy/futures_grid.py`), alle in der `StrategyRegistry` registriert wie jede andere Strategie:

| Strategie | Verhalten |
|---|---|
| `LongFuturesGridStrategy` (`futures_grid_long_v1`) | Handelt nur, wenn `GridSuitabilityScore.is_suitable`; feste Long-Richtung |
| `ShortFuturesGridStrategy` (`futures_grid_short_v1`) | Analog, feste Short-Richtung |
| `AdaptiveFuturesGridStrategy` (`futures_grid_adaptive_v1`) | Entscheidet Long/Short/kein Grid aus `suggested_direction` |

**Architekturentscheidung — eigenes Protocol statt Zweckentfremdung des bestehenden Signal-Pfads:** `TradingStrategy.generate_signal()` liefert genau ein Signal für genau eine Order; ein Grid besteht aus mehreren gleichzeitig aktiven Preis-Leveln. Grid-Strategien implementieren deshalb zusätzlich `GridTradingStrategy.evaluate() -> GridDecision`, bleiben aber gleichzeitig gültige `TradingStrategy`-Instanzen mit `generate_signal()` als **bewusstem No-Op** (liefert immer `None`). Ergebnis: Registrierung, Aktivierung, Validierungsstatus laufen unverändert über die bestehende `StrategyRegistry` — aber `StrategyEngine.process()` und der klassische `BacktestSimulator` erhalten von einer Grid-Strategie **niemals** ein Signal und werden dadurch **nicht** beeinflusst (Null-Interferenz, siehe Testabdeckung).

---

## 7. Execution Intelligence — Grid Controller

`sgr/execution/grid_controller.py::GridController` orchestriert den vollständigen Grid-Lifecycle: erstellen → Level-Orders erzeugen → Fills verarbeiten → Level neu auffüllen (Rebalancing) → überwachen (Funding/Stop-Loss/Take-Profit/Max-Holding-Time/Liquidationsrisiko) → schließen.

**Keine Strategie umgeht die Risikoschicht.** `open_grid()` prüft in dieser Reihenfolge, bevor auch nur ein Level generiert wird:

1. **Exchange/Produkt-Capability** (`sgr.exchanges.capabilities`)
2. **Compliance/Jurisdiktion/Account-Eligibility** (`sgr.compliance`, siehe Abschnitt 9)
3. **GridRiskEngine** (`sgr.risk.grid_risk`, siehe unten)

**Keine Fake-Shortcut-Execution.** Jede tatsächliche Order — Level-Open, Level-Close, Notfall-Exit — läuft durch **dieselbe** `sgr.execution.engine.ExecutionEngine.execute()` wie jede andere SGR-Order: Preflight, Kill Switch, Order Safety/Idempotency, Quantization, Live-Trading-Gate. Der Controller selbst platziert niemals eine Order direkt auf einem Exchange-Adapter.

**PAPER-Trading-Besonderheit, sauber gelöst:** `CCXTBaseAdapter._simulate_order()` füllt *jede* simulierte Order sofort zum aktuellen Marktpreis — es gibt keine „ruhenden" Limit-Orders in der Simulation (unverändertes, bewusstes Bestandsverhalten für alle Directional-Strategien). Ein Futures Grid besteht aber gerade aus ruhenden Limit-Orders auf mehreren Preis-Leveln. Der `GridController` löst das, **ohne** `_simulate_order()` selbst zu verändern (null Regressionsrisiko für bestehende Strategien): er hält den Grid-Zustand selbst (welche Level sind gefüllt) und entscheidet bei jedem Preis-Tick (`on_price_tick()`) über **Crossing-Erkennung** (nicht bloßen Preisvergleich — siehe unten), ob ein Level jetzt auslösen soll; erst dann geht eine echte (Markt-)Order durch die volle Sicherheitskette.

> **Gefundener und behobener Implementierungsfehler während der Entwicklung:** eine naive „`current_price <= level.price`"-Prüfung hätte beim allerersten Preis-Tick nach Grid-Eröffnung fälschlich **alle** Level oberhalb des Eröffnungspreises gleichzeitig ausgelöst. Die endgültige Implementierung verlangt eine **echte Überquerung** zwischen dem zuletzt bekannten und dem neuen Preis (`grid.last_price`), mit einer bewusst strikten Grenze auf der alten Preisseite, damit ein Level, das zufällig exakt auf dem Eröffnungspreis liegt, nicht bereits beim ersten Tick fälschlich triggert. Ein zweiter Fehler (Positionsmenge beim Schließen wurde am Exit-Preis statt am ursprünglichen Entry neu berechnet, wodurch `net_position_qty` nie exakt auf 0 zurückkehrte) wurde ebenfalls während der Testentwicklung gefunden und behoben — belegt durch `tests/execution/test_grid_paper_lifecycle.py`, den vollständigen End-zu-Ende-Lifecycle-Test.

### GridRiskEngine — eigenes Risikoprofil für Futures Grid

`sgr/risk/grid_risk.py` ergänzt — ersetzt nicht — die bestehende `RiskEngine`. Tenant-konfigurierbare Limits (`GridRiskLimitsConfig`, env-Prefix `GRID_RISK_`):

```
max_grid_exposure_usd · max_grid_position_usd · max_open_grids ·
max_grid_orders · max_grid_loss_usd · max_funding_cost_pct ·
max_liquidation_distance_pct · max_leverage · min_liquidity_usd ·
max_volatility_atr_pct
```

`evaluate_new_grid()` prüft vor Eröffnung: Anzahl offener Grids, Leverage, Grid-Order-Anzahl, Kapitalbindung dieses einen Grids **und** portfolioweite Grid-Exposure über alle offenen Grids, Liquidität, Funding-Kosten, Volatilität, sowie eine (bewusst dokumentiert vereinfachte) Liquidationsdistanz-Näherung. `check_ongoing_grid()` läuft periodisch gegen jedes aktive Grid (`GridController.monitor_grids()`) und liefert `GridViolation`s mit Severity `hard` (Grid muss geschlossen werden) oder `soft` (nur Warnung) — ein Grid baut dadurch **niemals unbegrenzt Positionen auf**.

---

## 8. Exchange Abstraction — Pionex-Integration

### Bestätigter und behobener Architekturmangel

ccxt führt (verifiziert 4.1.50 bis 4.5.81) keine `"pionex"`-Exchange-ID. `sgr/exchanges/pionex.py` versucht weiterhin zuerst den ccxt-Pfad (falls eine künftige ccxt-Version „pionex" wieder registriert — vollständig abwärtskompatibel, per Test mit gefakter ccxt-ID abgedeckt) und fällt sonst für **PAPER Mode** auf den bereits vorhandenen, getesteten `sgr.exchanges.pionex_client.PionexClient` zurück (öffentliche REST-Endpunkte: Ticker, Orderbook, OHLCV, Symbol-Metadaten). Paper Trading — inklusive Futures Grid Paper Trading — funktioniert dadurch **vollständig ohne echte Order-Übermittlung**.

**LIVE Order-Submission für Pionex bleibt bewusst NICHT implementiert.** Ohne verifizierten, signierten Private-REST-Client wäre eine geratene Implementierung für ein Handelssystem nicht vertretbar. `connect()` schlägt im LIVE-Modus ohne funktionierenden ccxt-Pfad **sofort und eindeutig** fehl (`AdapterFeatureNotImplementedError`) — statt eine Order später unkontrolliert scheitern zu lassen. Dies ist eine bewusste Sicherheitsentscheidung (siehe Aufgabenstellung: „Keine Implementierung von echtem Live Trading ohne bestehende SGR Sicherheits- und Freigabemechanismen").

Ein neuer Fehlertyp unterscheidet die beiden Fälle sauber: `NotSupportedFeatureError` (die Exchange kann es nachweislich nicht) vs. `AdapterFeatureNotImplementedError` (die Exchange kann es vermutlich, SGR hat es nur noch nicht verifiziert implementiert) — wichtig, um „Exchange-Grenze" nicht mit „SGR-Implementierungslücke" zu verwechseln.

Binance-Integration (Spot und Futures, `futures_mode`-Parameter) ist **unverändert**.

---

## 9. Compliance Layer

`sgr/compliance/` trennt zwei unabhängige Fragen strikt:

1. **Technische Verfügbarkeit** — beantwortet die Capability Layer (Abschnitt 2).
2. **Regulatorische Zulässigkeit** — beantwortet die `ComplianceEngine`.

**Deny-by-default, keine Ausnahme:**

```python
class ComplianceStatus(StrEnum):
    ELIGIBLE = "eligible"
    PRODUCT_NOT_AVAILABLE = "product_not_available"
    JURISDICTION_RESTRICTED = "jurisdiction_restricted"
    ACCOUNT_NOT_ELIGIBLE = "account_not_eligible"
    EXCHANGE_CAPABILITY_MISSING = "exchange_capability_missing"
    COMPLIANCE_CHECK_REQUIRED = "compliance_check_required"
```

Ohne eine vom Operator explizit registrierte `ProductAvailabilityRule` ist ein Produkt für **niemanden** verfügbar (`PRODUCT_NOT_AVAILABLE`) — verifiziert in `test_german_retail_not_automatically_assumed_legal`. Eine unbekannte Jurisdiktion (`jurisdiction=None`) ist **nicht** gleichbedeutend mit „überall erlaubt", sondern löst explizit `COMPLIANCE_CHECK_REQUIRED` aus. Fehlende KYC-Verifizierung, fehlende explizite `futures_trading_enabled`-Freigabe oder fehlende Risikoaufklärungs-Bestätigung führen zu `ACCOUNT_NOT_ELIGIBLE`. Jurisdiktion/Kontotyp/KYC-Status werden **ausschließlich** aus vom Tenant/Operator gepflegten Stammdaten gelesen (`sgr.saas.types.TenantConfig` → `to_account_eligibility()`) — **niemals** aus IP-Adresse, Spracheinstellung oder anderen technischen Heuristiken (keine VPN-/Residency-Umgehung, keine KYC-Umgehung, keine Umgehung von Exchange-Restriktionen).

**Explizit klargestellt:** Dieses System trifft **keine Annahme**, dass Pionex-Futures-Produkte für deutsche Retailkunden automatisch legal oder verfügbar sind. Die Freigabe eines konkreten Produkts für eine konkrete Jurisdiktion ist eine **manuelle, außerhalb dieses Codes zu treffende Entscheidung** eines Operators mit entsprechender rechtlicher Prüfung (siehe „Notwendige manuelle Entscheidungen" im Implementierungsbericht).

---

## 10. Exchange-Priorität: Pionex als First-Class-Ziel für Futures Grid

Architektonisch ist Pionex Futures Grid eine strategisch wichtige, prominent unterstützte Produktklasse. Binance bleibt vollständig unterstützt und funktional unverändert. Ein Tenant (oder künftig die Capital Allocation Engine) kann bevorzugt Pionex für Futures Grid verwenden, während Binance für andere Strategien/Märkte verwendet wird:

```bash
PRIMARY_EXCHANGE=pionex
PRIMARY_FUTURES_EXCHANGE=pionex
ENABLE_PIONEX_FUTURES_GRID=true
ENABLE_BINANCE=true
```

Diese Zuordnung ist **nicht hart codiert**: sie läuft über Konfiguration (`SGRConfig.primary_futures_exchange`, `TenantConfig.primary_futures_exchange`/`enable_pionex_futures_grid`/`enable_binance`) und über Capability-Matching, niemals über eine `if exchange == PIONEX`-Verzweigung in der Strategielogik. Diese Flags schalten **ausschließlich** die Exchange/Produkt-Zuteilung frei — sie umgehen an keiner Stelle Risk Engine, Compliance Engine oder Exchange-Capability-Prüfung.

---

## 11. Dashboard und Observability

Neue Prometheus-Metriken (`sgr/monitoring/metrics.py`, gleiches OTel-Muster wie bestehende Metriken, automatisches `tenant`-Label):

```
sgr_futures_grid_active · sgr_futures_grid_exposure_usd ·
sgr_futures_grid_pnl_usd · sgr_futures_grid_funding_cost_usd ·
sgr_futures_grid_orders · sgr_futures_grid_fills_total ·
sgr_futures_grid_liquidation_distance_pct · sgr_futures_grid_edge ·
sgr_futures_grid_regime_score
```

Neues, eigenständiges Grafana-Dashboard `monitoring/grafana/dashboards/sgr-futures-grid.json` (das bestehende `sgr-trading.json` bleibt unverändert) mit Filtervariablen `$exchange`/`$symbol`/`$strategy`/`$tenant` — ermöglicht genau die geforderte Sicht:

```
Pionex   / Futures Grid      / Gordon
Pionex   / Futures Grid      / Sumo
Binance  / Trend Following   / Gordon
Binance  / Mean Reversion    / Sumo
```

Ein struktureller Test (`tests/unit/test_grafana_futures_grid_dashboard.py`) verifiziert, dass jede im Dashboard referenzierte Metrik tatsächlich im Code exportiert wird (gleiche Namensübersetzung wie beim bestehenden Dashboard-Test).

---

## 12. Offene Punkte (bewusst nicht in dieser Erweiterung gelöst)

1. **Kein automatischer Live-Grid-Scheduler.** `is_active=True` für eine Grid-Strategie in der `StrategyRegistry` setzt nur das Registry-Flag — es startet **kein** automatisches Trading und weist **kein** Kapital zu. Es existiert noch kein periodischer Prozess, der aktive `GridTradingStrategy`-Instanzen automatisch gegen `GridController.open_grid()` ausführt. Das ist eine bewusste Grenze dieser Erweiterung: eine automatische Kapitalzuweisung ohne vollständige Compliance-Stammdaten pro Tenant (Jurisdiktion, KYC, `futures_trading_enabled`) wäre genau das unkontrollierte Verhalten, das die Aufgabenstellung ausdrücklich verbietet.
2. **Pionex LIVE Order-Submission fehlt.** Ein verifizierter, signierter Pionex-Private-REST-Client (Order platzieren/stornieren/abfragen, Balance, Leverage) ist nicht Teil dieser Erweiterung — siehe Abschnitt 8.
3. **Pionex-Funding-Rate/Open-Interest-Endpunkte** sind im nativen Fallback nicht verifiziert implementiert (`AdapterFeatureNotImplementedError`).
4. **Historische Pionex-Marktdaten für Backtesting/Walk-Forward** (`BacktestDataLoader.load_public_history`) nutzen weiterhin den ccxt-Pfad und sind vom selben strukturellen ccxt-Pionex-Problem betroffen; `GridValidationRunner` verwendet deshalb standardmäßig Binance als Datenquelle für den Backtest (die validierte Strategie-Logik selbst bleibt exchange-agnostisch).
5. **Capital Allocation Engine ist nicht live verdrahtet** — reines Analyse-/Vorschlagswerkzeug (siehe Abschnitt 5).
6. **GridRepository-Persistenz ist best-effort**, aber Crash-Recovery/Restart-Recovery für Grids (Wiederherstellung des In-Memory-`GridController`-Zustands aus der DB nach einem Neustart, analog zu `PortfolioEngine.restore_from_persistence()`) ist nicht Teil dieser Erweiterung.
7. **Liquidationspreis-Näherung ist vereinfacht** (kein exaktes Maintenance-Margin-Modell der jeweiligen Exchange) — sowohl im Backtest-Simulator als auch in der `GridRiskEngine`.
8. **Funding-Rate im Backtest ist ein konstant angenommener Durchschnittswert**, keine echte historische Funding-Zeitreihe (Candles tragen keine Funding-Daten).
9. **mypy strict** konnte in dieser Sitzung aufgrund eines internen mypy-Fehlers (Version 2.3.1) nicht vollständig gegen die neuen Module verifiziert werden; `ruff check`/`ruff format` sind sauber, `pytest` ist vollständig grün.

Keiner dieser Punkte blockiert Paper Trading oder die Validierungspipeline — sie betreffen ausschließlich den Weg zu echtem Live-Kapital, wo die Aufgabenstellung ohnehin ausdrücklich zusätzliche, hier nicht implementierte Freigabemechanismen verlangt.

---

## 13. Zusammenfassung

Futures Grid Trading ist ab dieser Version eine zentrale, in Market State Engine, Edge Engine, Capital Allocation, Strategy Evolution und Execution Intelligence tief integrierte Strategieklasse von SGR — mit Pionex als strategisch bevorzugter, aber nicht hart kodierter Zielexchange. SGR bleibt dabei, was es war: eine exchange-agnostische, autonome Trading-Intelligence-Plattform, die selbst erkennt, wann ein Futures Grid sinnvoll ist, welche Richtung geeignet ist, welche Range geeignet ist, wie viel Kapital eingesetzt werden darf, wann das Grid angepasst werden muss und wann die Strategie keinen nachgewiesenen Vorteil mehr besitzt — ohne Gewinnversprechen, ohne automatische regulatorische Annahmen und ohne die Risikoschicht zu umgehen. Binance bleibt als bestehende, weiterhin vollständig unterstützte Exchange erhalten.
