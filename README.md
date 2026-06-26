# TankControllerOrin

`TankControllerOrin` is the Orin-side voice command project for the tank controller system.

This repository focuses on the Jetson Orin workflow:

- local speech recording
- Korean STT with `whisper.cpp`
- local Qwen inference with `llama.cpp`
- fixed JSON command generation
- optional command transmission to the TankController TCP server

This repository does not include the Raspberry Pi runtime project as tracked source. The existing `TankControllerRasberryPi/` directory is treated as an external sibling project and is ignored by this repository.

## Included Project Scope

Main tracked areas:

- `voice_agent/`: Orin-side STT, Qwen parsing, and command sending
- `context1.md`: overall system planning notes
- `context2_stt.md`: STT baseline and Qwen tuning notes
- `scripts.txt`: local llama-server run notes

Ignored large or external components:

- `TankControllerRasberryPi/`
- `Qwen3-4B-Instruct-2507-Q6_K/`
- `llama.cpp/`
- `whisper.cpp/`
- generated audio, caches, logs, and large model/data artifacts

## Current Pipeline

Current validated flow:

1. Record microphone input on the Orin board.
2. Optionally stay in standby mode and wait for a keyboard trigger.
3. Optionally auto-stop after trailing silence.
4. Convert speech to text with `whisper.cpp`.
5. Send transcript to local Qwen running on `llama-server`.
6. Parse fixed JSON output.
7. If command is not `reject`, optionally send it to the TankController TCP server.

## Qwen Output Contract

Current command output is intentionally narrow and fixed.

Example:

```json
{
  "role": "player3_voice",
  "result": {
    "command": "reload"
  }
}
```

Currently allowed command values:

- `reload`
- `scanning`
- `reject`

## Prerequisites

Expected local assets and tools:

- Jetson Orin environment
- local Qwen GGUF model
- built `llama.cpp`
- built `whisper.cpp`
- `ffmpeg`
- `arecord`
- Python executable used in this workspace:
  `/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python`

## Running Qwen Server

Start the local Qwen server:

```bash
cd /home/usr1/TankControllerOrin/llama.cpp
./build/bin/llama-server \
  -m /home/usr1/TankControllerOrin/Qwen3-4B-Instruct-2507-Q6_K/Qwen_Qwen3-4B-Instruct-2507-Q6_K.gguf \
  -ngl 999 \
  -c 8192 \
  -fa auto \
  --host 127.0.0.1 \
  --port 8080
```

## Running Voice Command Parsing

Text-only test:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --text '재장전해'
```

Microphone test with auto-stop:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --record-seconds 5 \
  --auto-stop \
  --silence-stop-seconds 0.6 \
  --language ko
```

Standby loop with keyboard-triggered recording and automatic Qwen startup:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_pipeline.py \
  --start-qwen \
  --record-seconds 5 \
  --auto-stop \
  --silence-stop-seconds 0.6 \
  --trigger-key space \
  --quit-key q \
  --send-command \
  --tank-profile local
```

Behavior of the standby loop:

- Reuse an already-running Qwen server if `/health` is available.
- Otherwise start `llama-server` locally when `--start-qwen` is enabled.
- Wait for an immediate keyboard trigger instead of running STT for wake-word detection.
- When the trigger key is pressed, print a clear log message and terminal bell, then start command capture.
- Wait until Qwen parsing and optional command send finish, then return to standby mode.
- If TankController sending fails, log the send error and return to standby mode without terminating the loop.
- Press the quit key to leave standby mode cleanly.

## Sending Commands to TankController

To send non-reject commands using the TankController TCP transport:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --text '재장전해' \
  --send-command
```

For local loopback testing with a local TankController server:

```bash
cd /home/usr1/TankControllerOrin/TankControllerRasberryPi
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python run_pc_server.py --profile local
```

Then in another terminal:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --text '레이더 작동해서 적 위치 스캔해' \
  --send-command \
  --tank-profile local
```

## Repository Notes

- This repository root is `TankControllerOrin`.
- Project name is `TankControllerOrin`.
- Git itself does not store a separate human-readable repository name in local metadata, so the project name is represented through this README and repository structure.