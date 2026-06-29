# STT Implementation Status and Qwen Tuning Plan

## 2026-06-29 CUDA Default Update

This section is the current authoritative status for the active `TankControllerOrin` repository. Older notes later in this file include historical prototype details.

Current repository root for the active implementation:

- `/home/usr1/TankControllerOrin`

Current default STT binary selection order in `voice_agent/run_voice_to_qwen.py`:

1. `/home/usr1/TankControllerOrin/whisper.cpp/build-cuda-orin/bin/whisper-cli`
2. `/home/usr1/TankControllerOrin/whisper.cpp/build-cuda/bin/whisper-cli`
3. `/home/usr1/TankControllerOrin/whisper.cpp/build/bin/whisper-cli`

This matters because `WhisperCppTranscriber` derives `whisper-server` from the selected `whisper-cli` sibling path, so the default STT path now uses the CUDA server automatically when the Orin CUDA build exists.

Validated CUDA build command:

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

Validated measurements on `/home/usr1/TankControllerOrin/voice_agent/audio/last_command.wav`:

- CPU CLI: about `2.15s`
- CUDA CLI: about `1.15s`
- CUDA resident server first request: about `0.532s`
- CUDA resident server warm request: about `0.225s`
- Default STT path after the code change: about `0.97s`

Validated transcript for CPU and CUDA runs:

- `뒤로 1초간 후진.`

## Scope Status

The STT-to-Qwen prototype for the Orin board is complete enough to treat as the current baseline.

Current covered path:

1. Record microphone audio from the current default ALSA/PulseAudio input.
2. Optionally stop recording early after speech ends.
3. Convert the recorded audio to text with local `whisper.cpp`.
4. Send the transcript to local Qwen served by `llama-server`.
5. Print the raw Qwen output and parsed JSON result.

Current non-goals for this stage:

- no TankController command dispatch yet
- no continuous trigger listening yet
- no streaming partial STT yet

## Implemented Files

- `/home/usr1/bootcamp_ws/voice_agent/run_voice_to_qwen.py`
- `/home/usr1/bootcamp_ws/voice_agent/src/stt_engine.py`
- `/home/usr1/bootcamp_ws/voice_agent/src/qwen_client.py`
- `/home/usr1/bootcamp_ws/voice_agent/config/command_prompt.md`

## Current STT Runtime Choice

### Selected stack

- STT runtime: `whisper.cpp`
- default model: `ggml-small.bin`
- default profile: `fast`

### Why this was selected

- better deployment stability on Jetson/ARM than heavier Python-native stacks
- Korean multilingual support is available
- smaller model gives much lower latency for short command phrases
- local/offline operation matches the overall system design

### Installed model assets

- fast default: `/home/usr1/bootcamp_ws/whisper.cpp/models/ggml-small.bin`
- accuracy fallback: `/home/usr1/bootcamp_ws/whisper.cpp/models/ggml-medium.bin`

## Current Qwen Output Lock

The current Qwen command parser is now deliberately narrowed to a small test schema aligned with the `run_rpi2_turret.py` style of JSON use.

Current output contract:

```json
{
  "role": "player2_turret",
  "result": {
    "command": "reload"
  }
}
```

Allowed command values right now:

- `reload`
- `scanning`
- `reject`

Current semantic mapping:

- any voice intent containing or implying reload/rearm/reload-magazine meaning -> `reload`
- any voice intent containing or implying scan/radar/search-enemy-position meaning -> `scanning`
- anything else -> `reject`

Implementation files:

- prompt: `/home/usr1/bootcamp_ws/voice_agent/config/command_prompt.md`
- schema: `/home/usr1/bootcamp_ws/voice_agent/config/command_schema.json`
- runtime validation: `/home/usr1/bootcamp_ws/voice_agent/src/qwen_client.py`

## Current STT Profiles

### `fast`

- model: `small`
- threads: `12`
- `best_of=1`
- `beam_size=1`
- fallback: disabled
- command vocabulary prompt: enabled by default

Recommended for:

- short Korean voice commands
- interactive latency-sensitive runs

### `balanced`

- model: `small`
- `best_of=2`
- `beam_size=2`

Recommended for:

- when fast mode is slightly too unstable but `medium` is too slow

### `accurate`

- model: `medium`
- `best_of=5`
- `beam_size=5`
- fallback: enabled

Recommended for:

- evaluation runs
- difficult audio
- checking whether a recognition issue is caused by the fast STT profile

## Recording Modes

### Fixed recording mode

Example:

```bash
cd /home/usr1/bootcamp_ws
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --record-seconds 5 \
  --language ko
```

This records for the full requested duration before STT starts.

### Auto-stop recording mode

Example:

```bash
cd /home/usr1/bootcamp_ws
/home/usr1/.local/share/uv/python/cpython-3.14.3-linux-aarch64-gnu/bin/python \
  voice_agent/run_voice_to_qwen.py \
  --record-seconds 5 \
  --auto-stop \
  --silence-stop-seconds 0.6 \
  --language ko
```

This uses:

- speech start detection
- trailing silence detection
- early stop before the max recording duration

Current useful knobs:

- `--silence-stop-seconds`
- `--speech-start-threshold`
- `--speech-end-threshold`
- `--min-speech-seconds`

## Timestamped Runtime Visibility

The pipeline now prints timestamps and stage durations for:

- pipeline start
- recording start
- recording end
- STT start
- STT end
- Qwen request start
- Qwen response received
- pipeline total finish time

This makes it easy to separate delay caused by:

- recording duration
- STT latency
- Qwen latency

