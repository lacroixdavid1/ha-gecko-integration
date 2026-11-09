"""Give gecko-iot-client's MQTT connection the keep-alive it asks for.

gecko-iot-client 1.0.3 builds its MQTT5 client with ``keep_alive_secs=30``,
but awsiot's ``mqtt5_client_builder`` reads ``keep_alive_interval_sec``. The
unknown keyword is dropped without a word and every connection runs on the
builder's 1200 s default, so a dead link goes unnoticed for twenty minutes or
more while the integration keeps serving cached state
(geckoal/gecko-iot-client#15).

The library's client module gets its own wrapper of the builder: nothing else
that uses awsiot is touched. Once the library passes the right keyword, the
wrapper leaves the call alone.
"""

from __future__ import annotations

import functools
import logging
import types
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Above the ping timeout, as the builder documents, and within the 30-1200 s
# range AWS IoT accepts. A dead link is noticed within about 70 s.
KEEP_ALIVE_INTERVAL_SEC = 60
PING_TIMEOUT_MS = 10_000

_WRAPPED = "_gecko_keep_alive_fixed"


def fix_keep_alive_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return builder kwargs with the keep-alive under the name awsiot reads."""
    if "keep_alive_secs" not in kwargs or "keep_alive_interval_sec" in kwargs:
        return kwargs
    fixed = dict(kwargs)
    del fixed["keep_alive_secs"]
    fixed["keep_alive_interval_sec"] = KEEP_ALIVE_INTERVAL_SEC
    fixed.setdefault("ping_timeout_ms", PING_TIMEOUT_MS)
    return fixed


def install(client_module: Any) -> bool:
    """Wrap the builder used by ``client_module``. Return True if installed."""
    builder = getattr(client_module, "mqtt5_client_builder", None)
    build = getattr(builder, "direct_with_custom_authorizer", None)
    if build is None:
        _LOGGER.warning(
            "gecko_iot_client no longer exposes mqtt5_client_builder; "
            "the keep-alive fix is not installed"
        )
        return False
    if getattr(build, _WRAPPED, False):
        return True

    @functools.wraps(build)
    def direct_with_custom_authorizer(*args: Any, **kwargs: Any) -> Any:
        return build(*args, **fix_keep_alive_kwargs(kwargs))

    setattr(direct_with_custom_authorizer, _WRAPPED, True)
    shim = types.SimpleNamespace(**vars(builder))
    shim.direct_with_custom_authorizer = direct_with_custom_authorizer
    client_module.mqtt5_client_builder = shim
    return True
