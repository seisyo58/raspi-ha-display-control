#!/usr/bin/env python3
"""MQTT to Wayland display bridge.

The bridge intentionally accepts only the two exact command payloads ON and
OFF. The command topic is never published by this process, while state and
availability are retained according to the repository README.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # Keep configuration errors readable on a fresh Pi.
    mqtt = None  # type: ignore[assignment]


LOG = logging.getLogger("raspi-display-mqtt")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class Settings:
    broker_host: str
    broker_port: int
    username: str | None
    password: str | None
    client_id: str
    keepalive: int
    use_tls: bool
    tls_ca_cert: str | None
    command_topic: str
    state_topic: str
    availability_topic: str
    error_topic: str
    display_control: str
    command_timeout: float
    status_interval: float

    @classmethod
    def from_environment(cls) -> "Settings":
        broker_host = os.getenv("MQTT_HOST", "").strip()
        if not broker_host:
            raise ValueError("MQTT_HOST is required")

        try:
            broker_port = int(os.getenv("MQTT_PORT", "1883"))
            keepalive = int(os.getenv("MQTT_KEEPALIVE", "60"))
        except ValueError as exc:
            raise ValueError("MQTT_PORT and MQTT_KEEPALIVE must be integers") from exc
        if not 1 <= broker_port <= 65535:
            raise ValueError("MQTT_PORT must be between 1 and 65535")
        if keepalive <= 0:
            raise ValueError("MQTT_KEEPALIVE must be greater than zero")

        username = os.getenv("MQTT_USERNAME") or None
        password = os.getenv("MQTT_PASSWORD")
        if username and password is None:
            raise ValueError("MQTT_PASSWORD is required when MQTT_USERNAME is set")

        return cls(
            broker_host=broker_host,
            broker_port=broker_port,
            username=username,
            password=password,
            client_id=os.getenv("MQTT_CLIENT_ID", "raspi3-display-mqtt"),
            keepalive=keepalive,
            use_tls=env_bool("MQTT_TLS"),
            tls_ca_cert=os.getenv("MQTT_TLS_CA_CERT") or None,
            command_topic=os.getenv("MQTT_COMMAND_TOPIC", "home/raspi3/display/hdmi/set"),
            state_topic=os.getenv("MQTT_STATE_TOPIC", "home/raspi3/display/hdmi/state"),
            availability_topic=os.getenv(
                "MQTT_AVAILABILITY_TOPIC", "home/raspi3/display/availability"
            ),
            error_topic=os.getenv("MQTT_ERROR_TOPIC", "home/raspi3/display/error"),
            display_control=os.getenv(
                "DISPLAY_CONTROL_PATH", "/opt/raspi-ha-display-control/bin/display-control"
            ),
            command_timeout=env_float("DISPLAY_CONTROL_TIMEOUT", 50.0),
            status_interval=env_float("DISPLAY_STATUS_INTERVAL", 300.0),
        )


class DisplayBridge:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or self._create_client()
        self.operation_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self.status_thread = threading.Thread(
            target=self._status_loop, name="display-status", daemon=True
        )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.will_set(
            self.settings.availability_topic,
            payload="offline",
            qos=1,
            retain=True,
        )
        if self.settings.username:
            self.client.username_pw_set(self.settings.username, self.settings.password)
        if self.settings.use_tls:
            self.client.tls_set(ca_certs=self.settings.tls_ca_cert)

    def _create_client(self) -> Any:
        if mqtt is None:
            raise ImportError("paho-mqtt is not installed; install the Raspberry Pi package")
        # Paho 2.x requires an explicit callback API version. Keep support for
        # the Debian/Raspberry Pi Paho 1.x package as well.
        try:
            return mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=self.settings.client_id,
            )
        except (AttributeError, TypeError):
            return mqtt.Client(client_id=self.settings.client_id)

    def connect_forever(self) -> None:
        self.status_thread.start()
        LOG.info("connecting to MQTT broker %s:%s", self.settings.broker_host, self.settings.broker_port)
        while not self.stop_event.is_set():
            try:
                self.client.connect(
                    self.settings.broker_host,
                    self.settings.broker_port,
                    self.settings.keepalive,
                )
                break
            except Exception as exc:
                LOG.warning("MQTT connection failed: %s; retrying", exc)
                self.stop_event.wait(5)
        if self.stop_event.is_set():
            return
        try:
            self.client.loop_forever(retry_first_connection=True)
        finally:
            self.stop_event.set()
            self.connected_event.clear()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.connected_event.is_set():
            info = self.client.publish(
                self.settings.availability_topic, "offline", qos=1, retain=True
            )
            info.wait_for_publish(timeout=5)
        # Disconnect even when the bridge is between reconnect attempts so
        # paho's loop_forever() can leave its internal retry loop.
        try:
            self.client.disconnect()
        except Exception as exc:
            LOG.debug("MQTT disconnect during shutdown failed: %s", exc)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if int(reason_code) != 0:
            LOG.error("MQTT connection failed: %s", reason_code)
            return

        self.connected_event.set()
        client.subscribe(self.settings.command_topic, qos=1)
        client.publish(self.settings.availability_topic, "online", qos=1, retain=True)
        LOG.info("MQTT connected; subscribed to %s", self.settings.command_topic)
        threading.Thread(
            target=self.refresh_state,
            name="display-state-on-connect",
            daemon=True,
        ).start()

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self.connected_event.clear()
        reason = args[-2] if len(args) >= 2 else (args[0] if args else "unknown")
        LOG.warning("MQTT disconnected: %s; waiting for automatic reconnect", reason)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic != self.settings.command_topic:
            return
        if getattr(message, "retain", False):
            LOG.warning("ignoring retained command message on %s", message.topic)
            return

        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            LOG.warning("ignoring non-UTF-8 command payload")
            self.publish_error("invalid command payload: non-UTF-8")
            return

        if payload not in {"ON", "OFF"}:
            LOG.warning("ignoring invalid command payload: %r", payload)
            self.publish_error(f"invalid command payload: {payload[:80]}")
            return

        with self.operation_lock:
            self.apply_command(payload)

    def apply_command(self, payload: str) -> bool:
        argument = payload.lower()
        try:
            result = subprocess.run(
                [self.settings.display_control, argument],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.settings.command_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("display command %s failed: %s", payload, exc)
            self.publish_error(f"display command {payload} failed: {exc}")
            return False

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            LOG.error("display command %s failed with exit code %s: %s", payload, result.returncode, detail)
            self.publish_error(f"display command {payload} failed: {detail[:160]}")
            return False

        self.publish_state(payload)
        LOG.info("display command %s succeeded", payload)
        return True

    def refresh_state(self) -> bool:
        with self.operation_lock:
            try:
                result = subprocess.run(
                    [self.settings.display_control, "status"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.settings.command_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOG.error("display status check failed: %s", exc)
                self.publish_error(f"display status check failed: {exc}")
                return False

            state = result.stdout.strip()
            if result.returncode == 0 and state in {"ON", "OFF"}:
                self.publish_state(state)
                LOG.info("display state refreshed: %s", state)
                return True

            detail = (result.stderr or state or "UNKNOWN").strip().replace("\n", " ")
            LOG.warning("display state is unknown: %s", detail)
            self.publish_error(f"display state unknown: {detail[:160]}")
            return False

    def publish_state(self, state: str) -> None:
        self.client.publish(self.settings.state_topic, state, qos=1, retain=True)

    def publish_error(self, message: str) -> None:
        self.client.publish(self.settings.error_topic, message, qos=1, retain=False)

    def _status_loop(self) -> None:
        while not self.stop_event.wait(self.settings.status_interval):
            if self.connected_event.is_set():
                self.refresh_state()


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        settings = Settings.from_environment()
        bridge = DisplayBridge(settings)
    except (ValueError, ImportError) as exc:
        LOG.error("configuration error: %s", exc)
        return 78

    def handle_signal(signum: int, frame: Any) -> None:
        LOG.info("received signal %s; stopping", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        bridge.connect_forever()
    except Exception:
        LOG.exception("MQTT bridge stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
