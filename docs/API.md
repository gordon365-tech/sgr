# Project SGR – API Reference

## Authentication

All requests (except `/health`) require a valid JWT token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/...
```

### Login
```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "email": "trader@example.com",
  "password": "secure_password"
}
```

**Response:**
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Refresh Token
```http
POST /api/v1/auth/refresh
Content-Type: application/json

{
  "refresh_token": "<refresh_token>"
}
```

---

## Health & System

### Health Check
```http
GET /health
```

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2024-08-20T12:34:56Z",
  "components": {
    "database": "healthy",
    "redis": "healthy",
    "exchanges": "connected"
  }
}
```

### System Status
```http
GET /api/v1/system/status
Authorization: Bearer <token>
```

**Response:**
```json
{
  "environment": "production",
  "trading_mode": "paper",
  "uptime_seconds": 86400,
  "version": "0.1.0"
}
```

### Kill Switch (Emergency Stop)
```http
POST /api/v1/system/kill-switch
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "reason": "Manual stop – critical event detected"
}
```

---

## Market Data

### Get Recent Candles
```http
GET /api/v1/market/candles?symbol=BTC/USDT&timeframe=1h&limit=100
Authorization: Bearer <token>
```

**Query Parameters:**
- `symbol`: Trading pair (BTC/USDT, ETH/USDT, etc.)
- `timeframe`: 1m, 5m, 15m, 1h, 4h, 1d
- `limit`: Max 500

**Response:**
```json
{
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "candles": [
    {
      "timestamp": "2024-08-20T12:00:00Z",
      "open": 42500.50,
      "high": 42750.00,
      "low": 42400.00,
      "close": 42650.00,
      "volume": 1234.56
    },
    ...
  ]
}
```

### Subscribe to Live Candles (WebSocket)
```javascript
// Client-side (JavaScript)
const ws = new WebSocket('ws://localhost:8000/ws');

// Subscribe to candles
ws.send(JSON.stringify({
  type: 'subscribe_candles',
  symbol: 'BTC/USDT',
  timeframe: '1h'
}));

// Receive updates
ws.addEventListener('message', (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'candle') {
    console.log(data.candle);
  }
});
```

---

## Portfolio

### Get Portfolio
```http
GET /api/v1/portfolio
Authorization: Bearer <token>
```

**Response:**
```json
{
  "total_value": 50000.00,
  "cash": 30000.00,
  "positions": [
    {
      "symbol": "BTC/USDT",
      "quantity": 0.5,
      "entry_price": 40000.00,
      "current_price": 42650.00,
      "pnl": 1325.00,
      "pnl_pct": 6.625
    }
  ],
  "daily_pnl": 250.00,
  "daily_pnl_pct": 0.5
}
```

### Get Position
```http
GET /api/v1/portfolio/positions/{symbol}
Authorization: Bearer <token>
```

### Close Position
```http
POST /api/v1/portfolio/positions/{symbol}/close
Authorization: Bearer <token>
Content-Type: application/json

{
  "price": 42650.00  // Optional: specify exit price (default: market)
}
```

---

## Risk Management

### Get Risk Metrics
```http
GET /api/v1/risk/metrics
Authorization: Bearer <token>
```

**Response:**
```json
{
  "portfolio_heat": 0.65,
  "max_drawdown": 0.08,
  "var_95": 0.035,
  "leverage": 1.2,
  "open_positions_count": 3,
  "estimated_liquidation_price": 35000.00
}
```

### Get Risk Limits
```http
GET /api/v1/risk/limits
Authorization: Bearer <token>
```

**Response:**
```json
{
  "max_portfolio_drawdown": 0.15,
  "daily_loss_limit": 0.05,
  "max_single_position_pct": 0.10,
  "var_95_limit": 0.03,
  "portfolio_heat_limit": 0.70,
  "max_leverage": 3.0,
  "max_open_positions": 10
}
```

### Update Risk Limits
```http
PATCH /api/v1/risk/limits
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "max_portfolio_drawdown": 0.20,
  "daily_loss_limit": 0.08
}
```

---

## Strategies

