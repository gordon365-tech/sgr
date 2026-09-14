# Analyse: mean_reversion_v1 — Grundsätzliche Eignung für den untersuchten Zeitraum (Schritt 16)

**Basis-Commit:** `3326e6a` (docs: entry attribution and controlled entry-gate A/B experiment, Schritt 15)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage (2026-03-18 bis 2026-09-14), BTC/USDT und ETH/USDT, 1h und 4h
**Status:** Abschließende Eignungsbewertung. Keine Codeänderung an Production Code, Order Safety, Kill Switch oder Live-Konfiguration.

---

## 0. Simulator-Verifikation (vorab durchgeführt, wie gefordert)

Vor der eigentlichen Analyse wurde geprüft, ob `BacktestSimulator` Trades vollständig und korrekt simuliert.

**Ergebnis: Ja, mit einer wichtigen Einschränkung — ein bestätigter Buchhaltungsfehler bei Short-Positionen.**

- Kerzenladung, Bar-Verarbeitung, Trade-Timing, Preis-Plausibilität, Trade-ID-Eindeutigkeit: alle unauffällig (4320 Kerzen korrekt geladen, 161 Trades korrekt über den gesamten Zeitraum verteilt, Entry/Exit-Preise im plausiblen BTC-Preisbereich, keine Duplikate, keine fehlenden Werte, keine unplausiblen Haltedauern).
- **Bestätigter Fehler:** `BacktestSimulator._open_position()` (`sgr/backtesting/simulator.py:519`, `self._cash -= total_cost`) und `_close_position()` (Zeile ~674, `self._cash += exit_notional - exit_fee`) verbuchen **SHORT-Positionen mit derselben Cash-Formel wie LONG-Positionen** — korrekt nur für Long. Verifiziert per Instrumentierung: bei 84 Long-Trades stimmt `cash_delta` exakt mit `net_pnl` überein (84/84 exakte Übereinstimmung); bei 77 Short-Trades stimmt dies praktisch nie (5/77), stattdessen bewegt sich der tatsächliche Cash-Effekt näherungsweise mit **umgekehrtem Vorzeichen** relativ zum korrekt berechneten `net_pnl`.
- **Betroffen:** die interne Cash-/Equity-Kurve und alles, was daraus abgeleitet wird — `total_return_pct`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown_pct`, `calmar_ratio`, sowie die Positionsgrößen-Berechnung jedes nachfolgenden Trades (die von `_compute_portfolio_value()`, also vom bereits verzerrten `self._cash`, abhängt).
- **Nicht betroffen:** das `net_pnl`-Feld pro Trade selbst (korrekt vorzeichenrichtig über die separate `gross_pnl`-Formel berechnet, algebraisch verifiziert), sowie reine Zählungen (Hit-Rate, `exit_reason`-Verteilung, Trade-Anzahl) und die ATR-normierte Distanzanalyse aus Schritt 12 (unabhängig von Positionsgröße).
- **Einordnung der Vorgänger-Schritte:** Die in Schritt 10–15 berichteten Sharpe-/Return-/MaxDD-Werte sind dadurch **nicht vertrauenswürdig in absoluter Höhe**, aber die RICHTUNG der Befunde bleibt bestehen — eine mit korrigierter Methodik neu berechnete BTC/USDT-1h-Sharpe (siehe unten) fällt sogar **deutlich negativer** aus (−7,01 statt der zuvor berichteten −3,5) als bisher dokumentiert. Der Fehler hat die tatsächliche Schwäche der Strategie bisher eher **unterschätzt**, nicht überzeichnet. Schritt 15s A/B-Entscheidung (NICHT VERBESSERT) bleibt in der Tendenz plausibel, da beide A/B-Varianten dort gleichermaßen vom Fehler betroffen waren (Differenzbetrachtung bleibt näherungsweise gültig), sollte aber bei Bedarf mit der hier verwendeten bereinigten Methodik erneut bestätigt werden — nicht Teil dieser Session.
- **Keine Codeänderung:** Der Fehler wird hier ausschließlich dokumentiert, nicht behoben (kein Production-Code-Zugriff in diesem Auftrag). Für alle folgenden Berechnungen wird eine **bereinigte, vom Fehler unabhängige Methodik** verwendet (siehe unten).

### Bereinigte Methodik für diese Analyse

Pro Trade wird ein bereinigter Bruch-Return `net_pnl / (entry_price × quantity)` berechnet (dimensionslos, unabhängig vom Cash-Bug, da `quantity`/`entry_price` aus dem jeweiligen Trade-Datensatz selbst stammen, nicht aus der fortlaufenden Cash-Kurve). Daraus wird eine feste Einsatzgröße von 1.000 $ (10 % von 10.000 $ Referenzkapital, nicht compoundierend) rekonstruiert — Equity-Kurve, Max Drawdown und eine auf Trade-Frequenz annualisierte Sharpe Ratio werden aus dieser bereinigten Kurve berechnet, nicht aus `self._cash`.

---

## 1. Performance nach Symbol und Timeframe

Wichtiger Nebenbefund (bereits in Schritt 15 dokumentiert, hier durch korrekte Einzelsymbol-Aufrufe bestätigt): `BacktestSimulator.run()` verarbeitet nur `symbols[0]` — für eine echte Symbol-/Timeframe-Matrix wurde jede Kombination **einzeln** mit genau einem Symbol aufgerufen.

| Symbol | Timeframe | n Trades | Hit-Rate | Profit Factor | Return (bereinigt) | Max DD (bereinigt) | Sharpe (annualisiert, bereinigt) | Ø Gewinn | Ø Verlust |
|---|---|---|---|---|---|---|---|---|---|
| BTC/USDT | 1h | 161 | 34,8 % | 0,364 | −6,18 % | 6,59 % | **−7,012** | 6,32 $ | −9,26 $ |
| ETH/USDT | 1h | 167 | 46,1 % | 0,569 | −5,16 % | 5,62 % | −3,931 | 8,86 $ | −13,32 $ |
| BTC/USDT | 4h | 29 | 41,4 % | 0,471 | −1,80 % | 2,01 % | −2,090 | 13,39 $ | −20,06 $ |
| ETH/USDT | 4h | 31 | 41,9 % | 0,451 | −3,14 % | 4,39 % | −2,328 | 19,83 $ | −31,74 $ |

**Befund:** ETH/USDT wurde in Schritt 10–15 nie tatsächlich simuliert (Engine-Limitation) — korrekt einzeln ausgeführt, erzeugt es sogar eine **bessere** Hit-Rate und Profit Factor als BTC/USDT, bleibt aber ebenso klar unprofitabel. **Alle vier Symbol/Timeframe-Kombinationen sind durchgängig unprofitabel** (PF < 1 in jeder einzelnen Zelle). 4h ist in beiden Symbolen spürbar weniger schlecht als 1h (weniger Trades, geringere Drag durch Gebühren/Slippage, weniger Rauschen) — aber selbst dort bleibt PF bei 0,45–0,47, weit unter der Gewinnschwelle.

## 2. Performance nach Marktregime

Da `mean_reversion_v1` ausschließlich im RANGING-Regime handelt, ist eine trade-basierte "Performance nach Regime" trivial (100 % RANGING). Stattdessen wurde die **Regime-Klassifikation selbst unabhängig auf ihre Aussagekraft geprüft** — unabhängig von der Strategie, direkt auf den Kursreihen.

| | BTC/USDT | ETH/USDT |
|---|---|---|
| Regime-Verteilung | RANGING 62,6 %, TRENDING_UP 18,8 %, TRENDING_DOWN 18,6 % | RANGING 63,2 %, TRENDING_DOWN 19,9 %, TRENDING_UP 16,9 % |
| Variance-Ratio VR(5) / VR(10) / VR(20) / VR(50) | 0,911 / 0,877 / 0,905 / 1,035 | 0,965 / 0,958 / 0,976 / 1,032 |
| Forward-5-Bar-Return bei RANGING-Klassifikation | Ø −0,002 %, σ 0,872 %, 50,2 % positiv | Ø +0,063 %, σ 1,152 %, 52,0 % positiv |

**Befund:** Ein Variance-Ratio-Test (VR < 1 = Mean-Reversion, VR ≈ 1 = Random Walk, VR > 1 = Trend/Momentum) zeigt bei kurzen Horizonten (5–20 Stunden) einen VR von 0,88–0,98 — eine **reale, aber sehr schwache** Mean-Reversion-Tendenz, nahe am Random Walk. Bei 50 Stunden kippt VR bereits über 1 (Trend-Charakter). Innerhalb der als RANGING klassifizierten Bars ist der Forward-Return praktisch reines Rauschen (Mittelwert nahe 0, ~50 % positiv/negativ) — die RANGING-Klassifikation identifiziert zwar lokal trendärmere Phasen, aber **keinen statistisch nutzbaren Rückkehr-Vorteil** nach Kosten.

## 3. Gewinner vs. Verlierer nach Marktphase

Aufteilung in sechs ~30-Tage-Monate, korreliert mit dem tatsächlichen Kurstrend des jeweiligen Monats.

**BTC/USDT (1h):**

| Monat | Kurstrend | Trades | Hit-Rate | PF | Netto-PnL |
|---|---|---|---|---|---|
| M1 | +5,7 % | 21 | 57,1 % | **1,150** | **+10,70 $** |
| M2 | +1,3 % | 28 | 25,0 % | 0,226 | −88,54 $ |
| M3 | −15,1 % | 20 | 20,0 % | 0,151 | −79,07 $ |
| M4 | −3,2 % | 29 | 48,3 % | 0,482 | −67,95 $ |
| M5 | −2,1 % | 36 | 33,3 % | 0,305 | −87,58 $ |
| M6 | +23,6 % | 27 | 25,9 % | 0,149 | −106,61 $ |

**ETH/USDT (1h):**

| Monat | Kurstrend | Trades | Hit-Rate | PF | Netto-PnL |
|---|---|---|---|---|---|
| M1 | +6,1 % | 22 | 50,0 % | 0,803 | −24,11 $ |
| M2 | −10,0 % | 24 | 41,7 % | 0,538 | −44,56 $ |
| M3 | −17,2 % | 27 | 48,1 % | 0,728 | −30,86 $ |
| M4 | +4,2 % | 32 | 46,9 % | 0,577 | −83,03 $ |
| M5 | +0,1 % | 25 | **56,0 %** | **0,722** | −20,52 $ |
| M6 | +33,4 % | 37 | 37,8 % | 0,320 | **−127,91 $** |

**Befund — konsistent über beide Symbole:** Performance korreliert klar **invers mit der Trendstärke des Monats**. Der jeweils am stärksten trendende Monat (BTC M6: +23,6 %; ETH M6: +33,4 %) ist in beiden Symbolen der mit Abstand schlechteste (PF 0,149 bzw. 0,320). Der jeweils ruhigste Monat (BTC M1: +5,7 %, kleinster Betrag; ETH M5: +0,1 %, praktisch trendlos) ist der jeweils beste — bei BTC sogar marginal profitabel (PF 1,15, +10,70 $ auf einer Stichprobe von n=21), bei ETH immer noch unprofitabel, aber am wenigsten schlecht (PF 0,722). **Nur 1 von 12 Symbol-Monat-Kombinationen war profitabel**, und diese eine (BTC M1) ist statistisch nicht belastbar (kleine Stichprobe, geringer Betrag).

## 4. Sharpe, Return, Profit Factor, Hit Rate, Max DD — Zusammenfassung

Siehe Tabelle in Punkt 1 (bereinigte Methodik). Kernzahlen für die größte verfügbare Stichprobe (BTC/USDT 1h, n=161): **Sharpe −7,01, Return −6,18 %, Profit Factor 0,364, Hit Rate 34,8 %, Max DD 6,59 %.** Keine der vier getesteten Symbol/Timeframe-Kombinationen erreicht auch nur annähernd die Größenordnung, die für ein Go-Live-Gate erforderlich wäre (Sharpe ≥ 1,0, PF ≥ 1,3).

## 5. Welche Bedingungen für profitable Mean-Reversion tatsächlich vorhanden sind

- BTC/USDT stieg über die 180 Tage netto um 7,96 %, ETH/USDT um 12,37 % — **beide in einem Netto-Aufwärtstrend**, nicht seitwärts.
- Beide Symbole zeigten dabei enorme Zwischenschwankungen (Max Drawdown vom Zwischenhoch: BTC 29,4 %, ETH 37,9 %) — ein volatiler, richtungsstarker Markt, kein ruhiger Seitwärtsmarkt.
- Der Variance-Ratio-Test bestätigt eine reale, aber **sehr schwache** kurzfristige Mean-Reversion-Tendenz (VR 0,88–0,98) — zu schwach, um die Handelskosten (2× Taker-Fee + 2× Slippage ≈ 0,3 % pro Round-Trip) zuverlässig zu decken.
- Die bestehende RANGING-Klassifikation (ADX-basiert, ~63 % der Zeit) identifiziert zwar lokal trendärmere Abschnitte, aber innerhalb dieser Abschnitte ist der Forward-Return statistisch nicht von Null unterscheidbar — **kein nutzbarer Kanten-Vorteil nach Kosten.**
- Profitable Bedingungen (siehe Punkt 3) treten nur in den ruhigsten, am wenigsten trendenden Teilperioden auf — und selbst dort nur marginal (BTC) oder gar nicht (ETH).

**Fazit:** Die Bedingungen für profitable Mean-Reversion — ein echter, ausreichend starker, nach Kosten nutzbarer Rückkehr-zum-Mittelwert-Effekt in einem überwiegend seitwärts laufenden Markt — waren im untersuchten 180-Tage-Fenster **nicht in ausreichendem Maß vorhanden**. Der Markt lief netto aufwärts mit hoher Volatilität; die schwache statistische Mean-Reversion-Komponente reicht nicht aus, um Kosten und Fehlklassifikationen zu kompensieren.

## 6. Ist die Strategie grundsätzlich geeignet oder sollte sie verworfen werden?

Vier vorherige, unabhängige Diagnoserunden (Schritt 11: Entry-Filter; Schritt 13: engerer Stop; Schritt 14: `regime_change` entfernen; Schritt 15: datengetriebenes Entry-Gate mit Robustheitsprüfung) fanden **keinen** Hebel, der die Strategie in die Nähe der Profitabilität bringt. Diese Session ergänzt: (a) die Schwäche ist **nicht** auf ein einzelnes Symbol oder einen einzelnen Timeframe beschränkt — alle vier getesteten Kombinationen sind unprofitabel; (b) die zugrunde liegende Marktstatistik zeigt nur eine sehr schwache, nach Kosten nicht nutzbare Mean-Reversion-Komponente; (c) Profitabilität korreliert zwar nachvollziehbar mit geringer Trendstärke, aber dieses Fenster ist selten (1–2 von 6 Monaten je Symbol) und wird von der aktuellen Regime-Erkennung nicht zuverlässig im Voraus isoliert.

---

## Klassifikation

# **C — Strategie für den untersuchten Zeitraum grundsätzlich ungeeignet**

**Begründung:**
1. Kein einziges der vier getesteten Symbol/Timeframe-Fenster (BTC 1h/4h, ETH 1h/4h) erreicht Profit Factor ≥ 1,0, geschweige denn ≥ 1,3.
2. Der zugrunde liegende Markt zeigte im Zeitraum einen Netto-Aufwärtstrend mit hoher Volatilität, kein Seitwärtsregime — die Grundvoraussetzung für Mean-Reversion war strukturell selten gegeben.
3. Die unabhängig (ohne Strategie-Bias) gemessene statistische Mean-Reversion-Komponente ist real, aber zu schwach (Variance Ratio 0,88–0,98), um Handelskosten zu decken.
4. Die einzige identifizierbare "gute" Teilperiode (der jeweils ruhigste Monat je Symbol) ist selten, klein und nicht zuverlässig im Voraus erkennbar — das disqualifiziert eine B-Einstufung ("nur für klar identifizierbare Regime/Symbole/Timeframes geeignet"), da "klar identifizierbar" nicht erfüllt ist.
5. Vier vorherige, methodisch saubere Interventionsversuche (Entry-Filter, Exit-Stop, Regime-Change-Exit, kombiniertes Entry-Gate) konnten die Strategie in keinem Fall in Richtung Profitabilität bewegen.

**Nicht C, sondern eine Randnotiz für eine mögliche zukünftige B-Neubewertung:** Die in Punkt 3 gefundene Trendstärke-Korrelation ist mechanistisch plausibel und über beide Symbole konsistent — ein zukünftiger, expliziter Trendstärke-/Realisierte-Volatilität-Vorfilter (nicht Teil dieser Session, da das eine neue Parameteroptimierung wäre) könnte diese Einstufung ändern, wenn er auf einem längeren, unabhängigen Datensatz reproduzierbar eine zuverlässig identifizierbare gute Teilmenge liefert. Das ist eine Hypothese für eine separate, künftige Session — keine Grundlage, die aktuelle Einstufung zu ändern.

---

## Offener technischer Blocker (unabhängig von der Strategiefrage)

Der in Punkt 0 dokumentierte Short-Position-Cash-Bug in `BacktestSimulator` ist ein eigenständiger, produktionsrelevanter Befund: Er betrifft **jede** Strategie, die Short-Positionen im Backtest eröffnet (aktuell `mean_reversion_v1`; potenziell zukünftige Strategien), und korrumpiert Sharpe/Return/MaxDD/Calmar sowie nachgelagerte Positionsgrößen. Da `StrategyValidationRunner` (das Live-Aktivierungs-Gate) exakt diese Kennzahlen für die Go-Live-Entscheidung verwendet, sollte dieser Fehler behoben werden, **bevor** irgendeine Short-fähige Strategie auf Basis ihrer gemeldeten Sharpe/Return-Werte aktiviert wird — unabhängig vom Ausgang dieser Eignungsanalyse. Keine Codeänderung in dieser Session (außerhalb des Auftragsumfangs); als separater, zu priorisierender Fix zu behandeln.
