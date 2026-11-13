"""Times the connections tool on a set of queries, from a cold start.

    GTFS_DB=<index.sqlite> python scripts/bench_connections.py [YYYY-MM-DD] [HH:MM]

Prints the time of the warm-up (index, active trips, footpaths), then each
query's time, the number of connections, whether the result is partial and
the first connection's summary.
"""

from __future__ import annotations

import sys
import time

from jizdni_rady_mcp import server

QUERIES = [
    ("Drinopol", "Můstek"),
    ("z Drinopolu", "do Větřkovic"),
    ("Televizní věž", "Praha hlavní nádraží"),
    ("Drinopol", "Radlická"),
    ("Praha hlavní nádraží", "Brno hlavní nádraží"),
]


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else None
    at = sys.argv[2] if len(sys.argv) > 2 else None
    started = time.perf_counter()
    server.warm_up(day)
    print(f"warm-up: {time.perf_counter() - started:.2f} s")
    for origin, destination in QUERIES:
        started = time.perf_counter()
        result = server.connections(origin, destination, service_date=day, from_time=at)
        elapsed = time.perf_counter() - started
        found = result["connections"]
        print(f"{origin} -> {destination}: {elapsed:.2f} s, {len(found)} spojení"
              f"{', partial' if result.get('partial') else ''}")
        if found:
            print(f"    {found[0]['summary']}")


if __name__ == "__main__":
    main()
