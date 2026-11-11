"""Content-services router (WP8).

Despite the filename this router hosts all three content-service endpoints
(main.py pre-wires only stt.py + summarize.py for WP8, so unfurl and chibi
serving live here rather than in a new router file):

    POST /summarize {url}   (user auth)  -> {"url", "summary", "cached"}
        summary_cache, else fetch the public page (10s, ~2MB cap) ->
        main text -> SUMMARIZE_ENGINE. 400 non-public address; 429 over
        the per-user cap; 502 no page or text; 503 no engine or it fails.

    GET /unfurl?url=        (any actor)  -> {"url", "title", "description",
        "image_url"} — OG/twitter-card metadata, DB-cached 7 days
        (services/unfurl.py). Never errors on garbage pages: fields null.

    GET /chibi/{pack}/{category}/{filename}  (any actor) — serves a chibi
        PNG from DATA_DIR/assets/chibi_packs/, path-traversal safe. This is
        the URL shape clients derive from emote_ref strings
        "chibi:{pack}/{Category}/{File.png}" (WP10 renders these on bot
        messages).
"""

import math
from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import get_settings
from ..models import User
from ..services import chibi as chibi_service
from ..services import summarize as summarize_service
from ..services import throttle
from ..services import unfurl as unfurl_service
from .auth import Actor, get_actor, get_current_user

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]


# ---------------------------------------------------------------------------
# POST /summarize
# ---------------------------------------------------------------------------

class SummarizeRequest(BaseModel):
    url: str


@lru_cache
def _cap(limit: int, window: int) -> throttle.Limiter:
    return throttle.Limiter(limit, window)


def _hold_to_cap(user_id: int, spend: bool) -> None:
    settings = get_settings()
    if settings.SUMMARIZE_RATE_LIMIT <= 0:
        return
    limiter = _cap(settings.SUMMARIZE_RATE_LIMIT, settings.SUMMARIZE_RATE_WINDOW)
    key = str(user_id)
    wait = throttle.attempt([(limiter, key)]) if spend else limiter.retry_after(key)
    if wait:
        minutes = math.ceil(wait / 60)
        raise HTTPException(
            status_code=429,
            detail=(
                f"You have used your {settings.SUMMARIZE_RATE_LIMIT} new summaries for now. "
                f"Try again in {minutes} minute{'' if minutes == 1 else 's'}; "
                "links already summarized still open."
            ),
            headers={"Retry-After": str(wait)},
        )


@router.post("/summarize")
async def summarize_url(body: SummarizeRequest, user: CurrentUser) -> dict[str, Any]:
    engine = get_settings().SUMMARIZE_ENGINE
    summarizer = summarize_service.get_summarizer()
    if summarizer is None:
        raise HTTPException(
            status_code=503,
            detail=f"No summarization engine available (SUMMARIZE_ENGINE={engine!r})",
        )
    key = (summarize_service.normalize_url(body.url), engine, getattr(summarizer, "model", ""))
    cached = await summarize_service.cached_summary(*key)
    if cached is not None:
        return {"url": body.url, "summary": cached, "cached": True}

    _hold_to_cap(user.id, spend=False)
    try:
        html = await summarize_service.fetch_page(body.url)
    except summarize_service.PageRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except summarize_service.PageFetchError as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch page: {exc}")

    text = summarize_service.extract_text(html)
    if not text.strip():
        raise HTTPException(
            status_code=502, detail="Page fetched but no readable text was found"
        )

    _hold_to_cap(user.id, spend=True)
    try:
        summary = await summarizer.summarize(text, body.url)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Summarization backend ({engine}) returned HTTP "
                f"{exc.response.status_code}"
            ),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Summarization backend ({engine}) unreachable: "
                f"{str(exc) or exc.__class__.__name__}"
            ),
        )
    if summary:
        await summarize_service.store_summary(*key, summary)
    return {"url": body.url, "summary": summary, "cached": False}


# ---------------------------------------------------------------------------
# GET /unfurl
# ---------------------------------------------------------------------------

@router.get("/unfurl")
async def unfurl(actor: CurrentActor, url: str = Query(min_length=1),
                 refresh: bool = False) -> dict[str, Any]:
    return await unfurl_service.unfurl(url, refresh=refresh)


# ---------------------------------------------------------------------------
# GET /chibi/{pack}/{category}/{filename}
# ---------------------------------------------------------------------------

@router.get("/chibi/{pack}/{category}/{filename}")
async def serve_chibi(
    pack: str, category: str, filename: str, actor: CurrentActor
) -> FileResponse:
    path = chibi_service.safe_file(pack, category, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Chibi not found")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
    )
