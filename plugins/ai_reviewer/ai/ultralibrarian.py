import html as _html
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any, Dict, List


BASE_URL = "https://app.ultralibrarian.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"
)

_SEARCH_BLOCK_RE = re.compile(
    r'(<div class="search-result-item .*?</div>\s*</div>\s*</div>)',
    re.S,
)
_PART_LINK_RE = re.compile(
    r'<a class="part-link"[^>]*href="([^"]+)"[^>]*>\s*(.*?)\s*</a>',
    re.S | re.I,
)
_TEXT_CENTER_RE = re.compile(
    r'<div class="text-center">\s*(.*?)\s*</div>',
    re.S | re.I,
)
_DESC_RE = re.compile(
    r'<div class="search-result-item-description[^"]*">\s*(.*?)\s*</div>',
    re.S | re.I,
)
_PRICE_RE = re.compile(
    r'<div class="search-result-item-price">\s*(.*?)\s*</div>',
    re.S | re.I,
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_META_DESC_RE = re.compile(
    r'<meta\s+name="description"\s+content="([^"]*)"\s*/?>',
    re.S | re.I,
)
_SETUP_MFR_RE = re.compile(
    r"SetupMfrCADExport\(\s*'([^']*)'\s*,\s*'([^']*)'",
    re.S | re.I,
)
_DATASHEET_RE = re.compile(
    r'href="([^"]*/details/datasheet/[^"]+)"',
    re.S | re.I,
)
_PREVIEW_RE = re.compile(
    r'<img class="svg-image-inject" src="([^"]+)"[^>]*alt="([^"]*)"',
    re.S | re.I,
)


def _make_context() -> ssl.SSLContext:
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()  # type: ignore[attr-defined]


def _fetch(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_make_context()) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
                context=ssl._create_unverified_context(),  # type: ignore[attr-defined]
            ) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception:
            return ""


def _abs_url(url: str) -> str:
    return urllib.parse.urljoin(BASE_URL, url or "")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(text or "")).strip()


def _extract_first(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text or "")
    return _clean(match.group(1)) if match else ""


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _search_score(query: str, item: Dict[str, Any]) -> int:
    q = _normalize(query)
    title = _normalize(str(item.get("title", "")))
    manufacturer = _normalize(str(item.get("manufacturer", "")))
    description = _normalize(str(item.get("description", "")))
    score = 0
    if not q:
        return score
    if q == title:
        score += 200
    if q in title:
        score += 120
    if q in manufacturer:
        score += 35
    if q in description:
        score += 15
    q_tokens = [token for token in q.split() if token]
    if q_tokens and all(token in title for token in q_tokens):
        score += 50

    suffix = title[len(q):].strip() if q and title.startswith(q) else ""
    if suffix and not any(bad in suffix for bad in ("evm", "board", "kit", "adapter")):
        score += 65

    # Prefer the actual component over evaluation boards, demo kits, and EVMs.
    if not any(token in q for token in ("evm", "board", "kit", "adapter")):
        if any(bad in title for bad in ("evaluation board", "dev kit", "development board", "demo board")):
            score -= 140
        if any(bad in title for bad in (" evm", "evm ", "kit", "adapter", "board")):
            score -= 140

    return score


def search_parts(query: str, limit: int = 5) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "message": "Query is empty.", "query": query, "results": []}

    limit = max(1, min(int(limit or 5), 20))
    url = f"{BASE_URL}/Search?q={urllib.parse.quote(query)}"
    page = _fetch(url)
    if not page:
        return {
            "ok": False,
            "source": "ultralibrarian",
            "query": query,
            "search_url": url,
            "message": "UltraLibrarian did not answer in time.",
            "count": 0,
            "results": [],
        }

    results: List[Dict[str, Any]] = []
    for block in _SEARCH_BLOCK_RE.findall(page):
        link_match = _PART_LINK_RE.search(block)
        part_href = _abs_url(link_match.group(1)) if link_match else ""
        part_name = _clean(link_match.group(2)) if link_match else ""
        manufacturer = _extract_first(_TEXT_CENTER_RE, block)
        description = _extract_first(_DESC_RE, block)
        price = _extract_first(_PRICE_RE, block)

        entry = {
            "manufacturer": manufacturer,
            "title": part_name,
            "description": description,
            "detail_url": part_href,
            "available": "model-available-active" in block.lower(),
            "lead_free": "model-lead-free-active" in block.lower(),
            "rohs_compliant": "model-rohs-compliant-active" in block.lower(),
            "price": price,
        }
        if part_href or part_name:
            results.append(entry)

    results = sorted(results, key=lambda item: _search_score(query, item), reverse=True)

    return {
        "ok": True,
        "source": "ultralibrarian",
        "query": query,
        "search_url": url,
        "count": len(results),
        "results": results[:limit],
    }


def get_part_details(detail_url: str) -> Dict[str, Any]:
    detail_url = _abs_url(detail_url)
    if not detail_url:
        return {"ok": False, "message": "detail_url is empty.", "detail_url": detail_url}

    page = _fetch(detail_url)
    if not page:
        return {
            "ok": False,
            "source": "ultralibrarian",
            "detail_url": detail_url,
            "message": "UltraLibrarian did not answer in time.",
        }
    title = _extract_first(_TITLE_RE, page)
    meta_description = _extract_first(_META_DESC_RE, page)
    manufacturer = ""
    part_name = ""
    setup_match = _SETUP_MFR_RE.search(page)
    if setup_match:
        manufacturer = _clean(setup_match.group(1))
        part_name = _clean(setup_match.group(2))

    datasheet_url = ""
    datasheet_match = _DATASHEET_RE.search(page)
    if datasheet_match:
        datasheet_url = _abs_url(datasheet_match.group(1))

    previews = [
        {"url": _abs_url(src), "alt": _clean(alt)}
        for src, alt in _PREVIEW_RE.findall(page)
    ]

    return {
        "ok": True,
        "source": "ultralibrarian",
        "detail_url": detail_url,
        "title": title,
        "manufacturer": manufacturer,
        "part_name": part_name,
        "description": meta_description,
        "datasheet_page_url": datasheet_url,
        "symbol_available": "symbol available" in page.lower(),
        "footprint_available": "footprint available" in page.lower(),
        "model_3d_available": "3d model available" in page.lower(),
        "requires_login": "login to download" in page.lower(),
        "previews": previews,
    }


def lookup_part(query: str, limit: int = 5) -> Dict[str, Any]:
    search = search_parts(query, limit=limit)
    if not search.get("ok", False):
        return search

    results = search.get("results", []) or []
    if not results:
        return {
            "ok": True,
            "source": "ultralibrarian",
            "query": query,
            "search": search,
            "selected_part": {},
        }

    best = results[0]
    details: Dict[str, Any] = {}
    detail_url = str(best.get("detail_url", "")).strip()
    if detail_url:
        try:
            details = get_part_details(detail_url)
        except Exception as exc:
            details = {
                "ok": False,
                "source": "ultralibrarian",
                "detail_url": detail_url,
                "message": str(exc),
            }

    return {
        "ok": True,
        "source": "ultralibrarian",
        "query": query,
        "search": search,
        "best_match": best,
        "selected_part": details,
    }
