"""
Tests für die Redis-backed Cross-Prozess-Synchronisation des KillSwitch
(sgr/risk/kill_switch.py).

Hintergrund: seit der API/Worker-Trennung (sgr-api besitzt keinen eigenen
Trading Lifecycle mehr) muss der Kill-Switch-State zwischen Prozessen
sichtbar sein - der In-Memory-Kill-Switch im Worker bleibt dabei die
alleinige, synchrone Quelle der Wahrheit fuer den Hot-Path-Check
(is_active, O(1), kein Await); Redis ist rein additiv fuer Cross-Prozess-
Sichtbarkeit (SET fuer Reads durch z.B. sgr-api, Pub/Sub fuer aktive
Uebernahme durch andere Worker-Instanzen).

Bereits bestehende Tests fuer KillSwitch selbst (Trigger/Reset/Idempotenz
ohne Redis) liegen in tests/unit/test_risk_engine.py::TestKillSwitch -
diese Datei deckt AUSSCHLIESSLICH das neue Redis-Verhalten ab und
verifiziert insbesondere, dass ohne injizierten Redis-Client (der
Default) das bisherige Verhalten unveraendert bleibt (Regressionsschutz
fuer die 127 bereits bestehenden Call-Sites/Tests, die KillSwitch ohne
Redis konstruieren).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from sgr.core.types import TradingMode
from sgr.risk.kill_switch import (
    KillSwitch,
    _kill_switches,
    get_kill_switch,
    read_kill_switch_state_from_redis,
)


@pytest.fixture(autouse=True)
def _clean_kill_switch_singletons():
    """Singleton-Dict zwischen Tests zuruecksetzen - TestKillSwitchTenantScoping
    prueft get_kill_switch()-Identitaet, das darf nicht von vorherigen
    Tests (in dieser oder anderen Dateien) beeinflusst werden."""
    _kill_switches.clear()
    yield
    _kill_switches.clear()


@pytest.fixture
def fake_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    pubsub = AsyncMock()
    pubsub.subscribe = AsyncMock()

    async def _empty_listen():
        return
        yield  # pragma: no cover - makes this an async generator

    pubsub.listen = _empty_listen
    redis.pubsub = lambda: pubsub
    return redis


class TestKillSwitchWithoutRedis:
    """Regressionsschutz: Default-Verhalten (redis_client=None) darf sich
    durch diese Aenderung nicht veraendern."""

    async def test_trigger_without_redis_does_not_raise(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        await ks.trigger("test reason")
        assert ks.is_active

    async def test_reset_without_redis_does_not_raise(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        await ks.trigger("test reason")
        await ks.reset(reset_by="test_user")
        assert not ks.is_active

    async def test_start_remote_sync_without_redis_is_noop(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        await ks.start_remote_sync()  # darf nicht raisen, kein Task gestartet
        assert ks._subscriber_task is None


class TestKillSwitchRemoteSyncLifecycle:
    async def test_start_remote_sync_starts_background_task(self, fake_redis: AsyncMock) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)

        await ks.start_remote_sync()
        await asyncio.sleep(0)  # Event Loop einmal durchlaufen lassen, damit der Task startet

        assert ks._subscriber_task is not None
        fake_redis.pubsub().subscribe.assert_awaited_once_with("sgr:kill_switch:changes:paper")

        await ks.stop_remote_sync()
        assert ks._subscriber_task is None

    async def test_start_remote_sync_is_idempotent(self, fake_redis: AsyncMock) -> None:
        """Zweifacher Aufruf darf keinen zweiten Task starten (kein Leak)."""
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)

        await ks.start_remote_sync()
        first_task = ks._subscriber_task
        await ks.start_remote_sync()

        assert ks._subscriber_task is first_task
        await ks.stop_remote_sync()

    async def test_stop_remote_sync_without_running_task_does_not_raise(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        await ks.stop_remote_sync()  # kein Task lief - darf nicht raisen


class TestKillSwitchTriggerPublishesToRedis:
    async def test_trigger_writes_state_to_redis(self, fake_redis: AsyncMock) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)
        await ks.trigger("drawdown exceeded", triggered_by="risk_engine")

        fake_redis.set.assert_awaited_once()
        key, payload = fake_redis.set.call_args.args
        assert key == "sgr:kill_switch:state:paper"
        data = json.loads(payload)
        assert data["is_active"] is True
        assert data["reason"] == "drawdown exceeded"

    async def test_trigger_publishes_to_pubsub_channel(self, fake_redis: AsyncMock) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)
        await ks.trigger("drawdown exceeded")

        fake_redis.publish.assert_awaited_once()
        channel, payload = fake_redis.publish.call_args.args
        assert channel == "sgr:kill_switch:changes:paper"
        assert json.loads(payload)["is_active"] is True

    async def test_reset_writes_inactive_state_to_redis(self, fake_redis: AsyncMock) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)
        await ks.trigger("test")
        fake_redis.set.reset_mock()
        fake_redis.publish.reset_mock()

        await ks.reset(reset_by="gordon")

        fake_redis.set.assert_awaited_once()
        _key, payload = fake_redis.set.call_args.args
        assert json.loads(payload)["is_active"] is False

    async def test_second_trigger_when_already_active_does_not_touch_redis(
        self, fake_redis: AsyncMock
    ) -> None:
        """Idempotenz gilt auch fuer die Redis-Seite: ein bereits aktiver
        Kill Switch darf beim zweiten trigger()-Aufruf keinen zusaetzlichen
        Redis-Write/Publish ausloesen."""
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)
        await ks.trigger("first reason")
        fake_redis.set.reset_mock()
        fake_redis.publish.reset_mock()

        await ks.trigger("second reason")

        fake_redis.set.assert_not_awaited()
        fake_redis.publish.assert_not_awaited()


class TestKillSwitchRedisFailSafe:
    """Ein Redis-Fehler darf trigger()/reset() niemals fehlschlagen lassen
    - der In-Memory-State-Change (der eigentliche Schutzmechanismus) muss
    in jedem Fall bereits erfolgt sein, bevor der Redis-Publish-Versuch
    ueberhaupt beginnt."""

    async def test_trigger_still_activates_when_redis_set_fails(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
        redis.publish = AsyncMock()

        ks = KillSwitch(TradingMode.PAPER, redis_client=redis)
        await ks.trigger("test reason")  # darf NICHT raisen

        assert ks.is_active

    async def test_reset_still_deactivates_when_redis_publish_fails(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))

        ks = KillSwitch(TradingMode.PAPER, redis_client=redis)
        await ks.trigger("test")
        await ks.reset(reset_by="test_user")  # darf NICHT raisen

        assert not ks.is_active


class TestKillSwitchRemoteSyncApplication:
    """Prueft _apply_remote_state() direkt (ohne echten Pub/Sub-Loop) - das
    ist der Mechanismus, ueber den ein Worker einen von AUSSEN (z.B. der
    API) getriggerten Kill Switch uebernimmt."""

    async def test_apply_remote_active_state_activates_local_kill_switch(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        assert not ks.is_active

        await ks._apply_remote_state({"is_active": True, "reason": "triggered via API"})

        assert ks.is_active
        assert ks.state.reason == "triggered via API"

    async def test_apply_remote_inactive_state_resets_local_kill_switch(self) -> None:
        ks = KillSwitch(TradingMode.PAPER)
        await ks.trigger("local trigger")
        assert ks.is_active

        await ks._apply_remote_state({"is_active": False})

        assert not ks.is_active

    async def test_apply_remote_state_matching_current_state_is_noop(self) -> None:
        """Bereits synchron -> kein doppeltes trigger()/reset() intern."""
        ks = KillSwitch(TradingMode.PAPER)

        await ks._apply_remote_state({"is_active": False})

        assert not ks.is_active

    async def test_apply_remote_state_does_not_publish_back_to_redis(
        self, fake_redis: AsyncMock
    ) -> None:
        """WICHTIG: das Uebernehmen eines Remote-States darf NICHT erneut
        nach Redis zurueckschreiben/publizieren - sonst entstuende eine
        Echo-Schleife zwischen mehreren Worker-Instanzen."""
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis)

        await ks._apply_remote_state({"is_active": True, "reason": "from another process"})

        fake_redis.set.assert_not_awaited()
        fake_redis.publish.assert_not_awaited()


class TestReadKillSwitchStateFromRedis:
    """Der rein lesende Helper, den sgr-api nutzt (keine volle KillSwitch-
    Instanz noetig - kein Lock, kein Exchange Pool, keine Trigger-
    Faehigkeit)."""

    async def test_returns_parsed_state_when_present(self, fake_redis: AsyncMock) -> None:
        fake_redis.get = AsyncMock(
            return_value=json.dumps({"is_active": True, "reason": "test"})
        )

        result = await read_kill_switch_state_from_redis(fake_redis, TradingMode.PAPER)

        assert result == {"is_active": True, "reason": "test"}

    async def test_returns_none_when_no_state_written_yet(self, fake_redis: AsyncMock) -> None:
        fake_redis.get = AsyncMock(return_value=None)

        result = await read_kill_switch_state_from_redis(fake_redis, TradingMode.PAPER)

        assert result is None

    async def test_returns_none_on_redis_error_fail_safe(self, fake_redis: AsyncMock) -> None:
        """Fail-safe: bei einem Redis-Fehler soll der Aufrufer 'Status
        unbekannt' sehen (None), nicht einen Absturz - der Aufrufer muss
        das dann als 'unbekannt', NICHT als 'inaktiv' behandeln (liegt in
        der Verantwortung des Callers, hier nur sichergestellt dass keine
        Exception durchschlaegt)."""
        fake_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await read_kill_switch_state_from_redis(fake_redis, TradingMode.PAPER)

        assert result is None

    async def test_uses_correct_key_per_trading_mode(self, fake_redis: AsyncMock) -> None:
        """Paper und Live duerfen sich niemals denselben Redis-Key teilen -
        Cross-Contamination zwischen Trading-Modi ist explizit verboten
        (siehe KillSwitch Klassendoc: 'Niemals Cross-Contamination
        zwischen Modi')."""
        await read_kill_switch_state_from_redis(fake_redis, TradingMode.LIVE)

        fake_redis.get.assert_awaited_once_with("sgr:kill_switch:state:live")


class TestKillSwitchTenantScoping:
    """
    Tenant-Scoping (Audit nach Commit 5): Gordon und Sumo laufen beide mit
    TRADING_MODE=paper - ohne tenant_id im Redis-Key haetten sie sich
    einen einzigen globalen Kill Switch geteilt (Trigger bei Gordon haette
    Sumo mitgestoppt und umgekehrt). tenant_id=None bleibt der
    Single-Tenant-Fallback mit byte-identischem Key wie vor diesem Fix.
    """

    def test_get_kill_switch_none_tenant_returns_same_instance_as_before(self) -> None:
        """Regressionsschutz: get_kill_switch(mode) ohne tenant_id-Arg
        (Default None) muss weiterhin denselben Single-Tenant-Singleton
        wie vor diesem Fix liefern - bestehende Call-Sites uebergeben
        kein tenant_id-Argument."""
        ks1 = get_kill_switch(TradingMode.PAPER)
        ks2 = get_kill_switch(TradingMode.PAPER)
        assert ks1 is ks2

    def test_get_kill_switch_different_tenants_returns_different_instances(self) -> None:
        gordon_ks = get_kill_switch(TradingMode.PAPER, tenant_id="gordon-uuid")
        sumo_ks = get_kill_switch(TradingMode.PAPER, tenant_id="sumo-uuid")

        assert gordon_ks is not sumo_ks

    def test_get_kill_switch_same_tenant_returns_same_instance(self) -> None:
        ks1 = get_kill_switch(TradingMode.PAPER, tenant_id="gordon-uuid")
        ks2 = get_kill_switch(TradingMode.PAPER, tenant_id="gordon-uuid")
        assert ks1 is ks2

    def test_get_kill_switch_none_and_explicit_tenant_are_distinct(self) -> None:
        """tenant_id=None (Single-Tenant) und ein expliziter Tenant duerfen
        niemals dieselbe Instanz teilen, selbst im selben Prozess."""
        default_ks = get_kill_switch(TradingMode.PAPER)
        tenant_ks = get_kill_switch(TradingMode.PAPER, tenant_id="gordon-uuid")

        assert default_ks is not tenant_ks

    async def test_tenant_scoped_redis_key_includes_tenant_id(
        self, fake_redis: AsyncMock
    ) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis, tenant_id="gordon-uuid")
        await ks.trigger("drawdown exceeded")

        key, _payload = fake_redis.set.call_args.args
        assert key == "sgr:kill_switch:state:gordon-uuid:paper"

    async def test_none_tenant_redis_key_is_byte_identical_to_pre_scoping_format(
        self, fake_redis: AsyncMock
    ) -> None:
        """Kritisch fuer Abwaertskompatibilitaet: bestehende Single-Tenant-
        Deployments duerfen nach diesem Fix nicht ploetzlich unter einem
        anderen Redis-Key schreiben/lesen."""
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis, tenant_id=None)
        await ks.trigger("test")

        key, _payload = fake_redis.set.call_args.args
        assert key == "sgr:kill_switch:state:paper"

    async def test_tenant_scoped_pubsub_channel_includes_tenant_id(
        self, fake_redis: AsyncMock
    ) -> None:
        ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis, tenant_id="sumo-uuid")
        await ks.trigger("test")

        channel, _payload = fake_redis.publish.call_args.args
        assert channel == "sgr:kill_switch:changes:sumo-uuid:paper"

    async def test_two_tenants_same_trading_mode_do_not_share_redis_state(
        self, fake_redis: AsyncMock
    ) -> None:
        """Der eigentliche Kern des Audit-Fundes: Gordon triggert seinen
        Kill Switch, Sumo (separate Instanz, separater Redis-Key) bleibt
        davon vollstaendig unberuehrt."""
        gordon_ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis, tenant_id="gordon")
        sumo_ks = KillSwitch(TradingMode.PAPER, redis_client=fake_redis, tenant_id="sumo")

        await gordon_ks.trigger("gordon triggered this")

        assert gordon_ks.is_active
        assert not sumo_ks.is_active  # Sumos In-Memory-State unberuehrt

        gordon_key, _ = fake_redis.set.call_args.args
        assert gordon_key == "sgr:kill_switch:state:gordon:paper"
        assert "sumo" not in gordon_key

    async def test_read_kill_switch_state_from_redis_respects_tenant_id(
        self, fake_redis: AsyncMock
    ) -> None:
        await read_kill_switch_state_from_redis(
            fake_redis, TradingMode.PAPER, tenant_id="gordon-uuid"
        )

        fake_redis.get.assert_awaited_once_with("sgr:kill_switch:state:gordon-uuid:paper")

    async def test_read_kill_switch_state_from_redis_none_tenant_uses_legacy_key(
        self, fake_redis: AsyncMock
    ) -> None:
        await read_kill_switch_state_from_redis(fake_redis, TradingMode.PAPER, tenant_id=None)

        fake_redis.get.assert_awaited_once_with("sgr:kill_switch:state:paper")
