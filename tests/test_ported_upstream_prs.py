"""Upstream PRs carried in this fork, held to the reconnect rule.

Each feature reads its zone through the coordinator's current client, so it
keeps working after a reconnect replaces the client (see
test_zone_rebinding.py). One test per ported PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from gecko_iot_client.models.zone_types import ZoneType

from custom_components.gecko.const import DOMAIN
from custom_components.gecko.coordinator import GeckoVesselCoordinator
from custom_components.gecko.fan import GeckoFan

from .conftest import MONITOR_ID, build_zones

ROOT = Path(__file__).parents[1]


class FakeApi:
    async def async_get_monitor_livestream(self, monitor_id):
        return {"brokerUrl": "wss://fresh"}


async def _setup(hass, network, zones):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-1", data={})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(api_client=FakeApi(), coordinators=[])
    coordinator = GeckoVesselCoordinator(hass, entry.entry_id, "vessel-1", MONITOR_ID, "Spa")
    network.next_zones_on_connect = zones
    assert await coordinator.async_setup_monitor_connection("wss://first")
    await hass.async_block_till_done()
    return coordinator, entry


async def _reconnect(hass, network, coordinator, zones):
    network.clients[-1].emit_connectivity(False)
    network.next_zones_on_connect = zones
    await coordinator._simple_reconnect()
    await hass.async_block_till_done()


def _zone(coordinator, zone_type, zone_id):
    return next(z for z in coordinator.get_zones_by_type(zone_type) if z.id == zone_id)


def _update(entity) -> None:
    with patch.object(entity, "async_write_ha_state"):
        entity._handle_coordinator_update()


def test_pr54_manifest_depends_on_my():
    """geckoal/ha-gecko-integration#54: the OAuth callback needs the `my` integration."""
    manifest = json.loads((ROOT / "custom_components" / "gecko" / "manifest.json").read_text())
    assert "my" in manifest["dependencies"]


async def test_pr32_pump_exposes_why_it_runs(hass, network):
    """geckoal/ha-gecko-integration#32: the pump's initiators, from the current client."""
    coordinator, entry = await _setup(
        hass, network, build_zones({"flow": {"1": {"active": True, "speed": 100, "initiators_": ["UD"]}}})
    )
    fan = GeckoFan(coordinator, entry, _zone(coordinator, ZoneType.FLOW_ZONE, "1"))
    assert fan.extra_state_attributes["initiators"] == ["UD"]

    await _reconnect(
        hass, network, coordinator, build_zones({"flow": {"1": {"active": True, "speed": 30, "initiators_": ["FI"]}}})
    )
    _update(fan)
    assert fan.extra_state_attributes["initiators"] == ["FI"]


async def test_pr32_initiators_clear_when_the_pump_stops(hass, network):
    coordinator, entry = await _setup(
        hass, network, build_zones({"flow": {"1": {"active": True, "speed": 100, "initiators_": ["UD"]}}})
    )
    fan = GeckoFan(coordinator, entry, _zone(coordinator, ZoneType.FLOW_ZONE, "1"))

    await _reconnect(
        hass, network, coordinator, build_zones({"flow": {"1": {"active": False, "speed": 100, "initiators_": []}}})
    )
    _update(fan)
    assert fan.extra_state_attributes["initiators"] == []


async def _climate(hass, network, temperature_state):
    from custom_components.gecko.climate import GeckoClimate

    coordinator, entry = await _setup(
        hass, network, build_zones({"temperatureControl": {"1": {"temperature_": 38, "setPoint": 38, "status_": 0}}})
    )
    climate = GeckoClimate(coordinator, _zone(coordinator, ZoneType.TEMPERATURE_CONTROL_ZONE, "1"))
    await _reconnect(
        hass, network, coordinator,
        build_zones({"temperatureControl": {"1": {"temperature_": 38, "setPoint": 38, **temperature_state}}}),
    )
    _update(climate)
    return climate


async def test_pr60_cooling_and_defrost_are_not_idle(hass, network):
    """geckoal/ha-gecko-integration#60: every status maps to its HVAC action."""
    cooling = await _climate(hass, network, {"status_": 2})
    assert cooling.hvac_action == "cooling"
    assert cooling.extra_state_attributes["heat_source"] == "none"


async def test_pr60_heat_pump_defrost(hass, network):
    climate = await _climate(hass, network, {"status_": 7})
    assert climate.hvac_action == "defrosting"
    assert climate.extra_state_attributes["heat_source"] == "heat_pump"


async def test_pr60_heat_pump_error_is_surfaced(hass, network):
    climate = await _climate(hass, network, {"status_": 8})
    assert climate.hvac_action == "idle"
    assert climate.extra_state_attributes["heat_pump_error"] is True


async def test_pr60_electric_heating(hass, network):
    climate = await _climate(hass, network, {"status_": 1})
    assert climate.hvac_action == "heating"
    assert climate.extra_state_attributes["heat_source"] == "electric"
    assert climate.extra_state_attributes["heat_pump_error"] is False



async def test_pr56_eco_mode_from_the_current_client(hass, network):
    """geckoal/ha-gecko-integration#56: eco mode, next to #60's attributes."""
    climate = await _climate(hass, network, {"status_": 0, "mode_": {"eco": True}})
    assert climate.extra_state_attributes["eco_mode"] is True
    assert set(climate.extra_state_attributes) == {"heat_source", "heat_pump_error", "eco_mode"}


async def _light(hass, network, light_state):
    from custom_components.gecko.light import GeckoLight

    coordinator, entry = await _setup(hass, network, build_zones({"lighting": {"1": light_state}}))
    return coordinator, GeckoLight(coordinator, entry, _zone(coordinator, ZoneType.LIGHTING_ZONE, "1"))


async def test_pr57_on_off_light_stays_on_off(hass, network):
    """geckoal/ha-gecko-integration#57 offered colour to every light: every
    LightingZone has an rgbi attribute, None when the light reports no colour.
    A light that reports no colour must stay ON/OFF, with no effects."""
    from homeassistant.components.light import ColorMode, LightEntityFeature

    _, light = await _light(hass, network, {"active": False})
    assert light.supported_color_modes == {ColorMode.ONOFF}
    assert not light.supported_features & LightEntityFeature.EFFECT


async def test_pr57_colour_light_offers_rgb(hass, network):
    from homeassistant.components.light import ColorMode

    _, light = await _light(hass, network, {"active": True, "rgbi": [255, 0, 0, 100]})
    assert light.supported_color_modes == {ColorMode.RGB}


async def test_pr57_colour_follows_the_current_client(hass, network):
    coordinator, light = await _light(hass, network, {"active": True, "rgbi": [255, 0, 0, 100]})
    await _reconnect(hass, network, coordinator, build_zones({"lighting": {"1": {"active": True, "rgbi": [0, 0, 255, 100]}}}))
    _update(light)
    assert light.rgb_color is not None and light.rgb_color[2] > light.rgb_color[0]
