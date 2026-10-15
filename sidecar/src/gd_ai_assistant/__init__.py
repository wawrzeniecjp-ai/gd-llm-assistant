"""GD AI Assistant sidecar.

The sidecar is the WebSocket **server**; the Geode mod is the client
(SPEC.md 5, decision D1). It owns everything about the LLM: API key, model,
conversation, tool loop and session persistence. It never touches game memory
or the level file.

The LLM API key never leaves this process (SPEC.md 4, 12).
"""

__version__ = "0.1.0"
