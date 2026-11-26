"""Geometry for SPI guides in map coordinates."""
from math import hypot, isfinite, radians, tan

from .mapobj import MapObj

Point = tuple[float, float]
COURSE_TOLERANCE = tan(radians(1.0))


def course_aligned(movement: Point, position: Point, target: Point) -> bool:
    """Check whether the measured course points within 1° of the SPI in map pixels."""
    dx, dy = movement
    tx, ty = target[0] - position[0], target[1] - position[1]
    forward = dx * tx + dy * ty
    return forward > 0 and abs(dx * ty - dy * tx) <= forward * COURSE_TOLERANCE


def runway_approach(player: Point, target: Point, runways: list[MapObj],
                    width: float, height: float) -> tuple[Point, Point] | None:
    """Return the nearest friendly runway's visible outward extension, in scene pixels."""
    short_side = min(width, height)
    best_distance = float('inf')
    best = None
    for runway in runways:
        if runway.type != 'airfield' or runway.side != 'friend' or not runway.is_segment:
            continue
        endpoints = (runway.sx, runway.sy, runway.ex, runway.ey)
        if any(value is None or not isfinite(value) for value in endpoints):
            continue
        start = (runway.sx * width, runway.sy * height)
        end = (runway.ex * width, runway.ey * height)
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = hypot(dx, dy)
        if not isfinite(length) or length <= 0:
            continue
        ux, uy = dx / length, dy / length
        tx, ty = target[0] - start[0], target[1] - start[1]
        along = max(0.0, min(length, tx * ux + ty * uy))
        distance = hypot(tx - along * ux, ty - along * uy)
        if distance <= min(length * .75, short_side * .035) and distance < best_distance:
            best_distance = distance
            best = start, end, (ux, uy), length
    if best is None:
        return None

    start, end, direction, length = best
    threshold = min((start, end), key=lambda p: (hypot(p[0] - player[0], p[1] - player[1]), p))
    if threshold == start:
        direction = (-direction[0], -direction[1])
    return _clip_extension(threshold, direction, min(length * 5, short_side * .18), width, height)


def _clip_extension(start: Point, direction: Point, length: float,
                    width: float, height: float) -> tuple[Point, Point] | None:
    """Intersect the finite extension with the image rectangle."""
    first, last = 0.0, length
    for origin, step, bound in zip(start, direction, (width, height)):
        if step == 0:
            if not 0 <= origin <= bound:
                return None
            continue
        a, b = sorted((-origin / step, (bound - origin) / step))
        first, last = max(first, a), min(last, b)
    if last - first <= 1e-6:
        return None
    return ((start[0] + first * direction[0], start[1] + first * direction[1]),
            (start[0] + last * direction[0], start[1] + last * direction[1]))
