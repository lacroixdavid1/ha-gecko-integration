"""Support for Gecko fan entities (pumps with speed control)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .entity import GeckoEntityAvailabilityMixin, GeckoZoneEntityMixin
from .flow_speed import preset_for_reported, preset_speeds
from . import GeckoConfigEntry

from gecko_iot_client.models.zone_types import ZoneType, FlowZoneType
from gecko_iot_client.models.flow_zone import FlowZone

_LOGGER = logging.getLogger(__name__)

# Sent by turn_on to a zone whose speed range is unknown: the value this
# integration always sent, which an in.touch 3 runs at its low speed.
LEGACY_SPEED = 1

# How long a pump started low on its way to high gets to report that it runs
# (see _start_low_first). Under the 45 s the wall and the brassage wait.
START_TIMEOUT = 15

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GeckoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko fan entities from a config entry."""
    runtime_data = config_entry.runtime_data
    if not runtime_data or not runtime_data.coordinators:
        _LOGGER.error("No coordinators found in runtime_data for config entry %s", config_entry.entry_id)
        return
    created_entity_ids = set()
    def create_discovery_callback(coordinator: GeckoVesselCoordinator):
        def discover_new_fan_entities():
            new_entities = []
            vessel_coordinator: GeckoVesselCoordinator = coordinator
            pump_zones = vessel_coordinator.get_zones_by_type(ZoneType.FLOW_ZONE)
            flow_zones = [zone for zone in pump_zones if isinstance(zone, FlowZone)]
            for zone in flow_zones:
                entity_id = f"{vessel_coordinator.vessel_name}_pump_{zone.id}".lower()
                if entity_id not in created_entity_ids:
                    entity = GeckoFan(vessel_coordinator, config_entry, zone)
                    new_entities.append(entity)
                    created_entity_ids.add(entity_id)
                    _LOGGER.debug("Created fan entity for vessel %s, zone %s", vessel_coordinator.vessel_name, zone.id)
            if new_entities:
                async_add_entities(new_entities)
        return discover_new_fan_entities
    for coordinator in runtime_data.coordinators:
        discovery_callback = create_discovery_callback(coordinator)
        discovery_callback()
        coordinator.register_zone_update_callback(discovery_callback)

