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
    "command": "move_forward",
    "data": 1.0
  }
}
```

Currently allowed command values:

- `move_forward`
- `move_backward`
- `pivot_left`
- `pivot_right`
- `reload`
- `scanning`
- `reject`

All outgoing network packets now use role `player3_voice`.
When no command is active, the loop continuously sends `{"command":"none","data":0.0}` at about 10 fps.
Timed movement commands are repeated on that same stream for their active duration.
For body pivot and turret left/right rotation, angle-based commands use the rule `45 degrees = 1.0 second`.
One-shot commands such as `reload` and `scanning` are injected once into that stream.

## Prerequisites

Expected local assets and tools:

- Jetson Orin environment
- local Qwen GGUF model
- built `llama.cpp`
- built `whisper.cpp` CPU or CUDA binaries
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

## Running whisper.cpp with CUDA as Default

The STT entrypoints now prefer the following `whisper.cpp` binaries in this order:

1. `whisper.cpp/build-cuda-orin/bin/whisper-cli`
2. `whisper.cpp/build-cuda/bin/whisper-cli`
3. `whisper.cpp/build/bin/whisper-cli`

That means no extra STT flag is needed once the Orin CUDA build exists.

Build the Orin-specific CUDA version:

```bash
cd /home/usr1/TankControllerOrin/whisper.cpp
export PATH="/usr/local/cuda/bin:$PATH"
cmake -S . -B build-cuda-orin \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DWHISPER_BUILD_SERVER=ON \
  -DWHISPER_BUILD_TESTS=OFF \
  -DWHISPER_BUILD_EXAMPLES=ON
cmake --build build-cuda-orin -j12
```

Validated local measurements on `voice_agent/audio/last_command.wav`:

- CPU CLI: about `2.15s`
- CUDA CLI: about `1.15s`
- CUDA resident server first request: about `0.53s`
- CUDA resident server warm request: about `0.23s`

## Running Voice Command Parsing

Text-only test:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --text '1초간 전진해'
```

This now uses the CUDA STT build by default if `whisper.cpp/build-cuda-orin/bin/whisper-cli` exists.

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

To force CPU STT for comparison:

```bash
cd /home/usr1/TankControllerOrin
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --record-seconds 5 \
  --auto-stop \
  --silence-stop-seconds 0.6 \
  --language ko \
  --whisper-bin /home/usr1/TankControllerOrin/whisper.cpp/build/bin/whisper-cli
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
  --stream-hz 10 \
  --tank-profile local
```

Behavior of the standby loop:

- Reuse an already-running Qwen server if `/health` is available.
- Otherwise start `llama-server` locally when `--start-qwen` is enabled.
- Start a persistent `player3_voice` TCP stream immediately when `--send-command` is enabled.
- Continuously send idle packets with `command=none` and `data=0.0` at the configured stream rate.
- Wait for an immediate keyboard trigger instead of running STT for wake-word detection.
- In `auto` mode the program prefers a physical Linux keyboard event device and falls back to terminal input only if no readable keyboard event device is available.
- Some mini keyboards expose multiple event nodes. `auto` mode now listens to all readable keyboard event devices it finds.
- You can force a specific attached keyboard with `--trigger-input-mode event --trigger-event-device /dev/input/event7` or give multiple devices like `/dev/input/event7,/dev/input/event8`.
- When the trigger key is pressed, print a clear log message and terminal bell, then start command capture.
- When Qwen emits a timed movement command such as `move_forward`, keep sending that command on the persistent stream for the derived duration.
- When Qwen emits a one-shot command such as `reload` or `scanning`, send it once on the persistent stream and then return to idle packets.
- Wait until Qwen parsing and command queueing finish, then return to standby mode.
- If TankController sending fails, log the send error and return to standby mode without terminating the loop.
- Press the quit key to leave standby mode cleanly.

If the physical keyboard event device is not readable by the current user, add the user to the `input` group and re-login:

```bash
sudo usermod -aG input usr1
```

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
  --text '왼쪽으로 90도 회전해' \
  --send-command \
  --tank-profile local
```

## Repository Notes

- This repository root is `TankControllerOrin`.
- Project name is `TankControllerOrin`.
- Git itself does not store a separate human-readable repository name in local metadata, so the project name is represented through this README and repository structure.
