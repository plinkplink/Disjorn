"""Page summarization (WP8) behind a pluggable Summarizer protocol.

Engine swapping is config-driven: `SUMMARIZE_ENGINE` (default "ollama") keys
into the ENGINES registry; drop-in replacements register a zero-arg factory
there. `get_summarizer()` returns None when no engine matches — the router
turns that into a 503.

Also hosts the page-fetch + main-text-extraction helpers used by
POST /summarize (trafilatura with a crude stdlib tag-strip fallback), kept
here so tests can monkeypatch `fetch_page` without touching the router.
"""

import logging
from html.parser import HTMLParser
from typing import Callable, Optional, Protocol

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

PAGE_MAX_BYTES = 2 * 1024 * 1024
PAGE_FETCH_TIMEOUT = 10.0
SUMMARIZE_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; Disjorn/1.0; summarizer)"
MAX_INPUT_CHARS = 16_000  # keep prompts sane for small local models

PROMPT_TEMPLATE = (
    "Summarize this page in 3-5 sentences. Reply with only the summary, "
    "no preamble.\n\nURL: {url}\n\nPage text:\n{text}"
)


class Summarizer(Protocol):
    async def summarize(self, text: str, url: str) -> str: ...


class OllamaSummarizer:
    """Non-streaming POST to {OLLAMA_URL}/api/generate with OLLAMA_MODEL.

    Raises httpx.HTTPError when Ollama is unreachable or errors — the router
    maps that to 503.
    """

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.base_url = (base_url or settings.OLLAMA_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

    async def summarize(self, text: str, url: str) -> str:
        prompt = PROMPT_TEMPLATE.format(url=url, text=text[:MAX_INPUT_CHARS])
        async with httpx.AsyncClient(timeout=SUMMARIZE_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return str(resp.json().get("response", "")).strip()


# Engine registry — add entries for drop-in replacement engines.
ENGINES: dict[str, Callable[[], Summarizer]] = {
    "ollama": OllamaSummarizer,
}


def get_summarizer() -> Optional[Summarizer]:
    """Engine selected by SUMMARIZE_ENGINE; None if unknown/unconfigured."""
    factory = ENGINES.get(get_settings().SUMMARIZE_ENGINE)
    return factory() if factory is not None else None


# ---------------------------------------------------------------------------
# Page fetch + main-text extraction (used by POST /summarize)
# ---------------------------------------------------------------------------

class PageFetchError(Exception):
    """Page could not be fetched (network error, bad status, non-HTTP URL)."""


async def fetch_page(url: str) -> str:
    """Fetch up to PAGE_MAX_BYTES of a page as text. Raises PageFetchError."""
    if not url.startswith(("http://", "https://")):
        raise PageFetchError(f"unsupported URL scheme: {url!r}")
    try:
        async with httpx.AsyncClient(
            timeout=PAGE_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= PAGE_MAX_BYTES:
                        break
                body = b"".join(chunks)[:PAGE_MAX_BYTES]
                return body.decode(resp.charset_encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        raise PageFetchError(str(exc) or exc.__class__.__name__) from exc


class _TextStripper(HTMLParser):
    """Crude fallback extractor: all text content minus script/style."""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def extract_text(html: str) -> str:
    """Main text via trafilatura; fall back to a crude tag strip."""
    try:
        import trafilatura

        extracted = trafilatura.extract(html)
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception:  # noqa: BLE001 — fall back below
        pass
    stripper = _TextStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:  # noqa: BLE001
        pass
    return " ".join(stripper.parts)
