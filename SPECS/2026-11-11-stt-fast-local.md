# Spec: dictation on a warm local Parakeet model, GPU first when one answers

## Request
- **Verbatim**: "Fix the broken STT feature. It's super slow (is a local model spinning up somewhere?) and there are probably more accurate models available now."
- **Requester**: plink
- **Origin**: backlog #64, 2026-11-11 backlog sprint. Added the same day by plink through the keyboard seat: an RTX 5090 on another machine of the tailnet ("spirit"), not always on; try speech there first and fall back to this server's CPU.

## Agreed UX
Hold (or tap) the mic, speak, release. A grey "Transcribing…" appears beside
the mic button and the text lands in the composer about two seconds later
for a 15 s dictation, with punctuation and capitals. The first dictation
after a server restart is as fast as any other.

When something stops a dictation, the composer's error bar says what: the
microphone was refused, nothing was heard, the button was tapped where it
needed holding, the recording could not be read, the server failed, or the
server took too long (the client gives up after 60 s plus twice the length
of the recording). Starting a new recording clears the bar.

With a GPU machine configured, nothing looks different: the answer comes
from there when it is up, and from this server when it is not.

## What was measured (2026-11-11)

### Why it was slow
The host is an Intel i5-3570K (2012): 4 cores, AVX only (no AVX2, no FMA),
31 GB RAM, no GPU.

The journal held two `/stt` calls in 14 days, both on 2026-11-11:

| | clip | model load | language detect | decode | answer after |
|---|---|---|---|---|---|
| first call since the restart | 2.7 s | 16 s | 30 s, wrong (`nn`, p 0.24) | 36 s | 82 s |
| second call, model warm | 3.1 s | none | 30 s (`en`, p 0.98) | 37 s | 67 s |

- The model was not loaded per request. It loaded on first use and stayed.
  What made it slow was the model and its settings: `WhisperModel("large-v3")`
  with every default. On a CPU the float16 weights are widened to float32,
  beam size is 5, and with no language given Whisper runs its encoder once
  more to guess one. Whisper also pads every clip to a 30 s window, so a 3 s
  clip costs what a 30 s clip costs.
- That load peaks at 12.9 GB of RSS and leaves 8.7 GB resident in the server
  process until the next restart (measured in the bench, same settings).
- Every load calls huggingface.co to check the model revision.
- Decoding already ran in a worker thread; the event loop was not blocked.
- The server restarts on every deploy, so "first use since the restart"
  happens often.
- Client: while it waited, the only sign was the mic glyph turning into "…".
  The request had no timeout. A refused microphone, an empty transcript and
  a tap in hold mode set only the button's `title`, which a touch screen
  never shows: on a phone those failures were silent.

### Candidates
Throwaway venv, Python 3.13, 4 threads, faster-whisper 1.2.1 / ctranslate2
4.8.1, onnx-asr 0.12.0 / onnxruntime 1.27.0 (the versions the production
venv holds), sherpa-onnx 2.0.1, moonshine-voice 0.1.6. Audio: LibriSpeech
test-clean and test-other, re-encoded as the client records (webm/opus,
mono, 32 kbit/s). Timings include decoding the webm.

The box was never quiet: other build hands ran test suites throughout. Each
timing is the best of its repeats and carries, in brackets, how many cores
other processes were using while it ran. A bracket over about 0.4 means the
figure is inflated. CPU-seconds divided by 4 is a rough guide to the quiet
figure. The production journal gives the one truly quiet reading of the old
setup: 67 s.

