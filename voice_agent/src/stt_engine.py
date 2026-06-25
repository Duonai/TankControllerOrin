from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
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


class WhisperCppTranscriber:
    def __init__(self, config: WhisperConfig) -> None:
        self.config = config

    def ensure_ready(self) -> None:
        if not self.config.whisper_bin.exists():
            raise SttError(f"whisper binary not found: {self.config.whisper_bin}")
        if not self.config.model_path.exists():
            raise SttError(f"whisper model not found: {self.config.model_path}")
        if shutil.which("ffmpeg") is None:
            raise SttError("ffmpeg not found in PATH")

    def record_audio(self, output_path: Path, seconds: float, device: str = "default") -> Path:
        if shutil.which("arecord") is None:
            raise SttError("arecord not found in PATH")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = max(1, math.ceil(seconds))
        command = [
            "arecord",
            "-D",
            device,
            "-d",
            str(duration_seconds),
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            str(output_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise SttError(exc.stderr.strip() or exc.stdout.strip() or "audio recording failed") from exc

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

    def transcribe_file(self, input_audio_path: Path) -> str:
        self.ensure_ready()

        if not input_audio_path.exists():
            raise SttError(f"audio file not found: {input_audio_path}")

        with tempfile.TemporaryDirectory(prefix="voice-stt-") as temp_dir:
            temp_root = Path(temp_dir)
            normalized_audio = temp_root / "normalized.wav"
            output_prefix = temp_root / "transcript"

            self._normalize_audio(input_audio_path, normalized_audio)

            command = [
                str(self.config.whisper_bin),
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
                "-f",
                str(normalized_audio),
                "-nt",
                "-np",
                "-otxt",
                "-of",
                str(output_prefix),
            ]

            if self.config.no_fallback:
                command.append("-nf")

            if self.config.prompt.strip():
                command.extend(["--prompt", self.config.prompt.strip()])

            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as exc:
                raise SttError(exc.stderr.strip() or exc.stdout.strip() or "speech transcription failed") from exc

            transcript_path = output_prefix.with_suffix(".txt")
            if not transcript_path.exists():
                raise SttError(f"transcript output not found: {transcript_path}")

            transcript = transcript_path.read_text(encoding="utf-8").strip()
            transcript = " ".join(part for part in transcript.split())
            if not transcript:
                raise SttError("transcript is empty")

            return transcript

    def _normalize_audio(self, input_audio_path: Path, output_audio_path: Path) -> None:
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
