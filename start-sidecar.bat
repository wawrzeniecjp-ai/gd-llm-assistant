@echo off
setlocal
title GD AI Assistant - sidecar

REM Starts the Python sidecar that the mod talks to.
REM
REM Double-click this file, or run it from a terminal. Keep the window open:
REM the sidecar is a foreground process and the mod cannot connect without it.

cd /d "%~dp0sidecar" || (
    echo [ERROR] Could not find the "sidecar" folder next to this script.
    pause
    exit /b 1
)

REM --- 1. uv must be on PATH ------------------------------------------------
where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] "uv" is not on your PATH.
    echo.
    echo   Install it with:  winget install --id=astral-sh.uv -e
    echo   or see:           https://docs.astral.sh/uv/
    echo.
    pause
    exit /b 1
)

REM --- 2. First run: create .env from the example ---------------------------
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created sidecar\.env from .env.example
    echo.
    echo   For a real assistant, open that file and set:
    echo       llm_provider=anthropic   (or openai / openai_compatible)
    echo       api_key=...
    echo       model=...
    echo.
    echo   To try it with no API key at all, set instead:
    echo       llm_provider=scripted
    echo.
    echo   Starting anyway - the panel will say what still needs configuring.
    echo.
)

REM --- 3. Dependencies, then run -------------------------------------------
echo Syncing dependencies (first run takes a minute)...
uv sync --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] "uv sync" failed. Scroll up for the reason.
    pause
    exit /b 1
)

echo.
echo Starting the sidecar on ws://127.0.0.1:8765
echo Leave this window open while you use the mod. Press Ctrl+C to stop.
echo.
uv run gd-ai-assistant-sidecar

REM Reached when the sidecar stops. Keep the window so errors can be read.
echo.
echo The sidecar has stopped.
pause