| candidate | load s | RSS MB loaded / peak | 5 s clip | 15 s clip | 60 s clip | RTF 15 s | CPU-s 15 s | WER timed | WER clean | WER other |
|---|---|---|---|---|---|---|---|---|---|---|
| Whisper large-v3, as deployed (float32, beam 5, language auto) | 33.1 | 12886 / 12886 | 182.8 [2.5] | 226.6 [2.6] | 611.7 [2.7] | 15.43 | 290 | 3.3 (214 w) | - | - |
| Whisper large-v3, int8, `en`, beam 5 | 17.2 | 3362 / 4346 | 52.7 [1.2] | 51.9 [0.8] | 236.2 [1.7] | 3.54 | 143 | 3.3 (214 w) | - | - |
| Whisper large-v3-turbo, int8, `en`, beam 1 | 9.2 | 1981 / 2299 | 35.3 [0.9] | 55.0 [2.1] | 237.0 [2.7] | 3.75 | 100 | 0.0 (214 w) | - | - |
| distil-whisper large-v3.5, int8, `en`, beam 1 | 11.9 | 1861 / 2176 | 67.9 [2.5] | 128.5 [2.5] | 135.7 [1.7] | 8.75 | 194 | 6.5 (214 w) | - | - |
| Whisper small.en, int8, beam 5 | 2.4 | 1008 / 1336 | 4.2 [0.2] | 5.3 [0.1] | 19.5 [0.5] | 0.36 | 18 | 3.3 (449 w) | 3.2 | 6.8 |
| **Parakeet TDT 0.6b v2, fp32** (onnx-asr) | 9.5 | 2210 / 3000 | 0.8 [0.1] | 2.1 [0.2] | 9.3 [0.2] | 0.14 | 8 | 2.7 (449 w) | 1.9 | 3.3 |
| Parakeet TDT 0.6b v2, int8 (onnx-asr) | 8.1 | 745 / 1437 | 5.8 [2.7] | 14.4 [2.9] | 43.8 [2.7] | 0.98 | 16 | 2.4 (449 w) | - | - |
| Parakeet TDT 0.6b v3, fp32 (onnx-asr) | 10.4 | 2257 / 3028 | 0.9 [0.1] | 2.0 [0.2] | 11.3 [0.5] | 0.14 | 7 | 2.4 (449 w) | 2.4 | 3.9 |
| Parakeet TDT 0.6b v3, int8 (onnx-asr) | 10.9 | 752 / 1688 | 1.1 [0.1] | 2.9 [0.2] | 11.5 [0.1] | 0.20 | 11 | 4.2 (449 w) | 2.6 | 6.1 |
| Parakeet Ultra 0.6b, fp32 (onnx-asr) | 9.7 | 2257 / 3028 | 0.8 [0.0] | 2.3 [0.4] | 12.2 [0.7] | 0.16 | 8 | 2.4 (449 w) | 2.5 | 3.4 |
| Parakeet TDT 110m, int8 (sherpa-onnx) | 2.2 | 212 / 1135 | 0.6 [1.0] | 1.7 [1.3] | 5.0 [0.5] | 0.12 | 5 | 3.8 (449 w) | 2.4 | 6.1 |
| Moonshine medium streaming (moonshine-voice) | 1.2 | 759 / 918 | 1.3 [0.1] | 5.1 [0.5] | 28.1 [0.8] | 0.34 | 17 | 5.6 (449 w) | 3.1 | 7.3 |

- **5 s / 15 s / 60 s clip** are seconds of wall clock, warm. **load s** is
  from a cold page cache. **RSS** is after load / peak.
- The Parakeet v2 int8 row was taken while three other cores were busy; the
  v3 int8 row is the quiet reading of the same graph.
- The 60 s column is one pass over the whole clip. The built service cuts at
  30 s (below) and is faster there.
- **WER timed** is over the timed clips (word count in brackets). **WER
  clean / other** is over a second set of 40 + 40 single utterances, 1,702
  words in all; at that size a difference under about one point is noise.
- Whisper large-class models were given one clean clip per length and one
  pass: they are an order of magnitude off the target whatever the detail.
- Normalisation for WER: `whisper-normalizer` 1.0.1 (English), both sides.

Not benchmarked, and why:
- **Parakeet Redux** (moondream, 1.58-bit, "built for CPUs"): its runtime
  needs AVX-512 VNNI on x86. This CPU has AVX only.
- **nvidia/parakeet-unified-en-0.6b** (2026-04): same encoder size, so the
  same speed; its card claims 5.91 against v2's 6.04 average WER. No
  onnx-asr export from a known publisher yet, and its licence is NVIDIA's
  own rather than CC-BY.
