'use client';

import { useTradingStore } from '@/lib/store';
import { TrendingUp, TrendingDown, AlertCircle } from 'lucide-react';

export function PortfolioCard() {
  const portfolio = useTradingStore((state) => state.portfolio);

  if (!portfolio) {
    return (
      <div className="rounded-lg bg-gray-800 p-6 text-center">
        <AlertCircle className="mx-auto mb-2 h-6 w-6 text-gray-400" />
        <p className="text-gray-400">Loading portfolio...</p>
      </div>
    );
  }

  const pnlColor = portfolio.daily_pnl >= 0 ? 'text-sgr-success' : 'text-sgr-danger';
  const pnlIcon = portfolio.daily_pnl >= 0 ? TrendingUp : TrendingDown;
  const PnLIcon = pnlIcon;

  return (
    <div className="rounded-lg bg-gray-800 p-6">
      <h2 className="mb-4 text-lg font-semibold">Portfolio</h2>
      
      <div className="mb-6 grid grid-cols-2 gap-4">
        <div>
          <p className="text-sm text-gray-400">Total Value</p>
          <p className="text-2xl font-bold">${portfolio.total_value.toFixed(2)}</p>
        </div>
        <div>
          <p className="text-sm text-gray-400">Cash</p>
          <p className="text-2xl font-bold">${portfolio.cash.toFixed(2)}</p>
        </div>
      </div>

      <div className="rounded bg-gray-700 p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-400">Daily PnL</p>
            <p className={`text-xl font-bold ${pnlColor}`}>
              ${portfolio.daily_pnl.toFixed(2)} ({portfolio.daily_pnl_pct.toFixed(2)}%)
            </p>
          </div>
          <PnLIcon className={`h-8 w-8 ${pnlColor}`} />
        </div>
      </div>

      <div className="mt-6">
        <h3 className="mb-3 text-sm font-semibold text-gray-300">Positions ({portfolio.positions.length})</h3>
        <div className="space-y-2">
          {portfolio.positions.slice(0, 5).map((pos) => (
            <div key={pos.symbol} className="flex items-center justify-between rounded bg-gray-700 px-3 py-2 text-sm">
              <span className="font-mono">{pos.symbol}</span>
              <span className="text-gray-400">{pos.quantity}</span>
              <span className={pos.pnl >= 0 ? 'text-sgr-success' : 'text-sgr-danger'}>
                {pos.pnl_pct.toFixed(2)}%
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
