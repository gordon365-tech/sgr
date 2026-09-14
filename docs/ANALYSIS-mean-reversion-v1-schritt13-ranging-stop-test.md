# Analyse: mean_reversion_v1 — Test eines engeren RANGING-only Stops (Schritt 13)

**Basis-Commit:** `189b060` (docs: measure win/loss distance in ATR per exit_reason, Schritt 12)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage, BTC/USDT + ETH/USDT, 1h-Timeframe (gleicher Datensatz wie Schritt 11/12)
**Status:** Hypothese getestet und **falsifiziert**. Keine Codeänderung — Ergebnis ist eindeutig negativ.

---

## Ausgangsfrage

Schritt 12 empfahl als nächsten Schritt, für `regime_change`-Trades (75 Trades, 8,0 % Hit-Rate, ungünstiges Verhältnis 0,653) einen engeren, eigenen Preis-Stop nur für RANGING-Entry-Positionen zu testen — als schnellerer Circuit-Breaker, der eine Position schon vor der offiziellen Regime-Umklassifizierung beendet, wenn der Preis bereits deutlich gegen die Position gelaufen ist.

## Methodik

Vollständiger Ersatz von `BacktestSimulator._check_exits()` in einem lokalen Diagnoseskript (nicht Teil des Repos), der die bestehende Prioritätsreihenfolge exakt nachbildet, aber einen neuen Check `ranging_stop` an Position 0 einfügt — geprüft **vor** `regime_change`, nur für Positionen mit `pos.regime == RANGING` (betrifft ausschließlich `mean_reversion_v1`; `trend_following_v1` eröffnet nie in RANGING, strukturell unberührt). Getestet: 2,0× / 1,5× / 1,25× / 1,0× / 0,75× ATR, gegen dieselbe 180-Tage-Baseline aus Schritt 12 (161 Trades, `run_walk_forward=False`).

## Ergebnis

| Variante | Trades | Sharpe | Return | Profit Factor | Hit Rate | Max DD |
|---|---|---|---|---|---|---|
| Baseline (kein ranging_stop) | 161 | −3,52 | −2,08 % | 0,366 | 34,8 % | 2,66 % |
| ranging_stop 2,0× ATR | 180 | −3,94 | −2,42 % | 0,363 | 33,9 % | 2,99 % |
| ranging_stop 1,5× ATR | 195 | −4,10 | −2,64 % | 0,364 | 30,8 % | 3,19 % |
| ranging_stop 1,25× ATR | 214 | −4,34 | −2,83 % | 0,378 | 28,5 % | 3,43 % |
| ranging_stop 1,0× ATR | 234 | −4,45 | −2,93 % | 0,368 | 27,4 % | 3,50 % |
| ranging_stop 0,75× ATR | 265 | −5,20 | −3,50 % | 0,359 | 25,7 % | 4,01 % |

**Jede getestete Variante ist schlechter als die Baseline, und die Verschlechterung ist monoton mit der Enge des Stops** — je enger, desto schlechter (Sharpe, Return, Hit-Rate, Max-DD verschlechtern sich alle konsistent von 2,0× bis 0,75×).

### Exit-Reason-Breakdown (Beispiel: 1,0× ATR)

| exit_reason | n | Wins | Losses | Ø Win (ATR) | Ø Loss (ATR) | Ratio |
|---|---|---|---|---|---|---|
| `ranging_stop` | 107 | **0** | 107 | — | 1,726 | — (100 % Verlust) |
| `regime_change` | 63 | 10 | 53 | 0,756 | 0,412 | 1,836 |
| `target_reached` | 58 | 53 | 5 | 2,191 | 0,957 | 2,289 |
| `time_exit` | 5 | 1 | 4 | 0,956 | 0,191 | 5,010 |

(Gleiches Muster bei 2,0×/1,5×/1,25×/0,75× — `ranging_stop` ist in **jeder** getesteten Variante 100 % Verlust, 0 Gewinne.)

## Befund

Die Hypothese ist eindeutig widerlegt, und der Mechanismus ist klar erkennbar:

1. **Der neue `ranging_stop`-Pfad fängt bei keinem einzigen Multiplikator jemals einen Gewinner ab** (0 Wins über alle fünf Varianten, 85–143 Trades). Das bedeutet: der Preis bewegt sich bei diesen Positionen zunächst gegen den Entry, bevor er (falls überhaupt) zum Ziel zurückkehrt — ein enger Stop beendet die Position in genau dieser Anfangsphase und verhindert damit systematisch die Rückkehr zum Mean, die `target_reached` mit 88–91 % Hit-Rate in allen Varianten beweist als real erreichbar ist. Der Stop wandelt potenzielle Gewinner in garantierte Verlierer um, statt Verlierer zu begrenzen.

2. **Die Gesamt-Trade-Zahl steigt mit engerem Stop** (161 → 180 → 195 → 214 → 234 → 265): kürzere durchschnittliche Haltedauer gibt schneller Kapital für neue Positionen frei. Diese zusätzlichen Trades sind im Schnitt nicht besser als die bestehenden — die Hit-Rate sinkt trotz (oder wegen) der höheren Frequenz kontinuierlich (34,8 % → 25,7 %).

3. **`regime_change` verbessert sich zwar isoliert** (Ratio 0,653 → 1,836 bei 1,0× ATR, weil die schlimmsten Verlierer jetzt vorher vom `ranging_stop` abgefangen werden) — aber dieser scheinbare Erfolg ist eine Verschiebung, kein Gewinn: die abgefangenen Trades landen als 100-%-Verlust im neuen `ranging_stop`-Bucket, nicht als vermiedener Verlust. Insgesamt verschlechtert sich das Ergebnis.

## Einordnung

Damit ist inzwischen dreifach empirisch belegt, dass das Problem **nicht** in der Verlustgröße pro Trade liegt (weder generische R:R-Distanz [Schritt 12] noch ein isolierter, engerer RANGING-Stop [Schritt 13] helfen) und auch nicht allein im Entry-Volumen (Schritt 11). Der eigentliche Hebel scheint zu sein: **die Fähigkeit, frühzeitig zwischen einer Position zu unterscheiden, die zum Ziel zurückkehren wird, und einer, die nicht zurückkehren wird** — genau das kann ein reiner Preis-Distanz-Stop nicht leisten, weil er keine Information darüber hat, ob die Reversion noch bevorsteht oder bereits gescheitert ist.

## Empfehlung für den nächsten Schritt (nicht Teil dieser Session)

Nicht weiter an Stop-Distanzen drehen (Schritt 12 + 13 sprechen dagegen). Stattdessen:
- Prüfen, ob die `regime_change`-Verlierer (53–69 Trades je nach Variante) anhand von Merkmalen **beim Entry** vorhersagbar sind, die über die in Schritt 10 bereits geprüften hinausgehen (z. B. Trendstärke auf einem höheren Timeframe, Volumen-Anomalien, Distanz zum letzten Swing-High/Low) — nicht um sie später per Stop zu beenden, sondern um sie gar nicht erst zu eröffnen.
- Alternativ: testen, ob der `regime_change`-Exit selbst kontraproduktiv ist — ein Vergleich mit einer Variante, die `regime_change` komplett entfernt (Positionen laufen bis Target/generischer ATR-Stop/Zeit-Exit) könnte zeigen, ob die vorzeitige Beendigung bei Regimewechsel selbst ein Teil des Problems ist, nicht nur eine unvollständige Lösung.
- Wie immer: erst messen, dann Hypothese, dann Test, bevor Code geändert wird.