- **Canary 1b v2**: an encoder a third larger than Parakeet 0.6b's plus an
  attention decoder, on a CPU that only just makes the target with 0.6b.

Through the service as built (v2 fp32, preloaded, its own log lines):
`audio_s=5.2 decode_s=0.72`, `audio_s=14.7 decode_s=1.79`,
`audio_s=61.5 decode_s=8.32`; the first dictation after the preload took
0.81 s. Peak RSS 2.8 GB.

### Other findings
- **fp32 beats int8 on this CPU.** Without AVX2 the quantized kernels are
  slower than float (v3, 15 s clip: 2.0 s fp32, 2.9 s int8), and int8 lost
  accuracy on the harder set (v3, test-other: 6.1 % against 3.9 %).
- **Parakeet's cost grows faster than the clip.** One pass over 61 s took
  2919 MB peak; 307 s took 6132 MB and ran at half the speed per second of
  audio; 615 s was refused by the model outright. Cut at pauses into pieces
  of at most 30 s, the same 615 s decoded correctly in 3.1 GB, and four 60 s
  clips had the same errors cut (23/636) as whole (23/636).
- **A clip of a few milliseconds comes back as `<unk>` tokens.** Pieces
  under 0.25 s are skipped.
- **The fp32 model cannot load from the Hugging Face cache** with
  onnxruntime 1.27: the cache's files are symlinks into shared blob
  directories and onnxruntime refuses external weights that resolve outside
  the model's directory. It loads from a plain directory, which is also what
  keeps the load off the network.
- **The first decode after a load is slow** (about 3 s extra, contended).
  A two-second throwaway decode at load time absorbs most of it.
- **faster-whisper 1.2.1 breaks on av 19** (`open() got an unexpected
  keyword argument 'metadata_errors'`). An unpinned install picks av 19
  today. The production venv has av 18.0.0; `requirements-ml.txt` now pins
  it.

## Decision
**NVIDIA Parakeet TDT 0.6b v2 (English), fp32, through onnx-asr on the
onnxruntime already installed.**
- It is the only class of candidate that meets the target on this box: a
  15 s clip in about 2 s warm, against 67 s today. Every Whisper-large
  variant (v3, turbo, distil) carries the same 30 s encoder window and
  cannot: turbo, the fastest of them, took 35 s for a 5 s clip.
- The 110m Parakeet is faster still (15 s clip in 1.7 s with a core taken
  by others) but less accurate on the harder set (6.1 % against 3.3 %) and
  needs sherpa-onnx, a second runtime. Not worth either cost.
- Accuracy: 2.6 % on the 80-utterance set. Whisper large-v3 was too slow
  to run on that set here (about 90 minutes); on the three clean timed clips
  it made 7 errors in 214 words where v2 made 3 (turbo made none, at 35 s
  a clip). Published leaderboards put v2 ahead of large-v3 as well, but that
  figure was not re-read today.
- v2 over v3 (25 languages, 3.1 % here): the house dictates in English, v2
  is the better English model here and on the model cards read today
  (NVIDIA's parakeet-unified card lists v2 at 6.04 average WER, moondream's
  card lists v3 at 6.26), and an
  English-only model cannot answer in Norwegian as Whisper did above. v3 is
  one config line away and its files are kept beside v2's.
