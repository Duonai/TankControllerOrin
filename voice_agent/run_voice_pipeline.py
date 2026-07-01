from __future__ import annotations

import argparse
import os
import re
import select
import struct
import subprocess
import termios
import threading
import time
import tty
import wave
from pathlib import Path
from urllib import error, request

from run_voice_to_qwen import (
    build_arg_parser as build_command_parser,
    build_transcriber,
    format_elapsed,
    process_transcript,
    stage_message,
    transcribe_audio_path,
)
from src.stt_engine import SttError
from src.tank_command_sender import TankCommandSendError, VoiceCommandStreamSender
from src.qwen_client import QwenError


DEFAULT_TRIGGER_KEY = "space"
DEFAULT_QUIT_KEY = "q"
DEFAULT_TRIGGER_INPUT_MODE = "auto"
DEFAULT_TRIGGER_EVENT_DEVICE = "auto"
MIN_COMMAND_AUDIO_SECONDS = 0.2

INPUT_EVENT_STRUCT = struct.Struct("llHHI")
INPUT_EVENT_TYPE_KEY = 0x01
INPUT_EVENT_KEY_UP = 0x00
INPUT_EVENT_KEY_DOWN = 0x01

KEY_NAME_TO_EVENT_CODE = {
    "space": 57,
    "enter": 28,
    "a": 30,
    "b": 48,
    "c": 46,
    "d": 32,
    "e": 18,
    "f": 33,
    "g": 34,
    "h": 35,
    "i": 23,
    "j": 36,
    "k": 37,
    "l": 38,
    "m": 50,
    "n": 49,
    "o": 24,
    "p": 25,
    "q": 16,
    "r": 19,
    "s": 31,
    "t": 20,
    "u": 22,
    "v": 47,
    "w": 17,
    "x": 45,
    "y": 21,
    "z": 44,
    "0": 11,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = build_command_parser()
    parser.description = "Start local Qwen if needed, wait for a keyboard trigger, record a command, process it, and return to standby."
    root_dir = Path(__file__).resolve().parent
    workspace_dir = root_dir.parent

    parser.add_argument(
        "--trigger-key",
        default=DEFAULT_TRIGGER_KEY,
        help="Keyboard key that starts command recording: 'space', 'enter', or a single character",
    )
    parser.add_argument(
        "--quit-key",
        default=DEFAULT_QUIT_KEY,
        help="Keyboard key that exits standby mode: 'space', 'enter', or a single character",
    )
    parser.add_argument(
        "--trigger-input-mode",
        choices=["auto", "tty", "event"],
        default=DEFAULT_TRIGGER_INPUT_MODE,
        help="Trigger input source: auto prefers a physical keyboard event device, tty uses the current terminal, event forces a Linux input event device",
    )
    parser.add_argument(
        "--trigger-event-device",
        default=DEFAULT_TRIGGER_EVENT_DEVICE,
        help="Linux input event device path for trigger keys, 'auto' to detect readable keyboard devices, or a comma-separated list of event paths",
    )
    parser.add_argument("--listen-once", action="store_true", help="Exit after one successful trigger and command cycle")
    parser.add_argument("--start-qwen", action="store_true", help="Start the local Qwen server automatically if it is not already available")
    parser.add_argument(
        "--qwen-server-bin",
        default=str(workspace_dir / "llama.cpp" / "build" / "bin" / "llama-server"),
        help="Path to llama-server binary",
    )
    parser.add_argument(
        "--qwen-server-model",
        default=str(workspace_dir / "Qwen3-4B-Instruct-2507-Q6_K" / "Qwen_Qwen3-4B-Instruct-2507-Q6_K.gguf"),
        help="Path to local Qwen GGUF model",
    )
    parser.add_argument("--qwen-server-ngl", type=str, default="999", help="Value passed to llama-server --n-gpu-layers")
    parser.add_argument("--qwen-server-ctx-size", type=int, default=8192, help="Context size used when launching llama-server")
    parser.add_argument(
        "--qwen-server-flash-attn",
        choices=["auto", "on", "off"],
        default="auto",
        help="Flash attention mode passed to llama-server",
    )
    parser.add_argument("--qwen-start-timeout", type=float, default=90.0, help="Seconds to wait for the local Qwen server to become healthy")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    server_process: subprocess.Popen[str] | None = None
    command_stream_sender: VoiceCommandStreamSender | None = None

    try:
        trigger_key = resolve_key_spec(args.trigger_key)
        quit_key = resolve_key_spec(args.quit_key)
        if trigger_key == quit_key:
            raise ValueError("trigger key and quit key must be different")

        trigger_key_code = resolve_event_key_code(args.trigger_key)
        quit_key_code = resolve_event_key_code(args.quit_key)
        trigger_input_mode, trigger_event_devices = resolve_trigger_input_source(args)
        if trigger_input_mode != "event":
            raise RuntimeError(
                "hold-to-record requires a readable physical keyboard event device; use auto mode with input access or --trigger-input-mode event"
            )

        server_process = ensure_qwen_server(args)
        if args.send_command:
            command_stream_sender = VoiceCommandStreamSender(
                config_path=Path(args.tank_config),
                node_name=args.tank_node,
                profile_override=args.tank_profile,
                stream_hz=args.stream_hz,
            )
            command_stream_sender.start()
            args.command_stream_sender = command_stream_sender
            print(
                stage_message(
                    "SEND",
                    f"Persistent command stream started node={args.tank_node} profile={args.tank_profile or 'config-default'} hz={args.stream_hz:g}",
                ),
                flush=True,
            )
        print(
            stage_message(
                "WAIT",
                describe_wait_message(trigger_key, quit_key, trigger_input_mode, trigger_event_devices),
            ),
            flush=True,
        )
        while True:
            pressed_key = wait_for_trigger_key(
                trigger_key=trigger_key,
                quit_key=quit_key,
                trigger_key_code=trigger_key_code,
                quit_key_code=quit_key_code,
                input_mode=trigger_input_mode,
                event_devices=trigger_event_devices,
            )
            if pressed_key == quit_key:
                print(stage_message("RUN", "Quit key pressed; shutting down"), flush=True)
                return 0

            print("\a", end="", flush=True)
            print(stage_message("TRIGGER", f"Trigger key pressed: {describe_key(pressed_key)}"), flush=True)
            print(stage_message("SIGNAL", f"Command recording started; hold {describe_key(trigger_key)} and release to stop"), flush=True)

            cycle_completed = run_command_cycle(args, trigger_event_devices, trigger_key_code)
            if cycle_completed:
                print(stage_message("WAIT", "Returning to standby mode"), flush=True)
            else:
                print(stage_message("WAIT", "Returning to standby mode after send failure"), flush=True)

            if args.listen_once and cycle_completed:
                print(stage_message("RUN", "listen-once completed; exiting"), flush=True)
                return 0

    except KeyboardInterrupt:
        print(stage_message("RUN", "Interrupted by user; shutting down"), flush=True)
        return 130
    except (SttError, QwenError, TankCommandSendError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(stage_message("ERROR", str(exc)), flush=True)
        return 1
    finally:
        if command_stream_sender is not None:
            command_stream_sender.close()
        stop_qwen_server(server_process)


def ensure_qwen_server(args: argparse.Namespace) -> subprocess.Popen[str] | None:
    if is_qwen_healthy(args.qwen_url):
        print(stage_message("QWEN", "Existing Qwen server is healthy; reusing it"), flush=True)
        return None

    if not args.start_qwen:
        raise RuntimeError("Qwen server is not reachable; re-run with --start-qwen or start llama-server manually")

    server_bin = Path(args.qwen_server_bin)
    model_path = Path(args.qwen_server_model)
    if not server_bin.exists():
        raise RuntimeError(f"llama-server binary not found: {server_bin}")
    if not model_path.exists():
        raise RuntimeError(f"Qwen model not found: {model_path}")

    workspace_dir = Path(__file__).resolve().parent.parent
    log_dir = workspace_dir / "voice_agent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "qwen_server.log"
    flash_attn_value = args.qwen_server_flash_attn
    host, port = resolve_host_port(args.qwen_url)

    command = [
        str(server_bin),
        "-m",
        str(model_path),
        "-ngl",
        str(args.qwen_server_ngl),
        "-c",
        str(args.qwen_server_ctx_size),
        "-fa",
        flash_attn_value,
        "--host",
        host,
        "--port",
        str(port),
    ]

    print(stage_message("QWEN", f"Starting local llama-server; logs -> {log_path}"), flush=True)
    log_handle = log_path.open("a", encoding="utf-8")
    launch_env = os.environ.copy()
    library_dir = str(server_bin.resolve().parent)
    existing_library_path = launch_env.get("LD_LIBRARY_PATH", "")
    launch_env["LD_LIBRARY_PATH"] = library_dir if not existing_library_path else f"{library_dir}:{existing_library_path}"
    process = subprocess.Popen(
        command,
        cwd=server_bin.resolve().parents[2],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=launch_env,
    )
    process._voice_log_handle = log_handle  # type: ignore[attr-defined]

    started_at = time.perf_counter()
    while time.perf_counter() - started_at < args.qwen_start_timeout:
        if is_qwen_healthy(args.qwen_url):
            elapsed = time.perf_counter() - started_at
            print(stage_message("QWEN", f"Local Qwen server is ready (elapsed={format_elapsed(elapsed)})"), flush=True)
            return process

        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited early; inspect log: {log_path}")

        time.sleep(1.0)

    stop_qwen_server(process)
    raise RuntimeError(f"timed out waiting for local Qwen server to become healthy; inspect log: {log_path}")


def stop_qwen_server(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)

    log_handle = getattr(process, "_voice_log_handle", None)
    if log_handle is not None:
        log_handle.close()


def is_qwen_healthy(base_url: str) -> bool:
    url = f"{base_url.rstrip('/')}/health"
    try:
        with request.urlopen(url, timeout=2.0) as response:
            return response.status == 200
    except (error.URLError, TimeoutError, ValueError):
        return False


def wait_for_trigger_key(
    trigger_key: str,
    quit_key: str,
    trigger_key_code: int,
    quit_key_code: int,
    input_mode: str,
    event_devices: list[Path],
) -> str:
    if input_mode == "event":
        if not event_devices:
            raise RuntimeError("trigger input mode is 'event' but no keyboard event device is available")
        return wait_for_event_key(trigger_key, quit_key, trigger_key_code, quit_key_code, event_devices)

    tty_path = "/dev/tty"
    try:
        with open(tty_path, "rb", buffering=0) as tty_stream:
            file_descriptor = tty_stream.fileno()
            original_attrs = termios.tcgetattr(file_descriptor)
            try:
                tty.setraw(file_descriptor)
                while True:
                    pressed_key = tty_stream.read(1).decode("utf-8", errors="ignore")
                    if not pressed_key:
                        continue
                    if pressed_key in (trigger_key, quit_key):
                        return pressed_key
            finally:
                termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_attrs)
    except OSError as exc:
        raise RuntimeError(f"failed to read keyboard trigger from {tty_path}: {exc}") from exc


def wait_for_event_key(trigger_key: str, quit_key: str, trigger_key_code: int, quit_key_code: int, event_devices: list[Path]) -> str:
    streams: list[tuple[Path, object]] = []
    try:
        for event_device in event_devices:
            streams.append((event_device, event_device.open("rb", buffering=0)))

        while True:
            ready_streams, _, _ = select.select([stream for _, stream in streams], [], [])
            for ready_stream in ready_streams:
                event_stream = next(stream for _, stream in streams if stream == ready_stream)
                while True:
                    event_bytes = event_stream.read(INPUT_EVENT_STRUCT.size)
                    if len(event_bytes) != INPUT_EVENT_STRUCT.size:
                        break

                    _tv_sec, _tv_usec, event_type, event_code, event_value = INPUT_EVENT_STRUCT.unpack(event_bytes)
                    if event_type != INPUT_EVENT_TYPE_KEY or event_value != INPUT_EVENT_KEY_DOWN:
                        continue
                    if event_code == trigger_key_code:
                        return trigger_key
                    if event_code == quit_key_code:
                        return quit_key
    except PermissionError as exc:
        event_list = ", ".join(str(path) for path in event_devices)
        raise RuntimeError(
            f"cannot read keyboard event device {event_list}: permission denied; add user '{os.environ.get('USER', 'current user')}' to the 'input' group or run from a session with access"
        ) from exc
    except OSError as exc:
        event_list = ", ".join(str(path) for path in event_devices)
        raise RuntimeError(f"failed to read keyboard event device {event_list}: {exc}") from exc
    finally:
        for _event_device, event_stream in streams:
            event_stream.close()


def run_command_cycle(args: argparse.Namespace, event_devices: list[Path], trigger_key_code: int) -> bool:
    command_audio_path = record_command_audio(args, event_devices, trigger_key_code)
    if command_audio_path is None:
        return True

    transcriber, _, _, _, _ = build_transcriber(args)
    try:
        transcript = transcribe_audio_path(args, transcriber, command_audio_path)
    except SttError as exc:
        if str(exc) == "transcript is empty":
            print(stage_message("STT", "Skipped empty transcript from a very short button press"), flush=True)
            return True
        raise

    try:
        process_transcript(args, transcript)
    except TankCommandSendError as exc:
        print(stage_message("ERROR", str(exc)), flush=True)
        return False

    return True


def record_command_audio(args: argparse.Namespace, event_devices: list[Path], trigger_key_code: int) -> Path | None:
    transcriber, _, _, _, _ = build_transcriber(args)
    command_audio_path = Path(args.record_output)
    record_started_at = time.perf_counter()
    stop_event = threading.Event()
    release_watcher = threading.Thread(
        target=wait_for_event_key_release,
        args=(trigger_key_code, event_devices, stop_event),
        name="voice-trigger-release",
        daemon=True,
    )
    release_watcher.start()

    safety_limit = args.record_seconds if args.record_seconds > 0 else None
    limit_text = f" max={args.record_seconds:g}s" if safety_limit is not None else ""
    print(
        stage_message(
            "RECORD",
            f"Command recording started device='{args.record_device}'{limit_text}; release trigger key to stop",
        ),
        flush=True,
    )
    try:
        try:
            transcriber.record_audio_until_stop(
                command_audio_path,
                stop_event=stop_event,
                device=args.record_device,
                max_seconds=safety_limit,
            )
        except SttError as exc:
            if str(exc) == "recording produced no audio":
                print(stage_message("RECORD", "Ignored very short button tap because no audio was captured"), flush=True)
                return None
            raise
    finally:
        stop_event.set()
        release_watcher.join(timeout=1.0)

    elapsed = time.perf_counter() - record_started_at
    audio_duration_seconds = measure_wav_duration_seconds(command_audio_path)
    if audio_duration_seconds < MIN_COMMAND_AUDIO_SECONDS:
        print(
            stage_message(
                "RECORD",
                f"Ignored short recording ({audio_duration_seconds:.3f}s < {MIN_COMMAND_AUDIO_SECONDS:.1f}s)",
            ),
            flush=True,
        )
        return None

    print(stage_message("RECORD", f"Command audio saved to {command_audio_path} (elapsed={format_elapsed(elapsed)})"), flush=True)
    return command_audio_path


def wait_for_event_key_release(trigger_key_code: int, event_devices: list[Path], stop_event: threading.Event) -> None:
    streams: list[tuple[Path, object]] = []
    try:
        for event_device in event_devices:
            streams.append((event_device, event_device.open("rb", buffering=0)))

        while not stop_event.is_set():
            ready_streams, _, _ = select.select([stream for _, stream in streams], [], [], 0.1)
            for ready_stream in ready_streams:
                event_stream = next(stream for _, stream in streams if stream == ready_stream)
                while not stop_event.is_set():
                    event_bytes = event_stream.read(INPUT_EVENT_STRUCT.size)
                    if len(event_bytes) != INPUT_EVENT_STRUCT.size:
                        break

                    _tv_sec, _tv_usec, event_type, event_code, event_value = INPUT_EVENT_STRUCT.unpack(event_bytes)
                    if event_type != INPUT_EVENT_TYPE_KEY:
                        continue
                    if event_code == trigger_key_code and event_value == INPUT_EVENT_KEY_UP:
                        stop_event.set()
                        return
    finally:
        for _event_device, event_stream in streams:
            event_stream.close()


def measure_wav_duration_seconds(audio_path: Path) -> float:
    if not audio_path.exists():
        return 0.0

    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            if frame_rate <= 0:
                return 0.0
            return frame_count / frame_rate
    except wave.Error:
        return 0.0


def resolve_key_spec(key_spec: str) -> str:
    normalized = key_spec.strip().lower()
    if not normalized:
        raise ValueError("key spec must not be empty")
    if normalized == "space":
        return " "
    if normalized == "enter":
        return "\r"
    if len(normalized) == 1:
        return normalized
    raise ValueError("key spec must be 'space', 'enter', or a single character")


def resolve_event_key_code(key_spec: str) -> int:
    normalized = key_spec.strip().lower()
    event_code = KEY_NAME_TO_EVENT_CODE.get(normalized)
    if event_code is None:
        raise ValueError("event key spec must be 'space', 'enter', a-z, or 0-9")
    return event_code


def describe_key(key_value: str) -> str:
    if key_value == " ":
        return "SPACE"
    if key_value in {"\r", "\n"}:
        return "ENTER"
    return key_value.upper()


def resolve_trigger_input_source(args: argparse.Namespace) -> tuple[str, list[Path]]:
    if args.trigger_input_mode == "tty":
        return "tty", []

    event_devices = resolve_trigger_event_devices(args.trigger_event_device)
    if args.trigger_input_mode == "event":
        if not event_devices:
            raise RuntimeError("no keyboard event device was found; specify --trigger-event-device or use --trigger-input-mode tty")
        return "event", event_devices

    readable_event_devices = [event_device for event_device in event_devices if os.access(event_device, os.R_OK)]
    if readable_event_devices:
        return "event", readable_event_devices
    return "tty", event_devices


def resolve_trigger_event_devices(device_arg: str) -> list[Path]:
    if device_arg and device_arg != "auto":
        device_paths: list[Path] = []
        for item in device_arg.split(","):
            device_path = Path(item.strip())
            if not device_path.exists():
                raise RuntimeError(f"trigger event device not found: {device_path}")
            device_paths.append(device_path)
        return device_paths

    return detect_keyboard_event_devices()


def detect_keyboard_event_devices() -> list[Path]:
    by_id_dir = Path("/dev/input/by-id")
    if by_id_dir.exists():
        by_id_devices: list[Path] = []
        for entry in sorted(by_id_dir.iterdir()):
            if not entry.name.endswith("-event-kbd"):
                continue
            target_path = entry.resolve()
            if target_path.exists() and target_path not in by_id_devices:
                by_id_devices.append(target_path)
        if by_id_devices:
            return by_id_devices

    devices_path = Path("/proc/bus/input/devices")
    if not devices_path.exists():
        return []

    blocks = devices_path.read_text(encoding="utf-8", errors="replace").split("\n\n")
    preferred_events: list[Path] = []
    fallback_events: list[Path] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        name = ""
        handlers = ""
        for line in lines:
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].strip()

        if "kbd" not in handlers:
            continue

        name_lower = name.lower()
        if any(excluded in name_lower for excluded in ("microphone", "mouse", "headset jack", "hdmi/dp")):
            continue

        match = re.search(r"\bevent(\d+)\b", handlers)
        if not match:
            continue

        event_path = Path("/dev/input") / f"event{match.group(1)}"
        if not event_path.exists():
            continue

        if "keyboard" in name_lower or "leds" in handlers:
            if event_path not in preferred_events:
                preferred_events.append(event_path)
        elif event_path not in fallback_events:
            fallback_events.append(event_path)

    return preferred_events + [event_path for event_path in fallback_events if event_path not in preferred_events]


def describe_wait_message(trigger_key: str, quit_key: str, input_mode: str, event_devices: list[Path]) -> str:
    base = f"Standby mode entered; press {describe_key(trigger_key)} to record or {describe_key(quit_key)} to quit"
    if input_mode == "event" and event_devices:
        return f"{base} using keyboard event device(s) {', '.join(str(path) for path in event_devices)}"
    if input_mode == "tty" and event_devices:
        unreadable = [path for path in event_devices if not os.access(path, os.R_OK)]
        if unreadable:
            return f"{base} using terminal input because {', '.join(str(path) for path in unreadable)} is not readable by the current user"
    return f"{base} using terminal input"


def resolve_host_port(base_url: str) -> tuple[str, int]:
    if not base_url.startswith("http://"):
        raise RuntimeError(f"Only http:// Qwen URLs are supported for auto-start, got: {base_url}")

    host_port = base_url.removeprefix("http://").rstrip("/")
    host, sep, port_text = host_port.partition(":")
    if not sep:
        return host, 80
    return host, int(port_text)


if __name__ == "__main__":
    raise SystemExit(main())
