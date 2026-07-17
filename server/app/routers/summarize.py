"""Content-services router (WP8).

Despite the filename this router hosts all three content-service endpoints
(main.py pre-wires only stt.py + summarize.py for WP8, so unfurl and chibi
serving live here rather than in a new router file):

    POST /summarize {url}   (user auth)  -> {"url", "summary"}
        fetch page (10s, ~2MB cap) -> extract main text (trafilatura,
        crude tag-strip fallback) -> Summarizer engine (Ollama by default).
        502 when the page can't be fetched / has no text; 503 when no
        summarization engine is configured or Ollama is unreachable.

    GET /unfurl?url=        (any actor)  -> {"url", "title", "description",
        "image_url"} — OG/twitter-card metadata, DB-cached 7 days
        (services/unfurl.py). Never errors on garbage pages: fields null.

    GET /chibi/{pack}/{category}/{filename}  (any actor) — serves a chibi
        PNG from DATA_DIR/assets/chibi_packs/, path-traversal safe. This is
        the URL shape clients derive from emote_ref strings
        "chibi:{pack}/{Category}/{File.png}" (WP10 renders these on bot
        messages).
"""

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import get_settings
from ..models import User
from ..services import chibi as chibi_service
from ..services import summarize as summarize_service
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


@router.post("/summarize")
async def summarize_url(body: SummarizeRequest, user: CurrentUser) -> dict[str, str]:
    try:
        html = await summarize_service.fetch_page(body.url)
    except summarize_service.PageFetchError as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch page: {exc}")

    text = summarize_service.extract_text(html)
    if not text.strip():
        raise HTTPException(
            status_code=502, detail="Page fetched but no readable text was found"
        )

    summarizer = summarize_service.get_summarizer()
    if summarizer is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No summarization engine available "
                f"(SUMMARIZE_ENGINE={get_settings().SUMMARIZE_ENGINE!r})"
            ),
        )
    try:
        summary = await summarizer.summarize(text, body.url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Summarization backend unreachable at {get_settings().OLLAMA_URL}: "
                f"{exc or exc.__class__.__name__}"
            ),
        )
    return {"url": body.url, "summary": summary}


# ---------------------------------------------------------------------------
# GET /unfurl
# ---------------------------------------------------------------------------

@router.get("/unfurl")
async def unfurl(actor: CurrentActor, url: str = Query(min_length=1)) -> dict[str, Any]:
    return await unfurl_service.unfurl(url)


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
