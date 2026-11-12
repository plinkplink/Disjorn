"""The STT service and endpoint with every engine behind a fake: the suite
loads no model and needs no ML extra except where a test says so."""

import asyncio
import logging
import sys
import time
import types
import wave
from pathlib import Path

import httpx
import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import auth
from app.services import stt as stt_service

PASSWORD = "correct horse battery staple"
TWO_SECONDS = [0.0] * (2 * stt_service.SAMPLE_RATE)


class FakeEngine(stt_service.LocalTranscriber):
    engine = "fake"
    default_model = "fake-model"

    def __init__(self, load_failures: int = 0):
        super().__init__()
        self.load_failures = load_failures
        self.loads = 0
        self.active = 0
        self.most_at_once = 0

    def _load(self):
        self.loads += 1
        time.sleep(0.05)
        if self.loads <= self.load_failures:
            raise RuntimeError("model files missing")
        return object()

    def _decode(self, model, audio):
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        time.sleep(0.02)
        self.active -= 1
        return "the spoken secret"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(stt_service, "decode_audio", lambda path: TWO_SECONDS)
    stt_service.reset_transcriber_cache()
    yield
    stt_service.reset_transcriber_cache()
    stt_service.ENGINES.pop("testfake", None)


def use_engine(monkeypatch, engine, **env):
    stt_service.ENGINES["testfake"] = lambda: engine
    monkeypatch.setenv("STT_ENGINE", "testfake")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    stt_service.reset_transcriber_cache()


async def signed_in(client):
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        ("alice", auth.hash_password(PASSWORD), "Alice"),
    )
    r = await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200


def upload(client, name="clip.webm", mime="audio/webm"):
    return client.post("/stt", files={"audio": (name, b"not really audio", mime)})


async def test_concurrent_dictations_load_the_model_once_and_decode_one_at_a_time(tmp_db_path):
    engine = FakeEngine()
    texts = await asyncio.gather(*(engine.transcribe("clip.webm") for _ in range(6)))
    assert texts == ["the spoken secret"] * 6
    assert engine.loads == 1
    assert engine.most_at_once == 1


async def test_the_timing_line_carries_durations_and_never_the_transcript(tmp_db_path, caplog):
    engine = FakeEngine()
    with caplog.at_level(logging.INFO, logger="app.services.stt"):
        await engine.transcribe("clip.webm")
    line = next(r.getMessage() for r in caplog.records if "decode_s=" in r.getMessage())
    assert "engine=fake model=fake-model audio_s=2.0 decode_s=" in line
    assert "secret" not in caplog.text


async def started_with(monkeypatch, engine, **env):
    """Boots the app with `engine` configured, gives a preload time to land,
    and returns once the app has shut down again."""
    from app.main import create_app

    use_engine(monkeypatch, engine, **env)
    application = create_app()
    async with application.router.lifespan_context(application):
        for _ in range(25):
            if engine._model is not None:
                break
            await asyncio.sleep(0.02)
        loads_at_startup = engine.loads
        await engine.transcribe("clip.webm")
    await db.close()
    return loads_at_startup


async def test_preload_loads_the_model_at_startup_and_a_dictation_reuses_it(
    tmp_db_path, monkeypatch
):
    engine = FakeEngine()
    assert await started_with(monkeypatch, engine, STT_PRELOAD="true") == 1
    assert engine.loads == 1


async def test_startup_loads_nothing_unless_preload_is_set(tmp_db_path, monkeypatch):
    engine = FakeEngine()
    assert await started_with(monkeypatch, engine) == 0
    assert engine.loads == 1


async def test_a_failed_preload_is_logged_and_the_next_dictation_loads_again(
    tmp_db_path, monkeypatch, caplog
):
    engine = FakeEngine(load_failures=1)
    use_engine(monkeypatch, engine)
    await stt_service.preload()
    assert "stt preload failed" in caplog.text
    assert await engine.transcribe("clip.webm") == "the spoken secret"
    assert engine.loads == 2


async def test_an_upload_that_is_not_audio_is_refused_with_415(client, monkeypatch):
    use_engine(monkeypatch, FakeEngine())
    await signed_in(client)
    r = await upload(client, name="notes.txt", mime="text/plain")
    assert r.status_code == 415


async def test_a_recording_over_the_size_cap_is_a_413_and_is_never_decoded(client, monkeypatch):
    engine = FakeEngine()
    use_engine(monkeypatch, engine, STT_MAX_BYTES="8")
    await signed_in(client)
    r = await upload(client)
    assert r.status_code == 413 and "too long" in r.json()["detail"]
    assert engine.loads == 0


