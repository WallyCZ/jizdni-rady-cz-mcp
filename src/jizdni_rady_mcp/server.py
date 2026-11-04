"""Read-only MCP tools for the merged Czech GTFS feed."""

from __future__ import annotations

import csv
import gzip
import os
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Jízdní řády ČR")
DATASET = Path(os.environ.get("GTFS_DATASET", "data/merged"))


def _csv(name: str):
    path = DATASET / name
    if not path.is_file():
        raise FileNotFoundError(f"GTFS soubor nenalezen: {path}")
    return open(path, "r", encoding="utf-8-sig", newline="")


@lru_cache(maxsize=1)
def _stops() -> dict[str, dict[str, str]]:
    with _csv("stops.txt") as file:
        return {row["stop_id"]: row for row in csv.DictReader(file)}


@lru_cache(maxsize=1)
def _routes() -> dict[str, dict[str, str]]:
    with _csv("routes.txt") as file:
        return {row["route_id"]: row for row in csv.DictReader(file)}


@lru_cache(maxsize=1)
def _trips() -> dict[str, dict[str, str]]:
    with _csv("trips.txt") as file:
        return {row["trip_id"]: row for row in csv.DictReader(file)}


@lru_cache(maxsize=1)
def _calendar() -> dict[str, dict[str, str]]:
    with _csv("calendar.txt") as file:
        return {row["service_id"]: row for row in csv.DictReader(file)}


@lru_cache(maxsize=32)
def _exceptions(yyyymmdd: str) -> dict[str, str]:
    """Read only exceptions for a requested day; avoids retaining all dates."""
    with _csv("calendar_dates.txt") as file:
        return {
            row["service_id"]: row["exception_type"]
            for row in csv.DictReader(file)
            if row["date"] == yyyymmdd
        }


def _service_active(service_id: str, day: date) -> bool:
    day_key = day.strftime("%Y%m%d")
    exception = _exceptions(day_key).get(service_id)
    if exception is not None:
        return exception == "1"
    service = _calendar().get(service_id)
    if service is None or not (service["start_date"] <= day_key <= service["end_date"]):
        return False
    return service[("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]] == "1"


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


def _stop_tokens(value: str) -> set[str]:
    """Normalise common Czech word endings so Radlické matches Radlická."""
    vowels = "aeiouy"
    tokens = []
    for token in _normalized(value).replace(",", " ").split():
        tokens.append(token.rstrip(vowels) if len(token) > 4 else token)
    return set(tokens)


def _resolve_stop(query: str) -> dict[str, str]:
    query_tokens = _stop_tokens(query)
    if not query_tokens:
        raise ValueError("Název zastávky nesmí být prázdný.")
    candidates = []
    for stop in _stops().values():
        name = stop.get("stop_name", "")
        name_tokens = _stop_tokens(name)
        if query_tokens <= name_tokens:
            exact = _normalized(query) == _normalized(name)
            candidates.append((not exact, len(name), name, stop))
    if not candidates:
        raise ValueError(f"Zastávka nenalezena: {query}. Použijte search_stops pro přesný název.")
    return sorted(candidates, key=lambda item: item[:3])[0][3]


@mcp.tool()
def dataset_summary() -> dict[str, Any]:
    """Vrátí základní informace o lokálně připojeném GTFS datasetu."""
    return {
        "dataset": str(DATASET),
        "stops": len(_stops()),
        "routes": len(_routes()),
        "trips": len(_trips()),
        "stop_times_compressed_bytes": (DATASET / "stop_times.txt.gz").stat().st_size,
    }


@mcp.tool()
def search_stops(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Vyhledá zastávky podle části názvu a vrátí jejich ID a souřadnice."""
    needle = query.casefold().strip()
    if not needle:
        raise ValueError("Vyhledávací dotaz nesmí být prázdný.")
    limit = max(1, min(limit, 50))
    matches = [
        {key: row.get(key, "") for key in ("stop_id", "stop_name", "stop_lat", "stop_lon")}
        for row in _stops().values()
        if needle in row.get("stop_name", "").casefold()
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
    for row in _routes().values():
        searchable = " ".join((row.get("route_short_name", ""), row.get("route_long_name", ""))).casefold()
        if needle in searchable and (route_type is None or row.get("route_type") == str(route_type)):
            matches.append({key: row.get(key, "") for key in ("route_id", "route_short_name", "route_long_name", "route_type", "agency_id")})
    return sorted(matches, key=lambda item: (item["route_short_name"], item["route_long_name"]))[:limit]


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
    if stop_id not in _stops():
        raise ValueError(f"Neznámé stop_id: {stop_id}")
    limit = max(1, min(limit, 100))
    found: list[dict[str, str]] = []
    stop_times = DATASET / "stop_times.txt.gz"
    if not stop_times.is_file():
        raise FileNotFoundError(f"GTFS soubor nenalezen: {stop_times}")
    with gzip.open(stop_times, "rt", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row.get("stop_id") != stop_id:
                continue
            departure = row.get("departure_time", "")
            if not departure or not start <= _seconds(departure) <= end:
                continue
            trip = _trips().get(row["trip_id"])
            if trip is None or not _service_active(trip["service_id"], day):
                continue
            route = _routes().get(trip["route_id"], {})
            found.append({
                "departure_time": departure,
                "arrival_time": row.get("arrival_time", ""),
                "route_id": trip["route_id"],
                "route_short_name": route.get("route_short_name", ""),
                "route_long_name": route.get("route_long_name", ""),
                "trip_id": row["trip_id"],
            })
    return sorted(found, key=lambda item: item["departure_time"])[:limit]


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
    pending: dict[str, dict[str, str]] = {}
    connections: list[dict[str, str]] = []
    with gzip.open(DATASET / "stop_times.txt.gz", "rt", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            trip_id = row.get("trip_id", "")
            if row.get("stop_id") == origin["stop_id"]:
                departure = row.get("departure_time", "")
                trip = _trips().get(trip_id)
                if departure and _seconds(departure) >= start and trip and _service_active(trip["service_id"], day):
                    pending[trip_id] = row
            elif row.get("stop_id") == destination["stop_id"] and trip_id in pending:
                origin_row = pending.pop(trip_id)
                if int(row.get("stop_sequence", "0")) <= int(origin_row.get("stop_sequence", "0")):
                    continue
                trip = _trips()[trip_id]
                route = _routes().get(trip["route_id"], {})
                connections.append({
                    "departure_time": origin_row.get("departure_time", ""),
                    "arrival_time": row.get("arrival_time", ""),
                    "route_id": trip["route_id"],
                    "route_short_name": route.get("route_short_name", ""),
                    "route_long_name": route.get("route_long_name", ""),
                    "trip_id": trip_id,
                })
    return {
        "origin": {key: origin.get(key, "") for key in ("stop_id", "stop_name")},
        "destination": {key: destination.get(key, "") for key in ("stop_id", "stop_name")},
        "service_date": day.isoformat(),
        "connections": sorted(connections, key=lambda item: item["departure_time"])[:limit],
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
