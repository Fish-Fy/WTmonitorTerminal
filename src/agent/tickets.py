"""Estimate ground battle tickets from live capture points and HUD deaths."""

from math import ceil, isfinite

from .roster import normalize_key


START_TICKETS_BY_ZONE_COUNT = {1: 16_000, 3: 18_000}
CAPTURE_TICKETS_PER_SECOND = 28
TICKETS_PER_DEATH = 100


class TicketEstimate:
    def __init__(self, freshness: float):
        self.freshness = freshness
        self.reset()

    def reset(self) -> None:
        self.points: tuple[int, int] | None = None
        self.zone_count = 0
        self.losing_team: int | None = None
        self.last_frame_at = 0.0
        self.capture_bleed_seconds = [0.0, 0.0]
        self.death_losses = [0, 0]
        self.deaths_seen: set[tuple[str, float]] = set()
        self.pending_deaths: dict[tuple[str, float], str] = {}

    def observe(self, objects, now: float) -> bool:
        """Use live single-point or three-point frames, including mid-battle joins."""
        zones = {}
        for obj in objects:
            if obj.type != "capture_zone" and obj.icon != "capture_zone":
                continue
            key = obj.zone_label.upper() if obj.zone_label else obj.zone_id
            if key == -1:
                key = (obj.x, obj.y, obj.sx, obj.sy, obj.ex, obj.ey)
            owner = obj.zone_owner
            if key in zones and zones[key] != owner:
                return False
            zones[key] = owner
        count = len(zones)
        if count not in START_TICKETS_BY_ZONE_COUNT or count < self.zone_count:
            return False

        points = (sum(side == "friend" for side in zones.values()),
                  sum(side == "enemy" for side in zones.values()))
        if self.losing_team is not None:
            elapsed = max(0.0, now - self.last_frame_at)
            if points != self.points:
                elapsed = min(elapsed, self.freshness)
            self.capture_bleed_seconds[self.losing_team] += elapsed
        self.points = points
        self.zone_count = count
        if points[0] == points[1]:
            self.losing_team = None
        else:
            self.losing_team = 0 if points[0] < points[1] else 1
        self.last_frame_at = now
        return True

    def record_death(self, name: str | None, timestamp: float) -> None:
        if not name or not isfinite(timestamp) or timestamp <= 0:
            return
        key = (normalize_key(name), timestamp)
        if key not in self.deaths_seen:
            self.deaths_seen.add(key)
            self.pending_deaths[key] = name

    def resolve_deaths(self, side_of) -> None:
        teams = {"me": 0, "friend": 0, "enemy": 1}
        for key, name in tuple(self.pending_deaths.items()):
            team = teams.get(side_of(name))
            if team is not None:
                self.death_losses[team] += TICKETS_PER_DEATH
                del self.pending_deaths[key]

    def seconds_left(self, now: float) -> int | None:
        elapsed = max(0.0, now - self.last_frame_at)
        team = self.losing_team
        if team is None or elapsed > self.freshness:
            return None
        tickets = START_TICKETS_BY_ZONE_COUNT[self.zone_count] - self.death_losses[team]
        seconds = tickets / CAPTURE_TICKETS_PER_SECOND - self.capture_bleed_seconds[team] - elapsed
        return max(0, ceil(seconds))
