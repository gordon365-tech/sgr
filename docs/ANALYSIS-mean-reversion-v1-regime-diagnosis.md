# Analyse: mean_reversion_v1 — Regime-Diagnose (Schritt 10)

**Basis-Commit:** `31bb49a` (feat(backtesting): add regime-change exit for RANGING-entry positions)
**Datum:** 2026-09-14
**Zeitraum der Backtests:** 180 Tage, BTC/USDT, 1h-Timeframe, ~4120 Bars
**Status:** Diagnose abgeschlossen. Entry-Logik-Überarbeitung bewusst als offener Folgeschritt vorgemerkt, nicht Teil dieses Commits.

---

## Ausgangsfrage

Warum liefern `mean_reversion_v1` und `trend_following_v1` beide negativen Sharpe im Backtest, und was — falls überhaupt etwas — lässt sich daran mit vertretbarem Aufwand verbessern? Diese Analyse behandelt ausschließlich `mean_reversion_v1`.

## Methodik

Reine Diagnose vor jeder Codeänderung — jeder Schritt wurde erst mit echten Backtest-Daten verifiziert, bevor der nächste Schritt oder eine Änderung folgte. Kein Parameter-Tuning ohne vorherigen empirischen Befund. Alle Zahlen stammen aus manuellen Diagnoseläufen gegen den produktiven Worker-Container auf dem Server (nicht aus Unit-Tests), unter Verwendung von `StrategyValidationRunner`/`BacktestingEngine.run_full_validation()` mit Monkey-Patches auf `BacktestSimulator`, die ausschließlich zusätzliche Diagnosedaten mitschnitten, ohne die Simulationslogik selbst zu verändern.

## Diagnosekette

### 1. Baseline (vor jeder Änderung in dieser Session)

| Metrik | Wert |
|---|---|
| Trades | 119 |
| Sharpe | −1,52 |
| Return | −1,3 % |
| `exit_reason`-Verteilung | `time_exit`: 77 (64,7 %), `atr_stop`: 41 (34,5 %), `backtest_end`: 1 |

**Befund:** `exit_reason` wurde vor dieser Session nirgends persistiert — nur `log.debug`, dann verworfen (siehe Commit `2e381a9`, Teil A). Kein einziger Trade wurde je durch das eigentliche Mean-Reversion-Ziel (`target_price` aus `signal.metadata`, BB-Middle) geschlossen, weil dieser Exit-Pfad im Simulator schlicht nicht existierte.

### 2. Fix: `target_price` als Exit-Bedingung nutzen (Commit `fa408b5`, Teil B)

| Metrik | Vorher | Nachher |
|---|---|---|
| Trades | 119 | 155 |
| Sharpe | −1,52 | **−3,73** |
| Return | −1,3 % | **−2,84 %** |
| `exit_reason` | — | `target_reached`: 64 (89,1 % Win-Rate, +262,79), `time_exit`: 39 (33,3 %, −78,25), `atr_stop`: 51 (0,0 % Win-Rate, **−638,68**), `backtest_end`: 1 |

**Befund:** Der Fix funktioniert technisch korrekt — `target_reached` zeigt eine exzellente Win-Rate. Aber die Gesamt-Performance verschlechtert sich, weil die höhere Trade-Frequenz (schnellere Exits → mehr neue Positionen) auch mehr `atr_stop`-Trades erzeugt, und diese 51 Trades mit 0 % Win-Rate dominieren den Gesamtverlust (−638,68 von netto −375,89).

### 3. Diagnose des `atr_stop`-Verlustpfads (vor jeder weiteren Änderung)

**Entry-Merkmale, Stops (n=51) vs. Targets (n=64):**

| Merkmal | Stops | Targets | Trennschärfe? |
|---|---|---|---|
| Ø Confidence | 0,689 | 0,668 | Nein |
| Median Confidence | 0,647 | 0,647 | Nein (identisch) |
| Ø RSI | 47,79 | 49,09 | Nein |
| Median RSI | 43,13 | 43,30 | Nein |
| Ø BB-Position | 0,401 | 0,431 | Nein |
| Median BB-Position | 0,137 | 0,123 | Nein |
| **Ø ADX (Entry)** | **18,57** | **16,94** | **Ja** |
| ADX 20–25 ("Grauzone") | 17 (33,3 %) | 13 (20,3 %) | Ja |

**Befund:** Confidence, RSI und BB-Position trennen die beiden Gruppen praktisch nicht. Entry-ADX zeigt als einziges geprüftes Merkmal eine klare Trennschärfe — Stop-Trades sind überproportional in der ADX-Grauzone (20–25) vertreten, einem Bereich, den `_detect_regime_simple()` (siehe unten) fälschlich als `RANGING` klassifiziert.

