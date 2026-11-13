"""Read-only MCP tools for the merged Czech GTFS feed."""

from __future__ import annotations

import bisect
import functools
import heapq
import logging
import math
import os
import sys
import threading
import time
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from . import planner, realtime
from .index import build

mcp = FastMCP(
    "Jízdní řády ČR",
    instructions=(
        "Jízdní řády veřejné dopravy v ČR. Na otázku, jak se dostat z jednoho místa "
        "na druhé, zavolej connections s názvy obou zastávek: vrátí hotová spojení "
        "i s přestupy. departures je jen odjezdová tabule jedné zastávky."
    ),
)
DATASET = Path(os.environ.get("GTFS_DATASET", "data/merged"))
# The index: GTFS_DB, else the one installed beside the package (the Docker
# image puts it there, so it is found even when a launcher such as mcp-proxy
# starts the server with an empty environment), else one built on first use
# into the user's cache directory.
INSTALLED = Path(sys.prefix) / "share" / "jizdni-rady-cz-mcp" / "gtfs.sqlite"
CACHED = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "jizdni-rady-cz-mcp" / "gtfs.sqlite"
DATABASE = Path(os.environ["GTFS_DB"]) if os.environ.get("GTFS_DB") else INSTALLED if INSTALLED.is_file() else CACHED


log = logging.getLogger("jizdni_rady_mcp")


def _logged(tool):
    """Log each tool call, how long it took and any error to stderr, which the
    container log shows (mcp-proxy passes the server's stderr through)."""
    @functools.wraps(tool)
    def call(*args, **kwargs):
        started = time.perf_counter()
        try:
            result = tool(*args, **kwargs)
        except Exception as error:
            log.warning("%s(%s) failed after %.2f s: %s", tool.__name__, kwargs or args,
                        time.perf_counter() - started, error)
            raise
        log.info("%s(%s) %.2f s", tool.__name__, kwargs or args, time.perf_counter() - started)
        return result
    return call


