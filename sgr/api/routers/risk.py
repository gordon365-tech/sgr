"""
SGR Risk Router
=================
Read-only für Metriken/Status: Kill Switch und Risk Metrics kommen aus
Redis (vom Worker geschrieben), nicht mehr aus einem In-Memory RiskEngine
im API-Prozess.

Trigger/Reset bleiben als echte Aktionen erhalten (Sicherheitsprinzip
schlägt Architekturreinheit - der globale Notaus muss über die API
auslösbar bleiben, siehe Entscheidung zu Commit 3). Sie instanziieren
eine KillSwitch mit injiziertem Redis-Client und lösen darüber den
Pub/Sub-Broadcast an sgr-worker aus (siehe sgr/risk/kill_switch.py,
_publish_to_redis) - kein eigener Trading-Lifecycle-Zustand in der API,
nur die Zustandsverbreitung über den bereits von Commit 2 vorgesehenen
Kanal.

Symbol Kill Switch: bewusst noch NICHT auf Redis umgestellt (siehe
Entscheidung zu Commit 3 - eigener Folge-Commit, analog zum globalen
Kill Switch in Commit 2). Die Endpunkte bleiben bestehen, aber die
Response macht explizit, dass der Status nur für den lokalen Prozess
gilt, nicht cross-prozess-verlässlich ist.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis

from sgr.api.dependencies import (
    TokenData,
    get_redis_client,
    get_trading_mode,
    require_admin,
    require_auth,
    require_live_2fa,
)
from sgr.core.config import get_config
from sgr.core.types import TradingMode
from sgr.risk.kill_switch import KillSwitch, read_kill_switch_state_from_redis
from sgr.risk.metrics_cache import read_risk_metrics_from_redis
from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

router = APIRouter()


class RiskMetricsResponse(BaseModel):
    portfolio_value: str
    daily_pnl: str
    daily_pnl_pct: float
    drawdown_from_peak: float
    var_95: float
    expected_shortfall: float
    portfolio_heat: float
    active_positions: int
    kill_switch_active: bool
    kill_switch_reason: str | None
    stale: bool


class KillSwitchResponse(BaseModel):
    is_active: bool | None
    triggered_at: str | None
    reason: str | None
    trading_mode: str
    status_known: bool


class KillSwitchTriggerRequest(BaseModel):
    reason: str
    close_positions: bool = False


@router.get("/metrics", response_model=RiskMetricsResponse)
async def get_risk_metrics(
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> RiskMetricsResponse:
    """
    Zuletzt vom Worker berechnete Risk-Metriken (aus Redis, siehe
    sgr/risk/metrics_cache.py). `stale=True` bedeutet: der Worker hat seit
    über 120s (TTL) keine neuen Metriken mehr geschrieben, oder es wurde
    noch nie welche geschrieben - in diesem Fall sind alle numerischen
    Felder 0/Platzhalter und dürfen NICHT als "kein Risiko" interpretiert
    werden.
    """
    metrics = await read_risk_metrics_from_redis(redis_client, trading_mode)
    ks_state = await read_kill_switch_state_from_redis(redis_client, trading_mode)
    kill_switch_active = bool(ks_state["is_active"]) if ks_state is not None else None

    if metrics is None:
        return RiskMetricsResponse(
            portfolio_value="0",
            daily_pnl="0",
            daily_pnl_pct=0.0,
            drawdown_from_peak=0.0,
            var_95=0.0,
            expected_shortfall=0.0,
            portfolio_heat=0.0,
            active_positions=0,
            kill_switch_active=bool(kill_switch_active),
            kill_switch_reason=ks_state.get("reason") if ks_state else None,
            stale=True,
        )

    return RiskMetricsResponse(
        portfolio_value=str(metrics["portfolio_value"]),
        daily_pnl=str(metrics["daily_pnl"]),
        daily_pnl_pct=round(metrics["daily_pnl_pct"] * 100, 2),
        drawdown_from_peak=round(metrics["drawdown_from_peak"] * 100, 2),
        var_95=round(metrics["var_95"] * 100, 4),
        expected_shortfall=round(metrics["expected_shortfall"] * 100, 4),
        portfolio_heat=round(metrics["portfolio_heat"] * 100, 2),
        active_positions=metrics["active_positions"],
        kill_switch_active=bool(kill_switch_active),
        kill_switch_reason=ks_state.get("reason") if ks_state else None,
        stale=False,
    )


@router.get("/limits")
async def get_limits(
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """Aktuelle Risk-Limit-Konfiguration."""
    config = get_config()
    limits = config.risk_limits
    return {
        "hard_limits": {
            "max_portfolio_drawdown_pct": limits.max_portfolio_drawdown * 100,
            "daily_loss_limit_pct": limits.daily_loss_limit * 100,
            "max_single_position_pct": limits.max_single_position_pct * 100,
            "max_open_positions": limits.max_open_positions,
        },
        "soft_limits": {
            "var_95_limit_pct": limits.var_95_limit * 100,
            "portfolio_heat_limit_pct": limits.portfolio_heat_limit * 100,
            "max_correlation_exposure": limits.max_correlation_exposure,
            "max_slippage_pct": limits.max_slippage_pct * 100,
        },
        "futures": {
            "max_leverage": str(limits.max_leverage),
        },
        "trading_behavior": {
            "trade_cooldown_seconds": limits.trade_cooldown_seconds,
        },
    }


@router.get("/kill-switch", response_model=KillSwitchResponse)
async def get_kill_switch_status(
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    user: Annotated[TokenData, Depends(require_auth)],
) -> KillSwitchResponse:
    """
    Status des globalen Kill Switch, aus Redis (vom Worker geschrieben).
    `status_known=False` bedeutet: noch nie ein State geschrieben ODER
    Redis-Fehler - in diesem Fall MUSS der Status als unbekannt behandelt
    werden, nicht als "inaktiv" (fail-safe, siehe
    read_kill_switch_state_from_redis Docstring).
    """
    state = await read_kill_switch_state_from_redis(redis_client, trading_mode)
    if state is None:
        return KillSwitchResponse(
            is_active=None,
            triggered_at=None,
            reason=None,
            trading_mode=trading_mode.value,
            status_known=False,
        )
    return KillSwitchResponse(
        is_active=bool(state["is_active"]),
        triggered_at=state.get("triggered_at"),
        reason=state.get("reason"),
        trading_mode=trading_mode.value,
        status_known=True,
    )


@router.post("/kill-switch/trigger")
async def trigger_kill_switch(
    body: KillSwitchTriggerRequest,
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    user: Annotated[TokenData, Depends(require_live_2fa)],
) -> dict:
    """
    Manueller Kill Switch Trigger.
    Erfordert Auth + 2FA (Live Mode).

    Instanziiert eine eigene KillSwitch mit injiziertem Redis-Client -
    ihr lokaler In-Memory-State ist irrelevant (die API führt keine
    Trades aus), relevant ist ausschließlich der dadurch ausgelöste
    Redis-Write + Pub/Sub-Broadcast, den sgr-worker empfängt und lokal
    übernimmt (siehe sgr/risk/kill_switch.py).
    """
    ks = KillSwitch(trading_mode)
    ks.inject_redis(redis_client)
    await ks.trigger(
        reason=f"Manual: {body.reason}",
        triggered_by=f"user:{user.user_id}",
        close_positions=body.close_positions,
    )
    return {
        "triggered": True,
        "reason": body.reason,
        "triggered_by": user.user_id,
    }


@router.post("/kill-switch/reset")
async def reset_kill_switch(
    trading_mode: Annotated[TradingMode, Depends(get_trading_mode)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict:
    """
    Kill Switch zurücksetzen.
    Erfordert Admin-Rolle.
    Nur nach manueller Prüfung der Ursache aufrufen.
    """
    ks = KillSwitch(trading_mode)
    ks.inject_redis(redis_client)
    await ks.reset(reset_by=user.user_id)
    return {"reset": True, "reset_by": user.user_id}


# ---------------------------------------------------------------------
# Symbol Kill Switch
#
# HINWEIS: noch In-Memory, nicht Redis-backed (siehe Modul-Docstring).
# Status gilt nur fuer den API-Prozess selbst, NICHT fuer den Worker -
# ein hier aktiver/inaktiver Symbol-Eintrag sagt nichts darueber aus,
# was der tatsaechlich Trades ausfuehrende Worker gerade durchsetzt.
# ---------------------------------------------------------------------


class SymbolKillSwitchEntryResponse(BaseModel):
    symbol_key: str
    is_active: bool
    deactivated_at: str | None
    reason: str | None
    deactivated_by: str | None


@router.get("/symbol-kill-switch", response_model=list[SymbolKillSwitchEntryResponse])
async def list_symbol_kill_switches(
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[SymbolKillSwitchEntryResponse]:
    """
    Symbol-Kill-Switch-Eintraege des API-Prozesses (NICHT cross-prozess-
    verlaesslich, siehe Modul-Docstring - Redis-Umstellung ist ein
    eigener Folge-Commit).
    """
    sks = get_symbol_kill_switch()
    return [
        SymbolKillSwitchEntryResponse(
            symbol_key=entry.symbol_key,
            is_active=entry.is_active,
            deactivated_at=entry.deactivated_at.isoformat() if entry.deactivated_at else None,
            reason=entry.reason,
            deactivated_by=entry.deactivated_by,
        )
        for entry in sks.get_all().values()
    ]


@router.post("/symbol-kill-switch/{symbol_key:path}/deactivate")
async def deactivate_symbol(
    symbol_key: str,
    user: Annotated[TokenData, Depends(require_auth)],
    reason: str = "Manual deactivation",
) -> dict[str, str]:
    """
    Trading für ein einzelnes Symbol im API-Prozess deaktivieren.

    WARNUNG: wirkt derzeit NUR im API-Prozess, nicht im Worker (siehe
    Modul-Docstring) - kein zuverlässiger Notaus-Mechanismus für ein
    einzelnes Symbol, bis der Folge-Commit Redis-Backing nachzieht.
    Für einen sofortigen, verlässlichen Stopp den globalen Kill Switch
    verwenden.

    Bewusst require_auth statt require_admin: wie bei
    strategy.deactivate_strategy() ist Deaktivieren die defensive
    Richtung und darf niedrigschwelliger sein als Reaktivierung.
    """
    sks = get_symbol_kill_switch()
    await sks.deactivate(symbol_key, reason, deactivated_by=f"user:{user.user_id}")
    return {
        "deactivated": symbol_key,
        "reason": reason,
        "warning": "Only effective in the API process, not yet synced to sgr-worker.",
    }


@router.post("/symbol-kill-switch/{symbol_key:path}/activate")
async def activate_symbol(
    symbol_key: str,
    user: Annotated[TokenData, Depends(require_admin)],
) -> dict[str, str]:
    """
    Trading für ein zuvor deaktiviertes Symbol im API-Prozess wieder
    erlauben. Erfordert Admin-Rolle (Re-Aktivierung ist die riskante
    Richtung). Siehe Warnhinweis in deactivate_symbol().
    """
    sks = get_symbol_kill_switch()
    await sks.activate(symbol_key, activated_by=f"user:{user.user_id}")
    return {"activated": symbol_key}
