"""The schematic renderer: `render_schematic` (LEVEL-DESIGN-SPEC.md 13).

A screenshot shows what the player sees; the schematic shows **structure**. It is drawn
from the level snapshot, so it works without the game (previews, evals), and every shape
is a hitbox, not a sprite: what is drawn is what collides.

M1 draws the objects and the `grid` overlay. `route`, `windows` and `camera` need the
simulator (M2), `beats` the timeline (M4) and `sections` the plan (M4); asking for them
returns a note saying so rather than an error, so a call written for later milestones
still works.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .coords import CELL, Grid
from .grid import LAYER_ROLES
from .level_model import LevelModel, LevelObject
from .roles import GAMEPLAY_ROLES, Role

MAX_WIDTH = 1600
MAX_HEIGHT = 600
MIN_PX_PER_CELL = 4
DEFAULT_PX_PER_CELL = 12
_HEADROOM_ROWS = 2
_TRIGGER_STRIP = 8  # px at the top of the image for trigger ticks

OVERLAYS_NOW = frozenset({"grid"})
OVERLAYS_LATER = {
    "route": "M2 (simulator)",
    "windows": "M2 (simulator)",
    "camera": "M2 (simulator)",
    "beats": "M4 (timeline)",
    "sections": "M4 (level plan)",
}

_BACKGROUND = (250, 250, 248)
_GRID_MINOR = (228, 228, 224)
_GRID_MAJOR = (196, 196, 190)
_GROUND = (214, 200, 176)
_LABEL = (90, 90, 90)

_ROLE_COLOURS: dict[Role, tuple[int, int, int]] = {
    Role.SOLID: (74, 111, 165),
    Role.SLOPE: (110, 140, 185),
    Role.HAZARD: (217, 69, 59),
    Role.PORTAL_MODE: (155, 89, 182),
    Role.PORTAL_SPEED: (230, 126, 34),
    Role.PORTAL_GRAVITY: (59, 143, 217),
    Role.PORTAL_SIZE: (46, 204, 113),
    Role.PORTAL_OTHER: (127, 140, 141),
    Role.START_POS: (39, 174, 96),
    Role.COLLECTIBLE: (241, 196, 15),
    Role.TRIGGER: (136, 136, 136),
    Role.SPECIAL: (200, 0, 200),
    Role.DECO: (150, 150, 150),
}

_VARIANT_COLOURS: dict[str, tuple[int, int, int]] = {
    "yellow": (242, 194, 48),
    "pink": (242, 126, 194),
    "blue": (59, 143, 217),
    "green": (76, 199, 100),
    "red": (224, 69, 59),
    "black": (40, 40, 40),
    "dash": (92, 214, 160),
    "gravity_dash": (180, 92, 214),
    "teleport": (120, 200, 230),
    "custom": (160, 160, 160),
}


class SchematicError(ValueError):
    pass


@dataclass
class Schematic:
    png: bytes
    width: int
    height: int
    px_per_cell: int
    cols: tuple[int, int]
    rows: tuple[int, int]
    notes: list[str] = field(default_factory=list)
    drawn: dict[str, int] = field(default_factory=dict)

    def describe(self, grid: Grid) -> dict[str, Any]:
        c0, c1 = self.cols
        r0, r1 = self.rows
        return {
            "width": self.width,
            "height": self.height,
            "px_per_cell": self.px_per_cell,
            "cols": [c0, c1],
            "rows": [r0, r1],
            "viewport": {
                "x1": c0 * CELL, "x2": (c1 + 1) * CELL,
                "y1": grid.ground_top_y + r0 * CELL, "y2": grid.ground_top_y + (r1 + 1) * CELL,
            },
            "drawn": self.drawn,
            "notes": self.notes,
            "legend": (
                "Shapes are hitboxes. Blue: solid. Red: hazard. Circles: orbs (filled) and "
                "pads (flat). Tall outlined bars: portals, labelled. Grey, faint: deco. "
                "Ticks along the top: triggers. Brown: below the ground. Grid lines every "
                "cell, darker every 10, column numbers along the top."
            ),
        }


def _corners(obj: LevelObject, *, visual: bool = False) -> list[tuple[float, float]] | None:
    """The hitbox's corners (or, with `visual`, the sprite's) in level units, scaled and
    rotated (clockwise) about the centre."""
    box = obj.role_entry.hitbox
    if visual:
        size = obj.role_entry.visual
        if size is None or size[0] <= 0 or size[1] <= 0:
            return None
        bx, by, bw, bh = -size[0] / 2, -size[1] / 2, size[0], size[1]
    elif box is None or box.w <= 0 or box.h <= 0:
        return None
    else:
        bx, by, bw, bh = box.x, box.y, box.w, box.h
    sx, sy = obj.scale_x or 1.0, obj.scale_y or 1.0
    xs = (bx * sx, (bx + bw) * sx)
    ys = (by * sy, (by + bh) * sy)
    theta = math.radians(obj.rotation)
    cos, sin = math.cos(theta), math.sin(theta)
    out = []
    for x, y in ((xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1])):
        if obj.flip_x:
            x = -x
        if obj.flip_y:
            y = -y
        out.append((obj.x + x * cos + y * sin, obj.y - x * sin + y * cos))
    return out


def render_schematic(model: LevelModel, grid: Grid, args: dict[str, Any]) -> Schematic:
    """Draw the requested range. Raises `SchematicError` for a range it cannot draw."""
    layers = args.get("layers") or ["gameplay", "deco"]
    unknown = [layer for layer in layers if layer not in LAYER_ROLES]
    if unknown:
        raise SchematicError(f"unknown layers {unknown}; use {sorted(LAYER_ROLES)}")
    overlays = args.get("overlays") or ["grid"]
    notes = [
        f"overlay '{name}' is not available until {OVERLAYS_LATER[name]}"
        for name in overlays if name in OVERLAYS_LATER
    ]
    bad = [name for name in overlays if name not in OVERLAYS_NOW and name not in OVERLAYS_LATER]
    if bad:
        raise SchematicError(f"unknown overlays {bad}")

    x_range = args.get("x_range")
    if not x_range:
        raise SchematicError("give `x_range` in level units")
    lo, hi = sorted(float(v) for v in x_range)
    c0, c1 = math.floor(lo / CELL), max(math.floor(lo / CELL), math.ceil(hi / CELL) - 1)
    if args.get("y_range"):
        ylo, yhi = sorted(float(v) - grid.ground_top_y for v in args["y_range"])
        r0, r1 = math.floor(ylo / CELL), max(math.floor(ylo / CELL), math.ceil(yhi / CELL) - 1)
    else:
        objs = model.in_rect(c0 * CELL, -1e9, (c1 + 1) * CELL, 1e9, roles=GAMEPLAY_ROLES)
        top = max((grid.row_of(o.y) for o in objs), default=0)
        r0, r1 = -1, max(top, 0) + _HEADROOM_ROWS  # one row of ground for reference
    cols, rows = c1 - c0 + 1, r1 - r0 + 1

    wanted = int(args.get("px_per_cell") or DEFAULT_PX_PER_CELL)
    fit = min(wanted, MAX_WIDTH // cols, (MAX_HEIGHT - _TRIGGER_STRIP) // rows)
    if fit < MIN_PX_PER_CELL:
        raise SchematicError(
            f"{cols}x{rows} cells cannot fit {MAX_WIDTH}x{MAX_HEIGHT} px at the minimum of "
            f"{MIN_PX_PER_CELL} px per cell; narrow the range (at most "
            f"{MAX_WIDTH // MIN_PX_PER_CELL} columns)"
        )
    if fit < wanted:
        notes.append(f"px_per_cell reduced from {wanted} to {fit} to fit the size limit")
    ppc = fit
    width, height = cols * ppc, rows * ppc + _TRIGGER_STRIP

    x0 = c0 * CELL
    y_top = grid.ground_top_y + (r1 + 1) * CELL

    def px(x: float, y: float) -> tuple[float, float]:
        return (x - x0) / CELL * ppc, _TRIGGER_STRIP + (y_top - y) / CELL * ppc

    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default()

    # Ground.
    _, ground_py = px(0, grid.ground_top_y)
    if ground_py < height:
        draw.rectangle((0, max(ground_py, _TRIGGER_STRIP), width, height), fill=_GROUND)

    if "grid" in overlays:
        for i in range(cols + 1):
            colour = _GRID_MAJOR if (c0 + i) % 10 == 0 else _GRID_MINOR
            draw.line((i * ppc, _TRIGGER_STRIP, i * ppc, height), fill=colour)
        for j in range(rows + 1):
            colour = _GRID_MAJOR if (r1 + 1 - j) % 10 == 0 else _GRID_MINOR
            y = _TRIGGER_STRIP + j * ppc
            draw.line((0, y, width, y), fill=colour)
        if ppc >= 6:
            for i in range(cols):
                if (c0 + i) % 10 == 0:
                    draw.text((i * ppc + 2, _TRIGGER_STRIP + 1), str(c0 + i), fill=_LABEL,
                              font=font)

    wanted_roles: set[Role] = set()
    for layer in layers:
        wanted_roles |= LAYER_ROLES[layer]
    # Specials and collectibles are drawn with gameplay: hiding them would hide the very
    # objects the simulator refuses to judge.
    if "gameplay" in layers:
        wanted_roles |= {Role.SPECIAL, Role.COLLECTIBLE}
    margin = 3 * CELL
    objects = model.in_rect(x0 - margin, y_top - (rows + 3) * CELL, (c1 + 1) * CELL + margin,
                            y_top + margin, roles=wanted_roles)
    # Deco first, then gameplay, so structure is never hidden under decoration.
    objects.sort(key=lambda o: (o.role is not Role.DECO, o.role is Role.HAZARD))

    drawn: dict[str, int] = {}
    for obj in objects:
        if obj.role is Role.TRIGGER:
            tx, _ = px(obj.x, 0)
            if 0 <= tx <= width:
                draw.line((tx, 0, tx, _TRIGGER_STRIP - 2), fill=_ROLE_COLOURS[Role.TRIGGER],
                          width=1)
                drawn["trigger"] = drawn.get("trigger", 0) + 1
            continue
        if _draw_object(draw, obj, px, ppc, font):
            drawn[obj.role.value] = drawn.get(obj.role.value, 0) + 1

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return Schematic(buffer.getvalue(), width, height, ppc, (c0, c1), (r0, r1), notes, drawn)


def _draw_object(draw: ImageDraw.ImageDraw, obj: LevelObject, px, ppc: int, font) -> bool:
    role = obj.role
    colour = _VARIANT_COLOURS.get(obj.role_entry.variant or "", _ROLE_COLOURS.get(role))
    colour = colour or _ROLE_COLOURS[Role.SPECIAL]
    box = obj.role_entry.hitbox

    if box is not None and box.is_circle:
        cx, cy = px(obj.x, obj.y)
        r = box.radius * max(obj.scale_x, obj.scale_y) / CELL * ppc
        fill = (*_ROLE_COLOURS[Role.HAZARD], 200) if role is Role.HAZARD else (*colour, 200)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
        return True

    corners = _corners(obj)
    if corners is None:
        cx, cy = px(obj.x, obj.y)
        draw.rectangle((cx - 1, cy - 1, cx + 1, cy + 1), fill=colour)
        return True
    points = [px(x, y) for x, y in corners]

    if role is Role.DECO:
        draw.polygon(points, fill=(*colour, 102))  # 40% alpha
    elif role is Role.HAZARD:
        # The sprite's outline as a faint triangle pointing the way the spike does (base =
        # the box's bottom edge before rotation, apex = the middle of its top edge), and
        # the hitbox inside it solid: a spike's deadly area is far smaller than its art,
        # and the schematic is about what collides.
        sprite = _corners(obj, visual=True)
        if sprite is not None:
            (ax, ay), (bx, by), (cx_, cy_), (dx, dy) = [px(x, y) for x, y in sprite]
            apex = ((cx_ + dx) / 2, (cy_ + dy) / 2)
            draw.polygon([(ax, ay), (bx, by), apex], fill=(*colour, 70))
        draw.polygon(points, fill=(*colour, 255))
    elif role is Role.ORB:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        draw.ellipse((min(xs), min(ys), max(xs), max(ys)), fill=(*colour, 220),
                     outline=(40, 40, 40, 255))
    elif role is Role.PAD:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        draw.ellipse((min(xs), min(ys) - 1, max(xs), max(ys) + 1), fill=(*colour, 230))
    elif role in (Role.PORTAL_MODE, Role.PORTAL_SPEED, Role.PORTAL_GRAVITY, Role.PORTAL_SIZE,
                  Role.PORTAL_OTHER):
        draw.polygon(points, outline=(*colour, 255), width=2)
        label = obj.role_entry.variant or role.value.removeprefix("portal_")
        if ppc >= 8:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            draw.text((min(xs), min(ys) - 10), label, fill=(*colour, 255), font=font)
    elif role is Role.SLOPE:
        draw.polygon(points, fill=(*colour, 140), outline=(*colour, 255))
    elif role is Role.SPECIAL:
        draw.polygon(points, outline=(*colour, 255), width=1)
        a, _, c, _ = points
        draw.line((a, c), fill=(*colour, 255))
    else:
        draw.polygon(points, fill=(*colour, 255))
    return True
