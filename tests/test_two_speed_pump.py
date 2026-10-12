"""Jet 1 has two speeds; the integration offered on/off only.

gecko-iot-client 1.0.3 flattens a configuration speed range to its minimum
before it builds the zone (see flow_speed.py). These tests run the real parser
on an in.touch 3's real configuration (zones only, captured 2026-10-09) and
hold the integration to it.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.fan import FanEntityFeature

from gecko_iot_client import GeckoIotClient
from gecko_iot_client.models.zone_parser import ZoneConfigurationParser
from gecko_iot_client.models.zone_types import ZoneType

from custom_components.gecko.const import DOMAIN
from custom_components.gecko.coordinator import GeckoVesselCoordinator
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
