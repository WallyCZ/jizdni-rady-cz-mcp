"""Journeys with changes: the Connection Scan Algorithm over the index's hops.

The hops are read in departure order. For every number of rides k (1 =
direct, 2 = one change, ...) the scan keeps the earliest arrival at each stop,
so one pass gives the fastest journey for each number of changes. A trip is
boarded where a stop was reached in time: on foot from the previous ride or
by staying at the stop for the change time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

INF = 1 << 40
_CHECK_EVERY = 20_000      # hops between looks at the clock


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
    hops: Iterable[tuple[int, int, int, int, int, int]],
    footpaths: dict[int, list[tuple[int, int]]],
    origins: set[int],
    destinations: set[int],
    start: int,
    max_rides: int,
    change_time: int,
    deadline: float,
    boarding: dict[int, int] | None = None,
) -> tuple[list[tuple[int, list[Ride | Walk]]], bool]:
    """Pareto-optimal journeys (arrival, legs) leaving one of origins at start
    or later for one of destinations: each arrives earlier than every journey
    with fewer rides. hops are (departure, trip, from_stop, to_stop, arrival,
    stop_sequence)
    of the trips running that day, from start on, in departure order. The
    second value is False when the scan stopped at the deadline
    (time.monotonic()) before it was complete. boarding: trip -> seconds a
    change onto it takes on top (the metro's and trains' platforms)."""
    boarding = boarding or {}
    # arrival[k][stop]: earliest arrival there with k rides; ready[k][stop]:
    # when a trip can be boarded there (arrival plus the change time).
    arrival = [dict[int, int]() for _ in range(max_rides + 1)]
    ready = [dict[int, int]() for _ in range(max_rides + 1)]
    parent: list[dict[int, Ride | Walk | None]] = [{} for _ in range(max_rides + 1)]
    for origin in origins:
        arrival[0][origin] = ready[0][origin] = start
        parent[0][origin] = None
    for origin in origins:
        for other, seconds in footpaths.get(origin, ()):
            if other not in arrival[0]:
                arrival[0][other] = ready[0][other] = start + seconds
                parent[0][other] = Walk(origin, other, seconds)
    trip_rides: dict[int, int] = {}              # rides used when on this trip
    trip_board: dict[int, tuple[int, int]] = {}  # trip -> (stop, departure)
    best = INF
    complete = True
    count = 0

    for departure, trip, from_stop, to_stop, arrive, _ in hops:
        if departure >= best:
            break
        count += 1
        if count % _CHECK_EVERY == 0 and time.monotonic() > deadline:
            complete = False
            break
        rides = trip_rides.get(trip)
        # Board here if this stop was reached in time with fewer rides; a
        # change (k > 0) onto the metro or a train takes longer.
        extra = boarding.get(trip, 0)
        for k in range(0, max_rides if rides is None else rides - 1):
            if ready[k].get(from_stop, INF) + (extra if k else 0) <= departure:
                rides = trip_rides[trip] = k + 1
                trip_board[trip] = (from_stop, departure)
                break
        if rides is None:
            continue
        if arrive >= arrival[rides].get(to_stop, INF):
            continue
        board_stop, board_departure = trip_board[trip]
        arrival[rides][to_stop] = arrive
        ready[rides][to_stop] = arrive + change_time
        parent[rides][to_stop] = Ride(trip, board_stop, to_stop, board_departure, arrive)
        if to_stop in destinations and arrive < best:
            best = arrive
        for other, seconds in footpaths.get(to_stop, ()):
            if arrive + seconds < arrival[rides].get(other, INF):
                arrival[rides][other] = ready[rides][other] = arrive + seconds
                parent[rides][other] = Walk(to_stop, other, seconds)
                if other in destinations and arrive + seconds < best:
                    best = arrive + seconds

    journeys = []
    fastest = INF
    for k in range(1, max_rides + 1):
        reached = [(arrival[k][stop], stop) for stop in destinations if stop in arrival[k]]
        if not reached:
            continue
        arrive, stop = min(reached)
        if arrive < fastest:
            fastest = arrive
            journeys.append((arrive, _legs(parent, k, stop)))
    return journeys, complete


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
