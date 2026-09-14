# Analyse: mean_reversion_v1 — Win/Loss-Distanz in ATR je exit_reason (Schritt 12)

**Basis-Commit:** `847373c` (docs: document mean_reversion_v1 entry-filter test, Schritt 11)
**Datum:** 2026-09-14
**Zeitraum:** 180 Tage, BTC/USDT + ETH/USDT, 1h-Timeframe, 161 Trades (unveränderter Baseline-Lauf, kein Entry-Filter)
**Status:** Reine Messung, keine Codeänderung. **Korrigiert die R:R-Hypothese aus Schritt 11.**

---

## Ausgangsfrage

Schritt 11 formulierte die Hypothese, das Kernproblem sei ein pauschales Risk/Reward-Missverhältnis der Exit-Struktur ("Target = BB-Middle liegt im Schnitt näher am Entry als der Stop"). Diese Session misst das direkt: Distanz zwischen Entry- und Exit-Preis, normiert auf ATR(14) zum Einstiegszeitpunkt, aufgeschlüsselt nach `exit_reason` und Gewinn/Verlust.

**Korrektur der vorherigen Formulierung:** Der in Schritt 11 genannte "Stop = 1,5× ATR" ist falsch. `MeanReversionStrategy` berechnet zwar `stop_price = entry ± 1,5 × ATR` und legt ihn in `signal.metadata["stop_price"]` ab, aber `BacktestSimulator._check_exits()` liest dieses Feld nie — der tatsächlich simulierte Stop ist ein davon unabhängiger, hartcodierter **2,5× ATR**-Schwellenwert (`sgr/backtesting/simulator.py`, Abschnitt "2. ATR-basierter Stop"). Nur `target_price` aus dem Signal wird tatsächlich verwendet. Das strategie-eigene `stop_price` ist damit aktuell totes Datum im Backtest-Pfad — separater Befund, nicht Gegenstand dieser Messung.

## Methodik

Monkeypatch auf `BacktestSimulator._check_exits()`, der beim erstmaligen ATR-Check einer Position (≈1 Bar nach Entry) den ATR-Wert zusätzlich pro Position-ID zwischenspeichert — die Exit-Logik selbst (welcher `exit_reason` greift, zu welchem Preis geschlossen wird) bleibt unverändert. Nach dem Backtest: `distance_atr = |exit_price - entry_price| / atr_at_entry`, pro Trade aus `BacktestResult.trades` (bereits inkl. `exit_reason`, `entry_price`, `exit_price`, `net_pnl`). 161/161 Trades hatten einen erfassten ATR-Wert (keine fehlenden Daten). `run_walk_forward=False` — nur der Haupt-Backtest wird betrachtet, keine Walk-Forward-Splits.

## Ergebnis

| exit_reason | n | Wins | Losses | Ø Win (ATR) | Ø Loss (ATR) | Win/Loss-Ratio | Ø Bars gehalten |
|---|---|---|---|---|---|---|---|
| `atr_stop` | 19 | 0 | 19 | — | 3,356 | — (100 % Loss per Definition) | 8,3 |
| `regime_change` | 75 | 6 | 69 | 0,754 | 1,154 | 0,653 | 5,5 |
| `target_reached` | 54 | 48 | 6 | 1,929 | 0,838 | 2,301 | 6,1 |
| `time_exit` | 12 | 2 | 10 | 0,702 | 1,327 | 0,529 | 20,0 |
| `backtest_end` | 1 | 0 | 1 | — | 1,605 | — | 8,0 |
| **Gesamt** | **161** | **56** | **105** | **1,759** | **1,555** | **1,131** | — |

## Befund

**Die Gesamtbetrachtung widerlegt die pauschale Schritt-11-Hypothese:** über alle Trades hinweg sind Gewinn-Distanzen (1,759 ATR) im Schnitt sogar *größer* als Verlust-Distanzen (1,555 ATR) — Ratio 1,131, leicht günstig. Ein generisches "Stop zu weit, Target zu nah"-Problem liegt in Summe nicht vor. Der negative Profit Factor (0,366 in diesem Lauf) entsteht primär durch die **Hit-Rate**, nicht durch die Auszahlungsgröße pro Trade.

Aufgeschlüsselt nach `exit_reason` zeigt sich ein klar differenziertes Bild:

