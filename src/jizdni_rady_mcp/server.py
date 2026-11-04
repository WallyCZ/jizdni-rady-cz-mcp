"""Read-only MCP tools for the merged Czech GTFS feed."""

from __future__ import annotations

import os
import sqlite3
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from .index import build

mcp = FastMCP("Jízdní řády ČR")
DATASET = Path(os.environ.get("GTFS_DATASET", "data/merged"))
# The Docker image ships the index built from the dataset (GTFS_DB); outside it
# the index is built on first use into the user's cache directory.
DATABASE = Path(os.environ.get("GTFS_DB") or Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "jizdni-rady-cz-mcp" / "gtfs.sqlite")


@lru_cache(maxsize=1)
def _db() -> sqlite3.Connection:
    source = DATASET / "stop_times.txt.gz"
    stale = DATABASE.is_file() and source.is_file() and source.stat().st_mtime > DATABASE.stat().st_mtime
    if not DATABASE.is_file() or (stale and "GTFS_DB" not in os.environ):
        build(DATASET, DATABASE)
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


def _trip_rows(rows, active: frozenset[str], limit: int) -> list[dict[str, str]]:
    found = []
    for row in rows:
        if row["service_id"] not in active:
            continue
        found.append({
            "departure_time": _clock(row["departure"]),
            "arrival_time": _clock(row["arrival"]),
            "route_id": row["route_id"],
            "route_short_name": row["route_short_name"] or "",
            "route_long_name": row["route_long_name"] or "",
            "trip_id": row["trip_id"],
        })
        if len(found) == limit:
            break
    return found


@mcp.tool()
def departures(
    stop_id: str,
    service_date: str | None = None,
    from_time: str | None = None,
    to_time: str = "23:59:59",
    limit: int = 30,
) -> list[dict[str, str]]:
    """Vrátí plánované odjezdy ze zastávky pro zadané datum a časové okno.

    Nejprve použijte search_stops pro získání stop_id. Časy GTFS mohou přesáhnout 24:00.
    """
    day = _requested_date(service_date)
    start = _seconds(from_time) if from_time else 0
    end = _seconds(to_time)
    if start > end:
        raise ValueError("from_time musí být před to_time.")
    if _stop(stop_id) is None:
        raise ValueError(f"Neznámé stop_id: {stop_id}")
    limit = max(1, min(limit, 100))
    rows = _db().execute(
        """SELECT st.departure, st.arrival, t.trip_id, t.route_id, t.service_id,
                  r.route_short_name, r.route_long_name
           FROM stop_times st JOIN trips t ON t.id = st.trip
           LEFT JOIN routes r ON r.route_id = t.route_id
           WHERE st.stop = (SELECT id FROM stops WHERE stop_id = ?) AND st.departure BETWEEN ? AND ?
           ORDER BY st.departure""",
        (stop_id, start, end),
    )
    return _trip_rows(rows, _active_services(day), limit)


@mcp.tool()
def direct_connections(
    from_stop: str,
    to_stop: str,
    service_date: str | None = None,
    from_time: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Najde přímé spoje mezi dvěma zastávkami podle názvu.

    Zvládá běžné české pádové tvary názvů (např. „z Radlické“). Pro přestupy
    použijte samostatně search_stops a departures; tento nástroj hledá jen
    jednu linku bez přestupu.
    """
    origin, destination = _resolve_stop(from_stop), _resolve_stop(to_stop)
    day = _requested_date(service_date)
    start = _seconds(from_time) if from_time else 0
    limit = max(1, min(limit, 30))
    rows = _db().execute(
        """SELECT a.departure, b.arrival, t.trip_id, t.route_id, t.service_id,
                  r.route_short_name, r.route_long_name
           FROM stop_times a
           JOIN stop_times b ON b.trip = a.trip AND b.stop_sequence > a.stop_sequence
                            AND b.stop = (SELECT id FROM stops WHERE stop_id = ?)
           JOIN trips t ON t.id = a.trip
           LEFT JOIN routes r ON r.route_id = t.route_id
           WHERE a.stop = (SELECT id FROM stops WHERE stop_id = ?) AND a.departure >= ?
           ORDER BY a.departure""",
        (destination["stop_id"], origin["stop_id"], start),
    )
    return {
        "origin": {key: origin.get(key, "") for key in ("stop_id", "stop_name")},
        "destination": {key: destination.get(key, "") for key in ("stop_id", "stop_name")},
        "service_date": day.isoformat(),
        "connections": _trip_rows(rows, _active_services(day), limit),
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
