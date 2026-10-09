"""Runtime settings from environment (.env is read if present; real env wins)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    mode: str  # dev | prod
    llm_provider: str  # mock | anthropic
    tts_provider: str  # mock | elevenlabs | none
    ffmpeg: bool

    @property
    def dev(self) -> bool:
        return self.mode == "dev"


class SettingsError(Exception):
    pass


def get_settings(root: Path | None = None) -> Settings:
    root = (root or Path(os.environ.get("CONTENTBOT_ROOT", Path.cwd()))).resolve()
    load_dotenv(root / ".env")
    mode = os.environ.get("CONTENTBOT_MODE", "dev").strip() or "dev"
    if mode not in ("dev", "prod"):
        raise SettingsError(f"CONTENTBOT_MODE must be dev or prod, got '{mode}'")

    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    llm = os.environ.get("CONTENTBOT_LLM", "").strip() or ("anthropic" if has_anthropic else "mock")
    has_eleven = bool(os.environ.get("ELEVENLABS_API_KEY"))
    tts = os.environ.get("CONTENTBOT_TTS", "").strip() or ("elevenlabs" if has_eleven else "mock")

    if llm not in ("mock", "anthropic"):
        raise SettingsError(f"CONTENTBOT_LLM must be mock or anthropic, got '{llm}'")
    if tts not in ("mock", "elevenlabs", "none"):
        raise SettingsError(f"CONTENTBOT_TTS must be mock, elevenlabs or none, got '{tts}'")
    if mode == "prod":
        if llm == "mock" or tts == "mock":
            raise SettingsError("prod mode does not allow mock providers")
        if llm == "anthropic" and not has_anthropic:
            raise SettingsError("ANTHROPIC_API_KEY is required in prod mode")
        if tts == "elevenlabs" and not has_eleven:
            raise SettingsError("ELEVENLABS_API_KEY is required in prod mode")

    data_dir = Path(os.environ.get("CONTENTBOT_DATA_DIR", root / "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return Settings(
        root=root,
        data_dir=data_dir,
        mode=mode,
        llm_provider=llm,
        tts_provider=tts,
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
    )
