"""Tests für sgr.risk.grid_risk.GridRiskEngine (eigenes Grid-Risikoprofil)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sgr.core.grid_types import FuturesGridParameters, GridState
from sgr.core.types import ExchangeID, GridDirection, GridStatus, Symbol, TradingMode
from sgr.risk.grid_risk import GridPortfolioSnapshot, GridRiskEngine, GridRiskLimitsConfig


def _params(**overrides) -> FuturesGridParameters:
    base = dict(
        grid_lower_price=Decimal("90"),
        grid_upper_price=Decimal("110"),
        grid_count=5,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )
    base.update(overrides)
    return FuturesGridParameters(**base)


def _snapshot(open_grids: list | None = None) -> GridPortfolioSnapshot:
    return GridPortfolioSnapshot(open_grids=open_grids or [], portfolio_value=Decimal("10000"))


def _open_grid(notional: str) -> GridState:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
    return GridState(
        exchange=ExchangeID.PIONEX,
        symbol=symbol,
        strategy_name="futures_grid_long_v1",
        trading_mode=TradingMode.PAPER,
        direction=GridDirection.LONG,
        status=GridStatus.ACTIVE,
        parameters={"total_notional": notional},
        opened_at=datetime.now(tz=UTC),
    )


class TestApprovalHappyPath:
    def test_reasonable_grid_is_approved(self) -> None:
        engine = GridRiskEngine()
        result = engine.evaluate_new_grid(_params(), _snapshot(), current_price=Decimal("100"))

        assert result.approved is True


class TestMaxOpenGrids:
    def test_rejects_when_max_open_grids_reached(self) -> None:
        limits = GridRiskLimitsConfig(max_open_grids=1)
        engine = GridRiskEngine(limits)
        snapshot = _snapshot([_open_grid("100")])

        result = engine.evaluate_new_grid(_params(), snapshot, current_price=Decimal("100"))

        assert result.approved is False
        assert "offener Grids" in result.reason


class TestLeverageLimit:
    def test_rejects_leverage_above_limit(self) -> None:
        limits = GridRiskLimitsConfig(max_leverage=Decimal("3"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(leverage=Decimal("10")), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "Leverage" in result.reason


class TestMaxGridOrders:
    def test_rejects_grid_count_above_limit(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_orders=3)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(grid_count=10), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "grid_count" in result.reason


class TestMaximumGridExposure:
    def test_rejects_single_grid_over_position_cap(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_position_usd=Decimal("100"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(position_size=Decimal("50"), grid_count=5),  # total_notional=250
            _snapshot(),
            current_price=Decimal("100"),
        )

        assert result.approved is False
        assert "Grid-Notional" in result.reason

    def test_rejects_when_portfolio_wide_exposure_would_be_exceeded(self) -> None:
        limits = GridRiskLimitsConfig(
            max_grid_exposure_usd=Decimal("300"), max_grid_position_usd=Decimal("10000")
        )
        engine = GridRiskEngine(limits)
        snapshot = _snapshot([_open_grid("200")])

        result = engine.evaluate_new_grid(
            _params(position_size=Decimal("50"), grid_count=5),  # +250 notional
            snapshot,
            current_price=Decimal("100"),
        )

        assert result.approved is False
        assert "Portfolio-weite" in result.reason


class TestLiquidityAndVolatilityLimits:
    def test_rejects_illiquid_market(self) -> None:
        limits = GridRiskLimitsConfig(min_liquidity_usd=Decimal("1000000"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"), liquidity_usd=Decimal("1000")
        )

        assert result.approved is False
        assert "Liquiditaet" in result.reason

    def test_rejects_extreme_volatility(self) -> None:
        limits = GridRiskLimitsConfig(max_volatility_atr_pct=0.05)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"), volatility_atr_pct=0.20
        )

        assert result.approved is False
        assert "ATR" in result.reason


class TestFundingCostLimit:
    def test_rejects_unusual_funding_rate(self) -> None:
        limits = GridRiskLimitsConfig(max_funding_cost_pct=0.0005)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(),
            _snapshot(),
            current_price=Decimal("100"),
            funding_rate_annualized_pct=500.0,
        )

        assert result.approved is False
        assert "Funding" in result.reason


class TestLiquidationDistance:
    def test_rejects_leverage_too_close_to_liquidation(self) -> None:
        limits = GridRiskLimitsConfig(max_liquidation_distance_pct=0.5, max_leverage=Decimal("10"))
        engine = GridRiskEngine(limits)

        # leverage=2 -> Liquidationsdistanz = 1/2 = 50%, genau am Limit
        # von 50% - leverage=3 unterschreitet es (1/3 ≈ 33%).
        result = engine.evaluate_new_grid(
            _params(leverage=Decimal("3")), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "Liquidationsdistanz" in result.reason


class TestOngoingGridChecks:
    def test_max_grid_loss_triggers_hard_violation(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_loss_usd=Decimal("50"))
        engine = GridRiskEngine(limits)
        grid = _open_grid("250")
        grid.realized_pnl = Decimal("-100")

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("100"))

        codes = [v.code for v in violations]
        assert "max_grid_loss_exceeded" in codes
        assert all(v.severity == "hard" for v in violations if v.code == "max_grid_loss_exceeded")

    def test_no_violations_for_healthy_grid(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("100"))

        assert violations == []

    def test_stop_loss_hit_for_long_grid(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["stop_loss"] = "95"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("94"))

        assert any(v.code == "stop_loss_hit" for v in violations)

    def test_funding_violation_during_runtime(self) -> None:
        limits = GridRiskLimitsConfig(max_funding_cost_pct=0.0005)
        engine = GridRiskEngine(limits)
        grid = _open_grid("100")

        violations = engine.check_ongoing_grid(
            grid, current_price=Decimal("100"), funding_rate_annualized_pct=500.0
        )

        assert any(v.code == "funding_cost_limit_exceeded" for v in violations)


# ---------------------------------------------------------------------------
# Phase F: Grid-Cost-Guard
# ---------------------------------------------------------------------------


def _config_with_costs(
    maker: float = 0.0002, taker: float = 0.0005, slippage: float = 0.0005
) -> Any:
    from sgr.core.config import RiskLimitsConfig, SGRConfig
    from sgr.core.types import TradingMode as TM

    return SGRConfig(
        trading_mode=TM.PAPER,
        risk_limits=RiskLimitsConfig(
            paper_maker_fee_pct=maker,
            paper_taker_fee_pct=taker,
            paper_slippage_pct=slippage,
        ),  # type: ignore[arg-type]
    )


class TestCostGuard:
    def test_grid_below_breakeven_is_rejected(self, monkeypatch) -> None:
        """Grid-Spacing 0.1% liegt klar unter den Round-Trip-Kosten
        (Default ~0.17%) - muss abgelehnt werden."""
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        # grid_lower=100, upper=100.5, count=6 -> Spacing = 0.1 absolut =
        # 0.1% relativ bei current_price=100.
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("100.5"), grid_count=6
        )

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False
        assert "Kostenschwelle" in (result.reason or "")

    def test_grid_exactly_at_breakeven_without_margin_is_rejected(self, monkeypatch) -> None:
        """Spacing EXAKT gleich den reinen Round-Trip-Kosten (ohne
        Sicherheitsmarge) - die Sicherheitsmarge (>1x) macht das trotzdem
        zu einer Ablehnung, kein Grenzfall-Approval."""
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        # Round-Trip = maker(0.02%) + taker(0.05%) + 2*slippage(0.05%) = 0.17%
        # Spacing exakt 0.17% von current_price=100 -> 0.17 absolut.
        engine = GridRiskEngine()
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("100.17"), grid_count=2
        )

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False

    def test_grid_sufficiently_above_breakeven_passes(self, monkeypatch) -> None:
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        # Spacing 5% (Default _params()) >> 0.255% Kostenschwelle inkl. Marge.
        result = engine.evaluate_new_grid(_params(), _snapshot(), current_price=Decimal("100"))

        assert result.approved is True

    def test_high_slippage_pushes_grid_below_threshold(self, monkeypatch) -> None:
        """Ein Grid mit 3% Spacing besteht bei normaler Slippage (0.05%)
        problemlos, aber nicht mehr beim (in RiskLimitsConfig zulaessigen)
        Maximum von 1% Slippage je Seite (Round-Trip-Kosten inkl. Marge
        dann (0.02%+0.05%+2%)*1.5 = 3.105%)."""
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: _config_with_costs(slippage=0.01)
        )
        engine = GridRiskEngine()
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("103"), grid_count=2
        )

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False

    def test_high_fees_push_grid_below_threshold(self, monkeypatch) -> None:
        """Ein Grid mit 3% Spacing besteht bei normalen Fees (Kosten-
        schwelle inkl. Marge ~0.255%) problemlos, aber NICHT mehr, wenn
        Maker/Taker-Fee auf das (in RiskLimitsConfig zulaessige) Maximum
        von je 1% steigen (Round-Trip-Kosten inkl. Marge dann 3.15%)."""
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: _config_with_costs(maker=0.01, taker=0.01)
        )
        engine = GridRiskEngine()
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("103"), grid_count=2
        )

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False

    def test_funding_degrades_edge_and_rejects(self, monkeypatch) -> None:
        """Grid-Spacing/Kosten alleine waeren okay, aber eine sehr hohe
        annualisierte Funding-Rate frisst die Zyklus-Edge auf."""
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        # Spacing knapp ueber der reinen Kostenschwelle (0.3%, current=100),
        # aber deutlich unter max_liquidation_distance/anderen Limits.
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("101.2"), grid_count=5
        )

        result = engine.evaluate_new_grid(
            params,
            _snapshot(),
            current_price=Decimal("100"),
            funding_rate_annualized_pct=2000.0,  # extrem hoch, erzwingt Ablehnung
        )

        assert result.approved is False
        assert "Funding" in (result.reason or "")

    def test_cost_guard_applies_to_short_grid_identically(self, monkeypatch) -> None:
        """Der Cost Guard ist richtungsunabhaengig - Short-Grid mit
        identisch zu engem Spacing wird ebenso abgelehnt."""
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        params = _params(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("100.5"),
            grid_count=6,
            long_or_short=GridDirection.SHORT,
        )

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False

    def test_cost_guard_long_grid_with_wide_spacing_passes(self, monkeypatch) -> None:
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        params = _params(long_or_short=GridDirection.LONG)

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is True

    def test_cost_guard_never_mutates_parameters(self, monkeypatch) -> None:
        """Explizite Anforderung: keine versteckte Mutation der vom
        Nutzer validierten Grid-Parameter - bei Ablehnung bleibt
        adjusted_parameters leer/None, es wird NICHT automatisch ein
        neues Spacing vorgeschlagen."""
        monkeypatch.setattr("sgr.core.config.get_config", lambda: _config_with_costs())
        engine = GridRiskEngine()
        params = _params(
            grid_lower_price=Decimal("100"), grid_upper_price=Decimal("100.5"), grid_count=6
        )
        original_lower = params.grid_lower_price
        original_upper = params.grid_upper_price

        result = engine.evaluate_new_grid(params, _snapshot(), current_price=Decimal("100"))

        assert result.approved is False
        assert result.adjusted_parameters is None
        # FuturesGridParameters ist frozen - eine Mutation waere ohnehin
        # unmoeglich, aber explizit verifiziert, dass der Aufrufer sie
        # unveraendert zurueckbekommt.
        assert params.grid_lower_price == original_lower
        assert params.grid_upper_price == original_upper


# ---------------------------------------------------------------------------
# Phase L: Kombinierte Kapital-Obergrenze (Grid + direktional)
# ---------------------------------------------------------------------------


class TestCombinedExposureLimit:
    def test_disabled_by_default_no_effect(self) -> None:
        """max_combined_exposure_usd=None (Default) - directional_exposure_usd
        hat KEINEN Effekt, egal wie hoch."""
        engine = GridRiskEngine()
        result = engine.evaluate_new_grid(
            _params(),
            _snapshot(),
            current_price=Decimal("100"),
            directional_exposure_usd=Decimal("1000000"),
        )
        assert result.approved is True

    def test_combined_limit_rejects_when_directional_plus_grid_exceeds(self) -> None:
        limits = GridRiskLimitsConfig(max_combined_exposure_usd=Decimal("300"))
        engine = GridRiskEngine(limits)
        # _params() -> Grid-Notional 250 (position_size=50 * grid_count=5)
        result = engine.evaluate_new_grid(
            _params(),
            _snapshot(),
            current_price=Decimal("100"),
            directional_exposure_usd=Decimal("100"),  # 250+100=350 > 300
        )
        assert result.approved is False
        assert "Kombinierte Exposure" in (result.reason or "")

    def test_combined_limit_allows_when_within_budget(self) -> None:
        limits = GridRiskLimitsConfig(max_combined_exposure_usd=Decimal("1000"))
        engine = GridRiskEngine(limits)
        result = engine.evaluate_new_grid(
            _params(),
            _snapshot(),
            current_price=Decimal("100"),
            directional_exposure_usd=Decimal("100"),  # 250+100=350 <= 1000
        )
        assert result.approved is True


# ---------------------------------------------------------------------------
# Phase O: Range-Breakout-Schutz (Backtest-Live-Konsistenz)
# ---------------------------------------------------------------------------


class TestRangeBreakoutProtection:
    def test_price_within_range_no_violation(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["grid_lower_price"] = "90"
        grid.parameters["grid_upper_price"] = "110"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("100"))

        assert not any(v.code == "range_breakout" for v in violations)

    def test_price_within_buffer_no_violation(self) -> None:
        """Range [90,110], Breite 20, Buffer-Faktor 0.5 -> Puffer 10 ->
        Breakout-Grenzen [80, 120]. Preis 115 liegt noch INNERHALB."""
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["grid_lower_price"] = "90"
        grid.parameters["grid_upper_price"] = "110"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("115"))

        assert not any(v.code == "range_breakout" for v in violations)

    def test_price_beyond_buffer_triggers_hard_violation(self) -> None:
        """Preis 125 liegt ausserhalb [80, 120] -> Breakout."""
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["grid_lower_price"] = "90"
        grid.parameters["grid_upper_price"] = "110"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("125"))

        breakout = [v for v in violations if v.code == "range_breakout"]
        assert len(breakout) == 1
        assert breakout[0].severity == "hard"

    def test_price_below_buffer_triggers_violation(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["grid_lower_price"] = "90"
        grid.parameters["grid_upper_price"] = "110"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("75"))

        assert any(v.code == "range_breakout" for v in violations)

    def test_custom_buffer_factor_matches_backtest_default_semantics(self) -> None:
        """Identisches Verhalten wie GridBacktestSimulator bei
        range_breakout_buffer_factor=0.5 (Default in beiden Schichten)."""
        limits = GridRiskLimitsConfig(range_breakout_buffer_factor=0.1)
        engine = GridRiskEngine(limits)
        grid = _open_grid("100")
        grid.parameters["grid_lower_price"] = "90"
        grid.parameters["grid_upper_price"] = "110"
        # Buffer = 20*0.1 = 2 -> Grenzen [88, 112]. Preis 115 liegt jetzt
        # ausserhalb (waere bei Default 0.5 noch innerhalb gewesen).

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("115"))

        assert any(v.code == "range_breakout" for v in violations)

    def test_missing_price_bounds_skips_check_gracefully(self) -> None:
        """Ein Grid ohne grid_lower_price/grid_upper_price in parameters
        (z.B. altes/fremdes Format) darf den Check nicht crashen lassen."""
        engine = GridRiskEngine()
        grid = _open_grid("100")

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("999999"))

        assert not any(v.code == "range_breakout" for v in violations)


# ---------------------------------------------------------------------------
# Phase 9: Fail-Closed bei unbestimmbarer direktionaler Exposure
# ---------------------------------------------------------------------------


class TestCombinedExposureFailClosed:
    def test_none_exposure_with_limit_configured_is_rejected(self) -> None:
        """max_combined_exposure_usd konfiguriert, aber
        directional_exposure_usd=None (nicht bestimmbar) - MUSS ablehnen,
        darf NICHT stillschweigend 0 annehmen."""
        limits = GridRiskLimitsConfig(max_combined_exposure_usd=Decimal("10000"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"),
            directional_exposure_usd=None,
        )

        assert result.approved is False
        assert "fail-closed" in (result.reason or "").lower()

    def test_none_exposure_without_limit_configured_has_no_effect(self) -> None:
        """Ohne konfiguriertes Limit (Default) ist directional_exposure_usd
        irrelevant - None ist dann kein Problem."""
        engine = GridRiskEngine()  # max_combined_exposure_usd default None

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"),
            directional_exposure_usd=None,
        )

        assert result.approved is True

    def test_explicit_zero_exposure_is_accepted_as_a_real_value(self) -> None:
        """Decimal(0) ist ein GUELTIGER, bestaetigter Wert (echte Null-
        Exposure) - nur None (unbekannt) loest fail-closed aus."""
        limits = GridRiskLimitsConfig(max_combined_exposure_usd=Decimal("10000"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"),
            directional_exposure_usd=Decimal("0"),
        )

        assert result.approved is True
