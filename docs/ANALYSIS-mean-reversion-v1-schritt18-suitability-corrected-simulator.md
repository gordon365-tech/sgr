# Analyse: mean_reversion_v1 — Eignungsanalyse mit korrigiertem Simulator (Schritt 18)

**Basis-Commit:** `1584e08` (fix(backtesting): correct short-position cash accounting in BacktestSimulator, Schritt 17)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage, BTC/USDT und ETH/USDT, 1h und 4h — identische Methodik/Datenquelle wie Schritt 16, jetzt mit dem korrigierten Simulator als alleiniger Grundlage
**Status:** Reine Neumessung. Keine Codeänderung an Strategie, Entry/Exit-Regeln, Parametern, Risk-Sizing, Fees, Slippage-Annahmen, Production-Konfiguration, Order Safety, Kill Switch oder Deployment.

---

## Explizite Verifikation: korrigierter Simulator aktiv

Vor jeder Messung wurde per Instrumentierung erneut geprüft, ob der in Schritt 16 gefundene und in Schritt 17 behobene Cash-Buchungsfehler tatsächlich behoben ist:

| | Long | Short |
|---|---|---|
| Cash-Delta stimmt exakt mit `net_pnl` überein | 84/84 | **77/77** (vorher 5/77) |

Gesamt-Diskrepanz Cash vs. `Σnet_pnl`: **0,000000** (vorher 208,52 $). Natives `bt.sharpe_ratio` und `bt.total_return_pct` (direkt aus der Equity-Kurve, nicht mehr aus einer Workaround-Rekonstruktion) stimmen jetzt mit der in Schritt 16 verwendeten bereinigten Methodik überein (Sharpe −6,95 nativ vs. −7,01 bereinigt — im Rahmen methodisch bedingter kleiner Abweichungen). **Alle folgenden Zahlen stammen direkt aus `BacktestResult` (nativ), nicht aus einer Workaround-Rekonstruktion.**

---

## 1. Vorher-Nachher-Vergleich (BTC/USDT, 1h, identischer 180-Tage-Zeitraum)

| Kennzahl | Vorher (Bug, Schritt 10–15, nativ) | Nachher (korrigiert, nativ) | Veränderung |
|---|---|---|---|
| Trades | 161 | 161 | unverändert |
| Sharpe | ≈ −3,52 | **−6,98** | deutlich negativer |
| Return | ≈ −2,09 % | **−4,14 %** | ~doppelt so negativ |
| Max Drawdown | ≈ 2,66 % | **4,49 %** | deutlich höher |
| Profit Factor | 0,366 | 0,367 | **praktisch unverändert** |
| Hit Rate | 34,8 % | 34,8 % | **unverändert** |

**Erklärung:** Profit Factor und Hit Rate werden direkt aus den (bereits vor dem Fix korrekten) `net_pnl`-Werten pro Trade berechnet — daher unverändert. Sharpe, Return und Max Drawdown stammen aus der Equity-Kurve, die der Bug korrumpierte — hier zeigt sich der volle Effekt der Korrektur. **Der Fix macht das Bild klar schlechter, nicht besser.**

## 2. Vollständige korrigierte Kennzahlen

### Symbol × Timeframe (nativ, korrigiert)

| Symbol | TF | n | Hit-Rate | PF | Return | Max DD | Sharpe | Sortino | Calmar | Ø Gewinn | Ø Verlust |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC/USDT | 1h | 161 | 34,8 % | 0,368 | −4,12 % | 4,49 % | −6,949 | −4,673 | −1,822 | 4,30 $ | −6,22 $ |
| ETH/USDT | 1h | 167 | 46,1 % | 0,586 | −3,22 % | 3,61 % | −3,739 | −2,387 | −1,782 | 5,94 $ | −8,67 $ |
| BTC/USDT | 4h | 29 | 41,4 % | 0,473 | −1,18 % | 1,46 % | −2,000 | −1,201 | −1,625 | 8,88 $ | −13,25 $ |
| ETH/USDT | 4h | 31 | 41,9 % | 0,403 | −2,46 % | 3,34 % | −2,879 | −1,471 | −1,476 | 12,74 $ | −22,86 $ |

Alle vier Kombinationen bleiben unprofitabel (PF < 1). Das relative Ranking (ETH besser als BTC, 4h besser als 1h) ist identisch zu Schritt 16.

### Long vs. Short (BTC/USDT 1h, nativ, korrigiert)

| Seite | n | Hit-Rate | PF | Netto-PnL | Ø Gewinn | Ø Verlust |
|---|---|---|---|---|---|---|
| Long | 84 | 40,5 % | 0,395 | −206,56 $ | 3,97 $ | −6,83 $ |
| Short | 77 | 28,6 % | 0,338 | −206,35 $ | 4,79 $ | −5,67 $ |

Beide Seiten tragen nahezu gleich stark zum Gesamtverlust bei (−206,56 $ vs. −206,35 $) — keine Seite ist der "eigentliche" Verlustträger; ein reines Long-only- oder Short-only-Regime wäre keine Lösung (wäre ohnehin eine Parameteränderung und damit außerhalb des Auftrags).

