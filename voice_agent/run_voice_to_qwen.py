from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from src.qwen_client import QwenClient, QwenConfig, QwenError, load_prompt, load_schema
from src.stt_engine import SttError, WhisperConfig, WhisperCppTranscriber
from src.tank_command_sender import TankCommandSendError, send_tank_command


DEFAULT_STT_PROMPT = "마이크작동. 음성 명령. 전진. 후진. 정지. 좌회전. 우회전. 포탑. 발사. 안전 모드."


def build_arg_parser() -> argparse.ArgumentParser:
    root_dir = Path(__file__).resolve().parent
    workspace_dir = root_dir.parent

    parser = argparse.ArgumentParser(description="Record or load audio, transcribe it, and send the transcript to local Qwen.")
    parser.add_argument("--audio-file", default="", help="Existing audio file path to transcribe")
    parser.add_argument(
        "--record-seconds",
        type=float,
        default=0.0,
        help="If no audio file is provided, record this many seconds; with --auto-stop, this is the maximum recording time",
    )
    parser.add_argument("--record-device", default="default", help="ALSA capture device passed to arecord")
    parser.add_argument("--auto-stop", action="store_true", help="Stop microphone recording early after speech ends and silence is detected")
    parser.add_argument("--silence-stop-seconds", type=float, default=1.0, help="When --auto-stop is enabled, stop after this much trailing silence")
    parser.add_argument("--speech-start-threshold", type=int, default=700, help="RMS threshold to detect speech start in auto-stop mode")
    parser.add_argument("--speech-end-threshold", type=int, default=400, help="RMS threshold below which audio counts as silence after speech start")
    parser.add_argument("--min-speech-seconds", type=float, default=0.3, help="Minimum detected speech before trailing silence can stop recording")
    parser.add_argument(
        "--record-output",
        default=str(root_dir / "audio" / "last_command.wav"),
        help="Path where recorded audio will be saved",
    )
    parser.add_argument(
        "--whisper-bin",
        default=str(workspace_dir / "whisper.cpp" / "build" / "bin" / "whisper-cli"),
        help="Path to whisper.cpp CLI binary",
    )
    parser.add_argument(
        "--whisper-model",
        default="",
        help="Path to whisper.cpp model",
    )
    parser.add_argument("--language", default="ko", help="Whisper language code")
    parser.add_argument("--threads", type=int, default=12, help="Whisper CPU thread count")
    parser.add_argument(
        "--stt-profile",
        choices=["fast", "balanced", "accurate"],
        default="fast",
        help="Preset for STT latency versus accuracy",
    )
    parser.add_argument("--best-of", type=int, default=0, help="Override whisper best-of value; 0 keeps the profile default")
    parser.add_argument("--beam-size", type=int, default=0, help="Override whisper beam size; 0 keeps the profile default")
    parser.add_argument("--stt-prompt", default=DEFAULT_STT_PROMPT, help="Initial prompt to bias STT toward command vocabulary")
    parser.add_argument("--disable-stt-prompt", action="store_true", help="Disable the default STT prompt bias")
    parser.add_argument("--allow-fallback", action="store_true", help="Allow whisper temperature fallback even in fast profiles")
    parser.add_argument("--qwen-url", default="http://127.0.0.1:8080", help="Base URL of local llama-server")
    parser.add_argument("--qwen-model", default="qwen-local", help="Model field sent to the OpenAI-compatible API")
    parser.add_argument(
        "--prompt-file",
        default=str(root_dir / "config" / "command_prompt.md"),
        help="System prompt file for command normalization",
    )
    parser.add_argument(
        "--schema-file",
        default=str(root_dir / "config" / "command_schema.json"),
        help="JSON schema file used to validate the Qwen command output",
    )
    parser.add_argument("--send-command", action="store_true", help="Send non-reject Qwen output to the TankController TCP server")
    parser.add_argument(
        "--tank-config",
        default=str(workspace_dir / "TankControllerRasberryPi" / "config" / "runtime_config.json"),
        help="TankController runtime_config.json path",
    )
    parser.add_argument(
        "--tank-node",
        default="player3_voice",
        help="Node name used to resolve sender host/port/device_id from runtime_config.json",
    )
    parser.add_argument("--tank-profile", default="", help="Optional TankController network profile override")
    parser.add_argument("--text", default="", help="Debug-only shortcut to skip STT and send text directly to Qwen")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    return run_single_pass(args)


