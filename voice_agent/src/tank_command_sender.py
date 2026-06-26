from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TANKCONTROLLER_DIR = Path(__file__).resolve().parents[2] / "TankControllerRasberryPi"
if str(TANKCONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(TANKCONTROLLER_DIR))

from client.result_transport import create_client_socket, send_json_line  # type: ignore  # noqa: E402
from client.runtime_stream import ResilientJsonSender, load_runtime_config, resolve_sender_config  # type: ignore  # noqa: E402


class TankCommandSendError(RuntimeError):
    pass


TIMED_COMMANDS = {"move_forward", "move_backward", "pivot_left", "pivot_right"}
ONE_SHOT_COMMANDS = {"reload", "scanning"}
IDLE_COMMAND = "none"


def send_tank_command(
    command_payload: dict[str, Any],
    config_path: Path,
    node_name: str,
    profile_override: str = "",
    stream_hz: float = 10.0,
) -> dict[str, Any]:
    config = load_runtime_config(str(config_path))

    role = str(command_payload.get("role", ""))
    result = command_payload.get("result")

    if not role:
        raise TankCommandSendError("command payload is missing role")
    if not isinstance(result, dict):
        raise TankCommandSendError("command payload result must be an object")

    target_node_name = node_name
    sender_conf = resolve_sender_config(config, target_node_name, profile_override)

    command_name = str(result.get("command", ""))
    if command_name in TIMED_COMMANDS:
        return send_timed_command(result, sender_conf, role, stream_hz)

    payload = build_stream_payload(
        role=role,
        device_id=sender_conf["device_id"],
        frame_id=int(time.time() * 1000),
        fps=stream_hz,
        result=result,
    )

    try:
        with create_client_socket(sender_conf["host"], sender_conf["port"], timeout=2.0) as sock:
            send_json_line(sock, payload)
    except OSError as exc:
        raise TankCommandSendError(
            f"failed to send tank command to {sender_conf['host']}:{sender_conf['port']}: {exc}"
        ) from exc

    return {
        "host": sender_conf["host"],
        "port": sender_conf["port"],
        "profile": sender_conf["profile"],
        "node": target_node_name,
        "device_id": sender_conf["device_id"],
        "payload": payload,
    }


def send_timed_command(result: dict[str, Any], sender_conf: dict[str, Any], role: str, stream_hz: float) -> dict[str, Any]:
    command_name = str(result.get("command", ""))
    data = float(result.get("data", 0.0))
    motion_hz = max(float(stream_hz), 1.0)
    duration_sec = timed_command_duration(command_name, data)

    if duration_sec <= 0.0:
        raise TankCommandSendError(f"player1 motion duration must be positive, got {duration_sec}")

    motion_result = {"command": command_name, "data": data}
    packet_count = max(1, int(round(duration_sec * motion_hz)))
    frame_id = int(time.time() * 1000)
    last_payload: dict[str, Any] | None = None

    try:
        with create_client_socket(sender_conf["host"], sender_conf["port"], timeout=2.0) as sock:
            started_at = time.monotonic()
            for packet_index in range(packet_count):
                payload = build_stream_payload(
                    role=role,
                    device_id=sender_conf["device_id"],
                    frame_id=frame_id + packet_index,
                    fps=motion_hz,
                    result=motion_result,
                )
                send_json_line(sock, payload)
                last_payload = payload

                next_send_at = started_at + ((packet_index + 1) / motion_hz)
                sleep_seconds = next_send_at - time.monotonic()
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
    except OSError as exc:
        raise TankCommandSendError(
            f"failed to send tank command to {sender_conf['host']}:{sender_conf['port']}: {exc}"
        ) from exc

    return {
        "host": sender_conf["host"],
        "port": sender_conf["port"],
        "profile": sender_conf["profile"],
        "node": role,
        "device_id": sender_conf["device_id"],
        "payload": last_payload,
        "packets_sent": packet_count,
        "duration_sec": duration_sec,
    }


def build_stream_payload(role: str, device_id: str, frame_id: int, fps: float, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "device_id": device_id,
        "frame_id": frame_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fps": round(float(fps), 2),
        "result": result,
    }


def timed_command_duration(command_name: str, data: float) -> float:
    if command_name in {"move_forward", "move_backward"}:
        return max(float(data), 0.0)
    if command_name in {"pivot_left", "pivot_right"}:
        return max(float(data), 0.0) / 90.0
    raise TankCommandSendError(f"unsupported timed command: {command_name}")


class VoiceCommandStreamSender:
    def __init__(self, config_path: Path, node_name: str, profile_override: str = "", stream_hz: float = 10.0) -> None:
        config = load_runtime_config(str(config_path))
        sender_conf = resolve_sender_config(config, node_name, profile_override)

        self.config_path = config_path
        self.node_name = node_name
        self.profile_override = profile_override
        self.role = node_name
        self.device_id = sender_conf["device_id"]
        self.stream_hz = max(float(stream_hz), 1.0)
        self._sender = ResilientJsonSender(
            host=sender_conf["host"],
            port=sender_conf["port"],
            role=self.role,
            device_id=self.device_id,
            send_interval=0.01,
        )
        self._frame_id = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="voice-command-stream", daemon=True)
        self._one_shot_result: dict[str, Any] | None = None
        self._active_result: dict[str, Any] | None = None
        self._active_until = 0.0

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._sender.close()

    def submit_command(self, command_payload: dict[str, Any]) -> dict[str, Any]:
        result = command_payload.get("result")
        if not isinstance(result, dict):
            raise TankCommandSendError("command payload result must be an object")

        command_name = str(result.get("command", ""))
        data = float(result.get("data", 0.0))
        now = time.monotonic()

        with self._lock:
            if command_name in TIMED_COMMANDS:
                duration_sec = timed_command_duration(command_name, data)
                if duration_sec <= 0.0:
                    raise TankCommandSendError(f"timed command duration must be positive, got {duration_sec}")
                self._active_result = {"command": command_name, "data": data}
                self._active_until = now + duration_sec
                info = {"duration_sec": duration_sec, "packets_estimate": max(1, int(round(duration_sec * self.stream_hz)))}
            elif command_name in ONE_SHOT_COMMANDS:
                self._one_shot_result = {"command": command_name, "data": data}
                info = {"duration_sec": 0.0, "packets_estimate": 1}
            elif command_name == "reject":
                info = {"duration_sec": 0.0, "packets_estimate": 0}
            else:
                raise TankCommandSendError(f"unsupported command for stream sender: {command_name}")

        return {
            "host": self._sender.host,
            "port": self._sender.port,
            "profile": self.profile_override or "config-default",
            "node": self.node_name,
            "device_id": self.device_id,
            "command": command_name,
            **info,
        }

    def _run_loop(self) -> None:
        interval = 1.0 / self.stream_hz
        next_tick = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            result = self._current_result(now)
            self._frame_id += 1
            self._sender.send_result(self._frame_id, self.stream_hz, result)
            next_tick += interval
            sleep_seconds = next_tick - time.monotonic()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            else:
                next_tick = time.monotonic()

    def _current_result(self, now: float) -> dict[str, Any]:
        with self._lock:
            if self._one_shot_result is not None:
                result = dict(self._one_shot_result)
                self._one_shot_result = None
                return result

            if self._active_result is not None and now < self._active_until:
                return dict(self._active_result)

            self._active_result = None
            self._active_until = 0.0
            return {"command": IDLE_COMMAND, "data": 0.0}