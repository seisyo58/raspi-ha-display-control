import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "mqtt" / "display-mqtt-bridge.py"


class FakeMqttClient:
    def __init__(self):
        self.published = []
        self.subscriptions = []
        self.will = None

    def will_set(self, *args, **kwargs):
        self.will = (args, kwargs)

    def username_pw_set(self, *args):
        pass

    def tls_set(self, **kwargs):
        pass

    def subscribe(self, *args, **kwargs):
        self.subscriptions.append((args, kwargs))

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))


def load_module():
    fake_paho = types.ModuleType("paho")
    fake_mqtt = types.ModuleType("paho.mqtt.client")
    fake_mqtt.Client = object
    fake_mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1)
    fake_paho.mqtt = types.SimpleNamespace(client=fake_mqtt)
    with patch.dict(sys.modules, {"paho": fake_paho, "paho.mqtt": fake_paho.mqtt, "paho.mqtt.client": fake_mqtt}):
        spec = importlib.util.spec_from_file_location("display_mqtt_bridge", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def settings(self):
        env = {
            "MQTT_HOST": "broker",
            "MQTT_PORT": "1883",
            "MQTT_USERNAME": "user",
            "MQTT_PASSWORD": "password",
        }
        with patch.dict(os.environ, env, clear=False):
            return self.module.Settings.from_environment()

    def test_invalid_payload_is_not_executed(self):
        settings = self.settings()
        client = FakeMqttClient()
        bridge = self.module.DisplayBridge(settings, client=client)
        message = types.SimpleNamespace(
            topic=settings.command_topic, payload=b"ON ", retain=False
        )
        with patch.object(bridge, "apply_command") as apply:
            bridge._on_message(client, None, message)
        apply.assert_not_called()
        self.assertTrue(any("invalid command payload" in args[1] for args, _ in client.published))

    def test_retained_payload_is_not_executed(self):
        settings = self.settings()
        client = FakeMqttClient()
        bridge = self.module.DisplayBridge(settings, client=client)
        message = types.SimpleNamespace(
            topic=settings.command_topic, payload=b"ON", retain=True
        )
        with patch.object(bridge, "apply_command") as apply:
            bridge._on_message(client, None, message)
        apply.assert_not_called()

    def test_successful_command_publishes_retained_state(self):
        settings = self.settings()
        client = FakeMqttClient()
        bridge = self.module.DisplayBridge(settings, client=client)
        result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.module.subprocess, "run", return_value=result) as run:
            self.assertTrue(bridge.apply_command("ON"))
        run.assert_called_once()
        self.assertEqual(client.published[-1], ((settings.state_topic, "ON"), {"qos": 1, "retain": True}))

    def test_failed_command_does_not_publish_state(self):
        settings = self.settings()
        client = FakeMqttClient()
        bridge = self.module.DisplayBridge(settings, client=client)
        result = types.SimpleNamespace(returncode=1, stdout="", stderr="Wayland unavailable")
        with patch.object(self.module.subprocess, "run", return_value=result):
            self.assertFalse(bridge.apply_command("OFF"))
        self.assertFalse(any(args[0] == settings.state_topic for args, _ in client.published))


if __name__ == "__main__":
    unittest.main()
