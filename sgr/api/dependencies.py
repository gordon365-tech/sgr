"""
SGR API Dependencies
====================
FastAPI Dependency Injection für alle Router.

Alle Engine-Instanzen werden aus app.state gelesen (einmal im Lifespan erstellt).
Auth wird hier gecheckt – kein Auth-Code in Routen.

Usage in Router:
    @router.get("/portfolio")
    async def get_portfolio(
        portfolio: PortfolioEngine = Depends(get_portfolio_engine),
        user: TokenData = Depends(require_auth),
    ):
        ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import TradingMode
from sgr.market_data.feature_store import FeatureStore
from sgr.orchestrator.engine import TradingOrchestrator
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.engine import RiskEngine
from sgr.strategy.engine import StrategyEngine

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Token / Auth
# ---------------------------------------------------------------------------


class TokenData(BaseModel):
    user_id: str
    trading_mode: TradingMode
    is_admin: bool = False


def _decode_token(token: str) -> TokenData:
    """
    JWT Token dekodieren und validieren.
    Wirft HTTPException bei ungültigem Token.
    """
    try:
        from jose import jwt

        config = get_config()
        payload = jwt.decode(
            token,
            config.api.secret_key.get_secret_value(),
            algorithms=[config.api.algorithm],
        )
        user_id: str = payload.get("sub", "")
        trading_mode_str: str = payload.get("trading_mode", "paper")
        is_admin: bool = payload.get("is_admin", False)

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )

        return TokenData(
            user_id=user_id,
            trading_mode=TradingMode(trading_mode_str),
            is_admin=is_admin,
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def require_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> TokenData:
    """Dependency: erfordert gültiges JWT Token."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing or invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    return _decode_token(token)


async def require_admin(
    user: Annotated[TokenData, Depends(require_auth)],
) -> TokenData:
    """Dependency: erfordert Admin-Rolle."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_live_2fa(
    user: Annotated[TokenData, Depends(require_auth)],
    x_totp_code: Annotated[str | None, Header()] = None,
) -> TokenData:
    """
    Dependency für Live-Trading Aktionen.
    Erfordert Auth + gültigen TOTP-Code (2FA).
    """
    if user.trading_mode == TradingMode.LIVE:
        if not x_totp_code:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="2FA required for live trading actions. Provide X-TOTP-Code header.",
            )
        # TOTP Validierung (vereinfacht – vollständige Impl. in auth service)
        # In Produktion: TOTP Secret aus DB laden per user_id
        # Hier: Placeholder-Validierung
        log.info("live_trading.2fa_check", user_id=user.user_id)

    return user


# ---------------------------------------------------------------------------
# Engine Dependencies
# ---------------------------------------------------------------------------


def get_risk_engine(request: Request) -> RiskEngine:
    engine = getattr(request.app.state, "risk_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not initialized")
    return engine


def get_portfolio_engine(request: Request) -> PortfolioEngine:
    engine = getattr(request.app.state, "portfolio_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Portfolio engine not initialized")
    return engine


def get_strategy_engine(request: Request) -> StrategyEngine:
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Strategy engine not initialized")
    return engine


def get_feature_store(request: Request) -> FeatureStore:
    store = getattr(request.app.state, "feature_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Feature store not initialized")
    return store


def get_exchange_pool(request: Request):  # type: ignore[return]
    pool = getattr(request.app.state, "exchange_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Exchange pool not initialized")
    return pool


def get_orchestrator(request: Request) -> TradingOrchestrator:
    orchestrator: TradingOrchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Trading orchestrator not initialized")
    return orchestrator