## 3. Walk-Forward-Ergebnisse

**Wichtiger struktureller Befund, unabhängig vom Cash-Bug:** Der eingebaute `WalkForwardAnalyzer` liefert für dieses 180-Tage/1h-Dataset in **allen 6 Splits exakt 0 Trades** — nicht wegen schlechter Performance, sondern weil jedes OOS-Fenster (`oos_size = max(split_size // 6, 50)` ≈ 102 Bars) kleiner ist als die im Simulator hartcodierte Warmup-Anforderung von 200 Bars (`for bar_idx in range(warmup, len(candles))` — bei 102 Kerzen ist dieser Bereich leer, die Handelsschleife läuft nie). **Das bedeutet: jede in Schritt 10–16 berichtete "Walk-Forward inconsistent"-Bewertung maß in Wirklichkeit 0 Datenpunkte, nicht echte Inkonsistenz.** Dieser Befund betrifft die Splitgrößen-Logik in `WalkForwardAnalyzer`, nicht den behobenen Cash-Bug — keine Codeänderung vorgenommen (außerhalb des Auftragsumfangs), da rein analytischer Auftrag.

**Ersatzweise durchgeführt:** ein manueller In-Sample/Out-of-Sample-Split mit ausreichend großen Fenstern, unter Verwendung derselben, unveränderten `BacktestSimulator`/`PerformanceAnalyzer`-Klassen (keine Codeänderung, nur andere Fenstergrenzen).

**Hinweis zur Methodik:** `mean_reversion_v1` hat feste, nicht an Daten angepasste Parameter — es gibt keinen "Fitting"-Schritt. Dieser Test prüft daher zeitliche Stabilität der Performance, nicht klassisches ML-Overfitting.

| | Zeitraum | n | Sharpe | Return | PF | Hit-Rate | Max DD |
|---|---|---|---|---|---|---|---|
| In-Sample | erste 120 Tage | 98 | −5,442 | −2,14 % | 0,461 | 37,8 % | 2,52 % |
| Out-of-Sample | letzte 60 Tage | 54 | **−8,480** | −1,51 % | **0,244** | 31,5 % | 1,55 % |

**Out-of-Sample ist auf Sharpe und Profit Factor klar schlechter als In-Sample** — keine Verbesserung außerhalb der "gesehenen" Periode, im Gegenteil.

### OOS-Sub-Splits (3 × ~20 Tage, feinere Granularität)

| Split | n | Sharpe | Return | PF | Hit-Rate | Max DD |
|---|---|---|---|---|---|---|
| 1 | 15 | −6,731 | −0,28 % | 0,399 | 40,0 % | 0,36 % |
| 2 | 9 | −7,476 | −0,27 % | 0,212 | 22,2 % | 0,27 % |
| 3 | 12 | −9,700 | −0,47 % | 0,110 | 33,3 % | 0,51 % |

**Alle drei Sub-Splits sind negativ auf Sharpe und PF — mit einer sich verschlechternden Tendenz über die Zeit** (PF fällt monoton von 0,399 auf 0,110). Konsistent negativ, kein isolierter Ausreißer.

## 4. Robustheitsbewertung

- **Konsistenz über Symbole:** negativ in beiden getesteten Symbolen (BTC, ETH), auf beiden Timeframes (1h, 4h) — 4/4 Kombinationen unprofitabel.
- **Konsistenz über Zeit:** IS und OOS beide negativ; alle 3 OOS-Sub-Splits negativ, mit verschlechternder statt stabilisierender Tendenz.
- **Konsistenz über Richtung:** Long und Short beide unprofitabel, nahezu identischer Verlustbeitrag — keine ausnutzbare Asymmetrie.
- **Kein Hinweis auf isolierte Glücksperioden, die das Gesamtbild verzerren:** die Verschlechterung ist über Zeit, Symbol, Timeframe und Richtung hinweg konsistent, nicht auf einen einzelnen Ausreißer-Split zurückzuführen.

## 5. Kosten-Sensitivität — echter ökonomischer Edge?

Direkter Vergleich desselben 180-Tage-BTC/USDT-1h-Laufs mit realistischen Kosten vs. Null-Kosten (identische Signale, nur Fee/Slippage auf 0 gesetzt):

| | Realistisch (0,1 % Fee, 0,05 % Slippage) | Null-Kosten |
|---|---|---|
| Trades | 161 | 160 |
| Sharpe | −6,977 | **−1,427** |
| Return | −4,14 % | **−0,82 %** |
| Profit Factor | 0,367 | **0,823** |
| Hit-Rate | 34,8 % | **48,1 %** |
| Gesamtkosten (Fee+Slippage) | 271,29 $ | 0 $ |

