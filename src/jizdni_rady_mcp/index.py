"""Builds a SQLite index of a GTFS feed, so queries need not scan stop_times.

Besides the GTFS tables it holds what the journey planner scans: every hop of
every trip between two consecutive stops, ordered by departure, and the walks
between stops close to each other.

    python -m jizdni_rady_mcp.index <gtfs-dir> <output.sqlite>
"""

from __future__ import annotations

import csv
import gzip
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE stops (id INTEGER PRIMARY KEY, stop_id TEXT UNIQUE, stop_name TEXT,
                    stop_lat TEXT, stop_lon TEXT);
CREATE TABLE routes (route_id TEXT PRIMARY KEY, agency_id TEXT, route_short_name TEXT,
                     route_long_name TEXT, route_type TEXT);
CREATE TABLE trips (id INTEGER PRIMARY KEY, trip_id TEXT, route_id TEXT, service_id TEXT);
CREATE TABLE calendar (service_id TEXT PRIMARY KEY, days TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE calendar_dates (service_id TEXT, date TEXT, exception_type INTEGER);
-- trip and stop are the integer ids of trips and stops: a third of the size
-- of the GTFS text ids, five million times over.
CREATE TABLE stop_times (trip INTEGER, stop_sequence INTEGER, stop INTEGER,
                         arrival INTEGER, departure INTEGER,
                         PRIMARY KEY (trip, stop_sequence)) WITHOUT ROWID;
-- A trip's hop from from_stop to the next stop, keyed by departure so the
-- planner reads them in time order without a separate index.
CREATE TABLE hops (departure INTEGER, trip INTEGER, stop_sequence INTEGER,
                   from_stop INTEGER, to_stop INTEGER, arrival INTEGER,
                   PRIMARY KEY (departure, trip, stop_sequence)) WITHOUT ROWID;
CREATE TABLE footpaths (from_stop INTEGER, to_stop INTEGER, seconds INTEGER,
                        PRIMARY KEY (from_stop, to_stop)) WITHOUT ROWID;
"""

WALK_RADIUS = 400       # m, straight line
WALK_SPEED = 1.0        # m/s along the straight line; streets are not straight
MIN_WALK = 120          # s, the shortest change between two stops
MAX_SPEED_KMH = 200     # faster between two stops is a merged stop, not a ride
MIN_LEAP_KM = 2

INDEXES = """
CREATE INDEX stop_times_stop ON stop_times (stop, departure);
CREATE INDEX calendar_dates_date ON calendar_dates (date);
"""

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _seconds(value: str) -> int | None:
    if not value:
        return None
    hours, minutes, *rest = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + (int(rest[0]) if rest else 0)


def _rows(directory: Path, name: str):
    plain, packed = directory / name, directory / (name + ".gz")
    if plain.is_file():
        file = open(plain, "r", encoding="utf-8-sig", newline="")
    elif packed.is_file():
        file = gzip.open(packed, "rt", encoding="utf-8-sig", newline="")
    else:
        raise FileNotFoundError(f"GTFS soubor nenalezen: {plain}")
    with file:
        yield from csv.DictReader(file)


def _footpaths(stops):
    """Walks between stops at most WALK_RADIUS apart, both ways."""
    cell = WALK_RADIUS / 111_320          # degrees of latitude per grid cell
    grid: dict[tuple[int, int], list] = {}
    points = []
    for stop_id, lat, lon in stops:
        try:
            point = (stop_id, float(lat), float(lon))
        except ValueError:
            continue
        if not point[1] and not point[2]:      # 0,0: the feed does not know where
            continue
        points.append(point)
        grid.setdefault((int(point[1] / cell), int(point[2] / cell)), []).append(point)
    for stop_id, lat, lon in points:
        scale = math.cos(math.radians(lat))
        row, col = int(lat / cell), int(lon / cell)
        for dr in (-1, 0, 1):
            for dc in range(-3, 4):           # a degree of longitude is shorter
                for other, olat, olon in grid.get((row + dr, col + dc), ()):
                    if other == stop_id:
                        continue
                    metres = math.hypot(olat - lat, (olon - lon) * scale) * 111_320
                    if metres <= WALK_RADIUS:
                        yield stop_id, other, max(MIN_WALK, math.ceil(metres / WALK_SPEED / 60) * 60)


def build(directory: Path, output: Path) -> None:
    started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    partial.unlink(missing_ok=True)
    db = sqlite3.connect(partial)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)

    def load(table: str, columns: tuple[str, ...], rows) -> None:
        marks = ",".join("?" * len(columns))
        db.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", rows)

    stops: dict[str, int] = {}
    load("stops", ("id", "stop_id", "stop_name", "stop_lat", "stop_lon"),
         ((stops.setdefault(r["stop_id"], len(stops) + 1), r["stop_id"], r.get("stop_name", ""),
           r.get("stop_lat", ""), r.get("stop_lon", ""))
          for r in _rows(directory, "stops.txt")))
    load("routes", ("route_id", "agency_id", "route_short_name", "route_long_name", "route_type"),
         ((r["route_id"], r.get("agency_id", ""), r.get("route_short_name", ""),
           r.get("route_long_name", ""), r.get("route_type", ""))
          for r in _rows(directory, "routes.txt")))
    trips: dict[str, int] = {}
    load("trips", ("id", "trip_id", "route_id", "service_id"),
         ((trips.setdefault(r["trip_id"], len(trips) + 1), r["trip_id"], r["route_id"], r["service_id"])
          for r in _rows(directory, "trips.txt")))
    load("calendar", ("service_id", "days", "start_date", "end_date"),
         ((r["service_id"], "".join(r[d] for d in DAYS), r["start_date"], r["end_date"])
          for r in _rows(directory, "calendar.txt")))
    load("calendar_dates", ("service_id", "date", "exception_type"),
         ((r["service_id"], r["date"], int(r["exception_type"]))
          for r in _rows(directory, "calendar_dates.txt")))
    load("stop_times", ("trip", "stop", "stop_sequence", "arrival", "departure"),
         ((trips[r["trip_id"]], stops[r["stop_id"]], int(r["stop_sequence"]),
           _seconds(r.get("arrival_time", "")), _seconds(r.get("departure_time", "")))
          for r in _rows(directory, "stop_times.txt")))
    db.execute("""
        INSERT INTO hops
        SELECT departure, trip, stop_sequence, stop, next_stop, next_arrival FROM (
            SELECT trip, stop_sequence, stop, departure,
                   lead(stop) OVER w AS next_stop, lead(arrival) OVER w AS next_arrival
            FROM stop_times WINDOW w AS (PARTITION BY trip ORDER BY stop_sequence))
        WHERE next_stop IS NOT NULL AND departure IS NOT NULL AND next_arrival IS NOT NULL
        ORDER BY departure, trip, stop_sequence""")
    # The feed merges places of the same name into one stop (Říčany near Prague
    # and near Brno), so a trip seems to leap across the country from it. Such
    # hops would let the planner change there between the two places.
    dropped = db.execute(f"""
        DELETE FROM hops WHERE (departure, trip, stop_sequence) IN (
            SELECT h.departure, h.trip, h.stop_sequence FROM hops h
            JOIN stops a ON a.id = h.from_stop JOIN stops b ON b.id = h.to_stop
            WHERE a.stop_lat + 0 != 0 AND b.stop_lat + 0 != 0 AND
                  ((b.stop_lat - a.stop_lat) * 111.32) * ((b.stop_lat - a.stop_lat) * 111.32) +
                  ((b.stop_lon - a.stop_lon) * 71.6) * ((b.stop_lon - a.stop_lon) * 71.6)
                  > max({MIN_LEAP_KM ** 2},
                        ({MAX_SPEED_KMH} / 3600.0 * max(h.arrival - h.departure, 60))
                        * ({MAX_SPEED_KMH} / 3600.0 * max(h.arrival - h.departure, 60))))""").rowcount
    print(f"Vyřazeno {dropped} úseků s nemožnou rychlostí", file=sys.stderr)
    load("footpaths", ("from_stop", "to_stop", "seconds"),
         _footpaths(db.execute("SELECT id, stop_lat, stop_lon FROM stops").fetchall()))
    db.commit()
    db.execute("VACUUM")
    db.executescript(INDEXES)
    db.execute("INSERT INTO meta VALUES ('source', ?)", (str(directory),))
    db.commit()
    db.execute("ANALYZE")
    db.close()
    os.replace(partial, output)
    print(f"Index {output} sestaven za {time.monotonic() - started:.0f} s", file=sys.stderr)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("Použití: python -m jizdni_rady_mcp.index <gtfs-adresář> <výstup.sqlite>")
    build(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
