---
name: continue-ai-vtuber-phase5
description: Continue, debug, validate, or hand off this repository's Phase 5 AI VTuber orchestration and real-device acceptance. Use when work involves the Twitch-to-LLM-to-VTS-to-TTS pipeline, phase5-smoke, the one-hour acceptance, or deciding whether Phase 5 is complete; do not use for unrelated Python edits or Phase 6/7 work.
---

# Continue AI VTuber Phase 5

Preserve the verified Phase 1-4 components while finishing Phase 5 integration and real-device
acceptance truthfully and safely.

## Load the project context

1. Work from the repository root and inspect `git status --short --branch` plus the recent log.
2. Read `docs/phase-5-handoff-to-codex.md` completely.
3. Read `docs/phase-5-result.md` and `docs/phase-5-orchestration.md`.
4. Read only the relevant sections of `docs/phase-5-work-log.md`, Phase 3/4 documents, tests, and
   source files needed for the current issue. Treat the handoff's commit IDs and runtime state as a
   historical snapshot; verify mutable facts locally.

## Preserve these invariants

- Keep Twitch EventSub ingestion non-blocking and independent from downstream failures.
- Keep the message queue bounded, TTL-aware, priority-aware, and cooldown-aware.
- Permit only one primary TTS synthesis/playback at a time. A preempted native task must actually
  stop or enter explicit quarantine before a new one starts.
- Never retract, cancel, or resend a Twitch reply after the Helix send request crosses its commit
  point.
- Reject self-messages so the bot cannot create a reply loop.
- Send speech/actions onward only after schema, Traditional Chinese, emotion, and action validation.
  An action must exist in both `llm.allowed_actions` and the current model's local action mapping.
- Never invent VTS resources. Surface missing mappings through `missing_resources`.
- Always clean up audio, subtitles, MouthOpen, reactions, state, and quarantined work on success,
  cancellation, timeout, disconnect, model reload, and shutdown.
- Keep text-reply failure isolation: a validated chat reply may still be sent when TTS fails, while
  LLM/VTS/TTS failures must not stop Twitch message intake.

## Protect local data and licensing

- Check secrets and local resources with existence/metadata checks only. Never print or open the
  contents of Twitch/VTS tokens, DPAPI blobs, or the llama-server API key; never place them in logs,
  reports, prompts, commits, or chat.
- Keep `.local/`, model weights, `config/actions.local.yaml`, generated audio, and real-device
  benchmark files untracked.
- Use eSpeak NG only. Do not download or enable MeloTTS Chinese or another human/character voice
  until the user explicitly confirms a completed rights review.
- Do not start a public stream. Use a dedicated Twitch test channel and a second account for input.
- Do not add OBS, extra EventSub features, long-term memory, or other Phase 6/7 scope unless the user
  explicitly requests it after Phase 5 acceptance.

## Validate changes offline

Run the focused tests while iterating, then finish with:

```powershell
.\.venv\Scripts\python.exe -m pytest --tb=short
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

The handoff baseline is 162 passing tests. A newer total is acceptable when all tests pass. If the
offline suite fails, do not proceed to real-device acceptance or claim the phase is complete.

## Run real-device acceptance only when requested and ready

Before connecting, verify without exposing contents:

- `.local/secrets/vts-token.json`
- `.local/secrets/twitch-token.bin`
- `.local/secrets/llama-server-api-key.txt`
- `config/actions.local.yaml`
- `.local/runtime/llama.cpp/llama-server.exe`
- `models/gemma-4-12b-it-qat-q4_0.gguf`
- `.local/runtime/espeak-ng/eSpeak NG/espeak-ng.exe`
- `.local/runtime/espeak-ng/eSpeak NG/espeak-ng-data`

Also verify that VTube Studio is running with the calibrated NightRain model, llama-server is running
with the Phase 3 settings, its PID is known, the exact authorized test-channel login is supplied, and
a second account can send test messages after the command reports `listening`.

Run stages in order, stopping on any `blocked`, `failed`, or `timed_out` result:

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "<login>" --messages 1 --timeout 600 `
  --server-pid <pid> --output .local\benchmarks\phase5-single.json

.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "<login>" --messages 5 --timeout 900 `
  --server-pid <pid> --output .local\benchmarks\phase5-continuous.json

.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "<login>" --messages 60 --timeout 4200 `
  --server-pid <pid> --output .local\benchmarks\phase5-one-hour.json
```

Distribute the 60 messages across at least 3600 seconds. Sending them in a burst does not satisfy the
one-hour criterion.

## Decide and report acceptance

Declare Phase 5 complete only when the real report shows all of the following:

- `status == "passed"`
- `phase5_hour_acceptance == "passed"`
- `completed_turns == requested_turns`
- `measurements_complete == true`
- elapsed time is at least 3600 seconds
- first-token, decision, PCM-start, and playback-complete timing values are present and ordered
- system RAM, llama-server working set, and GPU VRAM samples are present; GPU utilization should
  be reported when available but is not currently a `measurements_complete` hard requirement
- VTS and MouthOpen have no degradation, rejected action, disconnect, or restore failure
- no unhandled error remains

After each run, keep both the generated JSON and Traditional Chinese Markdown report. Summarize the
outcome first in a compact table: offline tests, single-turn, continuous, one-hour, latency/resources,
and current Phase 5 verdict. Preserve failure evidence and distinguish `not run` from `failed`.

Update `docs/phase-5-result.md` and the README completion statement only after the report proves the
new state. Add technical details to `docs/phase-5-work-log.md`; never rewrite a blocked or failed run
as success.
