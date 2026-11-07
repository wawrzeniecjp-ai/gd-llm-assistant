"""The level design layer (docs/LEVEL-DESIGN-SPEC.md).

Everything here is deterministic, transport-agnostic and testable without the game
(LD1, P6). The agent's tools are thin adapters onto this package; the package itself
knows nothing about the tool loop or the WebSocket.

Modules:

- `coords`      grid <-> level coordinates and the measured constants behind them
- `roles`       what each object type *is* in gameplay terms (the role table)
- `level_model` a per-turn, read-only snapshot of the level with a spatial index
"""
