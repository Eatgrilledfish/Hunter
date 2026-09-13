"""Startup-only logging selection, independent of gameplay configuration."""
import os
from pathlib import Path


def resolve_mode(mode=None):
    selected = mode or os.environ.get("HUNTER_LOG_MODE")
    if not selected:
        config = Path(__file__).resolve().parents[2] / "log_mode.conf"
        selected = config.read_text(encoding="utf-8").rstrip("\n") if config.exists() else "compact"
    if selected not in {"off", "compact", "full"}:
        raise ValueError("invalid HUNTER_LOG_MODE: use off, compact or full")
    return selected


def configure_process_output():
    """Also cover direct main3.py launches, before loading Flask or the agent."""
    mode = resolve_mode()
    if mode == "off":
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
    return mode