**Post-Entry-ADX-Entwicklung** (17 Grauzone-Stop-Trades, ADX 3/5/10 Bars nach Entry gemessen): 12 von 17 (70,6 %) zeigen einen ADX-Anstieg von mehr als 5 Punkten innerhalb der ersten 10 Bars — der Markt entwickelte sich tatsächlich in Richtung Trend, während die Mean-Reversion-Position offen blieb.

### 4. `_detect_regime_simple()` — dokumentierte Ist-Logik

```
1. adx_14 oder rsi_14 fehlt         -> UNKNOWN
2. adx_14 > 25:
   2a. rsi_14 > 55 UND DI+ > DI-    -> TRENDING_UP
   2b. rsi_14 < 45 UND DI- > DI+    -> TRENDING_DOWN
   2c. sonst (adx_14 > 25, aber
       weder 2a noch 2b)            -> fällt durch zu Schritt 4/5
3. adx_14 < 20                      -> RANGING (sofort)
   [ADX 20-25, inklusive beider
   Grenzen: "Grauzone", fällt
   ebenfalls durch]
4. atr_pct > 0.05                   -> HIGH_VOLATILITY
5. Rest (inkl. ADX-Grauzone UND
   ADX > 25 ohne klare
   DI-Bestätigung)                  -> RANGING
```

Zwei Fallthrough-Pfade landen beide bei `RANGING`: die Grauzone `20 ≤ ADX ≤ 25` und `ADX > 25` ohne eindeutige Trendrichtung. Diese Klassifikation ist nicht per se falsch (siehe unten), aber sie ist die technische Wurzel dafür, dass RANGING-only-Strategien wie `mean_reversion_v1` in diesen Grenzbereichen Signale generieren.

### 5. Fix: Regime-Change-Exit für RANGING-Entry-Positionen (Commit `31bb49a`)

Bewusst eng gefasst, per expliziter Entscheidung: keine zweite parallele ADX-Schwelle, keine globale Simulator-Änderung. Wiederverwendet die bestehende, pro Bar ohnehin berechnete `_detect_regime_simple()`. Gilt nur für Positionen mit `pos.regime == RANGING` beim Entry — `trend_following_v1` ist strukturell nie betroffen (eröffnet nur in TRENDING_UP/DOWN), verifiziert per Regressionstest, nicht nur behauptet.

| Metrik | Nur `target_price` | + Regime-Change |
|---|---|---|
| Trades | 155 | 161 |
| Sharpe | −3,73 | **−3,565** |
| Return | −2,84 % | **−2,11 %** |
| `exit_reason` | siehe oben | `target_reached`: 54 (88,9 %, +217,50), `time_exit`: 12 (16,7 %, −36,11), `regime_change`: **75 (8,0 %, −368,59)**, `atr_stop`: 19 (0,0 %, −227,24), `backtest_end`: 1 |

**Befund:** `atr_stop` ging deutlich zurück (51 → 19 Trades, −638,68 → −227,24 Verlust) — der Regime-Change-Exit fängt einen erheblichen Teil der vorherigen Stop-Trades ab, bevor der volle ATR-Stop-Schaden entsteht. Aber `regime_change` selbst wird mit 75 Trades und nur 8,0 % Win-Rate zum neuen, fast gleich großen Verlustblock (−368,59). Kombiniert: `−368,59 − 227,24 = −595,83` gegen `target_reached`s `+217,50`. Netto-Ergebnis kaum verbessert.

**Wichtig — Erfolgskriterium nicht erreicht:** Ziel war nicht, `atr_stop`-Trades in `regime_change` umzubenennen, sondern den Gesamtverlust dieses Verlustpfads zu reduzieren. Das ist nur teilweise gelungen.

### 6. Timing-Diagnose der 75 `regime_change`-Trades

**`bars_held`-Verteilung:**

| Bucket | n | Anteil |
|---|---|---|
| 1–3 Bars | 38 | 50,7 % |
| 4–10 Bars | 25 | 33,3 % |
| >10 Bars | 12 | 16,0 % |

Median: 3 Bars, Durchschnitt: 5,5 Bars. **Die Erkennung selbst ist schnell — kein Timing-Problem der Regime-Erkennung.**

**MAE pro Bar seit Entry (alle 75 Trades):**

| Bar | Ø MAE |
|---|---|
| 1 | 145,14 |
| 2 | 181,48 |
| 3 | 228,80 |
| (Final, beim Exit) | 406,15 |

35,7 % des finalen Schadens ist bereits nach Bar 1 erreicht, 56,3 % nach Bar 3. **Der Schaden entsteht überwiegend sofort nach Entry, nicht durch einen langsam einsetzenden Trend.** Das schließt ein reines Exit-Timing-Problem aus — selbst eine theoretisch perfekte, sofortige Regime-Erkennung hätte einen erheblichen Teil dieses Schadens nicht verhindert.

