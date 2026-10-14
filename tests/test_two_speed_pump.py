"""Jet 1 has two speeds; the integration offered on/off only.

gecko-iot-client 1.0.3 flattens a configuration speed range to its minimum
before it builds the zone (see flow_speed.py). These tests run the real parser
on an in.touch 3's real configuration (zones only, captured 2026-10-09) and
hold the integration to it.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import HomeAssistantError

from gecko_iot_client import GeckoIotClient
from gecko_iot_client.models.zone_parser import ZoneConfigurationParser
from gecko_iot_client.models.zone_types import ZoneType

from custom_components.gecko.const import DOMAIN
from custom_components.gecko.coordinator import GeckoVesselCoordinator
from custom_components.gecko import fan as fan_module
from custom_components.gecko.fan import GeckoFan
from custom_components.gecko.flow_speed import preset_for_reported, restore_flow_speed_ranges

from .conftest import MONITOR_ID, build_zones, record_publishes

# The zones of an in.touch 3's configuration (config/get/accepted), as the spa
# sent them on 2026-10-09. Identifiers left out.
SPA_ZONES = {
    "flow": {
        "1": {"pumps": ["1"], "speed": {"maximum": 100, "minimum": 50, "stepIncrement": 50}},
        "2": {"pumps": ["2"], "speed": {"maximum": 100, "minimum": 100, "stepIncrement": 0}},
    },
    "lighting": {"1": {"lights": ["1"]}},
    "temperatureControl": {"1": {"minTemperatureSetPointC": 15, "maxTemperatureSetPointC": 40}},
}
SPA_CONFIGURATION = {
    "accessories": {"pumps": {"1": {"type": 1}, "2": {"type": 0}}},
    "zones": SPA_ZONES,
}
ON_OFF = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF


class FakeApi:
    """Answers the token refresh the way the Gecko API does: a fresh broker URL."""

    async def async_get_monitor_livestream(self, monitor_id):
        return {"brokerUrl": "wss://fresh"}


def spa_zones(reported=None):
    return build_zones(reported, config=SPA_ZONES)


async def _setup(hass, network, reported=None, configuration=SPA_CONFIGURATION):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-1", data={})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(api_client=FakeApi(), coordinators=[])
    coordinator = GeckoVesselCoordinator(hass, entry.entry_id, "vessel-1", MONITOR_ID, "Spa")
    network.configuration = copy.deepcopy(configuration)
    network.next_zones_on_connect = spa_zones(reported)
    assert await coordinator.async_setup_monitor_connection("wss://first")
    await hass.async_block_till_done()
    return coordinator, entry


def _zone(coordinator, zone_id):
    return next(z for z in coordinator.get_zones_by_type(ZoneType.FLOW_ZONE) if z.id == zone_id)


def _refresh(fan):
    with patch.object(fan, "async_write_ha_state"):
        fan._handle_coordinator_update()


def _pump(coordinator, entry, zone_id):
    fan = GeckoFan(coordinator, entry, _zone(coordinator, zone_id))
    _refresh(fan)
    return fan


def _report(coordinator, flow):
    """Apply a reported flow state to the coordinator's zones, as the client does."""
    ZoneConfigurationParser().apply_state_to_zones(
        coordinator.get_all_zones(), {"state": {"reported": {"zones": {"flow": flow}}}}
    )


def test_the_parser_loses_the_range():
    """The control: without the restore, pump 1 has no presets."""
    zones = spa_zones()
    pump_1 = next(z for z in zones[ZoneType.FLOW_ZONE] if z.id == "1")
    assert pump_1.speed_config is None and pump_1.presets == []


def test_restore_gives_pump_1_its_two_presets():
    zones = spa_zones()
    restore_flow_speed_ranges(SimpleNamespace(_configuration=SPA_CONFIGURATION), zones)
    pump_1, pump_2 = (next(z for z in zones[ZoneType.FLOW_ZONE] if z.id == i) for i in ("1", "2"))
    assert [p.speed for p in pump_1.presets] == [50, 100]
    assert pump_2.presets == []
    assert pump_2.speed_config == {"minimum": 100, "maximum": 100, "stepIncrement": 0}


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        {},
        {"zones": None},
        {"zones": {"flow": {"1": {"speed": 50}}}},
        {"zones": {"flow": {"1": {"speed": {"minimum": 50, "maximum": 100}}}}},
        {"zones": {"flow": {"1": {"speed": {"minimum": "50", "maximum": 100, "stepIncrement": 50}}}}},
    ],
)
def test_restore_leaves_a_zone_alone_without_a_valid_range(configuration):
    zones = spa_zones()
    restore_flow_speed_ranges(SimpleNamespace(_configuration=configuration), zones)
    pump_1 = next(z for z in zones[ZoneType.FLOW_ZONE] if z.id == "1")
    assert pump_1.speed_config is None


def test_real_client_keeps_its_raw_configuration():
    """flow_speed reads GeckoIotClient._configuration; a rename must fail here, not in a spa."""
    # The constructor registers callbacks on the transporter; any will do.
    client = GeckoIotClient("monitor", MagicMock())
    assert "_configuration" in vars(client)


@pytest.mark.parametrize(
    ("reported", "preset"),
    [(99, 50), (100, 100), (50, 50), (100.0, 100), (120, 100), (None, None), ("100", None), (True, None)],
)
def test_preset_for_reported_on_a_two_speed_pump(reported, preset):
    assert preset_for_reported([50, 100], reported) == preset


def test_preset_for_reported_without_presets():
    assert preset_for_reported([], 100) is None


