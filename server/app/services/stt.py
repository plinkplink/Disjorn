"""Speech-to-text behind a pluggable Transcriber protocol.

`STT_ENGINE` keys into the ENGINES registry of zero-arg factories. A factory
returns None when its backend is not installed (the engines are optional
extras in requirements-ml.txt); the router turns that into a 501.

A local engine holds one model for the life of the process: loaded once under
a lock, at startup when STT_PRELOAD is set and otherwise on first use, and
decoded in a worker thread, one clip at a time.
"""

import asyncio
import importlib.util
import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

from ..config import get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# Parakeet attends over the whole clip at once: time and memory grow faster
# than its length, and a ten-minute clip is refused outright.
MAX_PASS_SECONDS = 30
MIN_SPEECH_SECONDS = 0.25


class AudioError(Exception):
    """The upload holds no decodable audio."""


class Transcriber(Protocol):
    async def transcribe(self, audio_path: str) -> str: ...


def decode_audio(audio_path: str) -> Any:
    """Any container PyAV reads, as 16 kHz mono float32 samples."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    try:
        with av.open(audio_path, mode="r", metadata_errors="ignore") as container:
            for frame in container.decode(audio=0):
                chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
            chunks.extend(out.to_ndarray() for out in resampler.resample(None))
    except (av.error.FFmpegError, IndexError, ValueError) as exc:
        raise AudioError(str(exc) or exc.__class__.__name__) from exc
    if not chunks:
        raise AudioError("no audio frames")
    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0


def split_at_pauses(audio: Any, limit: int = MAX_PASS_SECONDS * SAMPLE_RATE) -> list[Any]:
    """Pieces of at most `limit` samples, each cut in the quietest fifth of a
    second of its second half."""
    if len(audio) <= limit:
        return [audio]
    import numpy as np

    block = SAMPLE_RATE // 5
    pieces, start = [], 0
    while len(audio) - start > limit:
        search = audio[start + limit // 2 : start + limit]
        loudness = np.abs(search[: len(search) // block * block]).reshape(-1, block).mean(axis=1)
        cut = start + limit // 2 + int(loudness.argmin()) * block + block // 2
        pieces.append(audio[start:cut])
        start = cut
    pieces.append(audio[start:])
    return pieces


class LocalTranscriber:
    """Base for in-process engines; a subclass supplies _load and _decode."""

    engine = ""
    default_model = ""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or get_settings().STT_MODEL or self.default_model
        self._model = None
        self._load_lock = threading.Lock()
        # One decode already uses every core, so a second clip waits its turn.
        self._decode_lock = threading.Lock()

    def _load(self) -> Any:
        raise NotImplementedError

    def _decode(self, model: Any, audio: Any) -> str:
        raise NotImplementedError

    def warm(self) -> Any:
        with self._load_lock:
            if self._model is None:
                started = time.monotonic()
                self._model = self._load()
                logger.info(
                    "stt model loaded engine=%s model=%s load_s=%.1f",
                    self.engine, self.model_name, time.monotonic() - started,
                )
            return self._model

    def _transcribe_sync(self, audio_path: str) -> str:
        received = time.monotonic()
        audio = decode_audio(audio_path)
        model = self.warm()
        with self._decode_lock:
            started = time.monotonic()
            text = self._decode(model, audio)
            done = time.monotonic()
        # The transcript is the user's speech: it never goes to the log.
        logger.info(
            "stt engine=%s model=%s audio_s=%.1f decode_s=%.2f total_s=%.2f",
            self.engine, self.model_name, len(audio) / SAMPLE_RATE,
            done - started, done - received,
        )
        return text

    async def transcribe(self, audio_path: str) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio_path)


class OnnxAsrTranscriber(LocalTranscriber):
    """onnx-asr on onnxruntime: NVIDIA Parakeet and the other models it names."""

    engine = "onnx_asr"
    default_model = "nemo-parakeet-tdt-0.6b-v3"

    def _load(self) -> Any:
        import numpy as np
        import onnx_asr

        settings = get_settings()
        model = onnx_asr.load_model(
            self.model_name,
            settings.STT_MODEL_DIR or None,
            quantization=settings.STT_QUANTIZATION or None,
        )
        # onnxruntime sizes its arenas on the first pass; spend that here, on
        # two seconds of hiss, rather than on the first dictation.
        hiss = np.random.default_rng(0).standard_normal(2 * SAMPLE_RATE) * 0.01
        model.recognize(hiss.astype(np.float32), sample_rate=SAMPLE_RATE)
        return model

    def _decode(self, model: Any, audio: Any) -> str:
        # A piece too short to hold a word comes back as <unk> tokens.
        texts = (
            model.recognize(piece, sample_rate=SAMPLE_RATE).strip()
            for piece in split_at_pauses(audio)
            if len(piece) >= MIN_SPEECH_SECONDS * SAMPLE_RATE
        )
        return " ".join(text for text in texts if text)


class FasterWhisperTranscriber(LocalTranscriber):
    """faster-whisper on CTranslate2."""

    engine = "faster_whisper"
    default_model = "small"

    def _load(self) -> Any:
        from faster_whisper import WhisperModel

        return WhisperModel(
            self.model_name, compute_type=get_settings().STT_QUANTIZATION or "default"
        )

    def _decode(self, model: Any, audio: Any) -> str:
        # Without a language Whisper spends a whole encoder pass guessing one,
        # and guesses wrong on a short clip.
        segments, _info = model.transcribe(
            audio, language=get_settings().STT_LANGUAGE or None
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


def _make_onnx_asr() -> Optional[Transcriber]:
    if any(importlib.util.find_spec(m) is None for m in ("onnx_asr", "onnxruntime", "av")):
        return None
    return OnnxAsrTranscriber()


def _make_faster_whisper() -> Optional[Transcriber]:
    if importlib.util.find_spec("faster_whisper") is None:
        return None
    return FasterWhisperTranscriber()


ENGINES: dict[str, Callable[[], Optional[Transcriber]]] = {
    "onnx_asr": _make_onnx_asr,
    "faster_whisper": _make_faster_whisper,
}

_instances: dict[str, Transcriber] = {}


def get_transcriber() -> Optional[Transcriber]:
    """Engine selected by STT_ENGINE; cached instance; None if unavailable."""
    engine = get_settings().STT_ENGINE
    if engine in _instances:
        return _instances[engine]
    factory = ENGINES.get(engine)
    instance = factory() if factory is not None else None
    if instance is not None:
        _instances[engine] = instance
    return instance


async def preload() -> None:
    """Load the configured engine's model off-loop; a failure is logged, and
    the next dictation retries the load."""
    transcriber = get_transcriber()
    warm = getattr(transcriber, "warm", None)
    if warm is None:
        return
    try:
        await asyncio.to_thread(warm)
    except Exception:
        logger.exception("stt preload failed")


def reset_transcriber_cache() -> None:
    """Drop cached transcriber instances — used by tests."""
    _instances.clear()
