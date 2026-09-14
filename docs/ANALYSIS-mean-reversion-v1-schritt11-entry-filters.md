# Analyse: mean_reversion_v1 — Entry-Filter-Test (Schritt 11)

**Basis-Commit:** `fb41491` (feat(risk): implement Kill Switch position closing via PositionLiquidator)
**Datum:** 2026-09-14
**Zeitraum der Backtests:** 180 Tage, BTC/USDT + ETH/USDT, 1h-Timeframe
**Status:** Diagnose abgeschlossen. Keine Codeänderung an der Strategie — beide getesteten Hypothesen reichen nicht aus, siehe Fazit. Nächster Schritt (Exit-Struktur) bewusst als offener Folgeschritt vermerkt, nicht Teil dieser Session.

---

## Ausgangsfrage

Schritt 10 (`docs/ANALYSIS-mean-reversion-v1-regime-diagnosis.md`) empfahl als nächsten Schritt eine Überarbeitung der Entry-Logik statt weiterer Exit-Anpassungen, konkret:
1. `bb_position_long`/`bb_position_short`-Schwellen schärfen oder als hartes Gate statt Scoring-Gewichtung.
2. ADX direkt als Entry-Gate nutzen (statt der impliziten Duldung der 20–25-Grauzone über das Regime-Gate).

Diese Session testet beide Hypothesen empirisch, vor jeder Codeänderung.

## Methodik

Gleiche Disziplin wie Schritt 10: erst messen, dann Hypothese, dann Test, dann erneut messen — keine Parameteränderung im Code ohne vorherigen empirischen Beleg. Getestet wurde per Monkeypatch auf `MeanReversionStrategy.generate_signal()` in einem lokalen Diagnoseskript (nicht Teil des Repos), das nach dem regulären Signal-Erzeugungspfad zusätzlich ADX- und/oder BB-Position-Kriterien hart prüft, bevor ein Signal durchgelassen wird — die eigentliche Scoring-Logik bleibt unverändert. Datenquelle: `BacktestingEngine.run_full_validation()` gegen öffentliche Binance-Mainnet-Daten (gleicher Pfad wie `StrategyValidationRunner` im Produktivbetrieb), 180 Tage, BTC/USDT + ETH/USDT, 1h.

## Ergebnisse

| Variante | Trades | Sharpe | Return | Profit Factor | Hit Rate | Max DD |
|---|---|---|---|---|---|---|
| Baseline (kein Gate) | 161 | −3,476 | −2,06 % | 0,365 | 34,8 % | 2,66 % |
| ADX-Gate < 20 | 107 | −2,150 | −1,08 % | 0,353 | 42,1 % | 1,73 % |
| ADX-Gate < 18 | 82 | −2,164 | −0,95 % | 0,352 | 43,9 % | 1,67 % |
| ADX-Gate < 15 | 55 | −3,184 | −1,08 % | 0,283 | 43,6 % | 1,30 % |
| ADX-Gate < 12 | 15 | −0,476 | −0,08 % | 0,241 | 40,0 % | 0,33 % (nur 15 Trades — nicht signifikant) |
| ADX < 18 + BB hart (long<0,10 / short>0,90) | 67 | −1,634 | −0,66 % | 0,379 | 43,3 % | 1,25 % |
| ADX < 18 + BB hart (long<0,05 / short>0,95) | 60 | −1,347 | −0,54 % | 0,377 | 40,0 % | 1,15 % |

(Der letzte Testlauf — reines BB-Hart-Gate ohne ADX-Gate — konnte wegen eines transienten lokalen Netzwerkfehlers zur Binance-API nicht mehr abgeschlossen werden; die bereits vorliegenden sieben Varianten liefern aber ein eindeutiges, konsistentes Bild.)

## Befund

Beide Hypothesen wirken in die erwartete Richtung — Hit Rate steigt von 34,8 % auf 40–44 %, Sharpe verbessert sich von −3,48 auf bis zu −1,35 (kombiniertes Gate), Max Drawdown sinkt deutlich. **Aber der Profit Factor bleibt in jeder einzelnen Variante zwischen 0,24 und 0,38 — weit unter dem Go-Live-Kriterium von 1,3, und kaum besser als die Baseline (0,365).**

Das ist der entscheidende Punkt: Entry-Filter (welche Trades überhaupt eröffnet werden) verbessern, wie oft die Strategie gewinnt, aber nicht, wie viel sie im Verhältnis gewinnt vs. verliert. Ein Profit Factor um 0,3–0,38 bei einer Hit Rate von 40–44 % bedeutet zwingend: der durchschnittliche Verlust-Trade ist um ein Vielfaches größer als der durchschnittliche Gewinn-Trade. Das ist strukturell ein Risk/Reward-Problem der Exit-Konfiguration (Target = BB-Middle/SMA20, Stop = 1,5× ATR), nicht ein Entry-Qualitätsproblem — kein Entry-Filter kann eine asymmetrische Auszahlungsstruktur kompensieren, in der jeder einzelne Verlust im Schnitt mehr kostet als jeder Gewinn einbringt.

**Erfolgskriterium nicht erreicht** — analog zu Schritt 10: Ziel war eine Strategie, die dem Go-Live-Gate näherkommt, nicht nur bessere Einzelmetriken. Keine der sieben Varianten kommt in die Nähe von PF ≥ 1,3.

## Entscheidung dieser Session

Bewusst **keine** dieser Änderungen in `sgr/strategy/mean_reversion.py` übernommen. Ein ADX- oder BB-Hart-Gate zu committen, das den Profit Factor nachweislich nicht über 0,38 hebt, wäre eine Parameteränderung ohne echten Nutzen für das eigentliche Ziel (Go-Live-Fähigkeit) — genau das, was vermieden werden soll. Die Strategie bleibt unverändert; die Deaktivierung durch den bestehenden `StrategyValidationRunner`-Gate bleibt vollständig korrekt und weiterhin die richtige Entscheidung.

## Empfehlung für den nächsten Schritt (nicht Teil dieser Session)

Das Risk/Reward-Verhältnis der Exit-Struktur direkt untersuchen, statt weiterer Entry-Filter-Varianten:
- Durchschnittliche Gewinn- vs. Verlust-Distanz (in ATR-Einheiten) je `exit_reason` separat messen — vermutlich liegt die BB-Middle im Schnitt deutlich näher am Entry als 1,5× ATR, was strukturell ein ungünstiges R:R erzeugt.
- Alternative Stop-Distanzen (z. B. 0,75×–1,0× ATR statt 1,5×) oder ein Mindest-R:R-Gate vor Signal-Erzeugung testen (Signal nur wenn `|target_price - entry| / |stop_price - entry| >= 1.0`).
- Erst danach erneut mit den hier getesteten Entry-Filtern kombinieren, falls das R:R-Problem gelöst ist — Entry-Filter und Exit-Struktur wirken vermutlich additiv, nicht als Ersatz füreinander.

Diese Diagnose ist reine Messung ohne Codeänderung an der Strategie selbst — keine Tests nötig, da kein Produktionscode geändert wurde.
