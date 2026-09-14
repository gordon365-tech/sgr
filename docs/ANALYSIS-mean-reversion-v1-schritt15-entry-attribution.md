# Analyse: mean_reversion_v1 — Entry-Attribution und kontrolliertes Gate-Experiment (Schritt 15)

**Basis-Commit:** `f4296e9` (docs: test removing the regime_change exit entirely, Schritt 14)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage, `regime_change` bleibt aktiv und unverändert
**Status:** Ein Entry-Gate entwickelt, A/B-getestet, Robustheit geprüft. **Ergebnis: NICHT VERBESSERT.** Kein Code in `sgr/strategy/mean_reversion.py` geändert — Experiment ausschließlich als Backtest-Monkeypatch, keine Live-Auswirkung.

---

## 0. Wichtige Methodik-Korrektur (betrifft auch Schritt 10–14 rückwirkend)

Bei der Rekonstruktion des Entry-Kontexts wurde ein struktureller Befund zur Backtest-Engine entdeckt: `BacktestingEngine._run_full_validation_body()` lädt Kerzen korrekt für **beide** konfigurierten Symbole (`BTC/USDT`, `ETH/USDT`), aber `BacktestSimulator.run()` verarbeitet nur `self._config.symbols[0]` (`sgr/backtesting/simulator.py`, dokumentierter MVP-Kommentar "Multi-Symbol: timestamps alignment nötig (für MVP: erstes Symbol)"). **Alle 161 Trades in Schritt 10–14 und in diesem Schritt sind faktisch BTC/USDT-only** — ETH/USDT-Daten werden geladen, aber nie simuliert. Das ist kein neuer Fehler dieser Session, sondern ein bestehendes, dokumentiertes Engine-Verhalten, das bisher in keiner der Analysen explizit erwähnt wurde. Auswirkung: Die Stichprobe ist ein einzelnes Symbol über einen einzelnen 180-Tage-Zeitraum, nicht zwei unabhängige Märkte — entsprechend vorsichtig sind alle Schlussfolgerungen (dieser und vorheriger Schritte) zu lesen. Keine Änderung in dieser Session — reine Offenlegung, Fix wäre ein separates, in sich geschlossenes Ticket (Multi-Symbol-Aggregation im Simulator).

---

## 1. Entry-Ursache — wo im Code der Entry entsteht

- **Entscheidungspunkt:** `sgr/strategy/mean_reversion.py`, `MeanReversionStrategy.generate_signal()` (Zeile 70–124).
- Regime-Gate zuerst: `if context.regime != MarketRegime.RANGING: return None` (Zeile 74).
- Score-Berechnung in `_score_long()` / `_score_short()` (Zeile 126–229): sechs gewichtete Kriterien (RSI-Extremität 2,5 / BB-Kantennähe 2,0 / MACD-Drehung 1,5 / Orderbook-Imbalance 1,0 / BB-Squeeze 1,0 / ADX-niedrig 0,5), Summe max. 8,5.
- Entscheidung: `long_conf = long_score / long_max`; Signal nur wenn `confidence >= min_confidence (0,55)` (Zeile 88, 106).
- **Strukturbefund:** `context.primary.orderbook` ist im Backtest immer `None` (`sgr/backtesting/simulator.py:277`, `orderbook=None`) — das Orderbook-Imbalance-Kriterium (Gewicht 1,0) trägt im Backtest **nie** zum Score bei, senkt aber die maximal erreichbare Confidence strukturell (max. real erreichbar ≈ 7,5/8,5 ≈ 0,882 statt 1,0). Betrifft alle Trades gleichermaßen, keine Verzerrung zwischen Gewinnern/Verlierern, aber relevant für die Interpretation von "Signalstärke".

## 2. Analyse der Entry-Bedingungen (alle 161 Trades)

Rekonstruiert durch unabhängiges Neuladen derselben Kerzen + Feature-Vorausberechnung (`BacktestSimulator._precompute_all_features()`, `_detect_regime_simple()`) und erneuten Aufruf von `strategy.generate_signal()` mit exakt dem Kontext, den der Simulator zum Signal-Zeitpunkt hatte — keine Simulationslogik verändert, nur zusätzlich ausgelesen.