### 7. Entry-Signal-Position: `regime_change` vs. `target_reached`

| Merkmal | `regime_change` (n=75) | `target_reached` (n=54) |
|---|---|---|
| Ø RSI | 50,15 | 47,70 |
| **Ø BB-Position** | **0,53** | **0,347** |
| **Ø ADX** | **20,47** | **16,43** |
| Long/Short | 35/40 | 33/21 |

**Befund:** Erstmals eine klare Entry-Qualitäts-Trennschärfe, sichtbar erst nach Aufschlüsselung nach dem neuen `regime_change`-Grund (in der ursprünglichen, unaufgeschlüsselten Stops-vs-Targets-Analyse aus Schritt 3 war diese Differenzierung nicht sichtbar, da sich `atr_stop`- und `regime_change`-Fälle dort noch vermischten). `regime_change`-Trades starten im Schnitt mit einer BB-Position von 0,53 — nahe der Bollinger-Band-Mitte, nicht am Rand. Das widerspricht dem Kernprinzip von Mean-Reversion (Einstieg nahe den Extremen). Zusätzlich Short-Übergewicht bei `regime_change` (40 Short vs. 35 Long), während `target_reached` ein Long-Übergewicht zeigt (33 vs. 21) — möglicher Hinweis auf einen strukturellen Nachteil der Short-Seite über den geprüften Zeitraum, nicht weiter untersucht.

## Gesamtfazit

Die Diagnosekette verschiebt die Ursache konsistent **vom Exit zum Entry**:

1. Fehlender Ziel-Exit war real, aber nicht die Hauptursache (Sharpe verschlechterte sich nach dem Fix).
2. Fehlender Regime-Change-Exit war real, reduzierte den `atr_stop`-Verlust deutlich, löste das Gesamtproblem aber nicht (neuer, fast gleich großer Verlustblock).
3. Timing-Diagnose schließt ein Exit-Timing-Problem aus (Erkennung ist schnell, Schaden entsteht trotzdem sofort).
4. Entry-Signal-Diagnose zeigt: ein erheblicher Teil der Verlust-Trades wird in einer Zone eröffnet, die weder klar RANGING (ADX) noch klar an den Bollinger-Rändern (BB-Position) liegt — der Entry selbst akzeptiert zu schwache Setups als handelbar.

**Empfehlung für den nächsten Schritt (nicht Teil dieser Session):** Entry-Logik von `mean_reversion_v1` überarbeiten, nicht weiteres Exit-Tuning. Konkret zu prüfen:
- `bb_position_long`/`bb_position_short`-Schwellen (aktuell 0,15/0,85) schärfen oder als härteres Gate statt nur Scoring-Gewichtung behandeln.
- ADX direkt als Entry-Gate nutzen (z. B. nur ADX < 15–18 statt der aktuellen impliziten Duldung der 20–25-Grauzone über das Regime-Gate).
- Long/Short-Asymmetrie in der Win-Rate separat untersuchen, bevor Parameter geändert werden.

Beide in dieser Session umgesetzten Exit-Fixes (`target_price`-Nutzung, Regime-Change-Exit) bleiben bestehen — sie sind architektonisch korrekt, vollständig getestet, und eine echte Verbesserung der Simulator-Treue (Strategie-Signale werden jetzt tatsächlich genutzt, statt ignoriert zu werden), auch wenn sie allein nicht ausreichten, um `mean_reversion_v1` in den Go-Live-Bereich zu bringen.

## Offener Nebenbefund (bewusst nicht Teil dieser Analyse)

Während der manuellen Diagnoseläufe trat wiederholt `Database not initialized. Call init_db() first.` auf, gefolgt von `db_read_failed` / `public_loading` / `public_loaded` / `persist_failed`. Ursache: die manuellen Diagnoseskripte riefen `StrategyValidationRunner`/`BacktestingEngine` direkt auf, außerhalb des normalen Worker-Startup-Lifecycles, in dem `init_db()` normalerweise in `sgr/api/main.py` vor jeder DB-Nutzung läuft. Die Backtests selbst blieben dadurch fachlich gültig (Daten wurden korrekt von Binance öffentlich geladen, nur ohne DB-Cache/Persistenz), aber der Befund sollte separat geprüft werden, falls künftige manuelle Diagnose-Skripte denselben Pfad nutzen sollen. Kein Produktionscode-Bug — der reguläre Worker-Startup ruft `init_db()` korrekt auf, siehe die server-seitig bereits verifizierten `db_cache_hit`-Logs aus den vorherigen Commits dieser Session.
