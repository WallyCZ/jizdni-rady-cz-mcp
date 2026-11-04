"""Builds a SQLite index of a GTFS feed, so queries need not scan stop_times.

    python -m jizdni_rady_mcp.index <gtfs-dir> <output.sqlite>
"""

from __future__ import annotations

import csv
import gzip
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
"""

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
