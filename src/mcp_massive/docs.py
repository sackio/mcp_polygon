"""Massive (formerly Polygon.io) documentation lookup tools.

Massive publishes an llms.txt index (https://massive.com/docs/llms.txt) listing every
doc page, each of which is also fetchable as raw markdown at its own `.md` URL. This
module lets tools/agents browse that index and pull a specific page without leaving
the MCP session.
"""
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

DOCS_BASE = "https://massive.com/docs/"
LLMS_INDEX_URL = urljoin(DOCS_BASE, "llms.txt")

_USER_AGENT = "mcp_massive/docs-tool"
_TIMEOUT_SECONDS = 15

# Cached in-process; the index is small (tens of KB) and changes rarely.
_index_cache: Optional[List[Dict[str, str]]] = None

_ENTRY_RE = re.compile(r"^-\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)(?::\s*(?P<desc>.*))?$")


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Failed to reach {url}: {e.reason}") from e


def _parse_index(text: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = _ENTRY_RE.match(line)
        if not match:
            continue
        entries.append({
            "section": section,
            "title": match.group("title").strip(),
            "url": match.group("url").strip(),
            "description": (match.group("desc") or "").strip(),
        })
    return entries


def get_index(refresh: bool = False) -> List[Dict[str, str]]:
    """Return the parsed llms.txt doc index, fetching (and caching) it if needed."""
    global _index_cache
    if refresh or _index_cache is None:
        _index_cache = _parse_index(_fetch(LLMS_INDEX_URL))
    return _index_cache


def list_docs(
    section: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    refresh: bool = False,
) -> List[Dict[str, str]]:
    """List/search doc index entries, optionally filtered by section and/or a
    case-insensitive substring match against title, description, and URL."""
    entries = get_index(refresh=refresh)

    if section:
        section_lower = section.lower()
        entries = [e for e in entries if section_lower in e["section"].lower()]

    if query:
        q = query.lower()
        entries = [
            e for e in entries
            if q in e["title"].lower() or q in e["description"].lower() or q in e["url"].lower()
        ]

    return entries[:max_results]


def list_sections(refresh: bool = False) -> List[str]:
    """List the distinct top-level sections in the doc index."""
    seen: List[str] = []
    for entry in get_index(refresh=refresh):
        if entry["section"] and entry["section"] not in seen:
            seen.append(entry["section"])
    return seen


def get_doc(path: str) -> Dict[str, str]:
    """Fetch one doc page as raw markdown.

    `path` may be a full https://massive.com/docs/... URL or a path relative to
    that base (e.g. "rest/crypto/market-operations/market-status.md" or the same
    without the trailing ".md" — both are accepted).
    """
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        if not path.endswith(".md"):
            path = f"{path}.md"
        url = urljoin(DOCS_BASE, path.lstrip("/"))

    if not url.startswith(DOCS_BASE) and url != LLMS_INDEX_URL:
        raise ValueError(f"Refusing to fetch outside {DOCS_BASE}: {url}")

    return {
        "url": url,
        "content": _fetch(url),
    }