def run_single_pass(args: argparse.Namespace) -> int:
    run_started_at = time.perf_counter()
    print(stage_message("RUN", "Pipeline started"), flush=True)

    try:
        if args.text.strip():
            transcript = args.text.strip()
            print(stage_message("INPUT", "Using provided text, skipping STT"), flush=True)
        else:
            transcript = transcribe_from_args(args)

        process_transcript(args, transcript)

        total_elapsed = time.perf_counter() - run_started_at
        print()
        print(stage_message("RUN", f"Pipeline finished (total={format_elapsed(total_elapsed)})"), flush=True)
        return 0
    except (SttError, QwenError, TankCommandSendError, FileNotFoundError, ValueError) as exc:
        total_elapsed = time.perf_counter() - run_started_at
        print(stage_message("ERROR", f"{exc} (after {format_elapsed(total_elapsed)})"), flush=True)
        return 1


def transcribe_from_args(args: argparse.Namespace) -> str:
    transcriber, _, _, _, _ = build_transcriber(args)

    if args.audio_file:
        audio_path = Path(args.audio_file)
        print(stage_message("AUDIO", f"Using existing file: {audio_path}"), flush=True)
    else:
        if args.record_seconds <= 0:
            raise ValueError("provide --audio-file or set --record-seconds to a positive value")
        record_started_at = time.perf_counter()
        if args.auto_stop:
            print(
                stage_message(
                    "RECORD",
                    f"Auto-stop recording started max={args.record_seconds:g}s device='{args.record_device}' "
                    f"silence_stop={args.silence_stop_seconds:g}s",
                ),
                flush=True,
            )
            audio_path = transcriber.record_audio_auto_stop(
                Path(args.record_output),
                max_seconds=args.record_seconds,
                device=args.record_device,
                silence_stop_seconds=args.silence_stop_seconds,
                speech_start_threshold=args.speech_start_threshold,
                speech_end_threshold=args.speech_end_threshold,
                min_speech_seconds=args.min_speech_seconds,
            )
        else:
            print(
                stage_message("RECORD", f"Recording for {args.record_seconds:g}s from ALSA device '{args.record_device}' ..."),
                flush=True,
            )
            audio_path = transcriber.record_audio(Path(args.record_output), args.record_seconds, args.record_device)
        record_elapsed = time.perf_counter() - record_started_at
        print(
            stage_message("RECORD", f"Saved audio to {audio_path} (elapsed={format_elapsed(record_elapsed)})"),
            flush=True,
        )

    return transcribe_audio_path(args, transcriber, audio_path)


def build_transcriber(args: argparse.Namespace) -> tuple[WhisperCppTranscriber, Path, int, int, bool]:
    model_path, best_of, beam_size, no_fallback = resolve_stt_profile(args)
    stt_prompt = "" if args.disable_stt_prompt else args.stt_prompt

    transcriber = WhisperCppTranscriber(
        WhisperConfig(
            whisper_bin=Path(args.whisper_bin),
            model_path=model_path,
            language=args.language,
            threads=max(args.threads, 1),
            best_of=best_of,
            beam_size=beam_size,
            no_fallback=no_fallback,
            prompt=stt_prompt,
        )
    )

    return transcriber, model_path, best_of, beam_size, no_fallback


def transcribe_audio_path(args: argparse.Namespace, transcriber: WhisperCppTranscriber, audio_path: Path) -> str:
    _, model_path, best_of, beam_size, no_fallback = build_transcriber(args)
    stt_started_at = time.perf_counter()
    print(
        stage_message(
            "STT",
            f"Transcribing with model {model_path} profile={args.stt_profile} "
            f"threads={max(args.threads, 1)} best_of={best_of} beam_size={beam_size} "
            f"fallback={'on' if not no_fallback else 'off'}",
        ),
        flush=True,
    )
    transcript = transcriber.transcribe_file(audio_path)
    stt_elapsed = time.perf_counter() - stt_started_at
    print(stage_message("STT", f"Transcription finished (elapsed={format_elapsed(stt_elapsed)})"), flush=True)
    return transcript


