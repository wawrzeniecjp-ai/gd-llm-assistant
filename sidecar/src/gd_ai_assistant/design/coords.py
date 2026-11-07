"""Grid and level coordinates (LEVEL-DESIGN-SPEC.md 4.1).

Level coordinates are what the protocol uses: x right, y up, GD units, positions at
object centres. Grid coordinates are integer cells of `CELL` units, with **row 0 the
first row of cells sitting on the ground**.

The ground's height is the one input that is not settled (spike S-L1), so it is a
parameter of `Grid` rather than a module constant, and `Grid.from_constants` refuses
when it has not been measured. Whatever S-L1 finds -- a constant, or a value the mod
reports per level -- only the construction of `Grid` changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import PhysicsConstants, load_constants

#: GD units per grid cell. The protocol's `grid_size` (SPEC.md 7.5) is this value.
CELL = 30.0


class GroundUnknownError(RuntimeError):
    """The ground height has not been measured, so grid rows cannot be placed."""


@dataclass(frozen=True)
class Cell:
    col: int
    row: int


@dataclass(frozen=True)
class Grid:
    """Converts between level and grid coordinates for one ground height."""

    ground_top_y: float

    @classmethod
    def from_constants(cls, constants: PhysicsConstants | None = None) -> Grid:
        constants = constants or load_constants()
        if not constants.ground_top_y.known:
            raise GroundUnknownError(
                "the ground height has not been measured yet (spike S-L1); "
                "grid rows cannot be converted to level coordinates"
            )
        assert constants.ground_top_y.value is not None
        return cls(ground_top_y=constants.ground_top_y.value)

    # -- level -> grid --------------------------------------------------

    def col_of(self, x: float) -> int:
        return math.floor(x / CELL)

    def row_of(self, y: float) -> int:
        return math.floor((y - self.ground_top_y) / CELL)

    def cell_of(self, x: float, y: float) -> Cell:
        return Cell(self.col_of(x), self.row_of(y))

    # -- grid -> level --------------------------------------------------

    def centre_x(self, col: int) -> float:
        return col * CELL + CELL / 2

    def centre_y(self, row: int) -> float:
        return self.ground_top_y + row * CELL + CELL / 2

    def centre(self, cell: Cell) -> tuple[float, float]:
        return self.centre_x(cell.col), self.centre_y(cell.row)

    def offset_from_centre(self, x: float, y: float) -> tuple[float, float]:
        """How far a point is from the centre of the cell containing it."""
        cell = self.cell_of(x, y)
        cx, cy = self.centre(cell)
        return x - cx, y - cy
