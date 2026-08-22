'use client';

import { useTradingStore } from '@/lib/store';
import { AlertCircle, PlayCircle, PauseCircle } from 'lucide-react';
import { useState } from 'react';

export function StrategiesCard() {
  const strategies = useTradingStore((state) => state.strategies);
  const [loading, setLoading] = useState<string | null>(null);

  if (!strategies || strategies.length === 0) {
    return (
      <div className="rounded-lg bg-gray-800 p-6 text-center">
        <AlertCircle className="mx-auto mb-2 h-6 w-6 text-gray-400" />
        <p className="text-gray-400">No strategies loaded</p>
      </div>
    );
  }

  const handleToggle = async (name: string, active: boolean) => {
    setLoading(name);
    try {
      const endpoint = active ? 'deactivate' : 'activate';
      const response = await fetch(`/api/v1/strategy/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!response.ok) throw new Error('Failed to toggle strategy');
    } catch (error) {
      console.error('Error toggling strategy:', error);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="rounded-lg bg-gray-800 p-6">
      <h2 className="mb-4 text-lg font-semibold">Strategies</h2>

      <div className="space-y-3">
        {strategies.map((strategy) => (
          <div key={strategy.name} className="flex items-center justify-between rounded bg-gray-700 p-3">
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{strategy.name}</span>
                <span className={`inline-block h-2 w-2 rounded-full ${strategy.active ? 'bg-sgr-success' : 'bg-gray-500'}`} />
              </div>
              <div className="mt-1 flex gap-4 text-xs text-gray-400">
                <span>Win Rate: {(strategy.win_rate * 100).toFixed(1)}%</span>
                <span>Trades: {strategy.total_trades}</span>
                {strategy.last_signal && (
                  <span>Last: {strategy.last_signal} ({strategy.last_signal_time})</span>
                )}
              </div>
            </div>
            <button
              onClick={() => handleToggle(strategy.name, strategy.active)}
              disabled={loading === strategy.name}
              className={`ml-4 rounded p-2 transition ${
                loading === strategy.name
                  ? 'opacity-50 cursor-not-allowed'
                  : strategy.active
                  ? 'hover:bg-gray-600'
                  : 'hover:bg-gray-600'
              }`}
            >
              {strategy.active ? (
                <PauseCircle className="h-5 w-5 text-sgr-warning" />
              ) : (
                <PlayCircle className="h-5 w-5 text-sgr-success" />
              )}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
