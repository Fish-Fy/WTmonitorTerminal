"""Associate friendly map positions across two frames without stable unit IDs."""
from __future__ import annotations

from collections import defaultdict, deque
from math import dist, isfinite

from .roster import normalize_key


Position = tuple[float, float]
Observation = tuple[str, Position]
DEATH_MATCH_WINDOW = 2.0
DEATH_EVIDENCE_TTL = 5.0


def match_teammates(previous: list[Observation], current: list[Observation],
                    max_distance: float, *, air_distance: float | None = None) -> list[tuple[int, int, bool]]:
    """Return one-to-one matches and whether a trail can safely join each pair."""
    current_by_kind = defaultdict(list)
    for index, (kind, position) in enumerate(current):
        current_by_kind[kind].append((index, position))

    candidates = sorted(
        (dist(old_position, new_position), old_index, new_index)
        for old_index, (kind, old_position) in enumerate(previous)
        for new_index, new_position in current_by_kind[kind]
    )
    old_choices = [[] for _ in previous]
    new_choices = [[] for _ in current]
    for distance, old_index, new_index in candidates:
        old_choices[old_index].append(distance)
        new_choices[new_index].append(distance)

    matches = []
    used_old, used_new = set(), set()
    furthest = max(max_distance, air_distance or 0.0)
    for distance, old_index, new_index in candidates:
        if distance > furthest:
            break
        limit = air_distance if previous[old_index][0] == "aircraft" and air_distance is not None else max_distance
        if distance > limit:
            continue
        if old_index in used_old or new_index in used_new:
            continue
        # Nearby alternatives at either endpoint make the identity uncertain.
        # The pair still counts as present for disappearance detection.
        limit = distance + max(0.002, distance * 0.5)
        continuous = all(len(choices) == 1 or choices[1] > limit
                         for choices in (old_choices[old_index], new_choices[new_index]))
        matches.append((old_index, new_index, continuous))
        used_old.add(old_index)
        used_new.add(new_index)
    return matches


class AircraftLossTracker:
    """Confirm a missing plane only across consecutive live frames without a replacement."""

    def __init__(self):
        self.pending: list[tuple[float, Position]] = []
        self.last_frame: float | None = None

    def reset(self):
        self.pending.clear()
        self.last_frame = None

    def observe(self, previous: list[Observation], current: list[Observation],
                matches: list[tuple[int, int, bool]], now: float) -> list[Position]:
        if self.last_frame is None or now - self.last_frame > 2.0:
            previous = []
            self.pending.clear()
        self.last_frame = now
        old_seen = {old for old, _, _ in matches}
        new_seen = {new for _, new, _ in matches}
        if any(kind == "aircraft" and i not in new_seen for i, (kind, _) in enumerate(current)):
            # Without unit IDs a newly seen aircraft could be any missing plane.
            self.pending.clear()
            return []
        self.pending.extend((now, pos) for i, (kind, pos) in enumerate(previous)
                            if kind == "aircraft" and i not in old_seen)
        confirmed = [pos for since, pos in self.pending if now - since >= 1.0]
        self.pending = [(since, pos) for since, pos in self.pending if now - since < 1.0]
        return confirmed


class TeammateIconLossTracker:
    """Use a lasting drop in live icon count, with no trail-distance cutoff."""

    def __init__(self):
        self._previous: dict[str, list[Observation]] = {}
        self._pending: dict[str, deque[tuple[float, Position]]] = {}
        self._last_frame: float | None = None

    def reset(self):
        self._previous.clear()
        self._pending.clear()
        self._last_frame = None

    def observe(self, observations: list[Observation], now: float) -> list[tuple[float, Position]]:
        if self._last_frame is not None and now - self._last_frame > 2.0:
            self.reset()
        current = defaultdict(list)
        for observation in observations:
            current[observation[0]].append(observation)

        confirmed = []
        for kind in self._previous.keys() | current.keys() | self._pending.keys():
            before = self._previous.get(kind, [])
            after = current.get(kind, [])
            pending = self._pending.get(kind, deque())
            if len(after) < len(before):
                # With anonymous map icons, positions only locate the missing icons
                # after a drop has been seen. Even a large move can keep an icon present.
                retained = {old for old, _, _ in match_teammates(before, after, float("inf"))}
                pending.extend((now, point) for i, (_, point) in enumerate(before) if i not in retained)
            elif len(after) > len(before):
                for _ in range(min(len(after) - len(before), len(pending))):
                    pending.popleft()
            while pending and now - pending[0][0] >= 1.0:
                confirmed.append(pending.popleft())
            if pending:
                self._pending[kind] = pending
            else:
                self._pending.pop(kind, None)
        self._previous = current
        self._last_frame = now
        return confirmed


class TeammateDeathMatcher:
    """Use anonymous icon losses to locate unique HUD player deaths."""

    def __init__(self):
        self._losses: list[tuple[float, Position]] = []
        self._deaths: dict[tuple[str, float], tuple[str, float]] = {}
        self._seen: set[tuple[str, float]] = set()

    def reset(self):
        self._losses.clear()
        self._deaths.clear()
        self._seen.clear()

    def record_losses(self, losses: list[tuple[float, Position]]) -> None:
        self._losses.extend(losses)

    def record_death(self, name: str, match_time: float, local_time: float) -> None:
        if not isfinite(match_time) or match_time <= 0 or not isfinite(local_time):
            return
        key = (normalize_key(name), match_time)
        if key not in self._seen:
            self._seen.add(key)
            self._deaths[key] = (name, local_time)

    def match(self, side_of, now: float) -> list[Position]:
        cutoff = now - DEATH_EVIDENCE_TTL
        self._losses = [(at, point) for at, point in self._losses if at >= cutoff]
        self._deaths = {key: value for key, value in self._deaths.items() if value[1] >= cutoff}
        if not self._losses or not self._deaths:
            return []

        friends = [(key, at) for key, (name, at) in self._deaths.items() if side_of(name) == "friend"]
        candidates = sorted(
            (abs(loss_at - death_at), index, key)
            for key, death_at in friends
            for index, (loss_at, _) in enumerate(self._losses)
            if abs(loss_at - death_at) <= DEATH_MATCH_WINDOW
        )
        matched = []
        used_losses = set()
        for _, index, key in candidates:
            if index in used_losses or key not in self._deaths:
                continue
            used_losses.add(index)
            del self._deaths[key]
            matched.append(self._losses[index][1])
        self._losses = [loss for index, loss in enumerate(self._losses) if index not in used_losses]
        return matched
