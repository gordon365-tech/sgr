# Analyse: Variante E — Preis-Regime-Dominanz-Check

**Basis-Commit:** `920be77` (feat: finalize production pipeline and regime dominance validation)
**Datum:** 2026-09-16
**Kandidatenmenge:** 91 bereits validierte Symbol/Strategie-Kombinationen (89 aus Batch `near_miss_opt_20260915`, 2 aus `control60_20260915` — alle 5 Backtest-Gates aus `sgr/backtesting/types.py::BacktestResult.is_acceptable` bestanden: Sharpe ≥ 1.0, Profit Factor ≥ 1.3, Max Drawdown ≤ 20 %, Hit Rate ≥ 40 %, Trades ≥ 30)
**Status:** Reine Zusatzmessung. Keine Codeänderung an Strategie, Gates, Walk-Forward-Schwellen oder Produktions-Konfiguration. Additiv zu Varianten A–D.

---

## Vorbemerkung: Rekonstruktion der Eingabedaten

Der ursprüngliche Lauf (gleicher Tag, vorherige Session) hatte sein Ergebnis nur nach `/tmp/sgr-wf-validation-decision.md` geschrieben — flüchtig, beim nächsten Container-Neustart verloren. Die 91-Kandidaten-Liste (`winners.psv`) existierte ebenfalls nur als Zwischendatei und war nicht mehr auffindbar.

Für diesen Lauf wurde die identische Kandidatenmenge direkt aus `strategy_symbol_validations` rekonstruiert (Filter: obige 5 Gates, Batches `near_miss_opt_20260915` ∪ `control60_20260915` → exakt 89 + 2 = 91 Zeilen, deckungsgleich mit der ursprünglich berichteten Zusammensetzung). Eingabedatei und vollständiger Ergebnis-Report liegen jetzt versioniert bei, nicht mehr in `/tmp`:

- [`docs/variant_e/winners_91_20260915.psv`](variant_e/winners_91_20260915.psv) — Eingabe (Symbol, Strategie, Parameter, Validation-Window)
- [`docs/variant_e/regime_dominance_report.json`](variant_e/regime_dominance_report.json) — vollständiger E1/E2-Report pro Kandidat

## Methodik (unverändert zur ursprünglichen Spezifikation)

- **E1** (preisbasiert, trade-unabhängig): Pooled-OOS-Fenster in feste Kalender-Chunks segmentiert (3-Tage- **und** 7-Tage-Chunks, beide gemessen — keine einzelne Chunk-Größe als Produktionsregel festgelegt). Ein Chunk gilt als Extremchunk, wenn sein Anteil an der kumulierten Preisbewegung > 35 % ist **oder** seine Spannweite > 5× den Median aller Chunks dieser Größe.
- **E2** (performance-basiert): Auf denselben gepoolten OOS-Trades (identischer Backtest, identische bereits persistierte Parameter — kein neuer Parameter-Search) werden alle Trades entfernt, deren Entry-Timestamp in einen Extremchunk fällt. Pseudo-Sharpe, Trade Count und PnL werden vorher/nachher verglichen. `regime_dominance_suspected = True`, wenn der Pseudo-Sharpe durch die Removal von positiv auf ≤ 0 kippt.

## Ergebnis

| Chunk-Größe | Ausgewertet | Mit Extremchunk (E1) | Regime-dominant (E2) | Selectivity |
|---|---|---|---|---|
| 3 Tage | 91 | 54 | **13** | 14,3 % |
| 7 Tage | 91 | 44 | **19** | 20,9 % |

0 technische Fehler (alle 91 Kandidaten erfolgreich verarbeitet — Candle-Daten vollständig aus DB/Exchange geladen, Backtest lief für jeden Kandidaten durch).

Die Sensitivität zwischen 3- und 7-Tage-Chunks ist real und erwartbar: größere Chunks bündeln mehr Kursbewegung pro Fenster, wodurch mehr Kandidaten die Extremchunk-Schwelle reißen — beide Chunk-Größen werden bewusst nebeneinander berichtet statt eine als "die" Regel festzulegen.

### TAIKO/USDT — bei beiden Chunk-Größen als regime-dominant erkannt

| Chunk | Extremchunks (E1) | Pseudo-Sharpe vorher → nachher | Trades entfernt |
|---|---|---|---|
| 3 Tage | 2 | 3,099 → **−0,235** | 4 / 47 |
| 7 Tage | 1 | 3,099 → **−0,028** | 5 / 47 |

### TUT/USDT — nur teilweise (1 von 2 Strategie-Kandidaten, nur bei 7-Tage-Chunks)

| Strategie | Chunk 3T | Chunk 7T |
|---|---|---|
| `momentum_v1` | nicht regime-dominant (Sharpe 2,67 → 0,726) | **regime-dominant** (Sharpe 2,67 → **−0,45**) |
| `trend_following_v1` | nicht regime-dominant | nicht regime-dominant |