class GeckoFan(GeckoZoneEntityMixin, GeckoEntityAvailabilityMixin, CoordinatorEntity, FanEntity):
    """Representation of a Gecko pump fan (multi-speed or variable speed)."""

    _attr_has_entity_name = True
    _zone_type = ZoneType.FLOW_ZONE
    coordinator: GeckoVesselCoordinator
    
    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: GeckoConfigEntry,
        zone: FlowZone,  # FlowZone from coordinator
    ) -> None:
        """Initialize the Pump Fan."""
        FanEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._coordinator: GeckoVesselCoordinator = coordinator
        self._zone = zone
        self._attr_name = zone.name
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_pump_{zone.id}"
        )

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        # Both recomputed from the current zone by _update_from_zone().
        self._attr_supported_features = FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON
        self._attr_speed_count = 1

        # Set icon based on zone type
        self._attr_icon = self._get_icon_for_zone_type()
        
        # Initialize state and availability from zone (will be set by async_added_to_hass event registration)
        self._attr_available = False
        self._update_from_zone()
 
        
    def _get_icon_for_zone_type(self) -> str:
        """Return icon based on flow zone type."""
        zone_type = self._zone.type
        if zone_type == FlowZoneType.WATERFALL_ZONE:
            return "mdi:waterfall"
        elif zone_type == FlowZoneType.BLOWER_ZONE:
            return "mdi:wind-power"
        else:  # FLOW_ZONE (pump)
            return "mdi:pump"
    
    async def async_added_to_hass(self) -> None:
        """Register update callback when entity is added to hass."""
        await super().async_added_to_hass()
        self.coordinator.async_add_listener(self._handle_coordinator_update)
        
  
    def _update_from_zone(self) -> None:
        """Update state attributes from zone data."""
        zone = self._current_zone()
        if zone is None:
            # Not on the current client: keep the last known state.
            return
        # Recomputed from the current zone: a reconnect builds new zones, and
        # the speed range is restored on each of them (flow_speed.py).
        speeds = preset_speeds(zone)
        features = FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON
        if speeds:
            features |= FanEntityFeature.SET_SPEED
        self._attr_supported_features = features
        self._attr_speed_count = max(len(speeds), 1)
        self._attr_is_on = bool(zone.active)
        if not speeds:
            self._attr_percentage = None
        elif not zone.active:
            self._attr_percentage = 0
        else:
            preset = preset_for_reported(speeds, zone.speed)
            self._attr_percentage = (
                None if preset is None else ordered_list_item_to_percentage(speeds, preset)
            )

        # geckoal/ha-gecko-integration#32: why the pump runs (UD user demand,
        # FI filtration, HT heating, PU purge, CF check-flow, ...). Rebuilt on
        # every update so a stopped pump clears its list.
        self._attr_extra_state_attributes = {
            "initiators": [getattr(i, "value", i) for i in (zone.initiators or [])],
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_zone()
        _LOGGER.debug(
            "Updating fan %s: is_on=%s, percentage=%s",
            self._attr_name, self._attr_is_on, self._attr_percentage,
        )
        self.async_write_ha_state()

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        """Turn the pump on, at the speed `percentage` names (the lowest without one)."""
        if percentage == 0:
            await self.async_turn_off()
            return
        zone = await self._zone_to_command()
        speeds = preset_speeds(zone)
        if speeds:
            speed = speeds[0] if percentage is None else percentage_to_ordered_list_item(speeds, percentage)
            if not zone.active and speed != speeds[0] and zone.speed == speed:
                zone = await self._start_low_first(zone, speeds[0])
        elif zone.speed_config:
            # One speed: the range's own value; the restored range rejects anything else.
            speed = zone.speed_config["maximum"]
        else:
            speed = LEGACY_SPEED
        _LOGGER.debug("Turning on pump %s at speed %s", self._attr_name, speed)
        zone.set_speed(speed)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed; 0 turns the pump off."""
        await self.async_turn_on(percentage=percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the pump off. The spa refuses while it filters or heats (RuntimeError)."""
        zone = self._current_zone()
        if zone is None:
            raise HomeAssistantError(f"{self._attr_name}: pump zone {self._zone.id} not found")
        zone.deactivate()

    async def _start_low_first(self, zone: FlowZone, low: int) -> FlowZone:
        """Start a pump low, and return once the spa reports it running.

        The spa acts on its shadow delta, desired against reported. A pump that
        stopped at high still reports that speed, so "on at high" leaves only
        `active` in the delta, and an in.touch 3 ignores a start that carries
        no speed (measured 2026-10-12, twice). A low start always carries one
        (low never reads back as the value sent); the high command then finds
        the spa reporting low, so it carries one too. Publishing both at once
        fails: the spa started low, then stopped by itself.
        """
        started = asyncio.Event()

        @callback
        def _reported_running() -> None:
            current = self._current_zone()
            # set_speed() writes active and speed into the zone before the spa
            # answers, and a coordinator poll can read that write. The spa
            # never reports the low value it was sent (it reads back 99), so
            # running at any other speed is the spa's own report.
            if current is not None and current.active and current.speed != low:
                # The speed the spa reported with `active`: 99 so far. A 100
                # here would mean the high command finds no delta to carry.
                _LOGGER.debug("Pump %s reported running at speed %s", self._attr_name, current.speed)
                started.set()

        remove = self.coordinator.async_add_listener(_reported_running)
        try:
            _LOGGER.debug("Starting pump %s low before high", self._attr_name)
            zone.set_speed(low)
            try:
                async with asyncio.timeout(START_TIMEOUT):
                    await started.wait()
            except TimeoutError as err:
                raise HomeAssistantError(
                    f"{self._attr_name} did not report running within {START_TIMEOUT} s"
                    " (it may still start at low speed)"
                ) from err
        finally:
            remove()
        return self._current_zone() or zone

    async def _zone_to_command(self) -> FlowZone:
        """The zone on the current client; refuse the command when there is none.

        A silent return let the caller's service call succeed, and the wall
        then waited 45 s for a state that never came before showing a refusal.
        """
        if not await self._coordinator.get_gecko_client():
            raise HomeAssistantError(f"{self._attr_name}: no Gecko client connected")
        zone = self._current_zone()
        if zone is None:
            raise HomeAssistantError(f"{self._attr_name}: pump zone {self._zone.id} not found")
        return zone

    @property
    def is_on(self) -> bool | None:
        """Return true if the entity is on."""
        return self._attr_is_on
