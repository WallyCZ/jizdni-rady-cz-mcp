"""Journeys with changes: the Connection Scan Algorithm over the index's hops.

The hops of a day are read in departure order. For every number of rides
k (1 = direct, 2 = one change, ...) the scan keeps the earliest arrival at each
stop, so one pass gives the fastest journey for each number of changes. A trip
is boarded where a stop was reached in time: on foot from the previous ride
or by staying at the stop for the change time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

INF = 1 << 40


@dataclass
class Ride:
    trip: int
    board_stop: int
    alight_stop: int
    departure: int
    arrival: int


@dataclass
class Walk:
    from_stop: int
    to_stop: int
    seconds: int


def earliest(
    db: sqlite3.Connection,
    active_trips: frozenset[int],
    footpaths: dict[int, list[tuple[int, int]]],
    origin: int,
    destination: int,
    start: int,
    max_rides: int,
    change_time: int,
    horizon: int,
) -> list[tuple[int, list[Ride | Walk]]]:
    """Pareto-optimal journeys (arrival, legs) leaving origin at start or later:
    each one arrives earlier than every journey with fewer rides."""
    # arrival[k][stop]: earliest arrival there with k rides; ready[k][stop]:
    # when a trip can be boarded there (arrival plus the change time).
    arrival = [dict[int, int]() for _ in range(max_rides + 1)]
    ready = [dict[int, int]() for _ in range(max_rides + 1)]
    parent: list[dict[int, Ride | Walk | None]] = [{} for _ in range(max_rides + 1)]
    arrival[0][origin] = ready[0][origin] = start
    parent[0][origin] = None
    for other, seconds in footpaths.get(origin, ()):
        arrival[0][other] = ready[0][other] = start + seconds
        parent[0][other] = Walk(origin, other, seconds)
    trip_rides: dict[int, int] = {}            # rides used when on this trip
    trip_board: dict[int, tuple[int, int]] = {}  # trip -> (stop, departure)
    best = arrival[0].get(destination, INF)

    rows = db.cursor()
    rows.row_factory = None                    # plain tuples: the scan reads ~10^5 rows
    rows.execute(
        "SELECT departure, trip, from_stop, to_stop, arrival FROM hops WHERE departure >= ? AND departure <= ?",
        (start, start + horizon),
    )
    for departure, trip, from_stop, to_stop, arrive in rows:
        if departure >= best:
            break
        if trip not in active_trips:
            continue
        rides = trip_rides.get(trip)
        # Board here if this stop was reached in time with fewer rides.
        for k in range(0, max_rides if rides is None else rides - 1):
            if ready[k].get(from_stop, INF) <= departure:
                rides = trip_rides[trip] = k + 1
                trip_board[trip] = (from_stop, departure)
                break
        if rides is None:
            continue
        if arrive >= arrival[rides].get(to_stop, INF):
            continue
        board_stop, board_departure = trip_board[trip]
        ride = Ride(trip, board_stop, to_stop, board_departure, arrive)
        arrival[rides][to_stop] = arrive
        ready[rides][to_stop] = arrive + change_time
        parent[rides][to_stop] = ride
        if to_stop == destination:
            best = min(best, arrive)
        for other, seconds in footpaths.get(to_stop, ()):
            if arrive + seconds < arrival[rides].get(other, INF):
                arrival[rides][other] = ready[rides][other] = arrive + seconds
                parent[rides][other] = Walk(to_stop, other, seconds)
                if other == destination:
                    best = min(best, arrive + seconds)

    journeys = []
    fastest = INF
    for k in range(1, max_rides + 1):
        arrive = arrival[k].get(destination, INF)
        if arrive < fastest:
            fastest = arrive
            journeys.append((arrive, _legs(parent, k, destination)))
    return journeys


def _legs(parent, k: int, stop: int) -> list[Ride | Walk]:
    legs: list[Ride | Walk] = []
    while True:
        step = parent[k][stop]
        if step is None:
            break
        legs.append(step)
        if isinstance(step, Walk):
            stop = step.from_stop
        else:
            stop, k = step.board_stop, k - 1
    legs.reverse()
    return legs
