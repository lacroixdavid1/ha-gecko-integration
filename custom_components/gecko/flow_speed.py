"""Pump speeds: the range the zone parser drops, and what the spa reports.

gecko-iot-client 1.0.3 (`zone_parser._extract_value_from_config`) replaces any
configuration value that is a dict holding a `minimum` with that minimum, then
builds the zone. A two-speed pump's
``{"minimum": 50, "maximum": 100, "stepIncrement": 50}`` reaches its FlowZone
as ``50``: ``speed_config`` is None, the zone has no presets, and the fan
offered on/off only. The client keeps the raw configuration, so the range can
be put back, and the library's own speed_config, capabilities, presets and
_validate_speed then work as written.
"""

from __future__ import annotations

from typing import Any

from gecko_iot_client.models.flow_zone import FlowZoneCapabilities
from gecko_iot_client.models.zone_types import ZoneType

_RANGE_KEYS = ("minimum", "maximum", "stepIncrement")


def _speed_range(value: Any) -> dict[str, int] | None:
    """A configuration speed range, or None when the value is not one."""
    if not isinstance(value, dict):
        return None
    if not all(type(value.get(key)) is int for key in _RANGE_KEYS):
        return None
    return {key: value[key] for key in _RANGE_KEYS}


def restore_flow_speed_ranges(gecko_client: Any, zones: Any) -> None:
    """Give each flow zone back the speed range its configuration declares.

    Reads the client's raw configuration (``_configuration``: private in
    gecko-iot-client 1.0.3, pinned by a test). A missing configuration or
    zone, or a malformed range, leaves the zone as the parser built it.
    Idempotent: it runs on every zone update, for every client.
    """
    configuration = getattr(gecko_client, "_configuration", None)
    if not isinstance(configuration, dict) or not isinstance(zones, dict):
        return
    zones_config = configuration.get("zones")
    flow = zones_config.get("flow") if isinstance(zones_config, dict) else None
    if not isinstance(flow, dict):
        return
    for zone in zones.get(ZoneType.FLOW_ZONE) or []:
        raw = flow.get(str(zone.id))
        speed = _speed_range(raw.get("speed")) if isinstance(raw, dict) else None
        config = getattr(zone, "config", None)
        if speed is not None and isinstance(config, dict):
            config["speed"] = speed


def preset_speeds(zone: Any) -> list[int]:
    """The zone's speeds, slowest first; empty for a one-speed zone."""
    if FlowZoneCapabilities.SUPPORTS_SPEED_PRESETS not in zone.capabilities:
        return []
    return [int(preset.speed) for preset in zone.presets]


def preset_for_reported(speeds: list[int], reported: Any) -> int | None:
    """The preset a reported speed means.

    The spa does not report the value it was sent: a two-speed pump reads 99
    at low and 100 at high (captured 2026-10-09). An exact preset is that
    preset; with two presets, anything below the top is the low one; with
    more (not seen on any spa yet), the nearest.
    """
    if not speeds or type(reported) not in (int, float):
        return None
    if reported in speeds:
        return int(reported)
    if len(speeds) == 2:
        return speeds[0] if reported < speeds[-1] else speeds[-1]
    return min(speeds, key=lambda speed: abs(speed - reported))