| Geprüftes Kriterium | Befund |
|---|---|
| Regime beim Entry | Immer RANGING (strategiebedingt) |
| Signalstärke (Confidence) | Kaum unterscheidend: `atr_stop` Ø 0,644 vs. `target_reached` Ø 0,655 — praktisch identisch |
| Entries unmittelbar nach Regimewechsel (≤2 bestätigte Bars) | Selten (2,7–5,3 % je nach Bucket) und **nicht** auf Verlierer konzentriert |
| Anzahl bestätigter Bars vor Entry | Kein klarer Zusammenhang mit Hit-Rate (Bucket [3,6) am schlechtesten mit 16,7 %, Bucket [6,12) am besten mit 43,8 % — kein monotones Muster, kleine Stichproben) |
| Gegen kurzfristige Preisbewegung | 98,8 % aller Trades erfüllen dieses Kriterium (159/161) — strukturell fast immer wahr bei Mean-Reversion, **keine Trennschärfe möglich** |
| Mehrfach-Entries unter ähnlichen Bedingungen | `atr_stop`-Zeitpunkte über den gesamten 180-Tage-Zeitraum verteilt (April–September), keine zeitliche Häufung in einem "schlechten Regime-Fenster" erkennbar |
| BB-Position (**korrigiert, seitennormiert**) | Siehe Punkt 0: die in Schritt 10 berichtete "BB-Position nahe 0,53" für `regime_change`-Trades war ein Artefakt unnormierter Long/Short-Mittelung. Seitennormiert (`edge_dist`) liegen **159 von 161 Trades** bereits innerhalb 0,3 vom nächsten Band-Rand — ein hartes BB-Gate bei der bestehenden 0,15-Schwelle würde nur 2 Trades entfernen |
| Preis bereits über das Band hinausgeschossen ("Overshoot", `edge_dist < 0`) | **Einziger real messbarer Unterschied:** Overshoot-Trades (n=83) Hit-Rate 30,1 % vs. innerhalb-des-Bandes (n=78) Hit-Rate 39,7 %; PF 0,363 vs. 0,370 — Unterschied real, aber klein |

## 3. Trade-Attribution

| exit_reason | n | Win | Loss | Ø Confidence | Ø ADX | Ø Regime-Streak (Bars) | Ø Verlust ($) |
|---|---|---|---|---|---|---|---|
| `target_reached` | 54 | 48 | 6 | 0,655 | 16,43 | 28,5 | −0,54 |
| `regime_change` | 75 | 6 | 69 | 0,736 | 20,47 | 23,8 | −5,61 |
| `atr_stop` | 19 | 0 | 19 | 0,644 | 15,83 | 24,6 | −11,96 |
| `time_exit` | 12 | 2 | 10 | 0,652 | 16,35 | 23,7 | −3,92 |

## 4. Muster bei `atr_stop`-Trades — die zentrale Frage

**Warum werden diese Trades überhaupt eröffnet?** Ehrliche Antwort nach Prüfung aller sechs angeforderten Dimensionen: **Es gibt kein erkennbares schlechtes Entry-Muster.** Im Detail:

- 16 von 19 `atr_stop`-Trades (84 %) liegen **innerhalb** des Bandes, nahe der Kante — nicht im Overshoot-Bereich, der sonst als schwächer identifiziert wurde.
- BB-Position bei den einzelnen `atr_stop`-Long-Trades: 0,003 / 0,051 / 0,078 / 0,099 / 0,104 / 0,109 / 0,125 / 0,137 / 0,140 / 0,145 / 0,148 — nahezu alle **innerhalb** der strengen 0,15-Schwelle, also nach den strategie-eigenen Kriterien "gute" Entries.
- ADX-Werte (7,7 bis 23,4, Median deutlich unter 20) zeigen keine Konzentration in der Grauzone.
- Confidence liegt im Mittel sogar knapp *unter* der von `target_reached`, nicht darüber — kein Hinweis auf "übermütige" Signale.
- Zeitliche Verteilung über das gesamte Halbjahr — keine Häufung in einer erkennbaren Problemphase.

**Schlussfolgerung:** Nach allen hier geprüften Entry-Merkmalen sind `atr_stop`-Trades von Gewinner-Trades **statistisch nicht unterscheidbar**. Sie sehen wie valide, lehrbuchmäßige Mean-Reversion-Setups aus und scheitern trotzdem — mit dem mit Abstand größten Einzelverlust (Ø −11,96 $, ATR-normiert Ø 3,36 ATR, siehe Schritt 12). Das spricht für eine Ursache **außerhalb** der hier verfügbaren Entry-Features (z. B. Kontext auf höherem Timeframe, Marktbreite, oder schlicht inhärentes Rauschen bei kleiner Stichprobe von n=19) — nicht für eine filterbare Entry-Schwäche.

