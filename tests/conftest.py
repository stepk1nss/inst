from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from contentbot.config.loader import load_all
from contentbot.core.pipeline import Pipeline
from contentbot.core.services import build_services, load_mock_fixtures
from contentbot.core.state import RunStore
from contentbot.demo import make_demo_ticket
from contentbot.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    """Tests never touch real providers."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ELEVENLABS_API_KEY", "CONTENTBOT_LLM", "CONTENTBOT_TTS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry():
    return load_all(ROOT)


@pytest.fixture
def demo_fixtures():
    return load_mock_fixtures(ROOT / "examples" / "dev" / "pdd_demo.mock.yaml")


def make_pipeline(tmp_path: Path, fixtures: dict | None = None, root: Path = ROOT, full_size: bool = False) -> Pipeline:
    settings = Settings(
        root=root, data_dir=tmp_path / "data", mode="dev", llm_provider="mock",
        tts_provider="mock" if HAS_FFMPEG else "none", ffmpeg=HAS_FFMPEG,
    )
    registry = load_all(root)
    assert not registry.errors, registry.errors
    if not full_size:  # small, fast renders for tests
        registry.system.render = registry.system.render.model_copy(update={"width": 180, "height": 320, "fps": 10})
    services = build_services(settings, registry.system, fixtures or {})
    return Pipeline(registry, services, RunStore(settings.data_dir))


@pytest.fixture
def demo_image(tmp_path):
    return make_demo_ticket(tmp_path / "ticket.png")


def copy_config_root(tmp_path: Path) -> Path:
    """Copy config/templates/projects so a test can break one project safely."""
    root = tmp_path / "root"
    for d in ("config", "templates", "projects"):
        shutil.copytree(ROOT / d, root / d)
    return root


def write_project(root: Path, project_id: str, data: dict) -> Path:
    d = root / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d