@lru_cache(maxsize=1)
def _db() -> sqlite3.Connection:
    if DATABASE == CACHED:
        source = DATASET / "stop_times.txt.gz"
        stale = DATABASE.is_file() and source.is_file() and source.stat().st_mtime > DATABASE.stat().st_mtime
        if not DATABASE.is_file() or stale:
            build(DATASET, DATABASE)
    elif not DATABASE.is_file():
        raise FileNotFoundError(f"Index jízdních řádů nenalezen: {DATABASE}")
    db = sqlite3.connect(f"{DATABASE.resolve().as_uri()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _query(sql: str, *args: Any) -> list[sqlite3.Row]:
    return _db().execute(sql, args).fetchall()


def _stop(stop_id: str) -> dict[str, str] | None:
    rows = _query("SELECT * FROM stops WHERE stop_id = ?", stop_id)
    return dict(rows[0]) if rows else None


@lru_cache(maxsize=8)
def _active_services(day: date) -> frozenset[str]:
    """Services running on the day: the calendar, then calendar_dates' exceptions."""
    key, weekday = day.strftime("%Y%m%d"), day.weekday() + 1
    active = {row[0] for row in _query(
        "SELECT service_id FROM calendar WHERE start_date <= ? AND ? <= end_date AND substr(days, ?, 1) = '1'",
        key, key, weekday)}
    for service_id, exception in _query("SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", key):
        (active.add if exception == 1 else active.discard)(service_id)
    return frozenset(active)


def _clock(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("Datum musí mít formát RRRR-MM-DD.") from error


def _requested_date(value: str | None) -> date:
    """Use Czech local date when the MCP client did not supply one."""
    return _date(value) if value else datetime.now(ZoneInfo("Europe/Prague")).date()


def _time(value: str) -> tuple[int, int, int]:
    try:
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            hours, minutes = parts
            seconds = 0
        elif len(parts) == 3:
            hours, minutes, seconds = parts
        else:
            raise ValueError
    except ValueError as error:
        raise ValueError("Čas musí mít formát HH:MM nebo HH:MM:SS.") from error
    if not (0 <= hours <= 47 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise ValueError("Čas musí být v rozsahu 00:00:00 až 47:59:59.")
    return hours, minutes, seconds


def _seconds(value: str) -> int:
    hours, minutes, seconds = _time(value)
    return hours * 3600 + minutes * 60 + seconds


def _normalized(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFD", value.casefold())
        if unicodedata.category(char) != "Mn"
    )


_PREPOSITIONS = {"z", "ze", "do", "na", "od", "k", "ke", "u", "v", "ve", "pres"}


def _stop_tokens(value: str) -> set[str]:
    """Normalise common Czech word endings so Radlické matches Radlická."""
    vowels = "aeiouy"
    tokens = []
    for token in _normalized(value).replace(",", " ").split():
        if token in _PREPOSITIONS:
            continue
        tokens.append(token.rstrip(vowels) if len(token) > 4 else token)
    return set(tokens)


@lru_cache(maxsize=1)
def _stop_index() -> list[tuple[dict[str, str], set[str]]]:
    return [(dict(row), _stop_tokens(row["stop_name"])) for row in _query("SELECT * FROM stops")]


def _town(name: str) -> str:
    return name.split(",")[0].strip()


def _resolve_stops(query: str) -> list[dict[str, str]]:
    """The stop the query names; when it names a town ("do Větřkovic") and no
    stop exactly, all the town's stops, so a search may arrive at any."""
    stop = _resolve_stop(query)
    if _normalized(stop["stop_name"]) == _normalized(query):
        return [stop]
    tokens = _stop_tokens(query)
    town = [candidate for candidate, _ in _stop_index()
            if "," in candidate["stop_name"] and _stop_tokens(_town(candidate["stop_name"])) == tokens]
    return town or [stop]


def _resolve_stop(query: str) -> dict[str, str]:
    query_tokens = _stop_tokens(query)
    if not query_tokens:
        raise ValueError("Název zastávky nesmí být prázdný.")
    candidates = []
    for stop, name_tokens in _stop_index():
        name = stop["stop_name"]
        if query_tokens <= name_tokens:
            exact = _normalized(query) == _normalized(name)
            candidates.append((not exact, len(name), name, stop))
    if not candidates:
        raise ValueError(f"Zastávka nenalezena: {query}. Použijte search_stops pro přesný název.")
    return sorted(candidates, key=lambda item: item[:3])[0][3]


@mcp.tool()
@_logged
def dataset_summary() -> dict[str, Any]:
    """Vrátí základní informace o lokálně připojeném GTFS datasetu."""
    def count(table: str) -> int:
        return _query(f"SELECT count(*) FROM {table}")[0][0]
    return {
        "index": str(DATABASE),
        "stops": count("stops"),
        "routes": count("routes"),
        "trips": count("trips"),
        "stop_times": count("stop_times"),
    }


@mcp.tool()
@_logged
def search_stops(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Vyhledá zastávky podle části názvu a vrátí jejich ID a souřadnice."""
    needle = query.casefold().strip()
    if not needle:
        raise ValueError("Vyhledávací dotaz nesmí být prázdný.")
    limit = max(1, min(limit, 50))
    matches = [
        {key: stop.get(key, "") for key in ("stop_id", "stop_name", "stop_lat", "stop_lon")}
        for stop, _ in _stop_index()
        if needle in stop["stop_name"].casefold()
    ]
    return sorted(matches, key=lambda item: item["stop_name"])[:limit]


@mcp.tool()
@_logged
def search_routes(query: str, route_type: int | None = None, limit: int = 20) -> list[dict[str, str]]:
    """Vyhledá linky podle čísla nebo názvu. GTFS route_type: autobus 3, vlak 2, tramvaj 0."""
    needle = query.casefold().strip()
    if not needle:
        raise ValueError("Vyhledávací dotaz nesmí být prázdný.")
    limit = max(1, min(limit, 50))
    matches = []
    for row in _query("SELECT route_id, route_short_name, route_long_name, route_type, agency_id FROM routes"):
        searchable = " ".join((row["route_short_name"], row["route_long_name"])).casefold()
        if needle in searchable and (route_type is None or row["route_type"] == str(route_type)):
            matches.append(dict(row))
    return sorted(matches, key=lambda item: (item["route_short_name"], item["route_long_name"]))[:limit]


def _start(day: date, from_time: str | None) -> int:
    """from_time in seconds; without it now if the day is today, else midnight."""
    if from_time:
        return _seconds(from_time)
    now = datetime.now(ZoneInfo("Europe/Prague"))
    return now.hour * 3600 + now.minute * 60 if day == now.date() else 0


def _stop_by_id_or_name(stop: str) -> dict[str, str]:
    return _stop(stop.strip()) or _resolve_stop(stop)


@mcp.tool()
@_logged
def departures(
    stop: str,
    service_date: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Odjezdová tabule jedné zastávky: nejbližší odjezdy linek a jejich směr.

    stop je název zastávky, jak ho řekl uživatel („Drinopol“, „z Anděla“),
    nebo stop_id ze search_stops. Bez service_date a from_time ukáže odjezdy
    dnes od teď, bez to_time na 3 hodiny dopředu. direction je konečná spoje.
    Na cestu z A do B použij connections, ne tento nástroj. Časy nad 24:00
    jsou po půlnoci.
    """
    place = _stop_by_id_or_name(stop)
    day = _requested_date(service_date)
    start = _start(day, from_time)
    end = _seconds(to_time) if to_time else start + 3 * 3600
    if start > end:
        raise ValueError("from_time musí být před to_time.")
    limit = max(1, min(limit, 100))
    active = _active_services(day)
    rows = _db().execute(
        """SELECT st.departure, t.trip_id, t.service_id, r.route_short_name, r.route_long_name,
                  r.route_type,
                  (SELECT s.stop_name FROM stop_times l JOIN stops s ON s.id = l.stop
                   WHERE l.trip = st.trip ORDER BY l.stop_sequence DESC LIMIT 1) AS direction,
                  (SELECT l.stop FROM stop_times l
                   WHERE l.trip = st.trip ORDER BY l.stop_sequence DESC LIMIT 1) = st.stop AS terminates
           FROM stop_times st JOIN trips t ON t.id = st.trip
           LEFT JOIN routes r ON r.route_id = t.route_id
           WHERE st.stop = (SELECT id FROM stops WHERE stop_id = ?) AND st.departure BETWEEN ? AND ?
           ORDER BY st.departure""",
        (place["stop_id"], start, end),
    )
    found = []
    for row in rows:
        if row["service_id"] not in active or row["terminates"]:
            continue
        found.append({
            "departure_time": _clock(row["departure"]),
            "route_short_name": row["route_short_name"] or "",
            "mode": _MODES.get(row["route_type"], row["route_type"] or ""),
            "direction": row["direction"],
            "route_long_name": row["route_long_name"] or "",
            "trip_id": row["trip_id"],
        })
        if len(found) == limit:
            break
    if _is_today(day):
        board = realtime.boards({place["stop_name"]}, _REALTIME_BUDGET).get(place["stop_name"])
        for item in found:
            _add_delay(item, board, item["route_short_name"], item["departure_time"], item["direction"])
    return {
        "stop": {key: place.get(key, "") for key in ("stop_id", "stop_name")},
        "service_date": day.isoformat(),
        "departures": found,
    }


@lru_cache(maxsize=1)
def _footpaths() -> dict[int, list[tuple[int, int]]]:
    paths: dict[int, list[tuple[int, int]]] = {}
    for from_stop, to_stop, seconds in _query("SELECT from_stop, to_stop, seconds FROM footpaths"):
        paths.setdefault(from_stop, []).append((to_stop, seconds))
    return paths


@lru_cache(maxsize=1)
def _service_numbers() -> dict[str, int]:
    return {service_id: number for number, service_id in _query("SELECT id, service_id FROM services")}


@lru_cache(maxsize=1)
def _coordinates() -> dict[int, tuple[float, float]]:
    """Stop id -> (lat, lon) for the stops whose position the feed knows."""
    points = {}
    for stop, _ in _stop_index():
        try:
            lat, lon = float(stop["stop_lat"]), float(stop["stop_lon"])
        except ValueError:
            continue
        if lat or lon:
            points[stop["id"]] = (lat, lon)
    return points


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], (a[1] - b[1]) * 0.643) * 111.32   # cos 50 degrees


# The planner's filters live in temporary tables of the shared connection:
# the services running on the day it last planned for, and the stops near both
# ends of the journey being planned. _planning serialises their use.
_planning = threading.Lock()
_planned_day: list[date] = []


def _prepare(day: date) -> None:
    db = _db()
    if not _planned_day:
        db.executescript("""
            CREATE TEMP TABLE IF NOT EXISTS active_services (service INTEGER PRIMARY KEY);
            CREATE TEMP TABLE IF NOT EXISTS near_origin (stop INTEGER PRIMARY KEY);
            CREATE TEMP TABLE IF NOT EXISTS near_destination (stop INTEGER PRIMARY KEY);
            CREATE TEMP TABLE IF NOT EXISTS corridor (stop INTEGER PRIMARY KEY);""")
    if _planned_day[:1] != [day]:
        numbers = _service_numbers()
        db.execute("DELETE FROM temp.active_services")
        db.executemany("INSERT INTO temp.active_services VALUES (?)",
                       ((numbers[service],) for service in _active_services(day) if service in numbers))
        _planned_day[:] = [day]


def warm_up(service_date: str | None = None) -> None:
    """Load what every journey search needs, so the first one is not slow."""
    _stop_index()
    _footpaths()
    _coordinates()
    with _planning:
        _prepare(_requested_date(service_date))


def _ends(stops: list[dict[str, str]]) -> tuple[tuple[float, float], float] | None:
    """Centre and radius (km) of the stops' positions, or None if none is known."""
    points = [_coordinates()[stop["id"]] for stop in stops if stop["id"] in _coordinates()]
    if not points:
        return None
    centre = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    return centre, max(_km(centre, p) for p in points)


def _hops(origins: list[dict[str, str]], destinations: list[dict[str, str]], start: int):
    """The hops a search from start reads: the day's running trips. For a long
    journey only long trips (rail, lines reaching 30+ km), but also local ones
    near the origin during the first _LEAVE_ORIGIN seconds and near the
    destination from the earliest arrival there at _TOP_SPEED_KMH on. Call with
    _planning held."""
    db = _db()
    here, there = _ends(origins), _ends(destinations)
    end = start + _HORIZON
    gap = _km(here[0], there[0]) - here[1] - there[1] if here and there else 0
    if gap >= _LONG_JOURNEY_KM:
        points = _coordinates()
        ellipse = gap + here[1] + there[1]
        _fill("corridor", (
            stop for stop, point in points.items()
            if _km(point, here[0]) + _km(point, there[0]) <= _CORRIDOR * ellipse + _NEAR_KM))
        _fill("corridor", (stop for stop, _ in _stop_index_ids() if stop not in points), clear=False)
        _fill("near_origin", (stop for stop, point in points.items()
                              if _km(point, here[0]) <= here[1] + _NEAR_KM))
        _fill("near_destination", (stop for stop, point in points.items()
                                   if _km(point, there[0]) <= there[1] + _NEAR_KM))
        soonest = start + int((gap - 2 * _NEAR_KM) / _TOP_SPEED_KMH * 3600)
        # Each stream comes in departure order: the merge reads them in step.
        return heapq.merge(
            _rows("""SELECT departure, trip, from_stop, to_stop, arrival, stop_sequence FROM hops_long
                     WHERE departure BETWEEN :since AND :until AND from_stop IN temp.corridor
                       AND service IN temp.active_services""", since=start, until=end),
            _local("near_origin", start, min(end, start + _LEAVE_ORIGIN)),
            _local("near_destination", max(start, soonest), end))
    return _rows("""SELECT departure, trip, from_stop, to_stop, arrival, stop_sequence FROM hops
                    WHERE departure BETWEEN :since AND :until AND service IN temp.active_services""",
                 since=start, until=end)


def _local(table: str, start: int, until: int):
    """The local hops from the stops in table, in departure order. A city's
    many stops are cheaper to filter while reading the hops in time order; a
    few are cheaper to look up by stop, a slice of time at a time."""
    if _db().execute(f"SELECT count(*) FROM temp.{table}").fetchone()[0] > _LOOKUP_STOPS:
        # +from_stop keeps SQLite off the stop index: it reads the primary key,
        # already in the order asked for, instead of sorting the rows by stop.
        return _rows(f"""SELECT departure, trip, from_stop, to_stop, arrival, stop_sequence FROM hops
                         WHERE departure BETWEEN :since AND :until AND NOT long
                           AND +from_stop IN temp.{table} AND service IN temp.active_services
                         ORDER BY departure, trip, stop_sequence""",
                     since=start, until=until)
    return _slices(f"""SELECT departure, trip, from_stop, to_stop, arrival, stop_sequence FROM hops
                       WHERE NOT long AND from_stop IN temp.{table} AND departure BETWEEN :since AND :until
                         AND service IN temp.active_services
                       ORDER BY departure, trip, stop_sequence""", start, until)


@lru_cache(maxsize=1)
def _stop_index_ids() -> list[tuple[int, str]]:
    return [(stop["id"], stop["stop_id"]) for stop, _ in _stop_index()]


def _fill(table: str, stops, clear: bool = True) -> None:
    db = _db()
    if clear:
        db.execute(f"DELETE FROM temp.{table}")
    db.executemany(f"INSERT OR IGNORE INTO temp.{table} VALUES (?)", ((stop,) for stop in stops))


def _slices(sql: str, start: int, end: int):
    """sql's rows for departures start..end, read _SLICE seconds at a time: its
    ORDER BY sorts one slice, and the slices after the search ends are never read."""
    for since in range(start, end + 1, _SLICE):
        yield from _rows(sql, since=since, until=min(end, since + _SLICE - 1))


def _rows(sql: str, **names: int):
    """Plain tuples from a cursor of its own: the planner reads ~10^5 of them."""
    cursor = _db().cursor()
    cursor.row_factory = None
    return cursor.execute(sql, names)


@lru_cache(maxsize=4096)
def _stop_name(number: int) -> str:
    return _query("SELECT stop_name FROM stops WHERE id = ?", number)[0][0]


def _leg(leg: planner.Ride | planner.Walk) -> dict[str, Any]:
    if isinstance(leg, planner.Walk):
        return {
            "type": "walk",
            "from_stop": _stop_name(leg.from_stop),
            "to_stop": _stop_name(leg.to_stop),
            "minutes": round(leg.seconds / 60),
        }
    trip = _query(
        """SELECT t.trip_id, r.route_short_name, r.route_long_name, r.route_type
           FROM trips t LEFT JOIN routes r ON r.route_id = t.route_id WHERE t.id = ?""",
        leg.trip,
    )[0]
    return {
        "type": "ride",
        "route_short_name": trip["route_short_name"] or "",
        "route_long_name": trip["route_long_name"] or "",
        "mode": _MODES.get(trip["route_type"], trip["route_type"]),
        "from_stop": _stop_name(leg.board_stop),
        "departure_time": _clock(leg.departure),
        "to_stop": _stop_name(leg.alight_stop),
        "arrival_time": _clock(leg.arrival),
        "trip_id": trip["trip_id"],
    }


def _short(clock: str) -> str:
    return clock[:5]


def _summary(legs: list[dict[str, Any]]) -> str:
    """The journey in one line: 14:02 tramvaj 22 z Praha, Drinopol do ... (14:31),
    with the current delay of a ride where Golemio knows it."""
    parts = []
    for leg in legs:
        if leg["type"] == "walk":
            parts.append(f"pěšky {leg['minutes']} min na {leg['to_stop']}")
        else:
            parts.append(f"{_short(leg['departure_time'])}{_lateness(leg)} {leg['mode']} "
                         f"{leg['route_short_name']} z {leg['from_stop']} do {leg['to_stop']} "
                         f"({_short(leg['arrival_time'])})")
    return ", ".join(parts)


def _lateness(item: dict[str, Any]) -> str:
    if item.get("canceled"):
        return " (zrušen)"
    minutes = item.get("delay_minutes")
    if minutes is None:
        return ""
    return " (včas)" if minutes <= 0 else f" (zpoždění {minutes} min)"


def _is_today(day: date) -> bool:
    return day == datetime.now(ZoneInfo("Europe/Prague")).date()


def _add_delay(item: dict[str, Any], board, route: str, departure: str,
               direction: str | None = None) -> None:
    """Add the realtime fields of route's departure at departure to item, if known."""
    found = realtime.delay(board, route, departure, direction)
    if found:
        item.update(found)


def _add_delays(found: list[dict[str, Any]]) -> None:
    """Current delays of the rides from Prague stops within the next few hours,
    their boards fetched together, summaries rewritten with them."""
    now = datetime.now(ZoneInfo("Europe/Prague"))
    soon = now.hour * 3600 + now.minute * 60 + realtime.MINUTES_AHEAD * 60
    rides = [leg for item in found for leg in item["legs"]
             if leg["type"] == "ride" and _seconds(leg["departure_time"]) <= soon]
    boards = realtime.boards({leg["from_stop"] for leg in rides}, _REALTIME_BUDGET)
    if not boards:
        return
    for leg in rides:
        _add_delay(leg, boards.get(leg["from_stop"]), leg["route_short_name"], leg["departure_time"])
    for item in found:
        item["summary"] = _summary(item["legs"])


_MODES = {"0": "tramvaj", "1": "metro", "2": "vlak", "3": "autobus", "4": "přívoz",
          "7": "lanovka", "11": "trolejbus"}


@mcp.tool()
@_logged
def connections(
    from_stop: str,
    to_stop: str,
    service_date: str | None = None,
    from_time: str | None = None,
    max_changes: int = 4,
    limit: int = 3,
) -> dict[str, Any]:
    """Vyhledá spojení z jedné zastávky do druhé, přímá i s přestupy. Použij ho na
    každý dotaz typu „jak se dostanu z A do B“, „najdi spojení“, „kdy mi to jede“.

    from_stop a to_stop jsou názvy zastávek, jak je řekl uživatel (stop_id ani
    search_stops nejsou potřeba, pádové tvary jako „z Drinopolu“ „na Radlickou“
    fungují). Bez service_date a from_time hledá dnes od teď, a když dnes už
    nic nejede, zítra od 4:00 (service_date ve výsledku říká, který den).

    Vrátí nejbližší spojení seřazená podle odjezdu. Každé má celou trasu v legs:
    jízdy (linka, druh dopravy, nástupní a výstupní zastávka, odjezd, příjezd)
    a přesuny pěšky mezi blízkými zastávkami. Výsledek je spočítaný z jízdního
    řádu včetně návazností, takže ho uživateli podej tak, jak je; neskládej
    spojení sám z departures. Časy nad 24:00 jsou po půlnoci.
    """
    origins, destinations = _resolve_stops(from_stop), _resolve_stops(to_stop)
    origin_ids = {stop["id"] for stop in origins}
    destination_ids = {stop["id"] for stop in destinations}
    if origin_ids & destination_ids:
        raise ValueError("Výchozí a cílová zastávka jsou stejné.")
    day = _requested_date(service_date)
    start = _start(day, from_time)
    limit = max(1, min(limit, 10))
    max_rides = max(0, min(max_changes, 4)) + 1
    footpaths = _footpaths()
    started = time.monotonic()

    def search(day: date, start: int) -> tuple[list[dict[str, Any]], bool]:
        found: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        deadline = started + _TIME_BUDGET
        search_from, last = start, start + _SEARCH_WINDOW
        with _planning:
            _prepare(day)
            hops = _Replay(_hops(origins, destinations, start))
            while len(found) < limit and search_from <= last:
                journeys, complete = planner.earliest(
                    hops.since(search_from), footpaths, origin_ids, destination_ids,
                    search_from, max_rides, _CHANGE_TIME, deadline)
                search_from = _collect(journeys, found, seen, search_from)
                if not journeys:
                    return found, not complete
                if not complete or time.monotonic() > min(deadline, started + _MORE_BUDGET):
                    return found, len(found) < limit
        return found, False

    found, partial = search(day, start)
    if not found and not partial and service_date is None and from_time is None:
        # Nothing more today: the nearest connection is tomorrow's first,
        # from the early morning (from midnight it would wait for hours).
        found, partial = search(day + timedelta(days=1), _NEXT_DAY_FROM)
        if found:
            day += timedelta(days=1)
    found.sort(key=lambda item: (item["departure_time"], item["arrival_time"]))
    del found[limit:]
    if _is_today(day):
        _add_delays(found)
    result = {
        "origin": _place(origins),
        "destination": _place(destinations),
        "service_date": day.isoformat(),
        "connections": found,
    }
    if partial:
        result["partial"] = True
    return result


class _Replay:
    """Hops read once and kept: the searches for later departures go through
    the same ones again from their own start instead of querying anew."""

    def __init__(self, rows):
        self._rows = iter(rows)
        self._kept: list[tuple[int, int, int, int, int]] = []

    def since(self, start: int):
        index = bisect.bisect_left(self._kept, start, key=lambda row: row[0])
        while index < len(self._kept):
            yield self._kept[index]
            index += 1
        for row in self._rows:
            self._kept.append(row)
            if row[0] >= start:
                yield row


def _collect(journeys, found: list[dict[str, Any]], seen: set[tuple], search_from: int) -> int:
    """Add the journeys not found yet; the next search starts a minute after
    the earliest first ride among them."""
    if not journeys:
        return search_from
    first_departures = []
    for arrival, legs in journeys:
        rides = [leg for leg in legs if isinstance(leg, planner.Ride)]
        lead = legs[0].seconds if isinstance(legs[0], planner.Walk) else 0
        departure = rides[0].departure - lead
        first_departures.append(rides[0].departure)
        key = tuple((leg.trip, leg.board_stop, leg.alight_stop) for leg in rides)
        if key in seen:
            continue
        seen.add(key)
        described = [_leg(leg) for leg in legs]
        found.append({
            "departure_time": _clock(departure),
            "arrival_time": _clock(arrival),
            "duration_minutes": round((arrival - departure) / 60),
            "changes": len(rides) - 1,
            "summary": _summary(described),
            "legs": described,
        })
    # A later departure arriving as early with no more changes makes an
    # earlier one pointless: the same train, caught by a later tram.
    found[:] = [item for item in found if not any(
        other is not item and other["departure_time"] >= item["departure_time"]
        and other["arrival_time"] <= item["arrival_time"] and other["changes"] <= item["changes"]
        for other in found)]
    return min(first_departures) + 60


def _place(stops: list[dict[str, str]]) -> dict[str, str]:
    """A resolved place: its stop, or for a whole town its first stop and the town."""
    if len(stops) == 1:
        return {key: stops[0].get(key, "") for key in ("stop_id", "stop_name")}
    return {"stop_id": stops[0]["stop_id"], "stop_name": _town(stops[0]["stop_name"])}


_CHANGE_TIME = 120          # s to change between trips at the same stop
_HORIZON = 12 * 3600        # s, the longest journey looked for
_SEARCH_WINDOW = 4 * 3600   # s after from_time in which departures are looked for
_TIME_BUDGET = 7.0          # s a search may take; then it returns what it has
_MORE_BUDGET = 1.5          # s after which it looks for no more connections
_REALTIME_BUDGET = 2.0      # s to wait for Golemio's departure boards
_NEXT_DAY_FROM = 4 * 3600   # s, from when tomorrow is searched when today has nothing
_LONG_JOURNEY_KM = 40       # beyond this the middle of a journey is long trips only
_NEAR_KM = 20               # around each end local trips count too
_LEAVE_ORIGIN = 90 * 60     # s in which a long journey leaves its origin's area
_TOP_SPEED_KMH = 160        # no journey covers the distance faster
_SLICE = 30 * 60            # s of local departures looked up by stop at a time
_LOOKUP_STOPS = 500         # up to this many stops near an end are looked up by stop
_CORRIDOR = 1.3             # a long journey's trips run within this ellipse


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=_warm_up_logged, daemon=True).start()
    mcp.run(transport="stdio")


def _warm_up_logged() -> None:
    started = time.perf_counter()
    try:
        warm_up()
    except Exception:
        log.exception("warm-up failed")
        return
    log.info("warm-up %.2f s", time.perf_counter() - started)


if __name__ == "__main__":
    main()
