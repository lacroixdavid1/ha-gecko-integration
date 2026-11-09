"""The MQTT connection gets a real keep-alive (geckoal/gecko-iot-client#15)."""

from __future__ import annotations

import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock

import awscrt.mqtt5
from awsiot import mqtt5_client_builder

ROOT = Path(__file__).parents[1]


def _load_module():
    """Load the shim without importing Home Assistant."""
    path = ROOT / "custom_components" / "gecko" / "mqtt_keepalive.py"
    spec = importlib.util.spec_from_file_location("gecko_mqtt_keepalive_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mqtt_keepalive = _load_module()

# What gecko-iot-client 1.0.3's MqttClient.connect() passes to the builder.
LIBRARY_KWARGS = dict(
    endpoint="example.invalid",
    auth_authorizer_name="authorizer",
    auth_username="",
    auth_password=b"",
    auth_token_key_name="token",
    auth_token_value="value",
    auth_authorizer_signature="signature",
    client_id="client",
    clean_start=True,
    keep_alive_secs=30,
)


def _client_options(build, **kwargs):
    """Run the real awsiot builder and return the options it gives the client."""
    seen = {}

    def capture(client_options):
        seen["options"] = client_options
        return mock.Mock()

    with mock.patch.object(awscrt.mqtt5, "Client", side_effect=capture):
        build(**kwargs)
    return seen["options"]


class KeepAliveTests(unittest.TestCase):
    """Check the keep-alive the native client is actually given."""

    def _module(self):
        return types.SimpleNamespace(mqtt5_client_builder=mqtt5_client_builder)

    def test_library_keyword_alone_leaves_the_1200_s_default(self) -> None:
        options = _client_options(
            mqtt5_client_builder.direct_with_custom_authorizer, **LIBRARY_KWARGS
        )
        self.assertEqual(options.connect_options.keep_alive_interval_sec, 1200)

    def test_installed_shim_sends_60_s_and_a_10_s_ping_timeout(self) -> None:
        module = self._module()
        self.assertTrue(mqtt_keepalive.install(module))

        options = _client_options(
            module.mqtt5_client_builder.direct_with_custom_authorizer,
            **LIBRARY_KWARGS,
        )

        self.assertEqual(options.connect_options.keep_alive_interval_sec, 60)
        self.assertEqual(options.ping_timeout_ms, 10_000)

    def test_awsiot_itself_is_untouched(self) -> None:
        original = mqtt5_client_builder.direct_with_custom_authorizer
        mqtt_keepalive.install(self._module())
        self.assertIs(mqtt5_client_builder.direct_with_custom_authorizer, original)

    def test_install_twice_wraps_once(self) -> None:
        module = self._module()
        mqtt_keepalive.install(module)
        first = module.mqtt5_client_builder
        mqtt_keepalive.install(module)
        self.assertIs(module.mqtt5_client_builder, first)

    def test_a_fixed_library_is_left_alone(self) -> None:
        kwargs = {"keep_alive_interval_sec": 45, "ping_timeout_ms": 5000}
        self.assertIs(mqtt_keepalive.fix_keep_alive_kwargs(kwargs), kwargs)

    def test_installed_library_still_uses_the_wrapped_name(self) -> None:
        from gecko_iot_client.transporters.mqtt import client

        self.assertTrue(hasattr(client, "mqtt5_client_builder"))


if __name__ == "__main__":
    unittest.main()
