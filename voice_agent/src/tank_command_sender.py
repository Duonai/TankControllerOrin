from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TANKCONTROLLER_DIR = Path(__file__).resolve().parents[2] / "TankControllerRasberryPi"
if str(TANKCONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(TANKCONTROLLER_DIR))

from client.result_transport import create_client_socket, send_json_line  # type: ignore  # noqa: E402
from client.runtime_stream import load_runtime_config, resolve_sender_config  # type: ignore  # noqa: E402


class TankCommandSendError(RuntimeError):
    pass


def send_tank_command(
    command_payload: dict[str, Any],
    config_path: Path,
    node_name: str,
    profile_override: str = "",
) -> dict[str, Any]:
    config = load_runtime_config(str(config_path))
    sender_conf = resolve_sender_config(config, node_name, profile_override)

    role = str(command_payload.get("role", ""))
    result = command_payload.get("result")

    if not role:
        raise TankCommandSendError("command payload is missing role")
    if not isinstance(result, dict):
        raise TankCommandSendError("command payload result must be an object")

    payload = {
        "role": role,
        "device_id": sender_conf["device_id"],
        "frame_id": int(time.time() * 1000),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fps": 0.0,
        "result": result,
    }

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
        "device_id": sender_conf["device_id"],
        "payload": payload,
    }