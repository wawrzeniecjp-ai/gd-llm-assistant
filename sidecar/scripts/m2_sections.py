"""The 20 M2 test sections as GridText (docs/M2-test-sections.md).

Slot k occupies columns 100k to 100k+99. Each section below is drawn from the slot's
column 0, so the string index is the column inside the slot: the runway is 0-9 (start
position `@` at 2, a speed portal at 5, 1x unless the slot draws another), the section
10-89, the buffer 90-99 is left out.
Rows are listed top first and the bottom one is row 0, on the ground. Short rows are
padded with `.` on the right.

Slots 1-10 are meant to be possible, 11-20 impossible, built as pairs: slot k+10 is slot
k one notch harder. These are a starting point, to be pushed closer to the edge by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

SLOT_COLS = 100
WIDTH = 90          # runway + section; the buffer is not drawn


@dataclass(frozen=True)
class Section:
    slot: int
    title: str
    rows: tuple[str, ...]

    @property
    def expected(self) -> str:
        return "possible" if self.slot <= 10 else "impossible"

    @property
    def col(self) -> int:
        return self.slot * SLOT_COLS

    def grid_text(self) -> str:
        lines = [f"@grid col={self.col} row=0 w={WIDTH} h={len(self.rows)}"]
        lines += [row.ljust(WIDTH, ".") for row in self.rows]
        return "\n".join(lines)


SPEED_PORTALS = "12345"
PORTAL_COL = 5


def _s(slot: int, title: str, *rows: str) -> Section:
    for row in rows:
        if len(row) > WIDTH:
            raise ValueError(f"slot {slot}: row longer than {WIDTH}: {row!r}")
    return Section(slot, title, _with_speed_portal(rows))


def _with_speed_portal(rows: tuple[str, ...]) -> tuple[str, ...]:
    """Every runway sets its speed: a 1x portal (`2`) unless it draws its own.

    A start position starts at its own speed (1x by default), not the speed the level had
    reached there, so without a portal a slot plays at 1x from its start position but at
    the previous slot's speed when the level is played through.
    """
    if any(c in SPEED_PORTALS for row in rows for c in row[:10]):
        return rows
    if len(rows) < 2:
        rows = ("", *rows)
    above = rows[-2].ljust(PORTAL_COL + 1, ".")
    assert above[PORTAL_COL] == ".", above
    above = above[:PORTAL_COL] + "2" + above[PORTAL_COL + 1:]
    return (*rows[:-2], above, rows[-1])


#  Column ruler for reading the rows below (slot-relative):
#             0         1         2         3         4         5         6         7
#             0123456789012345678901234567890123456789012345678901234567890123456789012345
SECTIONS: tuple[Section, ...] = (
    # ---------------------------------------------------------------- possible
    _s(1, "3 spikes in a row",
       "..@.................^^^"),
    _s(2, "a 2-block step up, then down",
       "....................######",
       "..@.................######"),
    _s(3, "a yellow orb 3 rows up over a 7-spike pit",
       ".....................o",
       "",
       "",
       "..@.................^^^^^^^"),
    _s(4, "a yellow pad one column before a 4-block wall",
       ".......................#",
       ".......................#",
       ".......................#",
       "..@.................._.#"),
    _s(5, "a 5-spike gap between two 1-block platforms",
       "..@..............#####^^^^^#####"),
    _s(6, "a staircase of 1-block steps with spikes between",
       "..................................#",
       ".............................#....#",
       "........................#....#....#",
       "..@.................#^^^#^^^^#^^^^#"),
    _s(7, "a corridor with its ceiling 3 blocks up, and one spike",
       "...............##########################",
       "",
       "",
       "..@.........................^"),
    _s(8, "a 2x portal in the runway, then 4 spikes",
       ".....3",
       "..@.................^^^^"),
    _s(9, "a 0.5x portal in the runway, then 2 spikes",
       ".....1",
       "..@.................^^"),
    _s(10, "a long mixed section",
       "...................................................#",
       "..................................##...............#......o",
       "..................................####.............#.................######",
       "..@.................^^....#####^^^####....^^^...._.#.....^^^^^^^.....######"),
    # ---------------------------------------------------------------- impossible
    _s(11, "4 spikes in a row",
       "..@.................^^^^"),
    _s(12, "a 3-block step",
       "....................######",
       "....................######",
       "..@.................######"),
    _s(13, "the orb pit, with the orb a row higher",
       ".....................o",
       "",
       "",
       "",
       "..@.................^^^^^^^"),
    _s(14, "the yellow pad flush against the same 4-block wall",
       ".......................#",
       ".......................#",
       ".......................#",
       "..@..................._#"),
    _s(15, "a 6-spike gap between two 1-block platforms",
       "..@..............#####^^^^^^#####"),
    _s(16, "the staircase, with one step 3 blocks higher than the last",
       "..................................#",
       ".............................#....#",
       ".............................#....#",
       ".............................#....#",
       "........................#....#....#",
       "..@.................#^^^#^^^^#^^^^#"),
    _s(17, "the corridor with its ceiling 2 blocks up",
       "...............##########################",
       "",
       "..@.........................^"),
    _s(18, "a 2x portal in the runway, then 5 spikes",
       ".....3",
       "..@.................^^^^^"),
    _s(19, "a 0.5x portal in the runway, then 3 spikes",
       ".....1",
       "..@.................^^^"),
    _s(20, "the long mixed section with 4 spikes hidden in the middle",
       "...................................................#",
       "..................................##...............#......o",
       "..................................####.............#.................######",
       "..@.................^^....#####^^^####....^^^^..._.#.....^^^^^^^.....######"),
)
