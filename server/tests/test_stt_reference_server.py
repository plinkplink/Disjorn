"""deploy/stt-gpu/reference_server.py against Disjorn's own remote engine,
with the Whisper model faked: both must speak the one contract."""

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

from app.config import reset_settings_cache
from app.services import stt as stt_service

SERVER_PY = Path(__file__).resolve().parents[2] / "deploy" / "stt-gpu" / "reference_server.py"


class FakeWhisper:
    made = []

    def __init__(self, name, device, compute_type):
        self.loaded = (name, device, compute_type)
        self.heard = []
        FakeWhisper.made.append(self)

    def transcribe(self, audio, language, vad_filter):
        self.heard.append((audio.read(), language, vad_filter))
        segments = [types.SimpleNamespace(text=" spoken on "), types.SimpleNamespace(text="the gpu.")]
        return segments, types.SimpleNamespace(duration=5.0)


@pytest.fixture
async def gpu(monkeypatch, tmp_db_path):
    """The speech server, started, with STT_GPU_TOKEN set to `tok`."""
    FakeWhisper.made = []
    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisper)
    )
    monkeypatch.setenv("STT_GPU_TOKEN", "tok")
    spec = importlib.util.spec_from_file_location("stt_gpu_server", SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with module.app.router.lifespan_context(module.app):
        yield module


def remote_for(gpu, monkeypatch, token):
    monkeypatch.setenv("STT_REMOTE_URL", "http://spirit.test:8500")
    monkeypatch.setenv("STT_REMOTE_TOKEN", token)
    reset_settings_cache()
    return stt_service.RemoteTranscriber(transport=httpx.ASGITransport(app=gpu.app))


async def test_the_remote_engine_and_the_reference_server_agree_on_the_wire(
    gpu, monkeypatch, tmp_path
):
    clip = tmp_path / "stt-1.webm"
    clip.write_bytes(b"opus bytes")
    text = await remote_for(gpu, monkeypatch, "tok").transcribe(str(clip))
    assert (text, text.engine) == ("spoken on the gpu.", "remote")
    (model,) = FakeWhisper.made
    assert model.loaded == ("large-v3", "cuda", "float16")
    assert model.heard == [(b"opus bytes", "en", True)]


async def test_the_gpu_server_refuses_a_wrong_token_and_disjorn_treats_that_as_down(
    gpu, monkeypatch, tmp_path
):
    clip = tmp_path / "stt-1.webm"
    clip.write_bytes(b"opus bytes")
    with pytest.raises(stt_service.RemoteError, match="HTTP 401"):
        await remote_for(gpu, monkeypatch, "wrong").transcribe(str(clip))
    assert FakeWhisper.made[0].heard == []


async def test_health_names_the_model_once_it_is_loaded(gpu):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gpu.app), base_url="http://spirit.test"
    ) as client:
        r = await client.get("/health")
    assert r.json() == {"status": "ok", "model": "large-v3", "device": "cuda"}
