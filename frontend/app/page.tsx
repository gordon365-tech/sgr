'use client';

import { WebSocketProvider } from '@/components/WebSocketProvider';
import { PortfolioCard } from '@/components/PortfolioCard';
import { RiskCard } from '@/components/RiskCard';
import { StrategiesCard } from '@/components/StrategiesCard';
import './globals.css';

export default function Dashboard() {
  return (
    <WebSocketProvider>
      <div className="min-h-screen bg-sgr-dark">
        {/* Header */}
        <header className="border-b border-gray-700 bg-gray-800 px-6 py-4">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-sgr-accent">Project SGR</h1>
              <p className="text-sm text-gray-400">Institutional AI-powered Trading System</p>
            </div>
            <div className="text-right">
              <div className="flex items-center gap-2">
                <div className="h-3 w-3 rounded-full bg-sgr-success" />
                <span className="text-sm text-gray-300">Live</span>
              </div>
            </div>
          </div>
        </header>

        {/* Main Grid */}
        <main className="p-6">
          <div className="grid gap-6 lg:grid-cols-3">
            {/* Left Column */}
            <div className="lg:col-span-2 space-y-6">
              <PortfolioCard />
              <StrategiesCard />
            </div>

            {/* Right Column */}
            <div>
              <RiskCard />
            </div>
          </div>
        </main>
      </div>
    </WebSocketProvider>
  );
}
