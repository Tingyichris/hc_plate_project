"""Publish plate lifecycle events to the configured MQTT broker."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from . import constants as const


LOGGER = logging.getLogger(__name__)

PLATE_COLOR_CODES = {
    "red": "#e0564c", "orange": "#f28c28", "green": "#4cae7a",
    "light_green": "#88d66c", "white": "#f2f2f2", "gray": "#808080",
    "black": "#2f333d", "unknown": "#8892a6",
}

@dataclass
class PlateLifecycle:
    plate_class: str
    plate_color: str
    last_seen: float
    last_progress: float
    moving_toward_exit: bool = False
    exit_candidate: bool = False
    missing_since: float | None = None


class PlateMqttPublisher:
    """Translate recorder-enriched detections into MQTT enter/update/exit events."""

    def __init__(self, on_connected: Callable[[], None] | None = None):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError(
                "MQTT publishing needs paho-mqtt. Run: pip install -r requirements.txt"
            ) from exc

        self.topic = f"sushi/events/{const.MQTT_CAMERA_ID}"
        self.status_topic = f"sushi/status/{const.MQTT_CAMERA_ID}"
        self.plates: dict[int, PlateLifecycle] = {}
        self.last_heartbeat = 0.0
        self.client_id = f"{const.REPORT_MQTT_CLIENT_ID}_{uuid4().hex[:12]}"
        self._connected = False
        self._closed = False
        self._on_connected = on_connected
        self._pending_payloads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._connect_retry_thread: threading.Thread | None = None

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,
        )
        username = str(const.REPORT_MQTT_USERNAME).strip()
        password = str(const.REPORT_MQTT_PASSWORD)
        if username:
            self.client.username_pw_set(username, password)

        offline = self._status_payload("offline")
        self.client.will_set(self.status_topic, offline, qos=const.REPORT_MQTT_QOS, retain=True)
        self.client.on_connect = self._handle_connect
        self.client.on_disconnect = self._handle_disconnect
        self.client.on_publish = self._handle_publish
        self.client.on_log = self._handle_log
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.loop_start()
        self._start_connect_retry_thread()
        LOGGER.info(
            "MQTT initialized broker=%s:%s client_id=%s topic=%s",
            const.REPORT_MQTT_BROKER_HOST, const.REPORT_MQTT_BROKER_PORT, self.client_id, self.topic,
        )

    def _handle_connect(
        self, _client, _userdata, _flags, reason_code, _properties
    ) -> None:
        self._connected = reason_code == 0
        if not self._connected:
            LOGGER.error("MQTT connect failed reason=%s", reason_code)
            return
        LOGGER.info("MQTT connected client_id=%s", self.client_id)
        self._publish_status("online")
        self._flush_pending_payloads()
        if self._on_connected:
            self._on_connected()

    def _handle_disconnect(
        self, _client, _userdata, _disconnect_flags, reason_code, _properties
    ) -> None:
        self._connected = False
        if not self._closed:
            LOGGER.warning("MQTT disconnected reason=%s; reconnecting", reason_code)

    @staticmethod
    def _handle_publish(
        _client, _userdata, mid, _reason_code, _properties
    ) -> None:
        LOGGER.debug("MQTT publish acknowledged mid=%s", mid)

    @staticmethod
    def _handle_log(_client, _userdata, level, message) -> None:
        LOGGER.debug("MQTT paho log level=%s message=%s", level, message)

    def _start_connect_retry_thread(self) -> None:
        if self._connect_retry_thread and self._connect_retry_thread.is_alive():
            return
        self._connect_retry_thread = threading.Thread(
            target=self._connect_with_retry,
            name="plate-mqtt-connect-retry",
            daemon=True,
        )
        self._connect_retry_thread.start()

    def _connect_with_retry(self) -> None:
        delay = 1
        attempt = 1
        while not self._stop_event.is_set() and not self._connected:
            try:
                LOGGER.info(
                    "MQTT connect attempt=%s broker=%s:%s",
                    attempt, const.REPORT_MQTT_BROKER_HOST, const.REPORT_MQTT_BROKER_PORT,
                )
                self.client.connect(
                    const.REPORT_MQTT_BROKER_HOST, const.REPORT_MQTT_BROKER_PORT, keepalive=const.MQTT_KEEPALIVE
                )
                return
            except (socket.gaierror, OSError):
                LOGGER.exception(
                    "MQTT connection failed attempt=%s retry_in=%ss",
                    attempt, delay,
                )
            attempt += 1
            self._stop_event.wait(delay)
            delay = min(delay * 2, 60)

    def _status_payload(self, state):
        return json.dumps({"state": state}, separators=(",", ":"))

    def _publish_status(self, state):
        if not self._connected:
            LOGGER.warning("MQTT disconnected; status publish skipped")
            return
        info = self.client.publish(
            self.status_topic,
            self._status_payload(state),
            qos=const.REPORT_MQTT_QOS,
            retain=True,
        )
        if info.rc != 0:
            LOGGER.error("MQTT status publish failed rc=%s", info.rc)
        self.last_heartbeat = time.monotonic()

    @staticmethod
    def _progress(instance, frame_shape):
        _x1, y1, _x2, y2 = (float(value) for value in instance["xyxy"])
        return ((y1 + y2) / 2.0) / max(1.0, float(frame_shape[0] - 1))

    def _publish_event(self, event, plate_id, plate_class, plate_color):
        payload = {
            "event": event,
            "plate_id": int(plate_id),
            "class": str(plate_class),
            "color": str(plate_color),
        }
        self._publish(self.topic, payload)

    @staticmethod
    def _pending_key(topic, payload):
        return (
            topic,
            str(payload.get("plate_id", "status")),
            str(payload.get("event", "update")),
        )

    def _publish(self, topic, payload):
        if not self._connected:
            self._queue_pending_payload(topic, payload)
            LOGGER.warning(
                "MQTT disconnected; queued topic=%s payload=%s", topic, payload
            )
            return
        result = self._publish_now(topic, payload)
        if result.rc != 0:
            self._queue_pending_payload(topic, payload)
            LOGGER.error("MQTT publish failed topic=%s rc=%s", topic, result.rc)

    def _publish_now(self, topic, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = self.client.publish(
            topic, payload=encoded, qos=const.REPORT_MQTT_QOS, retain=False
        )
        LOGGER.info(
            "MQTT publish topic=%s rc=%s mid=%s payload=%s",
            topic, result.rc, result.mid, encoded,
        )
        return result

    def _queue_pending_payload(self, topic, payload):
        key = self._pending_key(topic, payload)
        with self._pending_lock:
            self._pending_payloads[key] = dict(payload)

    def _flush_pending_payloads(self):
        with self._pending_lock:
            pending = list(self._pending_payloads.items())
        for key, payload in pending:
            result = self._publish_now(key[0], payload)
            if result.rc != 0:
                continue
            with self._pending_lock:
                if self._pending_payloads.get(key) == payload:
                    del self._pending_payloads[key]

    def observe(self, instances, frame_shape):
        now = time.monotonic()
        emitted = []
        if self._connected and now - self.last_heartbeat >= 10.0:
            self._publish_status("online")

        visible = set()
        for instance in instances:
            plate_id = instance.get("persistent_plate_id")
            stage = instance.get("tracking_stage", "approaching")
            if plate_id is None or stage == "approaching":
                continue
            plate_id = int(plate_id)
            visible.add(plate_id)
            plate_class = str(instance.get("stable_color", "unknown"))
            plate_color = PLATE_COLOR_CODES.get(
                plate_class.lower(), PLATE_COLOR_CODES["unknown"]
            )
            progress = self._progress(instance, frame_shape)
            state = self.plates.get(plate_id)

            if state is None:
                state = PlateLifecycle(plate_class, plate_color, now, progress)
                self.plates[plate_id] = state
                self._publish_event("entry", plate_id, plate_class, plate_color)
                emitted.append(("entry", plate_id, plate_class))
            else:
                state.moving_toward_exit = progress <= state.last_progress + 0.01
                state.plate_class = plate_class
                state.plate_color = plate_color
                state.last_seen = now
                state.last_progress = progress
                state.missing_since = None

            if stage == "exit_candidate" or (
                progress <= float(const.MQTT_EXIT_APPROACH_RATIO)
                and state.moving_toward_exit
            ):
                state.exit_candidate = True

        confirm_seconds = float(const.MQTT_DISAPPEAR_CONFIRM_SECONDS)
        for plate_id, state in list(self.plates.items()):
            if plate_id in visible:
                continue
            if state.missing_since is None:
                state.missing_since = now
                continue
            if now - state.missing_since < confirm_seconds:
                continue
            event = "exit" if state.exit_candidate else "catch"
            self._publish_event(event, plate_id, state.plate_class, state.plate_color)
            emitted.append((event, plate_id, state.plate_class))
            del self.plates[plate_id]
        return emitted

    def close(self):
        if self._closed:
            return
        if self._connected:
            self._publish_status("offline")
        self._closed = True
        self._stop_event.set()
        self.client.disconnect()
        self.client.loop_stop()
        if (
            self._connect_retry_thread
            and self._connect_retry_thread is not threading.current_thread()
        ):
            self._connect_retry_thread.join(timeout=5)
        LOGGER.info("MQTT publisher stopped")
