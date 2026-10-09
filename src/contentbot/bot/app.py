"""Wiring shared by the Telegram bot and the console simulator."""

from __future__ import annotations

import os

from ..config.loader import load_all
from ..core.pipeline import Pipeline
from ..core.services import build_services
from ..core.state import RunStore
from ..settings import Settings


def owner_ids_from_env() -> set[int]:
    raw = os.environ.get("OWNER_TELEGRAM_IDS", "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def build_pipeline(settings: Settings, mock_fixtures: dict | None = None) -> Pipeline:
    registry = load_all(settings.root)
    services = build_services(settings, registry.system, mock_fixtures if settings.llm_provider == "mock" else None)
    return Pipeline(registry, services, RunStore(settings.data_dir))