def process_transcript(args: argparse.Namespace, transcript: str) -> dict[str, object]:
    system_prompt = load_prompt(Path(args.prompt_file))
    command_schema = load_schema(Path(args.schema_file))
    qwen_started_at = time.perf_counter()
    print(
        stage_message("QWEN", f"Sending transcript to {args.qwen_url.rstrip('/')}/v1/chat/completions"),
        flush=True,
    )

    client = QwenClient(
        QwenConfig(
            base_url=args.qwen_url,
            model_name=args.qwen_model,
        )
    )
    result = client.normalize_command(transcript, system_prompt, command_schema)
    qwen_elapsed = time.perf_counter() - qwen_started_at

    print(stage_message("STT", "Transcript ready"), flush=True)
    print(result["transcript"])
    print()
    print(stage_message("QWEN", f"Raw content received (elapsed={format_elapsed(qwen_elapsed)})"), flush=True)
    print(result["content"])
    if result["parsed_json"] is not None:
        print()
        print(stage_message("QWEN", "Parsed JSON"), flush=True)
        print(json.dumps(result["parsed_json"], ensure_ascii=False, indent=2))
    else:
        print()
        print(stage_message("QWEN", "Parsed JSON"), flush=True)
        print("null")

    maybe_send_tank_command(args, result)
    return result


def resolve_stt_profile(args: argparse.Namespace) -> tuple[Path, int, int, bool]:
    root_dir = Path(__file__).resolve().parent
    workspace_dir = root_dir.parent

    if args.stt_profile == "accurate":
        default_model = workspace_dir / "whisper.cpp" / "models" / "ggml-medium.bin"
        default_best_of = 5
        default_beam_size = 5
        default_no_fallback = False
    elif args.stt_profile == "balanced":
        default_model = workspace_dir / "whisper.cpp" / "models" / "ggml-small.bin"
        default_best_of = 2
        default_beam_size = 2
        default_no_fallback = True
    else:
        default_model = workspace_dir / "whisper.cpp" / "models" / "ggml-small.bin"
        default_best_of = 1
        default_beam_size = 1
        default_no_fallback = True

    model_path = Path(args.whisper_model) if args.whisper_model else default_model
    best_of = args.best_of if args.best_of > 0 else default_best_of
    beam_size = args.beam_size if args.beam_size > 0 else default_beam_size
    no_fallback = False if args.allow_fallback else default_no_fallback

    return model_path, best_of, beam_size, no_fallback


def maybe_send_tank_command(args: argparse.Namespace, result: dict[str, object]) -> None:
    parsed_json = result.get("parsed_json")
    if not args.send_command:
        print(stage_message("SEND", "Skipped TankController send; --send-command not enabled"), flush=True)
        return

    if not isinstance(parsed_json, dict):
        raise TankCommandSendError("cannot send command because parsed_json is missing or invalid")

    result_object = parsed_json.get("result")
    if not isinstance(result_object, dict):
        raise TankCommandSendError("cannot send command because Qwen result object is invalid")

    command_name = result_object.get("command")
    if command_name == "reject":
        print(stage_message("SEND", "Skipped TankController send because command=reject"), flush=True)
        return

    send_started_at = time.perf_counter()
    print(
        stage_message(
            "SEND",
            f"Sending command to TankController using node={args.tank_node} profile={args.tank_profile or 'config-default'}",
        ),
        flush=True,
    )
    send_info = send_tank_command(parsed_json, Path(args.tank_config), args.tank_node, args.tank_profile)
    send_elapsed = time.perf_counter() - send_started_at
    print(
        stage_message(
            "SEND",
            f"Command sent to {send_info['host']}:{send_info['port']} device_id={send_info['device_id']} "
            f"(elapsed={format_elapsed(send_elapsed)})",
        ),
        flush=True,
    )


def stage_message(stage: str, message: str) -> str:
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    return f"[{timestamp}] [{stage}] {message}"


def format_elapsed(seconds: float) -> str:
    return f"{seconds:.3f}s"


if __name__ == "__main__":
    raise SystemExit(main())