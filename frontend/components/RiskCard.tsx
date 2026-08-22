'use client';

import { useTradingStore } from '@/lib/store';
import { AlertCircle, AlertTriangle } from 'lucide-react';

export function RiskCard() {
  const risk = useTradingStore((state) => state.risk_metrics);

  if (!risk) {
    return (
      <div className="rounded-lg bg-gray-800 p-6 text-center">
        <AlertCircle className="mx-auto mb-2 h-6 w-6 text-gray-400" />
        <p className="text-gray-400">Loading risk metrics...</p>
      </div>
    );
  }

  const heatColor = risk.portfolio_heat > 0.7 ? 'text-sgr-danger' : risk.portfolio_heat > 0.5 ? 'text-sgr-warning' : 'text-sgr-success';
  const drawdownColor = risk.max_drawdown > 0.15 ? 'text-sgr-danger' : 'text-sgr-success';

  return (
    <div className="rounded-lg bg-gray-800 p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Risk Metrics</h2>
        {risk.portfolio_heat > 0.7 && (
          <AlertTriangle className="h-5 w-5 text-sgr-danger" />
        )}
      </div>

      <div className="space-y-4">
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-sm text-gray-400">Portfolio Heat</span>
            <span className={`font-bold ${heatColor}`}>{(risk.portfolio_heat * 100).toFixed(1)}%</span>
          </div>
          <div className="h-2 rounded-full bg-gray-700">
            <div 
              className={`h-full rounded-full transition-all ${
                risk.portfolio_heat > 0.7 ? 'bg-sgr-danger' : risk.portfolio_heat > 0.5 ? 'bg-sgr-warning' : 'bg-sgr-success'
              }`}
              style={{ width: `${risk.portfolio_heat * 100}%` }}
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="rounded bg-gray-700 p-3">
            <p className="text-xs text-gray-400">Max Drawdown</p>
            <p className={`text-lg font-bold ${drawdownColor}`}>{(risk.max_drawdown * 100).toFixed(2)}%</p>
          </div>
          <div className="rounded bg-gray-700 p-3">
            <p className="text-xs text-gray-400">VaR 95%</p>
            <p className="text-lg font-bold text-sgr-accent">{(risk.var_95 * 100).toFixed(2)}%</p>
          </div>
          <div className="rounded bg-gray-700 p-3">
            <p className="text-xs text-gray-400">Leverage</p>
            <p className="text-lg font-bold">{risk.leverage.toFixed(2)}x</p>
          </div>
          <div className="rounded bg-gray-700 p-3">
            <p className="text-xs text-gray-400">Open Positions</p>
            <p className="text-lg font-bold">{risk.open_positions_count}</p>
          </div>
        </div>
      </div>
    </div>
  );
}
