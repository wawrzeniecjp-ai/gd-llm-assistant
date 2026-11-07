"""The gameplay simulator (LEVEL-DESIGN-SPEC.md 6).

Sim-1: the cube at every speed, with solids, hazards, yellow/pink orbs and pads, and speed
portals. Everything else in the simulated span makes it `unverifiable` (P4).

- `world.py`: the level's gameplay objects as collision boxes, indexed by x.
- `cube.py`: the per-tick model, measured in the game (SPEC.md 0.2c, 0.2d), and
  `simulate`, which plays a fixed input script.
"""