- v2 over "Parakeet Ultra" (moondream's further-trained v3, 2.9 % here):
  the difference is inside the noise of this test, and the only ONNX export
  is a third party's re-pack. Worth a second look when onnx-asr names it.
- Punctuation and capitals come from the model. Nothing was added.
- It outputs "mister" where Whisper wrote "Mr."; spelled-out forms are how
  this model writes.

**New dependency: `onnx-asr==0.12.0`** (MIT, one 4 MB pure-Python wheel, by
the publisher of the model export). Nothing else is installed: onnxruntime
1.27.0, numpy and av 18.0.0 are already in the production venv as
faster-whisper's own dependencies. The model is CC-BY-4.0, 2.4 GB on disk.

The cost: the server process holds about 2.2 GB once the model is loaded
(2.8 GB peak during a decode), where the old setup held 8.7 GB after the
first dictation.

## Architecture notes
- **`server/app/services/stt.py`**. `LocalTranscriber` is the base of both
  local engines: one model per process, loaded once under a lock, decodes
  one at a time under a second lock in a worker thread, one log line per
  dictation (`stt engine=… model=… audio_s=… decode_s=… total_s=…`; never
  the transcript). `OnnxAsrTranscriber` is the new engine;
  `FasterWhisperTranscriber` stays selectable and is now told the language.
  Both decode the upload themselves with PyAV (`decode_audio`), so an
  unreadable upload is one error (`AudioError`) whatever the engine.
- **Long audio** is cut by `split_at_pauses`: pieces of at most 30 s, each
  cut in the quietest fifth of a second of its second half.
- **Preload**: with `STT_PRELOAD=true` the lifespan starts a background task
  that loads the model off the event loop. A dictation that arrives during
  the load waits on the same lock. A failed preload is logged and the next
  dictation tries the load again.
- **`server/app/routers/stt.py`**: 400 for audio that cannot be decoded,
  503 when the engine fails (the traceback goes to the log, not the body),
  alongside the existing 501, 415 and 401. The body gains `engine`.
- **Client** (`MicButton.tsx`, `api.ts`): the "Transcribing…" label, the
  timeout (`transcribeTimeoutMs`), failures routed to the composer's error
  bar. No use is made of `engine`.
- **Tests**: `server/tests/test_stt.py` (engines faked: one load under
  concurrency, one decode at a time, preload, log content, every error
  path, the remote tier), `test_stt_reference_server.py`,
  `client/tests/stt.test.mjs` through `test_client_stt.py`.

### Config keys (`server/.env`)
| Key | Default | Meaning |
|---|---|---|
| `STT_ENGINE` | `faster_whisper` | `onnx_asr` or `faster_whisper`. |
| `STT_MODEL` | empty | Engine's own default when empty: `nemo-parakeet-tdt-0.6b-v2` / `small`. Was `small`. |
| `STT_MODEL_DIR` | empty | onnx_asr: load from this directory, no network. Empty: the Hugging Face cache (int8 only, see above). |
| `STT_QUANTIZATION` | empty | `int8` selects the quantized files (onnx_asr) or compute type (faster_whisper). Empty is fp32. |
| `STT_LANGUAGE` | `en` | faster_whisper and the remote tier. Empty: auto-detect. |
| `STT_PRELOAD` | `false` | Load the model at startup, in the background. |
| `STT_REMOTE_URL` | empty | Base URL of a speech server to try first. Empty: no remote tier. |
| `STT_REMOTE_MODEL` | `large-v3` | Sent as `model`. |
| `STT_REMOTE_TOKEN` | empty | Sent as a bearer token when set. Never logged. |
| `STT_REMOTE_TIMEOUT` | `20` | Seconds for the whole remote request. |
| `STT_REMOTE_CONNECT_TIMEOUT` | `0.5` | Seconds to connect. |
| `STT_REMOTE_COOLDOWN` | `60` | Seconds the remote is left alone after a failure. |

## GPU first, CPU fallback
The local engine above is the default and is the whole fix on its own. The
remote tier is off until `STT_REMOTE_URL` is set.

### Tier design
- `get_transcriber()` wraps the local engine in `TieredTranscriber` when
  `STT_REMOTE_URL` is set. Each dictation goes to the remote first.
- **Falls to local on**: connect refused or timed out, an answer slower
  than `STT_REMOTE_TIMEOUT`, any non-2xx (a 400 included: the local decoder
  may cope), a redirect, a body that is not JSON or has no `text` string.
  A 401 also logs that `STT_REMOTE_TOKEN` is wrong.
- **Does not fall on** `{"text": ""}` from a 200: silence is an answer.
- **Breaker**: a failure leaves the remote alone for `STT_REMOTE_COOLDOWN`
  seconds, then the next dictation tries it again; a success closes it. A
  machine that is switched off costs one connect timeout per cool-down.
  Measured against dead addresses: 0.57 s for a host that does not answer,
  0.10 s for a closed port, 0.18 s for a name that does not resolve.
- **The local model stays loaded** and preload still warms it, so the
  fallback costs only its own decode: 0.7 s for a 5 s clip, 1.8 s for 15 s,
  8.3 s for 60 s on a quiet box (the service's own log lines, above).
- **The audio goes to the configured host only.** Redirects are not
  followed. The URL is an internal host, so it does not pass through the
  public-address guard that link fetching uses.
- **Log**: `stt tier=remote model=… total_s=…` when the remote answers;
  `stt tier=remote failed (…) after …s; local for the next …s` once per
  failure, followed by the local engine's own line.
- `GET /health` is not called by Disjorn. The breaker's retry is a real
  request, and a probe before each dictation would add a round trip to
  every one. It is there for the smoke test.

### The contract Disjorn speaks
- `POST {STT_REMOTE_URL}/v1/audio/transcriptions`, multipart:
  - `file`: the audio as uploaded, with its original filename and content
    type (e.g. `voice.webm`, `audio/webm;codecs=opus`);
  - `model`: `STT_REMOTE_MODEL` (the service may ignore it);
  - `language`: `STT_LANGUAGE` when set, otherwise omitted.
- `Authorization: Bearer <STT_REMOTE_TOKEN>` when a token is set.
- `200` with JSON `{"text": "..."}`. Extra fields are ignored.
- `GET {STT_REMOTE_URL}/health`: `200 {"status":"ok","model":…,"device":"cuda"}`
  when ready, `503` otherwise.
- Port 8500 in examples. The expected URL is
  `http://spirit-wsl.tailca81ba.ts.net:8500`; it is deployment config, not
  a default in code.

`deploy/stt-gpu/reference_server.py` is one server that speaks this
(faster-whisper, model loaded before the port opens). It has **never run on
a GPU**. It was run on this CPU with Whisper small.en and Disjorn pointed at
it: transcription, the token check, the fall to local when it was killed and
the breaker all behaved as above. The real service is being built on the GPU
machine and need not resemble it.

### What has to happen on the GPU machine
1. A service answering the contract on port 8500, reachable at the WSL
   tailnet name (the way Ollama there already is).
2. From this server:
   `curl -s http://spirit-wsl.tailca81ba.ts.net:8500/health` and
   `curl -s -m 30 -F file=@/var/tmp/disjorn-stt-bench/smoke-15s.webm -F model=large-v3 -F language=en http://spirit-wsl.tailca81ba.ts.net:8500/v1/audio/transcriptions`
   (expect "Much later, when a friend of his was preparing an edition of
   all his Latin works, …").
3. In `server/.env`: `STT_REMOTE_URL=http://spirit-wsl.tailca81ba.ts.net:8500`
   (and `STT_REMOTE_TOKEN` if the service wants one), then restart `disjorn`.
4. Dictate once with the machine up (`stt tier=remote` in the journal) and
   once with it off (`stt tier=remote failed`, then `stt engine=onnx_asr`).

Notes for whoever builds that service, from what was tried here:
- faster-whisper's README (SYSTRAN/faster-whisper at `3ec025c`, 2026-11-10,
  "GPU") asks for cuBLAS for CUDA 12 and cuDNN 9, installable with
  `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*`, and says
  `LD_LIBRARY_PATH` must name their `lib` directories before Python starts.
- Pin `av==18.0.0` with faster-whisper 1.2.1 (see above).
- A pinned install (`faster-whisper==1.2.1 ctranslate2==4.8.1 av==18.0.0
  nvidia-cublas-cu12==12.9.3.13 nvidia-cudnn-cu12==9.28.0.42`) resolves on
  Python 3.13 and is 2.6 GB; whether those NVIDIA builds drive an RTX 5090
  was not tested. Draft install notes, a launcher and a user unit are in
  `/var/tmp/disjorn-stt-bench/gpu-side-drafts/`, outside the repo, unrun on
  a GPU.

## Deploy recipe (local engine)
After merge and the client build:

1. **Dependency** (one wheel; `--no-deps` because everything it needs is
   already installed at the benchmarked versions):
   `/home/plink/Disjorn/Disjorn/server/.venv/bin/pip install --no-deps onnx-asr==0.12.0`
2. **Model**, out of `/var/tmp` (systemd-tmpfiles ages that directory at
   30 days) to somewhere that stays:
   ```sh
   install -d /home/plink/.local/share/disjorn/stt
   cp -r /var/tmp/disjorn-stt-bench/models/parakeet-tdt-0.6b-v2 /home/plink/.local/share/disjorn/stt/
   ```
   3.0 GB as copied: 2.4 GB of fp32 files plus 631 MB of int8 files, kept so
   `STT_QUANTIZATION=int8` works. A fresh copy instead:
   `hf download istupakov/parakeet-tdt-0.6b-v2-onnx config.json vocab.txt nemo128.onnx encoder-model.onnx encoder-model.onnx.data decoder_joint-model.onnx --local-dir /home/plink/.local/share/disjorn/stt/parakeet-tdt-0.6b-v2`
3. **`server/.env`**: replace the `STT_MODEL=large-v3` line with
   ```
   STT_ENGINE=onnx_asr
   STT_MODEL=nemo-parakeet-tdt-0.6b-v2
   STT_MODEL_DIR=/home/plink/.local/share/disjorn/stt/parakeet-tdt-0.6b-v2
   STT_PRELOAD=true
   ```
4. **Check before restarting** (no login needed; loads the model in its own
   process for a few seconds):
   `cd /home/plink/Disjorn/Disjorn/server && .venv/bin/python -c "import asyncio; from app.services import stt; print(asyncio.run(stt.get_transcriber().transcribe('/var/tmp/disjorn-stt-bench/smoke-15s.webm')))"`
   It prints "Much later, when a friend of his was preparing an edition of
   all his Latin works, he remarked to his home circle …".
5. **Restart** `disjorn`. The journal shows
   `stt model loaded engine=onnx_asr model=nemo-parakeet-tdt-0.6b-v2 load_s=…`
   within about 15 s.
6. **Smoke test** against the endpoint, with a live session cookie and the
   house origin:
   `curl -s -H "Origin: $ORIGIN" -b "__Host-disjorn_session=$SESSION" -F 'audio=@/var/tmp/disjorn-stt-bench/smoke-15s.webm;type=audio/webm' http://127.0.0.1:8399/stt`
   answers `{"text":"Much later, …","engine":"onnx_asr"}` and the journal
   gains `stt engine=onnx_asr … audio_s=14.7 decode_s=…`.

**Rollback**: remove the four lines and restore `STT_MODEL=large-v3`, then
restart. Whisper is then told the language, so it is about half as slow as
it was. `STT_ENGINE=faster_whisper` with `STT_MODEL=small.en` and
`STT_QUANTIZATION=int8` is the usable Whisper fallback on this box (15 s
clip in 5.3 s).

**Left on disk for the owner to decide**: the old model in
`~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3` (3 GB),
and the v3 files in `/var/tmp/disjorn-stt-bench/models/parakeet-tdt-0.6b-v3`
(3.0 GB) for anyone who wants the 25-language model
(`STT_MODEL=nemo-parakeet-tdt-0.6b-v3` and its directory).

## Not verified
- A real browser recording. Every clip here is LibriSpeech re-encoded with
  ffmpeg as webm/opus; Safari's mp4/AAC goes through the same PyAV decoder
  but was not tried.
- Dictation speech. LibriSpeech is read audiobook speech; a phone mic in a
  room is harder for every model.
- The remote tier against a GPU, and the reference server on CUDA.
- A quiet box. See the brackets in the table.
- The "Transcribing…" label on a narrow phone screen: typechecked and
  built, not looked at.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: server/, client/ (and deploy/ files that describe them).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: no

## Expected diff tier
Tier 2: a new dependency, a new model in the server process, and an
outbound request carrying users' audio to a second machine. Advisory.

## Token estimate
One build-hand session; the benchmark was most of it.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`built@loop/2026-11-11-stt-fast-local`
