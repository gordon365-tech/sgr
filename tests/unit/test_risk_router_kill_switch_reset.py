"""
Tests fuer sgr.api.routers.risk.reset_kill_switch (Root-Cause-Fix,
Legacy-Position-Cleanup 2026-09-16).

Bug: eine frisch konstruierte KillSwitch-Instanz startet lokal IMMER mit
is_active=False (KillSwitchState-Feld-Default) - reset() selbst prueft
nur diesen lokalen Zustand und kehrt bei "already_inactive" fruehzeitig
zurueck, OHNE nach Redis zu publizieren. Der Endpoint konstruierte bei
JEDEM Aufruf eine neue KillSwitch-Instanz und meldete dadurch immer
reset=True, unabhaengig davon, ob ueberhaupt ein aktiver Kill Switch in
Redis existierte - live durch einen manuell nachgebauten Reset-Versuch
mit demselben Konstruktionsmuster bestaetigt (siehe Konversation).

Diese Tests rufen den Endpoint als Funktion direkt auf (kein FastAPI/
TestClient-Layer) mit einem echten, zustandsbehafteten Fake-Redis (Dict-
Backing statt AsyncMock mit hart auf None fixiertem .get()) - die
bestehende tests/integration/test_api.py::TestRiskEndpoints-Fixture kann
einen echten get()/set()-Round-Trip prinzipiell nicht abbilden.
"""

from __future__ import annotations

import json

from sgr.api.dependencies import TokenData
from sgr.api.routers.risk import reset_kill_switch
from sgr.core.types import TradingMode
from sgr.risk.kill_switch import _redis_key


class StatefulFakeRedis:
    """Minimaler Dict-backed Fake-Redis: get() liefert tatsaechlich
    zurueck, was set() zuvor geschrieben hat - im Unterschied zum
    AsyncMock-Fake in test_api.py, dessen .get() hart auf None fixiert
    ist und daher keinen echten Round-Trip abbilden kann."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    async def publish(self, channel: str, message: str) -> None:
        pass


def _admin_token(user_id: str = "test-admin") -> TokenData:
    return TokenData(user_id=user_id, trading_mode=TradingMode.PAPER, is_admin=True)


class TestResetKillSwitchNeverTriggered:
    async def test_reset_is_a_true_noop_when_never_triggered(self) -> None:
        redis = StatefulFakeRedis()
        result = await reset_kill_switch(
            trading_mode=TradingMode.PAPER,
            redis_client=redis,  # type: ignore[arg-type]
            user=_admin_token(),
        )
        assert result == {
            "reset": False,
            "reset_by": "test-admin",
            "detail": "Kill switch was not active",
        }
        # Und es wurde tatsaechlich nichts nach Redis geschrieben.
        assert redis._store == {}


class TestResetKillSwitchActuallyActive:
    async def test_reset_actually_clears_a_real_active_state(self) -> None:
        redis = StatefulFakeRedis()
        key = _redis_key(TradingMode.PAPER, "test-admin")
        await redis.set(
            key,
            json.dumps({"is_active": True, "reason": "Open positions 10 exceeds max 10"}),
        )

        result = await reset_kill_switch(
            trading_mode=TradingMode.PAPER,
            redis_client=redis,  # type: ignore[arg-type]
            user=_admin_token(),
        )

        assert result == {"reset": True, "reset_by": "test-admin"}
        stored = json.loads(redis._store[key])
        assert stored["is_active"] is False

    async def test_reset_does_not_affect_a_different_tenants_key(self) -> None:
        redis = StatefulFakeRedis()
        own_key = _redis_key(TradingMode.PAPER, "test-admin")
        other_key = _redis_key(TradingMode.PAPER, "other-tenant")
        await redis.set(own_key, json.dumps({"is_active": True, "reason": "breach"}))
        await redis.set(other_key, json.dumps({"is_active": True, "reason": "other breach"}))

        await reset_kill_switch(
            trading_mode=TradingMode.PAPER,
            redis_client=redis,  # type: ignore[arg-type]
            user=_admin_token(),
        )

        assert json.loads(redis._store[own_key])["is_active"] is False
        assert json.loads(redis._store[other_key])["is_active"] is True
