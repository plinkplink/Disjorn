"""Speech-to-text (WP8) behind a pluggable Transcriber protocol.

Engine swapping is config-driven: `STT_ENGINE` (default "faster_whisper")
keys into the ENGINES registry of zero-arg factories. A factory returns None
when its backend is unavailable (e.g. faster-whisper not installed — it is an
optional extra in requirements-ml.txt); the router turns that into a 501.

FasterWhisperTranscriber loads its model lazily on first transcribe (model
name from STT_MODEL, e.g. "large-v3") and runs the blocking transcription in
a thread executor so the event loop stays responsive.

Instances are cached per engine name — model loads are expensive.
"""

import asyncio
import importlib.util
import logging
import threading
from typing import Callable, Optional, Protocol

from ..config import get_settings

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    async def transcribe(self, audio_path: str) -> str: ...


class FasterWhisperTranscriber:
    """faster-whisper backend. Lazy model load; blocking work off-loop."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or get_settings().STT_MODEL
        self._model = None
        self._lock = threading.Lock()

    def _load_model(self):
        with self._lock:  # two first-requests must not both load the model
            if self._model is None:
                from faster_whisper import WhisperModel

                logger.info("loading faster-whisper model %r", self.model_name)
                self._model = WhisperModel(self.model_name)
            return self._model

    def _transcribe_sync(self, audio_path: str) -> str:
        model = self._load_model()
        segments, _info = model.transcribe(audio_path)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, audio_path: str) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio_path)


def _make_faster_whisper() -> Optional[Transcriber]:
    """None when faster-whisper isn't importable — degrade, don't crash."""
    if importlib.util.find_spec("faster_whisper") is None:
        return None
    return FasterWhisperTranscriber()


# Engine registry — add entries for drop-in replacement engines.
ENGINES: dict[str, Callable[[], Optional[Transcriber]]] = {
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


def reset_transcriber_cache() -> None:
    """Drop cached transcriber instances — used by tests."""
    _instances.clear()