## Observed Performance

These measurements are approximate and based on current local tests.

### Before STT optimization

- `medium` model STT on a short command file: about `11.6s`
- end-to-end after recording felt too slow for interactive use

### After STT optimization

- `small + fast decode` on the same command file: about `3.6s`
- `small + 12 threads + command prompt`: about `2.2s` STT in one test
- file-based `STT -> Qwen`: about `5.1s`

### Auto-stop example

One validated run produced roughly:

- record: `3.4s` out of a `5s` max window
- STT: `2.3s`
- Qwen: `2.4s`
- total: `8.1s`

Meaning:

- the main easy win came from shrinking STT latency
- the next easy win came from cutting wasted recording time with auto-stop

## Practical Limit of the Current Architecture

There is still a hard sequencing cost:

1. record audio
2. finish recording
3. run STT on completed audio
4. send final transcript to Qwen

Because of this, the current implementation is good as a baseline but not the final low-latency architecture.

## What Is the Most Realistic Next Step for Qwen Tuning?

The next problem is not STT anymore. It is making Qwen reliably transform noisy Korean command text into the exact structured command format the system wants.

The most realistic path is:

1. prompt and schema control first
2. evaluation set second
3. data collection from real STT transcripts third
4. only then consider LoRA fine-tuning

## Recommended Qwen Tuning Strategy

### Phase 1. Prompt-first tuning

This should be the default strategy first.

Use:

- one versioned system prompt file
- strict output contract
- explicit action catalog
- explicit reject behavior
- many concrete examples

Why:

- cheapest to iterate
- easiest to debug
- no retraining pipeline required
- likely sufficient for a narrow command domain

### Practical recommendation

Split the prompt design into multiple files instead of one long ad hoc prompt.

Recommended future structure:

```text
voice_agent/
  config/
    command_prompt.md
    command_schema.json
    action_catalog.md
    command_examples.jsonl
```

Where:

- `command_prompt.md`: high-level rules and behavior
- `command_schema.json`: exact JSON contract
- `action_catalog.md`: allowed action names and parameter semantics
- `command_examples.jsonl`: input/output examples for regression testing and future fine-tuning

### Phase 2. Enforce output structure harder

Prompt-only JSON instructions are useful, but not the final reliability layer.

The next realistic improvement is to constrain Qwen output with:

- JSON schema validation after response
- reject if invalid
- optional grammar/schema-constrained generation if supported cleanly by the current server path

Goal:

- Qwen should never emit arbitrary prose in the command path
- if the output is invalid, the system should fail closed

### Phase 3. Tune against real STT noise, not clean text

The model should be tuned using the actual imperfect transcripts produced by the microphone and STT stack.

This matters because the real problem is not:

- perfect Korean text -> command JSON

It is:

- noisy Korean STT text -> command JSON

So build a dataset like:

```json
{"transcript": "전진 오십 센치", "target": {"command_kind": "simple_motion", "target_role": "player1_tracks", "action": "move_forward", "params": {"distance_cm": 50}}}
{"transcript": "포탑 오른쪽 십오도", "target": {"command_kind": "simple_motion", "target_role": "player2_turret", "action": "yaw_right", "params": {"angle_deg": 15}}}
{"transcript": "조금만 움직여", "target": {"command_kind": "reject", "target_role": "system", "action": "reject", "params": {"reason": "ambiguous_command"}}}
```

### Phase 4. Only use LoRA if prompt+schema is not enough

LoRA fine-tuning is realistic, but it should not be the first move.

Use LoRA only if:

- the prompt is already good
- the action catalog is stable
- invalid/ambiguous outputs still happen too often
- you have real transcript/target pairs to train on

Recommended LoRA target:

- instruction tuning for transcript-to-JSON conversion only
- not general conversation
- train on real noisy transcript pairs from this system

Why this is the best tradeoff:

- smaller training cost than full fine-tuning
- more realistic than trying to “teach” the model everything with only a prompt
- still maintainable if the command set evolves slowly

## Best Practical Recommendation Right Now

The best next step is not immediate model fine-tuning.

The best realistic next step is:

1. freeze the command action catalog
2. strengthen the prompt into a versioned command parser spec
3. add machine validation of Qwen output
4. collect real transcript -> target pairs from actual microphone runs
5. evaluate failure cases
6. only then decide whether LoRA is necessary

## Concrete Tuning Plan for the Next Iteration

### Step 1

Define the exact allowed outputs.

Need:

- action names
- target roles
- required params per action
- reject reasons
- confirmation rules

### Step 2

Expand the prompt with many examples.

Examples should include:

- correct direct commands
- shorthand commands
- STT spelling noise
- ambiguous commands
- dangerous commands requiring confirmation

### Step 3

Add response validation.

Even if Qwen returns invalid JSON or unsupported fields, the system must reject the result rather than trying to guess.

### Step 4

Build an evaluation set.

For example:

- 50 to 200 real spoken command transcripts
- expected target JSON
- pass/fail comparison by exact action and params

### Step 5

Decide on LoRA only after the prompt baseline is measured.

If prompt+schema reaches acceptable reliability, stop there.

If not, train LoRA on transcript-to-command examples.

## Bottom Line

For this project, the best tuning strategy is:

- not full model tuning first
- not generic chatbot prompting
- not hand-wavy natural language parsing

Instead:

- strict prompt
- strict schema
- strict validation
- real STT transcript dataset
- LoRA only if the prompt baseline is insufficient

That is the most practical, lowest-risk, and most maintainable way to make Qwen produce the exact outputs needed for downstream control.