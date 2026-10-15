# GD AI Assistant

An in-editor assistant that lets you edit levels with natural language.

Type a request, and a local companion process (the *sidecar*) drives an LLM
which issues validated editor operations. Objects appear, move, and change in
real time while you keep working.

## How it works

The mod is the WebSocket **client**; the sidecar is the **server**, listening on
`ws://127.0.0.1:8765` by default. The game process opens no listening socket and
never sees your LLM API key — that lives only in the sidecar's configuration.

Every assistant turn is grouped into a single transaction, so one undo reverts
the whole turn.

## Setup

1. Start the sidecar (see the project's launcher script).
2. Open the level editor. With **Auto-connect on editor open** enabled, the panel
   connects automatically.

## Data disclosure

Level contents are sent to the LLM provider you configure in the sidecar.
Nothing is sent until you submit a message.
