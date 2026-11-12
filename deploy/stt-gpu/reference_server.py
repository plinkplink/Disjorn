"""REFERENCE ONLY, never run on a GPU: one way to serve the speech contract
Disjorn's STT_REMOTE_URL speaks. The real service need not look like this
inside; it only has to answer the same two routes.

    POST /v1/audio/transcriptions   multipart `file` (+ `model`, `language`)
                                    -> 200 {"text": "..."}
    GET  /health                    -> 200 {"status": "ok", "model", "device"}

faster-whisper loads once, before the port opens, and stays on the device.
The request's `model` is accepted and ignored. Run on a CPU for the contract
test and nothing more: STT_GPU_DEVICE=cpu STT_GPU_COMPUTE_TYPE=int8.
"""

import asyncio
import hmac
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

MODEL = os.environ.get("STT_GPU_MODEL", "large-v3")
DEVICE = os.environ.get("STT_GPU_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("STT_GPU_COMPUTE_TYPE", "float16")
TOKEN = os.environ.get("STT_GPU_TOKEN", "")

logger = logging.getLogger("stt-gpu")
state: dict = {}
# One model on one GPU: a second clip waits for the first.
decode_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from faster_whisper import WhisperModel

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    started = time.monotonic()
    state["model"] = await asyncio.to_thread(
        WhisperModel, MODEL, device=DEVICE, compute_type=COMPUTE_TYPE
    )
    logger.info("loaded %s on %s (%s) in %.1fs", MODEL, DEVICE, COMPUTE_TYPE,
                time.monotonic() - started)
    yield


app = FastAPI(title="stt-gpu", lifespan=lifespan)


def transcribe(audio: bytes, language: str | None) -> tuple[str, float]:
    with decode_lock:
        segments, info = state["model"].transcribe(
            io.BytesIO(audio), language=language, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip(), info.duration


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL, "device": DEVICE}


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(""),
    language: str = Form(""),
    authorization: str = Header(""),
) -> dict[str, str]:
    if TOKEN and not hmac.compare_digest(authorization.encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="Bad or missing bearer token")
    audio = await file.read()
    started = time.monotonic()
    try:
        text, duration = await asyncio.to_thread(transcribe, audio, language or None)
    except Exception:
        logger.exception("transcription failed")
        raise HTTPException(status_code=500, detail="Transcription failed")
    # The transcript is somebody's speech: it never goes to the log.
    logger.info("audio_s=%.1f decode_s=%.2f", duration, time.monotonic() - started)
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("STT_GPU_HOST", "0.0.0.0"),
        port=int(os.environ.get("STT_GPU_PORT", "8500")),
    )
