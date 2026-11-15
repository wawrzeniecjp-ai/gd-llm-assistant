"""Where in the level is which moment of the song (LEVEL-DESIGN-SPEC.md 8.1).

Horizontal speed depends only on speed portals, so time and x convert piecewise: each
portal begins a segment at its speed. A portal takes effect where the cube's box first
touches it, half the cube's width before the portal's left edge -- the simulator's rule
(`sim.cube.collide`), so the timeline and the simulator never disagree. Mode portals do
not change horizontal speed and are ignored.

Level time is counted from the level start (x 0 at the start speed). The song plays from
the level's song offset, so `song_ms = level_ms + song_offset_s * 1000` (S-L7 confirms the
sign: the offset skips into the song).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from .constants import SPEEDS
from .level_model import LevelModel
from .roles import Role
from .sim.cube import HALF, CubeParams
from .sim.world import world_from_model

TICKS_PER_SECOND = 240.0


@dataclass(frozen=True)
class Segment:
    x: float          # where this speed starts
    ms: float         # level time at `x`
    speed: str
    portal_uid: int | None = None


class Timeline:
    def __init__(self, segments: list[Segment], params: CubeParams,
                 song_offset_s: float = 0.0) -> None:
        self.segments = segments
        self._xs = [s.x for s in segments]
        self._ms = [s.ms for s in segments]
        self._params = params
        self.song_offset_ms = song_offset_s * 1000.0

    @classmethod
    def build(cls, portals: list[tuple[float, str, int | None]], params: CubeParams,
              start_speed: str = "1x", song_offset_s: float = 0.0) -> Timeline:
        """`portals`: (activation x, speed, uid), in any order."""
        if start_speed not in SPEEDS:
            raise ValueError(f"unknown start speed {start_speed!r}")
        segments = [Segment(0.0, 0.0, start_speed)]
        for x, speed, uid in sorted(portals, key=lambda p: p[0]):
            if x <= segments[-1].x:
                # A portal at or before the start (or two at one x): the later one wins.
                segments[-1] = Segment(segments[-1].x, segments[-1].ms, speed, uid)
                continue
            last = segments[-1]
            if speed == last.speed:
                continue                      # a portal that changes nothing
            # The cube touches the portal on the first tick its move ends past `x`, and the
            # new speed applies from the next move: so the change happens on a tick, a
            # whole number of the old speed's steps after the segment began.
            step = params.dx[last.speed]
            ticks = math.floor((x - last.x) / step + 1e-9) + 1
            switch_x = last.x + ticks * step
            segments.append(Segment(switch_x, last.ms + ticks * 1000.0 / TICKS_PER_SECOND,
                                    speed, uid))
        return cls(segments, params, song_offset_s)

    @classmethod
    def from_model(cls, model: LevelModel, params: CubeParams, start_speed: str = "1x",
                   song_offset_s: float = 0.0) -> Timeline:
        # The simulator's own bodies, so a portal's box is exactly the one it collides with.
        world = world_from_model(model)
        portals = [(b.box.x1 - HALF, b.variant, b.uid) for b in world.bodies
                   if b.role is Role.PORTAL_SPEED and b.variant in SPEEDS]
        return cls.build(portals, params, start_speed, song_offset_s)

    @staticmethod
    def _ups(params: CubeParams, speed: str) -> float:
        return params.dx[speed] * TICKS_PER_SECOND

    def units_per_second(self, x: float) -> float:
        return self._ups(self._params, self.speed_at(x))

    def speed_at(self, x: float) -> str:
        return self.segments[max(bisect_right(self._xs, x) - 1, 0)].speed

    def ms_at(self, x: float) -> float:
        """Level time when the cube reaches `x`."""
        seg = self.segments[max(bisect_right(self._xs, x) - 1, 0)]
        return seg.ms + (x - seg.x) / self._ups(self._params, seg.speed) * 1000.0

    def x_at(self, ms: float) -> float:
        """Where the cube is at level time `ms`."""
        seg = self.segments[max(bisect_right(self._ms, ms) - 1, 0)]
        return seg.x + (ms - seg.ms) / 1000.0 * self._ups(self._params, seg.speed)

    def song_ms_at(self, x: float) -> float:
        return self.ms_at(x) + self.song_offset_ms

    def x_at_song_ms(self, song_ms: float) -> float:
        return self.x_at(song_ms - self.song_offset_ms)

    def describe(self) -> list[dict[str, Any]]:
        return [{"x": round(s.x, 1), "ms": round(s.ms, 1), "speed": s.speed,
                 "portal_uid": s.portal_uid} for s in self.segments]