async def test_pump_1_offers_two_speeds(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    assert fan.supported_features & FanEntityFeature.SET_SPEED
    assert fan.speed_count == 2
    assert fan.percentage_step == 50


async def test_pump_2_stays_on_off(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"2": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "2")
    assert fan.supported_features == ON_OFF


@pytest.mark.parametrize(
    ("flow", "percentage"),
    [
        ({"active": True, "speed": 99}, 50),  # low, as the spa reports it
        ({"active": True, "speed": 100}, 100),
        ({"active": True, "speed": 50}, 50),
        ({"active": False, "speed": 100}, 0),
    ],
)
async def test_pump_1_reads_the_spa_readback(hass, network, flow, percentage):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    _report(coordinator, {"1": flow})
    _refresh(fan)
    assert fan.percentage == percentage
    assert fan.is_on is flow["active"]


@pytest.mark.parametrize(
    ("call", "published"),
    [
        (lambda fan: fan.async_set_percentage(100), {"speed": 100, "active": True}),
        (lambda fan: fan.async_set_percentage(50), {"speed": 50, "active": True}),
        (lambda fan: fan.async_set_percentage(30), {"speed": 50, "active": True}),
        (lambda fan: fan.async_turn_on(percentage=75), {"speed": 100, "active": True}),
        (lambda fan: fan.async_turn_on(), {"speed": 50, "active": True}),
        (lambda fan: fan.async_set_percentage(0), {"active": False}),
    ],
)
async def test_pump_1_commands(hass, network, call, published):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": True, "speed": 99}}})
    fan = _pump(coordinator, entry, "1")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await call(fan)
    assert sink and sink[-1][3] == published


async def test_pump_2_turn_on_sends_its_maximum(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"2": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "2")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await fan.async_turn_on()
    assert sink[-1][3] == {"speed": 100, "active": True}


async def test_without_a_configuration_pumps_stay_as_before(hass, network):
    """No raw configuration (a library change): on/off, and turn-on sends the old 1."""
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False}}}, configuration=None)
    fan = _pump(coordinator, entry, "1")
    assert fan.supported_features == ON_OFF
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await fan.async_turn_on()
    assert sink[-1][3] == {"speed": 1, "active": True}


async def test_speeds_survive_a_reconnect(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    network.clients[-1].emit_connectivity(False)
    network.next_zones_on_connect = spa_zones({"flow": {"1": {"active": True, "speed": 100}}})
    await coordinator._simple_reconnect()
    await hass.async_block_till_done()
    assert len(network.clients) == 2, "the reconnect did not build a new client"
    _refresh(fan)
    assert fan.supported_features & FanEntityFeature.SET_SPEED
    assert fan.percentage == 100
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await fan.async_set_percentage(50)
    assert sink[-1][3] == {"speed": 50, "active": True}
    assert sink[-1][0] is _zone(coordinator, "1"), "the command went to a replaced zone"


# The spa acts on its shadow delta (desired against reported), and ignores a
# pump-1 start whose delta carries no speed. A pump that stopped at high still
# reports 100, so "on at 100" leaves only `active` in the delta: nothing
# happens (2026-10-12 17:04:36 and 17:05:35). Starting low, then going high
# once the spa reports the pump running, works.


async def _yield(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def test_high_from_off_parked_at_high_starts_low_then_waits_for_the_spa(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)

    task = asyncio.ensure_future(fan.async_set_percentage(100))
    await _yield()
    assert [p[3] for p in sink] == [{"speed": 50, "active": True}], "start low first"

    # A coordinator poll before the echo sees set_speed's optimistic write.
    coordinator.async_update_listeners()
    await _yield()
    # The echo of our own publish: reported is still off at 100.
    _report(coordinator, {"1": {"active": False, "speed": 100}})
    coordinator.async_update_listeners()
    await _yield()
    assert len(sink) == 1, "high went out before the spa reported the pump running"

    # The spa's own report: running, low.
    _report(coordinator, {"1": {"active": True, "speed": 99}})
    coordinator.async_update_listeners()
    await asyncio.wait_for(task, 1)
    assert [p[3] for p in sink] == [{"speed": 50, "active": True}, {"speed": 100, "active": True}]


async def test_high_from_off_parked_at_low_is_one_publish(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 99}}})
    fan = _pump(coordinator, entry, "1")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await asyncio.wait_for(fan.async_set_percentage(100), 1)
    assert [p[3] for p in sink] == [{"speed": 100, "active": True}]


async def test_low_from_off_parked_at_high_is_one_publish(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    await asyncio.wait_for(fan.async_set_percentage(50), 1)
    assert [p[3] for p in sink] == [{"speed": 50, "active": True}]


async def test_high_start_that_never_runs_is_refused(hass, network, monkeypatch):
    monkeypatch.setattr(fan_module, "START_TIMEOUT", 0.05)
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 100}}})
    fan = _pump(coordinator, entry, "1")
    sink: list = []
    record_publishes(coordinator.get_all_zones(), sink)
    with pytest.raises(HomeAssistantError):
        await asyncio.wait_for(fan.async_set_percentage(100), 1)
    assert [p[3] for p in sink] == [{"speed": 50, "active": True}], "no high command without a report"


# A command that cannot go out must fail the service call, so the wall shows
# its refusal at once instead of waiting 45 s for a state that never comes.


async def test_turn_on_without_a_client_is_refused(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": False, "speed": 99}}})
    fan = _pump(coordinator, entry, "1")
    with patch.object(coordinator, "get_gecko_client", return_value=None):
        with pytest.raises(HomeAssistantError):
            await fan.async_set_percentage(100)


async def test_turn_off_of_a_missing_zone_is_refused(hass, network):
    coordinator, entry = await _setup(hass, network, {"flow": {"1": {"active": True, "speed": 99}}})
    fan = _pump(coordinator, entry, "1")
    with patch.object(fan, "_current_zone", return_value=None):
        with pytest.raises(HomeAssistantError):
            await fan.async_turn_off()
