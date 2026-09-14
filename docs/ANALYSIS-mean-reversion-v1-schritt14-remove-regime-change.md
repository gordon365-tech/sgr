# Analyse: mean_reversion_v1 — Test ohne regime_change-Exit (Schritt 14)

**Basis-Commit:** `413180f` (docs: test tighter RANGING-only stop for regime_change trades, Schritt 13)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage, BTC/USDT + ETH/USDT, 1h-Timeframe (gleicher Datensatz wie Schritt 11/12/13)
**Status:** Hypothese getestet. Ergebnis gemischt, kein Nettogewinn — keine Codeänderung.

---

## Ausgangsfrage

Schritt 13 warf als zweite, noch ungetestete Alternative auf: ist der `regime_change`-Exit selbst kontraproduktiv? Ein enger Stop danach schadet (Schritt 13), aber vielleicht ist bereits das *frühzeitige Beenden bei Regimewechsel an sich* das Problem — nicht seine Ausführung, sondern seine Existenz.

## Methodik

`BacktestSimulator._check_exits()` mit `current_regime=None` aufgerufen (statt des tatsächlich erkannten Regimes) — das ist exakt der Schalter, an dem die bestehende Logik selbst den `regime_change`-Zweig deaktiviert (`if current_regime is not None and ...`), alle anderen Zweige (ATR-Stop 2,5×, `target_reached`, `time_exit`) bleiben unveraendert aktiv. Kein Eingriff in die übrige Simulationslogik. Gleicher 180-Tage-Datensatz, gleiche Baseline wie Schritt 11–13.

## Ergebnis

| | Baseline (`regime_change` aktiv) | `regime_change` entfernt |
|---|---|---|
| Trades | 161 | 155 |
| Sharpe | −3,517 | −3,673 |
| Return | −2,09 % | **−2,80 %** |
| Profit Factor | 0,366 | 0,396 |
| Hit Rate | 34,8 % | **45,2 %** |
| Max Drawdown | 2,66 % | 3,40 % |
| Blocker | Sharpe, PF, Hit Rate (3) | Sharpe, PF (2 — Hit-Rate-Blocker fällt weg) |

### Exit-Reason-Breakdown ohne `regime_change`

| exit_reason | n | Wins | Losses | Ø Win (ATR) | Ø Loss (ATR) | Ratio | Ø Bars |
|---|---|---|---|---|---|---|---|
| `atr_stop` | 51 | 0 | 51 | — | 3,228 | — | 11,4 |
| `target_reached` | 64 | 57 | 7 | 1,999 | 0,806 | 2,481 | 7,1 |
| `time_exit` | 39 | 13 | 26 | 0,749 | 0,945 | 0,792 | 20,0 |

Zum Vergleich, Baseline (Schritt 12): `atr_stop` 19 Trades, `regime_change` 75, `target_reached` 54, `time_exit` 12.

## Befund

**Kein klarer Gewinn — die 75 vorher durch `regime_change` beendeten Trades verteilen sich neu, mit gegenläufigen Effekten:**

1. **9 zusätzliche Gewinner bei `target_reached`** (48 → 57): ein Teil der `regime_change`-Verlierer war tatsächlich ein "false positive" — die Position wäre bei längerem Halten zum Ziel zurückgekehrt. Das erklärt die verbesserte Hit-Rate (34,8 % → 45,2 %) und den leicht besseren Profit Factor (0,366 → 0,396).

2. **32 zusätzliche Trades landen bei `atr_stop`** (19 → 51), weiterhin ausnahmslos Verlust, bei nahezu gleich großer Distanz wie zuvor (~3,2–3,4 ATR). Das sind die "echten" Verlierer, die `regime_change` vorher früher und **kleiner** beendet hatte (Ø 1,154 ATR in der Baseline) — ohne diesen Frühwarn-Mechanismus laufen sie bis zum vollen, viel teureren 2,5×-ATR-Stop.

3. **Netto verschlechtert sich der Return** (−2,09 % → −2,80 %) und der Max Drawdown (2,66 % → 3,40 %), **obwohl** Hit-Rate und Profit Factor steigen. Der Grund: `regime_change` wirkte bisher als Verlustbegrenzung — es beendete Positionen im Schnitt bei 1,154 ATR Verlust statt sie bis zum vollen 2,5×-ATR-Stop (jetzt Ø 3,228 ATR) laufen zu lassen. Diese Verlustbegrenzung ging verloren, und der Dollar-Schaden der jetzt größeren `atr_stop`-Verluste übersteigt den Nutzen der zusätzlichen `target_reached`-Gewinne.

**Fazit:** `regime_change` ist weder eindeutig schädlich noch eindeutig hilfreich — es tauscht "viele mittelgroße, früh begrenzte Verluste" gegen "wenige kleine zusätzliche Gewinne plus wenige, aber sehr große Verluste" ein, wenn man es entfernt. In der für das Go-Live-Gate entscheidenden Kennzahl (Sharpe, der Return und Volatilität gemeinsam bewertet) ist die Baseline mit `regime_change` marginal besser (−3,517 vs. −3,673), trotz der schlechteren Hit-Rate/PF-Werte. Keine der beiden Varianten kommt in die Nähe der Go-Live-Schwellen.

## Entscheidung dieser Session

Keine Codeänderung. Weder "engerer Stop" (Schritt 13) noch "regime_change entfernen" (Schritt 14) verbessert das Gesamtbild eindeutig — beides bestätigt erneut, dass simple mechanische Eingriffe an der Exit-Seite (Stop-Distanz, Vorhandensein/Nichtvorhandensein eines einzelnen Exit-Zweigs) das Kernproblem nicht lösen.

## Empfehlung für den nächsten Schritt (nicht Teil dieser Session)

Die vier Diagnose-Runden (Schritt 10–14) zeigen konsistent: das Problem liegt nicht in Exit-Mechanik-Details (Zielpreis-Nutzung, Regime-Change-Exit, Stop-Distanz), sondern strukturell darin, dass ein erheblicher Teil der Entries — ob mit oder ohne `regime_change`-Exit — schlicht keine validen Mean-Reversion-Setups sind (Schritt 10: BB-Position bei diesen Trades im Schnitt nahe der Bollinger-Mitte, nicht am Rand). Sinnvollste nächste Schritte:
- Eine **Kombination** aus optimiertem Regime-Change-Exit (z. B. mit 2-Bar-Bestätigung statt sofortiger Reaktion, um die 9 "false positives" aus dieser Analyse zu vermeiden, ohne die Verlustbegrenzung für die echten 32 `atr_stop`-Fälle zu verlieren) UND einem strengeren Entry-Gate — die Kombination wurde in Schritt 11 nur mit dem *aktiven* `regime_change`-Exit getestet, nie mit einer modifizierten Variante.
- Grundsätzlicher: prüfen, ob `mean_reversion_v1` in der aktuellen Form für die letzten 180 Tage BTC/ETH-Marktbedingungen überhaupt geeignet ist (das Symbol/der Zeitraum könnte strukturell zu trendlastig für eine reine Mean-Reversion-Strategie sein) — ein Test auf einem längeren oder anderen Zeitraum/Symbol könnte klären, ob das Problem strategie- oder marktbedingt ist.
