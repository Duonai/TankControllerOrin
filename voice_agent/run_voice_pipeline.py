from __future__ import annotations

import argparse
import os
import subprocess
import termios
import time
import tty
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
from src.tank_command_sender import TankCommandSendError
from src.qwen_client import QwenError


DEFAULT_TRIGGER_KEY = "space"
DEFAULT_QUIT_KEY = "q"


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

    try:
        trigger_key = resolve_key_spec(args.trigger_key)
        quit_key = resolve_key_spec(args.quit_key)
        if trigger_key == quit_key:
            raise ValueError("trigger key and quit key must be different")

        server_process = ensure_qwen_server(args)
        print(
            stage_message(
                "WAIT",
                f"Standby mode entered; press {describe_key(trigger_key)} to record or {describe_key(quit_key)} to quit",
            ),
            flush=True,
        )
        while True:
            pressed_key = wait_for_trigger_key(trigger_key, quit_key)
            if pressed_key == quit_key:
                print(stage_message("RUN", "Quit key pressed; shutting down"), flush=True)
                return 0

            print("\a", end="", flush=True)
            print(stage_message("TRIGGER", f"Trigger key pressed: {describe_key(pressed_key)}"), flush=True)
            print(stage_message("SIGNAL", "Command recording started"), flush=True)

            cycle_completed = run_command_cycle(args)
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


def wait_for_trigger_key(trigger_key: str, quit_key: str) -> str:
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


def run_command_cycle(args: argparse.Namespace) -> bool:
    command_audio_path = record_command_audio(args)
    transcriber, _, _, _, _ = build_transcriber(args)
    transcript = transcribe_audio_path(args, transcriber, command_audio_path)

    try:
        process_transcript(args, transcript)
    except TankCommandSendError as exc:
        print(stage_message("ERROR", str(exc)), flush=True)
        return False

    return True


def record_command_audio(args: argparse.Namespace) -> Path:
    transcriber, _, _, _, _ = build_transcriber(args)
    command_audio_path = Path(args.record_output)
    record_started_at = time.perf_counter()

    if args.auto_stop:
        print(
            stage_message(
                "RECORD",
                f"Command auto-stop recording started max={args.record_seconds:g}s device='{args.record_device}' silence_stop={args.silence_stop_seconds:g}s",
            ),
            flush=True,
        )
        transcriber.record_audio_auto_stop(
            command_audio_path,
            max_seconds=args.record_seconds,
            device=args.record_device,
            silence_stop_seconds=args.silence_stop_seconds,
            speech_start_threshold=args.speech_start_threshold,
            speech_end_threshold=args.speech_end_threshold,
            min_speech_seconds=args.min_speech_seconds,
        )
    else:
        print(
            stage_message("RECORD", f"Command recording for {args.record_seconds:g}s from ALSA device '{args.record_device}' ..."),
            flush=True,
        )
        transcriber.record_audio(command_audio_path, args.record_seconds, args.record_device)

    elapsed = time.perf_counter() - record_started_at
    print(stage_message("RECORD", f"Command audio saved to {command_audio_path} (elapsed={format_elapsed(elapsed)})"), flush=True)
    return command_audio_path


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


def describe_key(key_value: str) -> str:
    if key_value == " ":
        return "SPACE"
    if key_value in {"\r", "\n"}:
        return "ENTER"
    return key_value.upper()


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