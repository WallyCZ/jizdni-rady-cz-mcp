"""Closures (výluky) and incidents (mimořádnosti) of Prague public transport.

pid.cz publishes both as RSS: an item has its title, link, the affected lines
(<lines><line>22</line>...), validity as Unix times (dateFrom, dateTo, empty
until further notice) and an HTML text (closures) or a one-line description
(incidents: "17.11. 07:35 - 17.11. 12:30; Dotčené linky: R10"). The feeds
are fetched at most every CACHE_SECONDS; a failed fetch keeps the last ones.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("jizdni_rady_mcp.disruptions")

FEEDS = {
    "výluka": "https://pid.cz/feed/rss-vyluky/",
    "mimořádnost": "https://pid.cz/feed/rss-mimoradnosti/",
}
PRAGUE = ZoneInfo("Europe/Prague")
CACHE_SECONDS = 600
TIMEOUT = 5.0
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"

_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def find(lines: list[str] | None, stop: str | None, active_now: bool, limit: int) -> list[dict]:
    """Closures and incidents affecting any of lines and naming stop in the
    title or text; active now, or with active_now False also those to come.
    The ones in force first, the most recently begun first: today's closure
    before a stop moved "until further notice" years ago."""
    now = time.time()
    wanted = {line.strip().upper() for line in lines or [] if line.strip()}
    place = _normalized(stop.split(",")[-1]) if stop else None   # "Praha, Drinopol" -> drinopol
    found = []
    for item in _items():
        if wanted and not wanted & {line.upper() for line in item["lines"]}:
            continue
        if place and place not in _normalized(item["title"] + " " + item["_text"]):
            continue
        starts, ends = item["_from"], item["_to"]
        running = (starts is None or starts <= now) and (ends is None or now <= ends)
        if active_now and not running:
            continue
        if not active_now and ends is not None and ends < now:
            continue
        found.append((not running, -(starts or 0) if running else starts or 0, item))
    found.sort(key=lambda entry: entry[:2])
    return [{key: value for key, value in item.items() if not key.startswith("_")}
            for _, _, item in found[:limit]]


def _items() -> list[dict]:
    items = []
    for kind, url in FEEDS.items():
        items += _feed(kind, url)
    return items


def _feed(kind: str, url: str) -> list[dict]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(url)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    try:
        started = time.perf_counter()
        request = urllib.request.Request(url, headers={"User-Agent": "jizdni-rady-cz-mcp"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            items = _parse(kind, response.read())
        log.info("PID feed %s: %d items, %.2f s", kind, len(items), time.perf_counter() - started)
    except Exception as error:
        log.warning("PID feed %s: %s", url, error)
        if cached:
            return cached[1]        # stale rather than nothing
        raise RuntimeError(f"Výluky PID se nepodařilo načíst: {error}") from error
    with _lock:
        _cache[url] = (now, items)
    return items


def _parse(kind: str, xml: bytes) -> list[dict]:
    items = []
    for node in ET.fromstring(xml).iter("item"):
        lines = [line.text.strip() for line in node.iter("line") if line.text and line.text.strip()]
        text = _text(node.findtext(CONTENT) or "") or _text(node.findtext("description") or "")
        if not lines:
            lines = _listed_lines(text)
        starts, ends = _stamp(node.findtext("dateFrom")), _stamp(node.findtext("dateTo"))
        items.append({
            "kind": kind,
            "title": (node.findtext("title") or "").strip(),
            "lines": lines,
            "valid_from": _clock(starts),
            "valid_to": _clock(ends) if ends else "do odvolání",
            "link": (node.findtext("link") or "").strip(),
            "description": _summary(text),
            "_from": starts, "_to": ends, "_text": text,
        })
    return items


def _listed_lines(text: str) -> list[str]:
    """The lines of "Linky: 5, 7" or "Dotčené linky: R10, S88" in a text."""
    match = re.search(r"(?:Dotčené linky|Linky):\s*([^;|\n]+)", text)
    return [line.strip() for line in match.group(1).split(",") if line.strip()] if match else []


def _text(markup: str) -> str:
    """Plain text of an item's HTML: tags out, entities decoded, spaces folded."""
    plain = re.sub(r"<br\s*/?>|</(p|h\d|li|div)>", " | ", markup)
    plain = html.unescape(re.sub(r"<[^>]+>", " ", plain))
    plain = re.sub(r"\s+", " ", plain)
    return re.sub(r"(\s*\|\s*)+", " | ", plain).strip(" |")


def _summary(text: str, length: int = 300) -> str:
    """The text without its leading list of lines, cut at a word near length."""
    text = re.sub(r"^(?:Dotčené linky|Linky):[^|]*\|\s*", "", text)
    text = re.sub(r"^\d{1,2}\.\d{1,2}\. \d{1,2}:\d{2} - [^;]+;\s*(?:Dotčené linky:.*)?$", "", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + " …"


def _stamp(value: str | None) -> float | None:
    try:
        return float(value) if value and value.strip() else None
    except ValueError:
        return None


def _clock(stamp: float | None) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, PRAGUE).strftime("%Y-%m-%d %H:%M")


def _normalized(value: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFD", value.casefold())
        if unicodedata.category(char) != "Mn").split())