async def test_undecodable_audio_is_a_400_the_user_can_read(client, monkeypatch):
    def refuse(path):
        raise stt_service.AudioError("no audio frames")

    use_engine(monkeypatch, FakeEngine())
    monkeypatch.setattr(stt_service, "decode_audio", refuse)
    await signed_in(client)
    r = await upload(client)
    assert r.status_code == 400
    assert "could not be read" in r.json()["detail"]


async def test_an_engine_failure_is_a_503_and_the_upload_is_still_deleted(
    client, monkeypatch, tmp_path
):
    use_engine(monkeypatch, FakeEngine(load_failures=1))
    await signed_in(client)
    r = await upload(client)
    assert r.status_code == 503
    assert "model files missing" not in r.text
    assert list((tmp_path / "data" / "tmp").iterdir()) == []


def test_onnx_asr_loads_once_from_the_configured_directory(tmp_db_path, monkeypatch):
    pytest.importorskip("numpy")
    calls = []

    class Model:
        def recognize(self, audio, sample_rate):
            calls.append(("recognize", len(audio) / sample_rate))
            return " Hello there. "

    def load_model(name, path, quantization):
        calls.append(("load", name, path, quantization))
        return Model()

    monkeypatch.setitem(sys.modules, "onnx_asr", types.SimpleNamespace(load_model=load_model))
    monkeypatch.setenv("STT_MODEL_DIR", "/models/parakeet")
    monkeypatch.setenv("STT_QUANTIZATION", "int8")
    reset_settings_cache()

    engine = stt_service.OnnxAsrTranscriber()
    assert engine._transcribe_sync("a.webm") == "Hello there."
    assert engine._transcribe_sync("b.webm") == "Hello there."
    assert calls == [
        ("load", "nemo-parakeet-tdt-0.6b-v2", "/models/parakeet", "int8"),
        ("recognize", 2.0),
        ("recognize", 2.0),
        ("recognize", 2.0),
    ]


