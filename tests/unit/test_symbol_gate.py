"""Tests fuer sgr.strategy.symbol_gate.SymbolStrategyGate.

Fokus: das Fallback-Prinzip (siehe Modul-Docstring) - kein Ergebnis
fuer ein Symbol darf niemals den bestehenden Live-Trading-Pfad
blockieren.
"""

from __future__ import annotations

from sgr.strategy.symbol_gate import SymbolStrategyGate


def _fresh_gate() -> SymbolStrategyGate:
    gate = SymbolStrategyGate()
    return gate


class TestFallbackPrinciple:
    def test_unknown_symbol_allows_any_strategy(self) -> None:
        gate = _fresh_gate()
        assert gate.is_allowed("BTC/USDT", "trend_following_v1") is True
        assert gate.is_allowed("BTC/USDT", "anything_at_all") is True

    def test_has_result_for_reports_false_for_unknown_symbol(self) -> None:
        gate = _fresh_gate()
        assert gate.has_result_for("BTC/USDT") is False


class TestActiveSymbol:
    def test_only_the_active_strategy_is_allowed_for_that_symbol(self) -> None:
        gate = _fresh_gate()
        gate._active_strategy_by_symbol["BTC/USDT"] = "trend_following_v1"

        assert gate.is_allowed("BTC/USDT", "trend_following_v1") is True
        assert gate.is_allowed("BTC/USDT", "mean_reversion_v1") is False

    def test_other_symbols_remain_unaffected(self) -> None:
        gate = _fresh_gate()
        gate._active_strategy_by_symbol["BTC/USDT"] = "trend_following_v1"

        assert gate.is_allowed("ETH/USDT", "mean_reversion_v1") is True


class TestNoValidStrategySymbol:
    def test_validated_but_no_strategy_suitable_blocks_all_strategies(self) -> None:
        """Ein Symbol, das batch-validiert wurde, aber KEINE Strategie
        bestanden hat (None-Eintrag), muss ALLE Strategien blockieren -
        siehe Modul-Docstring 'keine Umgehung von mark_validated()'."""
        gate = _fresh_gate()
        gate._active_strategy_by_symbol["XYZ/USDT"] = None

        assert gate.is_allowed("XYZ/USDT", "trend_following_v1") is False
        assert gate.is_allowed("XYZ/USDT", "mean_reversion_v1") is False


class TestStaleness:
    def test_fresh_instance_is_stale(self) -> None:
        assert _fresh_gate().stale() is True

    async def test_refresh_failure_does_not_clear_existing_cache(self, monkeypatch) -> None:
        """Fail-open: ein DB-Fehler beim Refresh darf den bestehenden
        Cache-Stand nicht loeschen (siehe refresh() Docstring)."""
        gate = _fresh_gate()
        gate._active_strategy_by_symbol["BTC/USDT"] = "trend_following_v1"

        async def _broken_get_repositories():
            raise RuntimeError("db down")

        monkeypatch.setattr(
            "sgr.core.repositories.get_repositories",
            lambda: (_ for _ in ()).throw(RuntimeError("db down")),
        )

        await gate.refresh()

        assert gate.is_allowed("BTC/USDT", "trend_following_v1") is True
