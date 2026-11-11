"""The STT service and endpoint with every engine behind a fake: the suite
loads no model and needs no ML extra except where a test says so."""

import asyncio
import logging
import sys
import time
import types
import wave
from pathlib import Path

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
        ("load", "nemo-parakeet-tdt-0.6b-v3", "/models/parakeet", "int8"),
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
