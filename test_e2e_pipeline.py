#!/usr/bin/env python
"""
Simple E2E Test Script for Trading Pipeline
Verifies the complete flow without pytest infrastructure issues.
"""

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

# Add project to path
sys.path.insert(0, '.')

async def test_pipeline():
    """Test the complete trading pipeline."""
    print("Starting SGR Trading Pipeline Test...")

    try:
        # Import after path is set
        from sgr.core.types import (
            ExchangeID,
            MarketRegime,
            Signal,
            SignalDirection,
            Symbol,
            TradingCycleStatus,
            TradingMode,
        )
        from sgr.exchanges.factory import ExchangePool
        from sgr.execution.engine import ExecutionEngine
        from sgr.market_data.feature_store import FeatureStore
        from sgr.market_data.types import OHLCV, FeatureSet, MarketContext
        from sgr.orchestrator.engine import TradingOrchestrator
        from sgr.portfolio.engine import PortfolioEngine
        from sgr.risk.engine import RiskEngine
        from sgr.strategy.base import BaseStrategy, ValidationStatus
        from sgr.strategy.engine import StrategyEngine
        from sgr.strategy.registry import StrategyRegistry

        print("✓ All imports successful")

        # Setup: Create test strategy
        class TestStrategy(BaseStrategy):
            name = "test_strategy"
            version = "1.0.0"
            supported_regimes = [MarketRegime.UNKNOWN]

            def generate_signal(self, context: MarketContext) -> Signal | None:
                return Signal(
                    symbol=context.symbol,
                    strategy_name=self.name,
                    direction=SignalDirection.LONG,
                    confidence=Decimal("0.85"),
                    timestamp=datetime.now(tz=UTC),
                    metadata={},
                )

        print("✓ Test strategy created")

        # Initialize components
        pool = ExchangePool()
        await pool.initialize([ExchangeID.PIONEX], TradingMode.PAPER)
        print("✓ Exchange pool initialized (PAPER mode)")

        feature_store = FeatureStore()
        await feature_store.connect()
        print("✓ Feature store connected")

        portfolio_engine = PortfolioEngine(
            trading_mode=TradingMode.PAPER,
            initial_cash=Decimal("10000"),
        )
        print("✓ Portfolio engine initialized with $10,000 USDT")

        risk_engine = RiskEngine(TradingMode.PAPER)
        await risk_engine.initialize()
        print("✓ Risk engine initialized")

        # Setup strategy registry
        registry = StrategyRegistry.get()
        registry.clear()
        test_strat = TestStrategy()
        registry.register_instance(test_strat)
        registry.mark_validated(
            test_strat.name,
            ValidationStatus(can_go_live=True),
        )
        print("✓ Strategy registered and validated")

        strategy_engine = StrategyEngine(TradingMode.PAPER, feature_store, registry)
        await strategy_engine.start()
        print("✓ Strategy engine started")

        execution_engine = ExecutionEngine(pool, TradingMode.PAPER)
        print("✓ Execution engine initialized")

        orchestrator = TradingOrchestrator(
            strategy_engine=strategy_engine,
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio_engine=portfolio_engine,
            feature_store=feature_store,
            trading_mode=TradingMode.PAPER,
        )
        print("✓ Trading orchestrator initialized")

        # Prepare market data
        btc_symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
        now = datetime.now(tz=UTC)
        ohlcv = OHLCV(
            timestamp=now,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
        )
        features = FeatureSet(
            symbol=btc_symbol,
            timeframe="1h",
            timestamp=now,
            ohlcv=ohlcv,
            indicators={
                "sma_20": Decimal("49900"),
                "sma_50": Decimal("49000"),
                "rsi_14": Decimal("65"),
                "atr_14": Decimal("500"),
                "bb_upper": Decimal("52000"),
                "bb_lower": Decimal("48000"),
            },
        )

        await feature_store.set_latest(str(btc_symbol), "1h", features)
        print("✓ Market data loaded: BTC/USDT @ $50,500")

        # RUN THE CYCLE
        print("\n" + "="*60)
        print("RUNNING TRADING CYCLE: CandleEvent → Signal → Risk → Execution")
        print("="*60)

        result = await orchestrator.run_cycle(
            symbol_key=f"{ExchangeID.PIONEX.value}:BTC/USDT",
            timeframe="1h",
            regime=MarketRegime.UNKNOWN,
        )

        print("\n✓ Trading cycle completed")
        print(f"  Status: {result.status.value}")
        print(f"  Signal: {result.signal.direction.value if result.signal else 'None'}")
        print(f"  Risk Decision: {result.assessment.decision.value if result.assessment else 'N/A'}")
        print(f"  Order Status: {result.order_result.status.value if result.order_result else 'N/A'}")

        # Verify pipeline
        print("\n" + "="*60)
        print("PIPELINE VERIFICATION")
        print("="*60)

        checks = {
            "Signal Generated": result.signal is not None,
            "Risk Assessment Done": result.assessment is not None,
            "Order Executed": result.order_result is not None,
            "Happy Path (ORDER_FILLED)": result.status == TradingCycleStatus.ORDER_FILLED,
        }

        for check, passed in checks.items():
            status = "✓" if passed else "✗"
            print(f"  {status} {check}")

        # Check portfolio state
        print(f"\n  Portfolio Value: ${portfolio_engine.portfolio_value}")
        print(f"  Cash Available: ${portfolio_engine.cash}")
        print(f"  Open Positions: {len(portfolio_engine.positions)}")

        if len(portfolio_engine.positions) > 0:
            pos = portfolio_engine.positions[0]
            print(f"    - {pos.symbol.ccxt_symbol} {pos.side.value}")
            print(f"      Qty: {pos.quantity}, Entry: ${pos.entry_price}")

        # Safety checks
        print(f"\n  Trading Mode: {TradingMode.PAPER.value} ✓ (Not LIVE)")

        # Summary
        print("\n" + "="*60)
        if result.status == TradingCycleStatus.ORDER_FILLED:
            print("✓ FULL END-TO-END PIPELINE WORKING")
            print("  CandleEvent → Signal → Risk Approved → Order Filled → Position Updated")
        else:
            print(f"⚠ Cycle completed with status: {result.status.value}")
        print("="*60)

        await feature_store.close()
        await pool.close_all()

        return result.status == TradingCycleStatus.ORDER_FILLED

    except Exception as e:
        print(f"\n✗ Error during test: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_pipeline())
    sys.exit(0 if success else 1)
