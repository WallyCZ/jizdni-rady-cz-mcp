"""Current delays of Prague public transport (PID) from the Golemio API.

The departure boards of api.golemio.cz give, for a PID stop, each departure's
timetable and predicted time. The feed this server plans with (CIS JŘ) has
other trip and stop ids, so a departure is matched by stop name, route and
timetable time. Needs GOLEMIO_API_KEY (free at api.golemio.cz); without it,
or when Golemio does not answer in time, there are no delays and the tools
work from the timetable alone.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("jizdni_rady_mcp.realtime")

URL = "https://api.golemio.cz/v2/pid/departureboards"
PRAGUE = ZoneInfo("Europe/Prague")
TIMEOUT = 3.0             # s for one board
CACHE_SECONDS = 30        # a board is asked for again after this long
MINUTES_AHEAD = 180       # how far ahead a board reaches
PREFIX = "Praha, "        # CIS JŘ names of Prague stops; PID names lack it

_cache: dict[str, tuple[float, dict[tuple, dict] | None]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="golemio")


def enabled() -> bool:
    return bool(os.environ.get("GOLEMIO_API_KEY"))


def pid_name(stop_name: str) -> str | None:
    """The PID name of a CIS JŘ stop in Prague ("Praha, Drinopol" -> "Drinopol")."""
    if stop_name.startswith(PREFIX) and len(stop_name) > len(PREFIX):
        return stop_name[len(PREFIX):].strip()
    return None


def boards(stop_names: set[str], budget: float) -> dict[str, dict[tuple[str, str], dict]]:
    """Departure boards of the Prague stops among stop_names (CIS JŘ names),
    each {(route, "HH:MM" timetable departure): delay}, fetched in parallel
    within budget seconds. Stops outside Prague, and boards that failed or
    came late, are missing."""
    if not enabled():
        return {}
    wanted = {name: pid for name in stop_names if (pid := pid_name(name))}
    futures = {name: _pool.submit(_board, pid) for name, pid in wanted.items()}
    wait(futures.values(), timeout=budget)
    result = {}
    for name, future in futures.items():
        if future.done() and future.result() is not None:
            result[name] = future.result()
    return result


def delay(board: dict[tuple, dict] | None, route: str, departure: str,
          direction: str | None = None) -> dict | None:
    """The delay of route's departure at departure ("HH:MM:SS", may pass 24:00)
    on board; with direction (the CIS JŘ last stop), the one going there when
    the route leaves both ways in the same minute."""
    if not board:
        return None
    hours, minutes = int(departure[:2]) % 24, departure[3:5]
    key = (route, f"{hours:02d}:{minutes}")
    if direction:
        found = board.get(key + (_headsign(pid_name(direction) or direction),))
        if found:
            return found
    return board.get(key)


def _headsign(name: str) -> str:
    return " ".join(name.casefold().split())


def _board(pid: str) -> dict[tuple[str, str], dict] | None:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(pid)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    board = None
    try:
        board = _parse(_fetch(pid))
    except Exception as error:        # no delays rather than no answer
        log.warning("Golemio board %s: %s", pid, error)
    with _lock:
        _cache[pid] = (now, board)
    return board


def _fetch(pid: str) -> dict:
    query = urllib.parse.urlencode({
        "names[]": pid, "minutesBefore": 5, "minutesAfter": MINUTES_AHEAD,
        "limit": 1000, "order": "timetable", "preferredTimezone": "Europe_Prague",
    })
    request = urllib.request.Request(f"{URL}?{query}", headers={
        "X-Access-Token": os.environ["GOLEMIO_API_KEY"], "Accept": "application/json",
        "User-Agent": "jizdni-rady-cz-mcp",
    })
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.load(response)
    log.info("Golemio board %s %.2f s", pid, time.perf_counter() - started)
    return data


def _parse(data: dict) -> dict[tuple[str, str], dict]:
    board = {}
    for departure in data.get("departures") or []:
        route = (departure.get("route") or {}).get("short_name")
        times = departure.get("departure_timestamp") or {}
        scheduled = _local(times.get("scheduled"))
        if not route or not scheduled:
            continue
        lateness = departure.get("delay") or {}
        trip = departure.get("trip") or {}
        found = {
            "delay_minutes": (lateness.get("seconds") or 0) // 60 if lateness.get("is_available") else None,
            "realtime_departure": (_local(times.get("predicted")) or scheduled).strftime("%H:%M"),
            "canceled": bool(trip.get("is_canceled")),
        }
        key = (str(route), scheduled.strftime("%H:%M"))
        board.setdefault(key, found)
        if trip.get("headsign"):
            board[key + (_headsign(trip["headsign"]),)] = found
    return board


def _local(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(PRAGUE) if moment.tzinfo else moment