def test_onnx_asr_decodes_a_long_clip_in_pieces_and_skips_a_sliver(tmp_db_path, monkeypatch):
    np = pytest.importorskip("numpy")
    rate = stt_service.SAMPLE_RATE
    heard = []

    class Model:
        def recognize(self, audio, sample_rate):
            heard.append(len(audio) / rate)
            return f"part{len(heard)}."

    engine = stt_service.OnnxAsrTranscriber()
    assert engine._decode(Model(), np.ones(rate // 10, np.float32)) == ""
    assert heard == []

    audio = np.ones(70 * rate, np.float32)
    audio[22 * rate : 22 * rate + rate // 2] = 0.0
    audio[50 * rate : 50 * rate + rate // 2] = 0.0
    assert engine._decode(Model(), audio) == "part1. part2. part3."
    assert sum(heard) == 70
    assert max(heard) <= stt_service.MAX_PASS_SECONDS
    assert 22 <= heard[0] <= 22.5 and 50 <= heard[0] + heard[1] <= 50.5


@pytest.mark.parametrize("configured, passed", [(None, "en"), ("", None), ("de", "de")])
def test_faster_whisper_is_told_the_language_unless_it_is_cleared(
    tmp_db_path, monkeypatch, configured, passed
):
    seen = {}

    class WhisperModel:
        def __init__(self, name, compute_type):
            seen["model"] = name
            seen["compute_type"] = compute_type

        def transcribe(self, audio, language):
            seen["language"] = language
            return [types.SimpleNamespace(text=" one "), types.SimpleNamespace(text="two")], None

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel)
    )
    monkeypatch.setenv("STT_MODEL", "large-v3")
    if configured is not None:
        monkeypatch.setenv("STT_LANGUAGE", configured)
    reset_settings_cache()

    assert stt_service.FasterWhisperTranscriber()._transcribe_sync("a.webm") == "one two"
    assert seen == {"model": "large-v3", "compute_type": "default", "language": passed}


def test_the_onnx_asr_engine_is_offered_only_when_its_packages_are_installed(
    tmp_db_path, monkeypatch
):
    monkeypatch.setenv("STT_ENGINE", "onnx_asr")
    reset_settings_cache()
    monkeypatch.setattr(stt_service.importlib.util, "find_spec", lambda name: None)
    assert stt_service.get_transcriber() is None
    monkeypatch.setattr(stt_service.importlib.util, "find_spec", lambda name: object())
    assert isinstance(stt_service.get_transcriber(), stt_service.OnnxAsrTranscriber)


def test_a_real_recording_decodes_to_16k_mono_and_junk_is_an_audio_error(
    tmp_path, monkeypatch
):
    pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    monkeypatch.undo()

    wav = tmp_path / "tone.wav"
    samples = (np.sin(np.arange(8000) * 0.2) * 12000).astype("<i2")
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(np.repeat(samples, 2).tobytes())
    audio = stt_service.decode_audio(str(wav))
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert abs(len(audio) - stt_service.SAMPLE_RATE) < 200
    assert 0.2 < float(np.abs(audio).max()) <= 1.0

    for name, body in (("junk.webm", b"not really audio"), ("empty.webm", b"")):
        bad = Path(tmp_path / name)
        bad.write_bytes(body)
        with pytest.raises(stt_service.AudioError):
            stt_service.decode_audio(str(bad))


class Remote:
    """A fake transcription server: answers with `reply`, counts what it saw."""

    def __init__(self, reply=None):
        self.reply = reply or (lambda request: httpx.Response(200, json={"text": " from the gpu "}))
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        answer = self.reply(request)
        return await answer if asyncio.iscoroutine(answer) else answer

    def tier(self, monkeypatch, local, **env):
        env = {"STT_REMOTE_URL": "http://gpu.test:8500/", **env}
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings_cache()
        remote = stt_service.RemoteTranscriber(transport=httpx.MockTransport(self.handle))
        return stt_service.TieredTranscriber(remote, local)


def refuse(request):
    raise httpx.ConnectError("connection refused", request=request)


async def test_the_remote_answers_first_and_is_sent_the_clip_in_the_openai_shape(
    tmp_db_path, monkeypatch, tmp_path
):
    clip = tmp_path / "stt-1.webm"
    clip.write_bytes(b"opus bytes")
    remote, local = Remote(), FakeEngine()
    tier = remote.tier(monkeypatch, local, STT_REMOTE_TOKEN="s3cret")
    text = await tier.transcribe(
        str(clip), filename="voice.webm", content_type="audio/webm;codecs=opus"
    )
    assert (text, text.engine) == ("from the gpu", "remote")
    assert local.loads == 0
    sent = remote.requests[0]
    assert str(sent.url) == "http://gpu.test:8500/v1/audio/transcriptions"
    assert sent.headers["authorization"] == "Bearer s3cret"
    fields = sent.content.split(b"--" + sent.headers["content-type"].split("boundary=")[1].encode())
    assert sorted(f.split(b'name="')[1].split(b'"')[0] for f in fields[1:-1]) == [
        b"file", b"language", b"model",
    ]
    body = sent.content
    assert b'name="model"\r\n\r\nlarge-v3' in body and b'name="language"\r\n\r\nen' in body
    assert b'name="file"; filename="voice.webm"\r\nContent-Type: audio/webm;codecs=opus' in body
    assert b"opus bytes" in body


async def test_silence_from_the_remote_is_an_answer_and_no_language_means_none_is_sent(
    tmp_db_path, monkeypatch, tmp_path
):
    clip = tmp_path / "a.webm"
    clip.write_bytes(b"x")
    remote = Remote(lambda request: httpx.Response(200, json={"text": "", "duration": 3.1}))
    local = FakeEngine()
    tier = remote.tier(monkeypatch, local, STT_LANGUAGE="")
    text = await tier.transcribe(str(clip))
    assert (text, text.engine) == ("", "remote")
    assert local.loads == 0
    assert b'name="language"' not in remote.requests[0].content
    assert "authorization" not in remote.requests[0].headers


async def test_a_401_falls_to_local_and_says_the_token_is_wrong_without_printing_it(
    tmp_db_path, monkeypatch, tmp_path, caplog
):
    clip = tmp_path / "a.webm"
    clip.write_bytes(b"x")
    remote = Remote(lambda request: httpx.Response(401, json={"detail": "bad token"}))
    tier = remote.tier(monkeypatch, FakeEngine(), STT_REMOTE_TOKEN="s3cret-value")
    text = await tier.transcribe(str(clip))
    assert text.engine == "fake"
    assert "STT_REMOTE_TOKEN does not match" in caplog.text
    assert "s3cret-value" not in caplog.text


async def test_a_refused_connection_falls_to_local_and_the_remote_rests_for_the_cooldown(
    tmp_db_path, monkeypatch, tmp_path, caplog
):
    clip = tmp_path / "a.webm"
    clip.write_bytes(b"x")
    remote, local = Remote(refuse), FakeEngine()
    tier = remote.tier(monkeypatch, local)
    now = [1000.0]
    tier.clock = lambda: now[0]

    text = await tier.transcribe(str(clip))
    assert (text, text.engine) == ("the spoken secret", "fake")
    assert "tier=remote failed (ConnectError)" in caplog.text

    now[0] += 59
    await tier.transcribe(str(clip))
    assert len(remote.requests) == 1

    now[0] += 2
    remote.reply = lambda request: httpx.Response(200, json={"text": "back"})
    assert await tier.transcribe(str(clip)) == "back"
    assert await tier.transcribe(str(clip)) == "back"
    assert len(remote.requests) == 3
    assert "secret" not in caplog.text and "back" not in caplog.text


async def slow(request):
    await asyncio.sleep(5)
    return httpx.Response(200, json={"text": "too late"})


@pytest.mark.parametrize("reply", [
    slow,
    lambda request: httpx.Response(500, text="boom"),
    lambda request: httpx.Response(503, json={"detail": "model loading"}),
    lambda request: httpx.Response(307, headers={"location": "http://elsewhere.test/"}),
    lambda request: httpx.Response(200, text=""),
    lambda request: httpx.Response(200, json={"words": []}),
    lambda request: httpx.Response(200, json={"text": None}),
], ids=["slow", "500", "503", "redirect", "empty", "no-text", "null-text"])
async def test_a_remote_that_gives_no_transcript_in_time_is_answered_locally(
    tmp_db_path, monkeypatch, tmp_path, reply
):
    clip = tmp_path / "a.webm"
    clip.write_bytes(b"x")
    remote, local = Remote(reply), FakeEngine()
    tier = remote.tier(monkeypatch, local, STT_REMOTE_TIMEOUT="0.2")
    started = time.monotonic()
    text = await tier.transcribe(str(clip))
    assert (text, text.engine) == ("the spoken secret", "fake")
    assert time.monotonic() - started < 2
    assert [r.url.host for r in remote.requests] == ["gpu.test"]
    await tier.transcribe(str(clip))
    assert len(remote.requests) == 1


async def test_a_clip_the_remote_refuses_is_answered_locally_and_the_remote_stays_in_use(
    tmp_db_path, monkeypatch, tmp_path
):
    clip = tmp_path / "a.webm"
    clip.write_bytes(b"x")
    remote = Remote(lambda request: httpx.Response(400, json={"detail": "could not decode"}))
    tier = remote.tier(monkeypatch, FakeEngine())
    text = await tier.transcribe(str(clip))
    assert (text, text.engine) == ("the spoken secret", "fake")
    await tier.transcribe(str(clip))
    assert len(remote.requests) == 2


async def test_with_no_remote_url_the_local_engine_is_the_whole_service(client, monkeypatch):
    local = FakeEngine()
    use_engine(monkeypatch, local)
    assert stt_service.get_transcriber() is local
    await signed_in(client)
    r = await upload(client)
    assert r.json() == {"text": "the spoken secret", "engine": "fake"}


async def test_with_a_remote_url_the_endpoint_names_the_tier_and_preload_warms_local(
    client, monkeypatch
):
    local = FakeEngine()
    use_engine(monkeypatch, local, STT_REMOTE_URL="http://gpu.test:8500")
    tier = stt_service.get_transcriber()
    assert isinstance(tier, stt_service.TieredTranscriber) and tier.local is local
    await stt_service.preload()
    assert local.loads == 1

    remote = Remote()
    tier.remote = stt_service.RemoteTranscriber(transport=httpx.MockTransport(remote.handle))
    await signed_in(client)
    assert (await upload(client)).json() == {"text": "from the gpu", "engine": "remote"}
    assert (
        b'filename="clip.webm"\r\nContent-Type: audio/webm\r\n\r\nnot really audio'
        in remote.requests[0].content
    )
    tier.remote = stt_service.RemoteTranscriber(transport=httpx.MockTransport(refuse))
    assert (await upload(client)).json() == {"text": "the spoken secret", "engine": "fake"}
    assert local.loads == 1


async def test_a_dead_remote_with_no_local_engine_is_a_503(client, monkeypatch):
    stt_service.ENGINES["testfake"] = lambda: None
    monkeypatch.setenv("STT_ENGINE", "testfake")
    monkeypatch.setenv("STT_REMOTE_URL", "http://gpu.test:8500")
    reset_settings_cache()
    stt_service.reset_transcriber_cache()
    stt_service.get_transcriber().remote = stt_service.RemoteTranscriber(
        transport=httpx.MockTransport(refuse)
    )
    await signed_in(client)
    assert (await upload(client)).status_code == 503
