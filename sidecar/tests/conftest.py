"""Shared test configuration.

The one thing set up here is **isolation from the developer's `.env`**.

`SidecarConfig` is a `pydantic-settings` model configured with `env_file=".env"`, so
every construction reads `sidecar/.env` from the working directory. That is right for
the application and wrong for the suite: once a developer has a real provider
configured locally, three tests that assert the `needs_setup` state start failing --
not because anything is broken, but because the machine happens to have an API key.

That failure mode is worth eliminating rather than working around per test, because it
makes a green suite depend on who runs it. `CONFIG_ENV_FILE_DISABLED` is checked by
`SidecarConfig`'s settings config, so the whole suite ignores the file and each test
states exactly the configuration it needs.
"""

from __future__ import annotations

import os

# Set before any test module imports `gd_ai_assistant.config`.
os.environ["GD_AI_ASSISTANT_IGNORE_ENV_FILE"] = "1"
