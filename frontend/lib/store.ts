import { create } from 'zustand';

interface Portfolio {
  total_value: number;
  cash: number;
  positions: Array<{
    symbol: string;
    quantity: number;
    entry_price: number;
    current_price: number;
    pnl: number;
    pnl_pct: number;
  }>;
  daily_pnl: number;
  daily_pnl_pct: number;
}

interface RiskMetrics {
  portfolio_heat: number;
  var_95: number;
  max_drawdown: number;
  leverage: number;
  open_positions_count: number;
}

interface StrategyStatus {
  name: string;
  active: boolean;
  last_signal: string | null;
  last_signal_time: string | null;
  win_rate: number;
  total_trades: number;
}

interface TradingState {
  portfolio: Portfolio | null;
  risk_metrics: RiskMetrics | null;
  strategies: StrategyStatus[];
  connected: boolean;
  
  setPortfolio: (portfolio: Portfolio) => void;
  setRiskMetrics: (metrics: RiskMetrics) => void;
  setStrategies: (strategies: StrategyStatus[]) => void;
  setConnected: (connected: boolean) => void;
}

export const useTradingStore = create<TradingState>((set) => ({
  portfolio: null,
  risk_metrics: null,
  strategies: [],
  connected: false,
  
  setPortfolio: (portfolio) => set({ portfolio }),
  setRiskMetrics: (risk_metrics) => set({ risk_metrics }),
  setStrategies: (strategies) => set({ strategies }),
  setConnected: (connected) => set({ connected }),
}));
