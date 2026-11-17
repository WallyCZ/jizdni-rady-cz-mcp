"""Current departures of Prague public transport (PID) from the Golemio API.

The departure boards of api.golemio.cz give, for a PID stop, each departure's
timetable and predicted time. The feed this server plans with (CIS JŘ) has
other trip and stop ids, so a departure is matched by stop name, route and
timetable time. Boards also hold what that feed lacks: replacement transport
(X22 during a tram closure) from temporary stands such as "Drinopol Z1".
Needs GOLEMIO_API_KEY (free at api.golemio.cz); without it, or when Golemio
does not answer in time, the tools work from the timetable alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("jizdni_rady_mcp.realtime")

URL = "https://api.golemio.cz/v2/pid/departureboards"
PRAGUE = ZoneInfo("Europe/Prague")
TIMEOUT = 3.0             # s for one board
CACHE_SECONDS = 30        # a board is asked for again after this long
MINUTES_AHEAD = 180       # how far ahead a board reaches
PREFIX = "Praha, "        # CIS JŘ names of Prague stops; PID names lack it
PID_STOP_ID = re.compile(r"^U\d+Z\d+[A-Z]?$")   # GTFS id of a PID stand, U103Z51P


@dataclass
class Departure:
    """One departure on a Golemio board."""
    route: str
    mode: int | None
    headsign: str
    scheduled: datetime
    predicted: datetime
    delay_minutes: int | None
    canceled: bool
    substitute: bool
    platform: str | None
    stop_id: str | None


@dataclass
class Board:
    delays: dict[tuple, dict] = field(default_factory=dict)
    departures: list[Departure] = field(default_factory=list)
    stop_names: dict[str, str] = field(default_factory=dict)   # stop id -> PID name


_cache: dict[str, tuple[float, Board | None]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="golemio")


def enabled() -> bool:
    return bool(os.environ.get("GOLEMIO_API_KEY"))


def pid_name(stop_name: str) -> str | None:
    """The PID name of a CIS JŘ stop in Prague ("Praha, Drinopol" -> "Drinopol")."""
    if stop_name.startswith(PREFIX) and len(stop_name) > len(PREFIX):
        return stop_name[len(PREFIX):].strip()
    return None


def boards(stop_names: set[str], budget: float) -> dict[str, Board]:
    """Departure boards of the Prague stops among stop_names (CIS JŘ names),
    all their stands each, fetched in parallel within budget seconds. Stops
    outside Prague, and boards that failed or came late, are missing."""
    if not enabled():
        return {}
    wanted = {name: pid for name in stop_names if (pid := pid_name(name))}
    futures = {name: _pool.submit(_board, "names[]", pid) for name, pid in wanted.items()}
    wait(futures.values(), timeout=budget)
    return {name: future.result() for name, future in futures.items()
            if future.done() and future.result() is not None}


def stand_board(query: str, budget: float) -> tuple[Board, str | None] | None:
    """The board of a stop the timetable does not know: a PID stand id
    ("U103Z51P") or a name with a stand ("Drinopol Z1"); also a plain PID
    name. With the stand, the platform code to keep. None without a key, for
    an answer that failed or came late, or when Golemio has no such stop."""
    if not enabled():
        return None
    query = " ".join(query.split())
    if PID_STOP_ID.match(query):
        parameter, value, platform = "ids[]", query, None
    else:
        match = re.match(r"^(.*?)\s+([A-Z]\d*)$", query)
        name, platform = (match.group(1), match.group(2)) if match else (query, None)
        parameter, value = "names[]", pid_name(name) or name
    future = _pool.submit(_board, parameter, value)
    wait([future], timeout=budget)
    if not future.done() or future.result() is None or not future.result().stop_names:
        return None
    return future.result(), platform


def delay(board: Board | None, route: str, departure: str, direction: str | None = None) -> dict | None:
    """The delay of route's departure at departure ("HH:MM:SS", may pass 24:00)
    on board; with direction (the CIS JŘ last stop), the one going there when
    the route leaves both ways in the same minute."""
    if not board:
        return None
    hours, minutes = int(departure[:2]) % 24, departure[3:5]
    key = (route, f"{hours:02d}:{minutes}")
    if direction:
        found = board.delays.get(key + (_headsign(pid_name(direction) or direction),))
        if found:
            return found
    return board.delays.get(key)


def _headsign(name: str) -> str:
    return " ".join(name.casefold().split())


def _board(parameter: str, value: str) -> Board | None:
    key = f"{parameter}={value}"
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
    board = None
    try:
        board = _parse(_fetch(parameter, value))
    except Exception as error:        # the timetable alone rather than no answer
        log.warning("Golemio board %s: %s", key, error)
    with _lock:
        _cache[key] = (now, board)
    return board


def _fetch(parameter: str, value: str) -> dict:
    query = urllib.parse.urlencode({
        parameter: value, "minutesBefore": 5, "minutesAfter": MINUTES_AHEAD,
        "limit": 1000, "order": "timetable", "preferredTimezone": "Europe_Prague",
    })
    request = urllib.request.Request(f"{URL}?{query}", headers={
        "X-Access-Token": os.environ["GOLEMIO_API_KEY"], "Accept": "application/json",
        "User-Agent": "jizdni-rady-cz-mcp",
    })
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.load(response)
    log.info("Golemio board %s=%s %.2f s", parameter, value, time.perf_counter() - started)
    return data


def _parse(data: dict) -> Board:
    board = Board()
    for stop in data.get("stops") or []:
        if stop.get("stop_id"):
            board.stop_names[stop["stop_id"]] = stop.get("stop_name") or ""
    for item in data.get("departures") or []:
        route = item.get("route") or {}
        name = route.get("short_name")
        times = item.get("departure_timestamp") or {}
        scheduled = _local(times.get("scheduled"))
        if not name or not scheduled:
            continue
        lateness = item.get("delay") or {}
        trip = item.get("trip") or {}
        stop = item.get("stop") or {}
        departure = Departure(
            route=str(name),
            mode=route.get("type"),
            headsign=trip.get("headsign") or "",
            scheduled=scheduled,
            predicted=_local(times.get("predicted")) or scheduled,
            delay_minutes=(lateness.get("seconds") or 0) // 60 if lateness.get("is_available") else None,
            canceled=bool(trip.get("is_canceled")),
            substitute=bool(route.get("is_substitute_transport")) or str(name).upper().startswith("X"),
            platform=stop.get("platform_code"),
            stop_id=stop.get("id"),
        )
        board.departures.append(departure)
        found = {
            "delay_minutes": departure.delay_minutes,
            "realtime_departure": departure.predicted.strftime("%H:%M"),
            "canceled": departure.canceled,
        }
        if departure.platform:
            found["platform"] = departure.platform
        key = (departure.route, scheduled.strftime("%H:%M"))
        board.delays.setdefault(key, found)
        if departure.headsign:
            board.delays[key + (_headsign(departure.headsign),)] = found
    return board


def _local(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(PRAGUE) if moment.tzinfo else moment