## 5. Neuer Entry-Gate — vor der Implementierung beschrieben

Da `atr_stop` selbst kein Muster zeigt, basiert der einzige entwickelte Gate auf dem einen real gemessenen Unterschied (Punkt 2, Overshoot vs. innerhalb Band):

**Gate:** Verlange `0,0 ≤ bb_position ≤ 0,15` (Long) bzw. `0,85 ≤ bb_position ≤ 1,0` (Short) als hartes Kriterium — reine Wiederverwendung der bereits im Code vorhandenen `bb_position_long`/`bb_position_short`-Schwellen (kein neuer, frei erfundener Parameter), aber als Ablehnungs- statt Scoring-Kriterium. Preis, der bereits über das Band hinausgeschossen ist, wird abgelehnt.

- **Entfernte Entry-Klasse:** "Verspätete" Mean-Reversion-Entries, bei denen der Preis das Band bereits verlassen hat, bevor die Position eröffnet wird — Signal kommt in diesem Sinne "zu spät" (die Extremposition ist bereits überschritten).
- **Erwartete Trade-Reduktion (statische Vorab-Schätzung):** 83 von 161 Trades (alle mit `edge_dist < 0`).
- **Erwartete Verschiebung:** Hit-Rate der verbleibenden Population 39,7 % statt 34,8 %; Profit Factor 0,370 statt 0,366 (praktisch unverändert).
- **Getestete Hypothese:** Overshoot-Entries sind eine geringerwertige Entry-Klasse; ihre Entfernung verbessert die risikoadjustierte Performance.

## 6. A/B-Backtest

`regime_change` bleibt in A und B unverändert aktiv. Gate implementiert als reiner Backtest-Monkeypatch (`MeanReversionStrategy.generate_signal` in einem lokalen Diagnoseskript, nicht Teil des Repos) — **keine Zeile in `sgr/strategy/mean_reversion.py` geändert.**

### Vollzeitraum (180 Tage)

| | A: Baseline | B: + Entry-Gate | Δ |
|---|---|---|---|
| Trades | 161 | 121 | −40 (nicht die statisch geschätzten 83 — Signal-Entfernung verschiebt Folge-Entries, siehe Robustheit unten) |
| Sharpe | −3,539 | **−4,458** | **−0,919 (schlechter)** |
| Return | −2,10 % | −2,12 % | −0,02pp (marginal schlechter) |
| Profit Factor | 0,367 | 0,408 | +0,041 (leicht besser) |
| Hit Rate | 34,8 % | 40,5 % | +5,7pp (besser) |
| Max Drawdown | 2,66 % | 2,51 % | −0,15pp (leicht besser) |
| `atr_stop`-Anzahl | 19 | 18 | −1 (Gate berührt `atr_stop` kaum, wie in Punkt 4 erwartet) |
| Ø Gewinn | 4,32 $ | 3,83 $ | −0,49 $ (kleiner) |
| Ø Verlust | −6,29 $ | −6,39 $ | −0,10 $ (unverändert bis leicht schlechter) |

## 7. Robustheit — Zeitsplit in zwei ~90-Tage-Hälften

| | H1 A | H1 B | H2 A | H2 B |
|---|---|---|---|---|
| Trades | 69 | 53 | 82 | 62 |
| Sharpe | −2,282 | −2,593 | −3,942 | **−5,701** |
| Return | −0,60 % | −0,55 % | −1,16 % | −1,35 % |
| Profit Factor | 0,437 | 0,499 | 0,323 | 0,352 |
| Max DD | 1,08 % | 0,96 % | 1,51 % | 1,42 % |

**Richtung konsistent, Ausmaß nicht:** Sharpe verschlechtert sich in **beiden** Hälften (H1: −0,311, H2: −1,759 — in H2 sogar deutlich stärker). Profit Factor verbessert sich in beiden Hälften geringfügig (H1: +0,062, H2: +0,029). Max Drawdown verbessert sich in beiden Hälften leicht. Return-Effekt ist uneinheitlich (H1 minimal besser, H2 schlechter). Die Verschlechterung des Sharpe ist damit **kein Zufallsartefakt einer einzelnen Periode**, sondern zeigt sich robust in beiden unabhängigen Zeitfenstern — mit zunehmender statt abnehmender Wirkung in der zweiten Hälfte.

