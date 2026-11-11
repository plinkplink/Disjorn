"""Voice-to-text endpoint.

    POST /stt  (user auth) — multipart field "audio" (webm/ogg/wav/mp3/m4a)
               -> {"text": "..."}

The mic button records with MediaRecorder and POSTs the blob; the text goes
into the composer for the user to edit. The upload is written under
DATA_DIR/tmp, transcribed by services.stt.get_transcriber() and deleted.

Every failure carries a sentence the composer can show: 501 when no engine is
installed, 415 for a type that is not audio, 400 when the audio cannot be
decoded, 503 when the engine itself fails.
"""

import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..config import get_settings
from ..models import User
from ..services import stt as stt_service
from .auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

CurrentUser = Annotated[User, Depends(get_current_user)]

ALLOWED_SUFFIXES = {".webm", ".ogg", ".oga", ".opus", ".wav", ".mp3", ".m4a", ".flac"}
ALLOWED_MIME_PREFIXES = ("audio/", "video/webm")  # MediaRecorder may say video/webm
CHUNK = 1024 * 1024


def _pick_suffix(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in ALLOWED_SUFFIXES:
        return suffix
    mime = (upload.content_type or "").lower()
    for prefix, fallback in (("audio/webm", ".webm"), ("video/webm", ".webm"),
                             ("audio/ogg", ".ogg"), ("audio/wav", ".wav"),
                             ("audio/x-wav", ".wav"), ("audio/mpeg", ".mp3")):
        if mime.startswith(prefix):
            return fallback
    return ""


@router.post("/stt")
async def transcribe_audio(
    user: CurrentUser, audio: UploadFile = File(...)
) -> dict[str, str]:
    transcriber = stt_service.get_transcriber()
    if transcriber is None:
        engine = get_settings().STT_ENGINE
        raise HTTPException(
            status_code=501,
            detail=(
                f"Speech-to-text is unavailable: engine '{engine}' could not be "
                "loaded. Install the optional ML dependencies "
                "(server/requirements-ml.txt) or set STT_ENGINE to an "
                "available engine."
            ),
        )

    suffix = _pick_suffix(audio)
    mime = (audio.content_type or "").lower()
    if not suffix and not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=415,
            detail="Unsupported audio format — send webm, ogg, wav, mp3, or m4a",
        )

    tmp_dir = get_settings().data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"stt-{uuid.uuid4().hex}{suffix or '.webm'}"
    try:
        with tmp_path.open("wb") as fh:
            while chunk := await audio.read(CHUNK):
                fh.write(chunk)
        text = await transcriber.transcribe(str(tmp_path))
    except stt_service.AudioError:
        raise HTTPException(
            status_code=400,
            detail="That recording could not be read — try recording again",
        )
    except Exception:
        logger.exception("stt engine failed")
        raise HTTPException(
            status_code=503,
            detail="Speech-to-text failed on the server — try again in a moment",
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"text": text}
