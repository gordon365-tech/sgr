"""
Alert Manager für Telegram/Slack Notifications
===============================================
"""

from __future__ import annotations

from enum import Enum

import httpx

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


async def send_telegram_alert(
    message: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> bool:
    """Sends alert via Telegram."""
    config = get_config()

    if not config.monitoring.telegram_bot_token or not config.monitoring.telegram_chat_id:
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.telegram.org/bot{config.monitoring.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": config.monitoring.telegram_chat_id,
                    "text": f"🔔 [{severity.upper()}] {message}",
                    "parse_mode": "HTML",
                },
                timeout=10,
            )

        if response.status_code == 200:
            log.info("monitoring.telegram_alert_sent", severity=severity)
            return True
    except Exception as e:
        log.error("monitoring.telegram_alert_failed", error=str(e))

    return False


async def alert_high_drawdown(current_drawdown: float, threshold: float) -> None:
    """Alert if drawdown exceeds threshold."""
    if current_drawdown > threshold:
        await send_telegram_alert(
            f"⚠️ Portfolio drawdown: {current_drawdown:.1f}% (threshold: {threshold:.1f}%)",
            AlertSeverity.CRITICAL,
        )


async def alert_api_error_rate(error_rate: float, threshold: float = 0.05) -> None:
    """Alert if API error rate exceeds threshold."""
    if error_rate > threshold:
        await send_telegram_alert(
            f"⚠️ High API error rate: {error_rate:.1%}",
            AlertSeverity.WARNING,
        )


async def alert_strategy_degradation(
    strategy_name: str,
    win_rate: float,
    threshold: float = 0.4,
) -> None:
    """Alert if strategy win rate drops below threshold."""
    if win_rate < threshold:
        await send_telegram_alert(
            f"⚠️ Strategy '{strategy_name}' degradation: {win_rate:.1%} win rate",
            AlertSeverity.WARNING,
        )


async def alert_critical_event(title: str, details: str) -> None:
    """Send critical alert."""
    await send_telegram_alert(
        f"🚨 CRITICAL: {title}\n{details}",
        AlertSeverity.CRITICAL,
    )