### List Strategies
```http
GET /api/v1/strategy/list
Authorization: Bearer <token>
```

**Response:**
```json
[
  {
    "name": "trend_following_v1",
    "active": true,
    "last_signal": "BUY BTC/USDT",
    "last_signal_time": "2024-08-20T12:00:00Z",
    "win_rate": 0.58,
    "total_trades": 142
  },
  {
    "name": "mean_reversion_v2",
    "active": false,
    "last_signal": null,
    "win_rate": 0.52,
    "total_trades": 87
  }
]
```

### Activate Strategy
```http
POST /api/v1/strategy/activate
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "mean_reversion_v2"
}
```

### Deactivate Strategy
```http
POST /api/v1/strategy/deactivate
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "mean_reversion_v2",
  "reason": "Poor recent performance"
}
```

---

## Orders

### Get Order History
```http
GET /api/v1/orders?limit=50&offset=0
Authorization: Bearer <token>
```

**Query Parameters:**
- `limit`: Results per page (default 50)
- `offset`: Pagination offset
- `status`: pending, filled, cancelled
- `since`: ISO timestamp (filter by date)

**Response:**
```json
{
  "total": 250,
  "orders": [
    {
      "id": "order_123",
      "symbol": "BTC/USDT",
      "side": "buy",
      "quantity": 0.5,
      "price": 42650.00,
      "status": "filled",
      "fees": 21.33,
      "executed_at": "2024-08-20T12:05:00Z"
    }
  ]
}
```

### Cancel Order
```http
DELETE /api/v1/orders/{order_id}
Authorization: Bearer <token>
```

---

## Trading

### Manual Trade Execution
```http
POST /api/v1/trading/execute
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "symbol": "BTC/USDT",
  "side": "buy",
  "quantity": 0.5,
  "price": 42650.00,  // Optional: market order if omitted
  "reason": "Manual override – support decision"
}
```

---

## Backtesting

### Run Full Validation
```http
POST /api/v1/backtesting/validate
Authorization: Bearer <token>
Content-Type: application/json

{
  "strategy_names": ["trend_following_v1"],
  "symbols": ["BTC/USDT"],
  "timeframe": "1h",
  "start_date": "2023-01-01",
  "end_date": "2023-12-31",
  "run_walk_forward": true,
  "run_monte_carlo": true,
  "monte_carlo_runs": 1000
}
```

**Response:**
```json
{
  "go_live_decision": "GO",
  "decision_summary": "All gates passed...",
  "blockers": [],
  "warnings": [],
  "backtest_kpis": {
    "total_return_pct": 25.5,
    "cagr_pct": 8.2,
    "sharpe_ratio": 1.05,
    "sortino_ratio": 1.35,
    "max_drawdown_pct": 12.5,
    "total_trades": 142,
    "hit_rate_pct": 58.0
  }
}
```

---

## Error Responses

### 400 Bad Request
```json
{
  "detail": "Invalid parameter: symbol must be in format BASE/QUOTE"
}
```

### 401 Unauthorized
```json
{
  "detail": "Invalid or missing authentication token"
}
```

### 403 Forbidden
```json
{
  "detail": "Insufficient permissions for this action"
}
```

### 429 Too Many Requests
```json
{
  "detail": "Rate limit exceeded. Retry after 60 seconds.",
  "retry_after": 60
}
```

### 500 Internal Server Error
```json
{
  "detail": "Internal server error",
  "request_id": "req_abc123xyz"  # For support tickets
}
```

---

## Rate Limits

| Endpoint | Limit | Window |
|----------|-------|--------|
| `/api/v1/market/*` | 100 | 1 minute |
| `/api/v1/portfolio/*` | 50 | 1 minute |
| `/api/v1/strategy/activate` | 10 | 1 minute |
| `/api/v1/trading/execute` | 5 | 1 minute |
| `/auth/login` | 5 | 5 minutes |

---

## Webhooks (Future)

Not yet implemented. Subscribe to updates:
- Portfolio changes
- Order fills
- Risk alerts
- Strategy signals

**Coming in v0.2.0**