**Befund:** Selbst bei vollständigem Kostenausschluss bleibt der Profit Factor mit 0,823 unter 1,0 — die Strategie ist **auch ohne jede Handelskosten nicht profitabel**, allerdings deutlich näher am Breakeven (Hit-Rate fast 50/50). Realistische Kosten machen ~80 % des tatsächlichen Gesamtverlusts aus (von −0,82 % auf −4,14 % Return). **Interpretation:** Es existiert ein schwaches, aber reales statistisches Signal (kein reines Rauschen — sonst läge PF bei Null-Kosten nicht bei 0,823 mit Hit-Rate nahe 48 %), das jedoch bei der aktuellen Handelsfrequenz (161 Trades / 180 Tage) nicht ausreicht, um realistische Transaktionskosten zu decken. Das beantwortet die Frage nach "ökonomisch bedeutsamem Edge" präzise: **ein sehr schwacher Edge ist vorhanden, aber er ist nicht ökonomisch nutzbar** — weder isolierte Glücksperiode noch überhaupt kein Signal, sondern ein reales, aber zu dünnes Signal.

## 6. Marktregime-Befunde (unverändert aus Schritt 16, vom Cash-Bug nicht betroffen)

Die in Schritt 16 durchgeführte marktunabhängige Charakterisierung (Variance-Ratio-Test, RANGING-Klassifikations-Forward-Return-Test, Trendstärke-Korrelation nach Monat) basiert ausschließlich auf Kursdaten und Indikatorwerten — **unberührt vom Cash-Buchungsfehler**, da dieser ausschließlich die Cash-/Equity-Buchhaltung betraf, nicht die Kursreihen, Indikatoren oder Regime-Klassifikation. Diese Befunde bleiben unverändert gültig: BTC +7,96 %/ETH +12,37 % Netto-Aufwärtstrend über den Zeitraum, Variance Ratio 0,88–0,98 (schwache Mean-Reversion-Tendenz), RANGING-klassifizierte Bars ohne nutzbaren Forward-Return-Edge, Performance invers korreliert mit monatlicher Trendstärke.

---

## Abschluss

### 1. Vorher-vs-korrigiert-Vergleich
Siehe Punkt 1. Profit Factor/Hit-Rate unverändert (waren nie vom Bug betroffen); Sharpe, Return und Max Drawdown verschlechtern sich nach der Korrektur deutlich (Sharpe −3,52 → −6,98, Return −2,09 % → −4,14 %, Max DD 2,66 % → 4,49 %).

### 2. Vollständige korrigierte Kennzahlen
Siehe Punkt 2 (Symbol×Timeframe-Grid, Long/Short-Aufschlüsselung).

### 3. Walk-Forward-Ergebnisse
Eingebauter Mechanismus strukturell unbrauchbar für dieses Datenfenster (0 Trades in allen 6 Splits — Fenstergröße kleiner als Warmup-Anforderung, unabhängig vom behobenen Bug). Manueller IS/OOS-Ersatzsplit: IS Sharpe −5,44 / PF 0,461, OOS Sharpe −8,48 / PF 0,244 — Out-of-Sample-Performance ist schlechter, nicht besser, mit konsistent negativen Sub-Splits.

### 4. Robustheitsbewertung
Konsistent negativ über Symbole (BTC, ETH), Timeframes (1h, 4h), Zeitperioden (IS, OOS, 3 Sub-Splits) und Handelsrichtung (Long, Short). Kein Hinweis auf isolierte Ausreißerperioden.

### 5. Klassifikation

# **C — Strategie für den untersuchten Zeitraum weiterhin grundsätzlich ungeeignet**

Die Klassifikation ändert sich **nicht** gegenüber Schritt 16. Der korrigierte Simulator zeigt ein **deutlicheres, nicht milderes** Bild der Unprofitabilität.

### 6. War die C-Klassifikation durch den Simulator-Bug verursacht, oder ist die Strategie grundsätzlich ungeeignet?

**Eindeutig Letzteres.** Der Cash-Buchungsfehler betraf ausschließlich die Equity-Kurven-basierten Kennzahlen (Sharpe, Return, Max Drawdown, Calmar) sowie nachgelagerte Positionsgrößen — nicht die trade-basierten Kennzahlen (Profit Factor, Hit-Rate, `net_pnl`), die bereits vor der Korrektur korrekt waren und unverändert im unprofitablen Bereich lagen (PF 0,366–0,367 vor und nach dem Fix). Nach der Korrektur zeigt sich: der Fehler hatte die tatsächliche Schwäche der Strategie **unterschätzt**, nicht überzeichnet — die korrigierte Sharpe Ratio ist fast doppelt so negativ wie zuvor berichtet. Zusätzlich liefert diese Session neue, vom Bug unabhängige Belege (Kosten-Sensitivität: unprofitabel selbst bei Null-Kosten, PF 0,823; Out-of-Sample-Performance schlechter als In-Sample; Long und Short beide gleichermaßen unprofitabel), die die Schritt-16-Einschätzung eigenständig bestätigen, nicht nur wiederholen. **Die C-Klassifikation war und ist korrekt — sie beruhte nicht auf dem Simulator-Fehler.**
