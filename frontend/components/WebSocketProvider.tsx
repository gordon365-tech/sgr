'use client';

import { useEffect } from 'react';
import useWebSocket from 'use-websocket';
import { useTradingStore } from '@/lib/store';

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000/ws';

export function WebSocketProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const { lastJsonMessage, readyState } = useWebSocket(WS_URL, {
    onOpen: () => {
      console.log('[WS] Connected');
      useTradingStore.setState({ connected: true });
    },
    onClose: () => {
      console.log('[WS] Disconnected');
      useTradingStore.setState({ connected: false });
    },
    onError: (event) => {
      console.error('[WS] Error:', event);
    },
    shouldReconnect: () => true,
    reconnectInterval: 3000,
  });

  // Process incoming messages
  useEffect(() => {
    if (lastJsonMessage) {
      const msg = lastJsonMessage as any;
      
      if (msg.type === 'portfolio') {
        useTradingStore.setState({ portfolio: msg.data });
      } else if (msg.type === 'risk_metrics') {
        useTradingStore.setState({ risk_metrics: msg.data });
      } else if (msg.type === 'strategies') {
        useTradingStore.setState({ strategies: msg.data });
      }
    }
  }, [lastJsonMessage]);

  return <>{children}</>;
}