Dieses gemischte Ergebnis (ein Strategie-Kandidat betroffen, der andere nicht; nur bei der größeren Chunk-Größe) ist unverändert gegenüber der ursprünglichen Messung — bewusst nicht geglättet oder zu einer einheitlichen Aussage vereinfacht.

## Vollständige Liste: als regime-dominant markiert (E2)

**3-Tage-Chunks (13 von 91):**

| Symbol | Strategie | Sharpe vorher → nachher | Trades entfernt/gesamt |
|---|---|---|---|
| BEAT/USDT | trend_following_v1 | 0,295 → −0,649 | 4/54 |
| BEL/USDT | mean_reversion_v1 | 0,737 → −1,289 | 2/52 |
| BILL/USDT | trend_following_v1 | 1,216 → −1,555 | 4/66 |
| EPIC/USDT | trend_following_v1 | 1,042 → −0,013 | 1/24 |
| EUL/USDT | trend_following_v1 | 0,935 → −6,315 | 6/52 |
| HEI/USDT | momentum_v1 | 0,164 → −5,095 | 4/50 |
| ONG/USDT | trend_following_v1 | 1,745 → −0,505 | 7/67 |
| RPL/USDT | trend_following_v1 | 2,099 → −0,082 | 4/34 |
| SKR/USDT | momentum_v1 | 1,754 → −0,349 | 6/57 |
| SKR/USDT | trend_following_v1 | 1,621 → −1,116 | 6/59 |
| TAIKO/USDT | trend_following_v1 | 3,099 → −0,235 | 4/47 |
| VELVET/USDT | momentum_v1 | 1,443 → −2,465 | 9/48 |
| VELVET/USDT | trend_following_v1 | 1,015 → −4,743 | 7/38 |

**7-Tage-Chunks (19 von 91):**

| Symbol | Strategie | Sharpe vorher → nachher | Trades entfernt/gesamt |
|---|---|---|---|
| BEAT/USDT | trend_following_v1 | 0,295 → −3,144 | 8/54 |
| BICO/USDT | momentum_v1 | 2,404 → −5,151 | 14/54 |
| BICO/USDT | trend_following_v1 | 2,542 → −5,49 | 15/56 |
| BILL/USDT | trend_following_v1 | 1,216 → −3,964 | 8/66 |
| COW/USDT | mean_reversion_v1 | 0,006 → −0,026 | 14/80 |
| EPIC/USDT | trend_following_v1 | 1,042 → −0,013 | 1/24 |
| EUL/USDT | trend_following_v1 | 0,935 → −6,872 | 8/52 |
| HEI/USDT | momentum_v1 | 0,164 → −5,404 | 6/50 |
| ONG/USDT | trend_following_v1 | 1,745 → −2,025 | 12/67 |
| RPL/USDT | trend_following_v1 | 2,099 → −1,854 | 5/34 |
| SKR/USDT | momentum_v1 | 1,754 → −1,841 | 4/57 |
| SKR/USDT | trend_following_v1 | 1,621 → −2,563 | 4/59 |
| SKYAI/USDT | momentum_v1 | 4,206 → −0,392 | 14/59 |
| SKYAI/USDT | trend_following_v1 | 4,062 → −0,521 | 14/62 |
| SLX/USDT | momentum_v1 | 0,988 → −0,823 | 5/59 |
| SLX/USDT | trend_following_v1 | 0,638 → −1,969 | 6/63 |
| TAIKO/USDT | trend_following_v1 | 3,099 → −0,028 | 5/47 |
| TUT/USDT | momentum_v1 | 2,67 → −0,45 | 13/68 |
| USELESS/USDT | trend_following_v1 | 3,259 → −0,353 | 8/59 |

## Einordnung

- **Keine Gate-Änderung, keine Aktivierung/Deaktivierung.** Variante E ist ein zusätzlicher Diagnose-Layer, kein neues Produktions-Gate. Die 13 bzw. 19 markierten Kandidaten sind bereits validierte Kandidaten, deren Performance überdurchschnittlich stark von einzelnen Kursereignissen abhängt — das ist ein Signal für manuelle Review vor einem eventuellen Live-Rollout dieser spezifischen Symbol/Strategie-Paare, kein automatischer Ausschluss.
- Die Ergebnisse dieses Laufs sind **deckungsgleich** mit der ursprünglich (und danach verlorenen) Messung vom selben Tag: 13/91 (3T), 19/91 (7T), TAIKO bei beiden, TUT nur teilweise bei 7-Tage-Chunks — die Rekonstruktion der Eingabedaten aus der DB war also exakt.
- **Empfehlung:** Bei den 19 (7-Tage-Chunk) markierten Symbol/Strategie-Paaren vor Live-Aktivierung einzeln prüfen, ob das identifizierte Extremereignis ein einmaliges, nicht wiederkehrendes Marktereignis war (z. B. Listing-Pump, Delisting-Ankündigung) oder ein wiederkehrendes Muster — das entscheidet, ob Regime-Dominanz hier ein Ausschlussgrund oder ein akzeptables, seltenes Tail-Risiko ist.