**Mechanismus:** Ø Gewinn sinkt in jeder einzelnen Periode (Voll: 4,32→3,83; H1: 5,29→4,69; H2: 3,73→3,14), während Ø Verlust nicht sinkt. Das Gate entfernt also nicht bevorzugt Verlierer, sondern kappt tendenziell auch überdurchschnittlich große Gewinner (vermutlich Trades, die nach starkem Momentum-Überschießen kräftig zurückschnappen) — das erklärt, warum Hit-Rate und PF leicht steigen, während die risikoadjustierte Kennzahl (Sharpe) und der Gesamt-Return sich verschlechtern.

---

## Abschlussbericht

**1. Entry-Ursache:** `MeanReversionStrategy.generate_signal()` (`sgr/strategy/mean_reversion.py:70`), gewichtetes 6-Kriterien-Scoring mit Schwelle `confidence >= 0,55`. Orderbook-Kriterium im Backtest strukturell inaktiv (immer `None`).

**2. Identifiziertes schlechtes Entry-Muster:** Kein zuverlässiges Muster bei den eigentlichen Verlierern (`atr_stop`) gefunden — sie sind auf allen geprüften Dimensionen (Confidence, ADX, BB-Position, Regime-Bestätigungsdauer, Momentum-Richtung, Zeitverteilung) statistisch nicht von Gewinnern unterscheidbar. Der einzige real messbare Unterschied (Overshoot-Entries vs. Band-Entries) betrifft primär `regime_change`, kaum `atr_stop`.

**3. Neuer Entry-Gate:** Harte BB-Band-Grenze (`0 ≤ bb_position ≤ 0,15` long / `0,85 ≤ bb_position ≤ 1,0` short) — Wiederverwendung bestehender Strategie-Schwellen als Ablehnungs- statt Scoring-Kriterium, kein neuer Parameter.

**4. Anzahl entfernter Trades:** 40 von 161 im Live-A/B-Lauf (statische Vorab-Schätzung: 83; Differenz durch Folge-Entry-Verschiebungen).

**5. A/B-Kennzahlen (Vollzeitraum):** Sharpe −3,539 → −4,458 (schlechter), Return −2,10 % → −2,12 % (marginal schlechter), Profit Factor 0,367 → 0,408 (leicht besser), Hit Rate 34,8 % → 40,5 % (besser), Max DD 2,66 % → 2,51 % (leicht besser), `atr_stop` 19 → 18 (nahezu unverändert), Ø Gewinn 4,32 $ → 3,83 $ (kleiner), Ø Verlust −6,29 $ → −6,39 $ (unverändert).

**6. Robustheit:** Sharpe-Verschlechterung zeigt sich konsistent in beiden Zeithälften (H1 und H2), mit zunehmender Stärke in H2 — kein Einperioden-Artefakt. PF-Verbesserung ebenfalls in beiden Hälften konsistent, aber klein.

**7. Entscheidung: NICHT VERBESSERT.**

Begründung gemäß vorgegebener Priorität (Sharpe → Return → Profit Factor → Max Drawdown): Sharpe verschlechtert sich deutlich und robust über beide Zeitperioden, Return bleibt im Wesentlichen unverändert bis leicht schlechter. Die Verbesserungen bei Profit Factor, Hit Rate und Max Drawdown sind real, aber klein und reichen nicht annähernd an die Go-Live-Schwellen heran (PF weiterhin bei 0,37–0,41 gegenüber gefordert 1,3) — und gehen laut ausdrücklicher Vorgabe der Priorität dem verschlechterten Sharpe nicht vor. Kein Code in `sgr/strategy/mean_reversion.py` geändert; das Gate existierte ausschließlich als Backtest-Monkeypatch und wird nicht übernommen.

**Einordnung:** Damit ist nun auch die Entry-Seite mit derselben Sorgfalt wie zuvor die Exit-Seite (Schritt 12–14) getestet worden, ohne einen belastbaren Verbesserungshebel zu finden. In Kombination mit dem in Schritt 10 dokumentierten Befund (BB-Position bei `regime_change`-Trades näher an der Mitte) und der jetzt widerlegten statistischen Grundlage dafür (Punkt 0) ist die nächste sinnvolle Hypothese nicht ein weiterer Filter, sondern **die grundsätzliche Eignung von `mean_reversion_v1` für den untersuchten BTC/USDT-Zeitraum** — wie in der Aufgabenstellung selbst vorgeschlagen. Das ist bewusst nicht Teil dieser Session.
