"""
SGR API Dependencies
====================
FastAPI Dependency Injection für alle Router.

Read-Only Dependency-Schicht (sgr-api Zielarchitektur)
-------------------------------------------------------
Seit der sgr-api/sgr-worker-Trennung besitzt die API KEINEN eigenen
Trading Lifecycle mehr. sgr-worker ist alleiniger Owner aller Engines
(RiskEngine, PortfolioEngine, StrategyEngine, ExecutionEngine,
Orchestrator, ReconciliationEngine, MarketDataEngine).

Diese Datei liest daher ausschließlich:
    - Redis (aktuelle/schnelle Zustände: Kill Switch, Risk Metrics)
    - Repository/DB (persistente Zustände: Portfolio Snapshots, Trades,
      Orders, Positionen, Strategien)

Explizit NICHT mehr Teil dieser Schicht: Zugriff auf In-Memory-Engines
über app.state als fachliche Datenquelle. app.state.feature_store bleibt
bestehen, aber ausschließlich als Redis-Konnektivitäts-Fassade (siehe
get_redis_client()) - FeatureStore selbst ist bereits Redis-nativ und
kein In-Memory-Trading-Lifecycle-Bestandteil.

Auth wird weiterhin hier gecheckt – kein Auth-Code in Routen.

Usage in Router:
    @router.get("/portfolio")
    async def get_overview(
        repos: Annotated[Repositories, Depends(get_repos)],
        user: Annotated[TokenData, Depends(require_auth)],
    ):
        snapshot = await repos.portfolio_snapshots.get_latest(...)
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.repositories import Repositories, get_repositories
from sgr.core.types import TradingMode
from sgr.market_data.feature_store import FeatureStore

if TYPE_CHECKING:
    from redis.asyncio import Redis

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
# Trading Mode (deployment-weit, aus Config - nicht pro User)
# ---------------------------------------------------------------------------


def get_trading_mode() -> TradingMode:
    """
    Trading Mode dieses Deployments. Ein sgr-api-Prozess bedient genau
    einen Trading Mode (Paper ODER Live) - identisch zur Konfiguration
    des zugehörigen sgr-worker-Prozesses, mit dem sich die API Redis-Keys
    und DB-Zeilen teilt. Nicht zu verwechseln mit TokenData.trading_mode
    (Prüfung im Auth-Kontext, z.B. für 2FA-Gating bei Live-Aktionen).
    """
    return get_config().trading_mode


# ---------------------------------------------------------------------------
# Redis (aktuelle/schnelle Zustände: Kill Switch, Risk Metrics)
# ---------------------------------------------------------------------------


def get_redis_client(request: Request) -> Redis:
    """
    Rein lesender Zugriff auf die Redis-Verbindung, die die API ohnehin
    hält (app.state.feature_store - bereits Redis-nativ, siehe
    FeatureStore.redis_client). Für Kill-Switch- und Risk-Metrics-Reads,
    die keine eigene zweite Redis-Connection aufbauen sollen.

    Wichtig: dies ist KEIN Zugriff auf eine In-Memory-Engine als fachliche
    Datenquelle - FeatureStore selbst speichert nichts im Prozess, er ist
    nur die Verbindungs-Fassade. Die eigentlichen Daten (Kill-Switch-State,
    Risk-Metrics) liegen in Redis selbst, geschrieben vom Worker.

    Wirft 503, wenn keine Verbindung besteht. Für Endpunkte, für die
    Redis zwingend erforderlich ist (z.B. risk.py). Für Status-/Health-
    Endpunkte, die eine fehlende Verbindung stattdessen als Statuszeile
    melden sollen, siehe get_redis_client_or_none().
    """
    redis_client = get_redis_client_or_none(request)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis connection not available")
    return redis_client


def get_redis_client_or_none(request: Request) -> Redis | None:
    """
    Wie get_redis_client(), aber gibt None statt eine 503-Exception zu
    werfen. Für Status-/Health-Endpunkte (system.py, health.py), bei
    denen eine fehlende Redis-Verbindung selbst das Ergebnis der Abfrage
    ist ("Komponente X: unavailable"), nicht ein Fehler, der die gesamte
    Anfrage abbrechen soll.
    """
    store = getattr(request.app.state, "feature_store", None)
    return getattr(store, "redis_client", None) if store is not None else None


def get_feature_store_connection(request: Request) -> FeatureStore:
    """
    Die im lifespan() tatsächlich verbundene FeatureStore-Instanz
    (app.state.feature_store) - NICHT das Prozess-globale Singleton aus
    sgr.market_data.feature_store.get_feature_store(), dessen .connect()
    in der API nie aufgerufen wird und das daher bei jedem Zugriff mit
    RuntimeError fehlschlagen würde.

    Bewusst KEIN Verstoß gegen "kein app.state als fachliche
    Datenquelle": FeatureStore ist bereits Redis-nativ und hält selbst
    keinen In-Memory-Trading-Lifecycle-Zustand - die eigentlichen Daten
    (berechnete Features) liegen in Redis, geschrieben vom Worker.
    app.state.feature_store ist hier nur die Verbindungs-Fassade,
    exakt wie bei get_redis_client() oben.
    """
    store = getattr(request.app.state, "feature_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Feature store not available")
    return cast(FeatureStore, store)


# ---------------------------------------------------------------------------
# Repositories (persistente Zustände: Snapshots, Trades, Orders, Positionen,
# Strategien)
# ---------------------------------------------------------------------------


def get_repos() -> Repositories:
    """
    Repository-Bündel für alle DB-Reads der Router. Singleton, hält keine
    Verbindung selbst (jede Repository-Methode öffnet ihre eigene Session
    über get_session(), siehe sgr/core/repositories.py).
    """
    return get_repositories()