1. **`target_reached` (54 Trades, 33,5 % aller Trades) ist strukturell gesund:** 88,9 % Hit-Rate, günstiges Verhältnis 2,301 — bestätigt Schritt 10 erneut. Kein Handlungsbedarf hier.

2. **`regime_change` und `time_exit` sind beide doppelt belastet** — niedrige Hit-Rate (8,0 % bzw. 16,7 %) *und* ein ungünstiges Verhältnis (0,653 bzw. 0,529, Verluste größer als Gewinne innerhalb dieser Kategorien). Zusammen 87 von 161 Trades (54 %). Das erklärt direkt, warum die in Schritt 11 getesteten Entry-Filter (ADX-/BB-Position-Gate) den Profit Factor nicht über 0,38 heben konnten: sie reduzieren das *Volumen* dieser beiden Kategorien (z. B. `regime_change` 75→weniger Trades bei ADX<18), verändern aber nicht die *Schieflage innerhalb* der verbleibenden Trades — die überlebenden `regime_change`-Trades bleiben im Schnitt mit größerem Verlust als Gewinn behaftet.

3. **`atr_stop` (19 Trades, 11,8 %) ist zwangsläufig 100 % Verlust** (per Definition — sonst wäre der Stop nicht das Exit-Kriterium), aber mit durchschnittlich **3,356 ATR** realisierter Distanz deutlich mehr als der nominale 2,5×-ATR-Schwellenwert. Ursache: der Exit prüft `close < entry - 2,5×ATR` und schließt dann exakt auf diesem Close — bei 1h-Bars kann der Close den Schwellenwert bereits deutlich überschritten haben (kein Intrabar-Check). Der reale Stop-Verlust ist damit im Schnitt rund 34 % größer als der nominale Schwellenwert suggeriert.

## Korrigierte Einordnung gegenüber Schritt 11

Schritt 11 hatte richtig erkannt, dass Entry-Filter allein nicht ausreichen, aber die Ursache falsch als generisches R:R-Problem der Exit-Konfiguration benannt. Die präzisere Diagnose: **zwei von vier Exit-Pfaden (`regime_change`, `time_exit`) sind strukturell verlustträchtig in Hit-Rate UND Auszahlungsgröße gleichzeitig**, während `target_reached` einwandfrei funktioniert und `atr_stop` primär ein Bar-Granularität-/Overshoot-Thema ist, kein Sizing-Thema.

## Empfehlung für den nächsten Schritt (nicht Teil dieser Session)

Nicht weiter an Entry-Filtern oder an einer pauschalen Stop/Target-Distanz drehen. Stattdessen gezielt:
- **`regime_change`:** Trades mit 0,653 Ratio und 8 % Hit-Rate untersuchen — reagiert der Exit zu spät (Verlust läuft schon, bevor das Regime kippt) oder ist bereits der Entry in dieser Untergruppe das Problem (siehe Schritt-10-Befund: BB-Position bei `regime_change`-Trades im Schnitt 0,53, nahe der Bollinger-Mitte, kein Extremwert)? Ein engerer, eigener Stop *nur* für RANGING-Entry-Positionen (unabhängig vom generischen `atr_stop`) könnte den Ratio-Nachteil begrenzen, ohne die gesunden `target_reached`-Trades zu beeinträchtigen.
- **`time_exit`:** mit nur 12 Trades statistisch dünn, aber die längste Haltedauer (Ø 20 Bars = Zeitlimit) bei ungünstigem Ratio spricht dafür, dass Positionen, die weder Ziel noch Stop innerhalb der Frist erreichen, im Schnitt bereits im Verlust stehen — ein früherer Zeit-Exit (z. B. 10–12 statt 20 Bars) könnte diese Kategorie verkleinern, sollte aber gegen `target_reached`-Trades mit längerer Haltedauer abgeglichen werden (Ø 6,1 Bars dort, aber Verteilung nicht geprüft).
- **`atr_stop`-Overshoot:** prüfen, ob ein Intrabar-Stop-Check (High/Low statt nur Close) den realisierten Verlust näher an den nominalen 2,5×-ATR-Schwellenwert bringt — das wäre eine reine Simulator-Genauigkeitsverbesserung, keine Strategieänderung, und würde alle Strategien mit ATR-Stop betreffen, nicht nur `mean_reversion_v1`.

Wie in Schritt 10/11: erst diese Hypothesen einzeln messen, bevor eine Codeänderung erfolgt.
