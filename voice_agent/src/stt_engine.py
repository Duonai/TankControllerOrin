from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path


class SttError(RuntimeError):
    pass


@dataclass(slots=True)
class WhisperConfig:
    whisper_bin: Path
    model_path: Path
    language: str = "ko"
    threads: int = 12
    best_of: int = 1
    beam_size: int = 1
    no_fallback: bool = True
    prompt: str = ""


_SERVER_BACKENDS: dict[tuple[str, ...], "WhisperServerBackend"] = {}


class WhisperServerBackend:
    def __init__(self, config: WhisperConfig) -> None:
        self.config = config
        self.server_bin = config.whisper_bin.parent / "whisper-server"
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None

    def ensure_ready(self) -> None:
        if self.process is not None and self.process.poll() is None and self.port is not None and self._is_healthy(self.port):
            return

        if not self.server_bin.exists():
            raise SttError(f"whisper-server binary not found: {self.server_bin}")

        self.stop()
        self.port = self._find_free_port()

        run_env = os.environ.copy()
        library_dir = str(self.config.whisper_bin.resolve().parent)
        existing_library_path = run_env.get("LD_LIBRARY_PATH", "")
        run_env["LD_LIBRARY_PATH"] = library_dir if not existing_library_path else f"{library_dir}:{existing_library_path}"

        command = [
            str(self.server_bin),
            "-m",
            str(self.config.model_path),
            "-l",
            self.config.language,
            "-t",
            str(self.config.threads),
            "-bo",
            str(max(self.config.best_of, 1)),
            "-bs",
            str(max(self.config.beam_size, 1)),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]

        if self.config.no_fallback:
            command.append("-nf")

        if self.config.prompt.strip():
            command.extend(["--prompt", self.config.prompt.strip()])

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=run_env,
        )

        started_at = time.monotonic()
        while time.monotonic() - started_at < 30.0:
            if self.process.poll() is not None:
                raise SttError("whisper-server exited before becoming ready")
            if self._is_healthy(self.port):
                return
            time.sleep(0.1)

        raise SttError("timed out waiting for whisper-server to become ready")

    def transcribe_file(self, audio_path: Path) -> str:
        if self.port is None:
            raise SttError("whisper-server port is not initialized")

        boundary = "----copilot" + uuid.uuid4().hex
        audio_bytes = audio_path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{audio_path.name}\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
                audio_bytes,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )

        request_object = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        try:
            with urllib.request.urlopen(request_object, timeout=120.0) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SttError(f"whisper-server HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SttError(f"whisper-server connection failed: {exc}") from exc

        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise SttError(f"whisper-server returned invalid JSON: {response_body[:200]}") from exc

        transcript = str(payload.get("text", "")).strip()
        transcript = " ".join(part for part in transcript.split())
        if not transcript:
            raise SttError("transcript is empty")
        return transcript

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        self.process = None
        self.port = None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _is_healthy(port: int) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as response:
                return response.status == 200
        except Exception:
            return False


def _shutdown_server_backends() -> None:
    for backend in list(_SERVER_BACKENDS.values()):
        backend.stop()


atexit.register(_shutdown_server_backends)


class WhisperCppTranscriber:
    def __init__(self, config: WhisperConfig) -> None:
        self.config = config

    def ensure_ready(self) -> None:
        if not self.config.whisper_bin.exists():
            raise SttError(f"whisper binary not found: {self.config.whisper_bin}")
        if not self.config.model_path.exists():
            raise SttError(f"whisper model not found: {self.config.model_path}")

    def record_audio(self, output_path: Path, seconds: float, device: str = "default") -> Path:
        if shutil.which("arecord") is None:
            raise SttError("arecord not found in PATH")
        if seconds <= 0:
            raise SttError("recording duration must be positive")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "arecord",
            "-D",
            device,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "-t",
            "raw",
        ]

        sample_rate = 16000
        chunk_duration_seconds = 0.1
        chunk_samples = int(sample_rate * chunk_duration_seconds)
        chunk_bytes = chunk_samples * 2
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        total_seconds = 0.0
        try:
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)

                while total_seconds < seconds:
                    if process.stdout is None:
                        raise SttError("audio capture stream is unavailable")

                    remaining_seconds = max(seconds - total_seconds, 0.0)
                    target_bytes = chunk_bytes if remaining_seconds >= chunk_duration_seconds else max(2, int(remaining_seconds * sample_rate) * 2)
                    chunk = process.stdout.read(target_bytes)
                    if not chunk:
                        break

                    wav_file.writeframes(chunk)
                    total_seconds += len(chunk) / 2 / sample_rate
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)

        stderr_output = ""
        if process.stderr is not None:
            stderr_output = process.stderr.read().decode("utf-8", errors="replace").strip()

        if process.returncode is None:
            process.wait(timeout=1.0)

        terminated_by_us = "Aborted by signal Terminated" in stderr_output
        has_audio_output = output_path.exists() and output_path.stat().st_size > 44

        if terminated_by_us and has_audio_output:
            stderr_output = ""

        if process.returncode not in (0, -15, 143) and not (terminated_by_us and has_audio_output):
            raise SttError(stderr_output or f"audio recording failed with exit code {process.returncode}")

        if not output_path.exists() or output_path.stat().st_size <= 44:
            raise SttError("recording produced no audio")

        return output_path

    def record_audio_auto_stop(
        self,
        output_path: Path,
        max_seconds: float,
        device: str = "default",
        silence_stop_seconds: float = 1.0,
        speech_start_threshold: int = 700,
        speech_end_threshold: int = 400,
        min_speech_seconds: float = 0.3,
    ) -> Path:
        if shutil.which("arecord") is None:
            raise SttError("arecord not found in PATH")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        sample_rate = 16000
        chunk_duration_seconds = 0.1
        chunk_samples = int(sample_rate * chunk_duration_seconds)
        chunk_bytes = chunk_samples * 2

        command = [
            "arecord",
            "-D",
            device,
            "-f",
            "S16_LE",
            "-r",
            str(sample_rate),
            "-c",
            "1",
            "-t",
            "raw",
        ]

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        total_seconds = 0.0
        speech_seconds = 0.0
        silence_seconds = 0.0
        speech_started = False

        try:
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)

                while total_seconds < max(max_seconds, chunk_duration_seconds):
                    if process.stdout is None:
                        raise SttError("audio capture stream is unavailable")

                    chunk = process.stdout.read(chunk_bytes)
                    if not chunk:
                        break

                    wav_file.writeframes(chunk)
                    duration = len(chunk) / 2 / sample_rate
                    total_seconds += duration

                    rms = compute_pcm16_rms(chunk)

                    if not speech_started and rms >= speech_start_threshold:
                        speech_started = True

                    if speech_started:
                        if rms >= speech_end_threshold:
                            speech_seconds += duration
                            silence_seconds = 0.0
                        else:
                            silence_seconds += duration

                        if speech_seconds >= min_speech_seconds and silence_seconds >= silence_stop_seconds:
                            break
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)

        stderr_output = ""
        if process.stderr is not None:
            stderr_output = process.stderr.read().decode("utf-8", errors="replace").strip()

        if process.returncode is None:
            process.wait(timeout=1.0)

        terminated_by_us = "Aborted by signal Terminated" in stderr_output
        has_audio_output = output_path.exists() and output_path.stat().st_size > 44

        if terminated_by_us and has_audio_output:
            stderr_output = ""

        if process.returncode not in (0, -15, 143) and not (terminated_by_us and has_audio_output):
            raise SttError(stderr_output or f"audio recording failed with exit code {process.returncode}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise SttError("auto-stop recording produced no audio")

        return output_path

    def record_audio_until_stop(
        self,
        output_path: Path,
        stop_event: threading.Event,
        device: str = "default",
        max_seconds: float | None = None,
    ) -> Path:
        if shutil.which("arecord") is None:
            raise SttError("arecord not found in PATH")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "arecord",
            "-D",
            device,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "-t",
            "raw",
        ]

        sample_rate = 16000
        chunk_duration_seconds = 0.1
        chunk_samples = int(sample_rate * chunk_duration_seconds)
        chunk_bytes = chunk_samples * 2
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        total_seconds = 0.0
        try:
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)

                while not stop_event.is_set():
                    if max_seconds is not None and total_seconds >= max_seconds:
                        break

                    if process.stdout is None:
                        raise SttError("audio capture stream is unavailable")

                    chunk = process.stdout.read(chunk_bytes)
                    if not chunk:
                        break

                    wav_file.writeframes(chunk)
                    total_seconds += len(chunk) / 2 / sample_rate
        finally:
            stop_event.set()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)

        stderr_output = ""
        if process.stderr is not None:
            stderr_output = process.stderr.read().decode("utf-8", errors="replace").strip()

        if process.returncode is None:
            process.wait(timeout=1.0)

        terminated_by_us = "Aborted by signal Terminated" in stderr_output
        has_audio_output = output_path.exists() and output_path.stat().st_size > 44

        if terminated_by_us and has_audio_output:
            stderr_output = ""

        if process.returncode not in (0, -15, 143) and not (terminated_by_us and has_audio_output):
            raise SttError(stderr_output or f"audio recording failed with exit code {process.returncode}")

        if not output_path.exists() or output_path.stat().st_size <= 44:
            raise SttError("recording produced no audio")

        return output_path

    def transcribe_file(self, input_audio_path: Path) -> str:
        self.ensure_ready()

        if not input_audio_path.exists():
            raise SttError(f"audio file not found: {input_audio_path}")

        with tempfile.TemporaryDirectory(prefix="voice-stt-") as temp_dir:
            temp_root = Path(temp_dir)
            normalized_audio = temp_root / "normalized.wav"
            audio_path = input_audio_path if self._can_use_audio_file_directly(input_audio_path) else normalized_audio
            if audio_path == normalized_audio:
                self._normalize_audio(input_audio_path, normalized_audio)

            backend = self._get_server_backend()
            backend.ensure_ready()
            return backend.transcribe_file(audio_path)

    def _get_server_backend(self) -> WhisperServerBackend:
        backend_key = (
            str(self.config.whisper_bin.resolve().parent),
            str(self.config.model_path.resolve()),
            self.config.language,
            str(self.config.threads),
            str(max(self.config.best_of, 1)),
            str(max(self.config.beam_size, 1)),
            "1" if self.config.no_fallback else "0",
            self.config.prompt.strip(),
        )

        backend = _SERVER_BACKENDS.get(backend_key)
        if backend is None:
            backend = WhisperServerBackend(self.config)
            _SERVER_BACKENDS[backend_key] = backend
        return backend

    def _can_use_audio_file_directly(self, input_audio_path: Path) -> bool:
        if input_audio_path.suffix.lower() != ".wav":
            return False

        try:
            with wave.open(str(input_audio_path), "rb") as wav_file:
                return (
                    wav_file.getnchannels() == 1
                    and wav_file.getframerate() == 16000
                    and wav_file.getsampwidth() == 2
                    and wav_file.getcomptype() == "NONE"
                )
        except wave.Error:
            return False

    def _normalize_audio(self, input_audio_path: Path, output_audio_path: Path) -> None:
        if shutil.which("ffmpeg") is None:
            raise SttError("ffmpeg not found in PATH")
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output_audio_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise SttError(exc.stderr.strip() or exc.stdout.strip() or "audio normalization failed") from exc


def compute_pcm16_rms(chunk: bytes) -> int:
    if len(chunk) < 2:
        return 0

    sample_count = len(chunk) // 2
    total = 0

    for index in range(0, sample_count * 2, 2):
        sample = int.from_bytes(chunk[index : index + 2], byteorder="little", signed=True)
        total += sample * sample

    return int(math.sqrt(total / sample_count))
