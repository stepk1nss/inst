"""Run state (one post) and its file-based store.

M1 keeps every run as data/runs/<run_id>/state.json next to its files; the
SQLite journal for publications arrives with publishing (M3).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MediaItem(BaseModel):
    path: str  # relative to the run dir
    sha256: str
    kind: str = "image"


class StepRecord(BaseModel):
    key: str
    fields: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    # what the step was allowed to see — shown in the preview as proof of isolation
    seen_fields: list[str] = Field(default_factory=list)
    seen_media: bool = False
    seen_caption: bool = False
    at: str = ""


class PlatformPost(BaseModel):
    platform: str
    connection: str
    format: str
    media_kind: str | None  # photo | video | None
    media_path: str | None  # relative to the run dir
    text: str
    title: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    reveal_secrets: bool = False
    html: bool = False
    split_caption: bool = False  # telegram: text too long for a media caption
    mode: str = "direct"
    visibility: str = "public"
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Validation(BaseModel):
    errors: list[str] = Field(default_factory=list)  # block publishing until fixed
    block_auto: list[str] = Field(default_factory=list)  # force manual confirmation
    needs_review: list[str] = Field(default_factory=list)  # confirmation locked until a person reviews
    review_button: str = ""
    warnings: list[str] = Field(default_factory=list)


class RunState(BaseModel):
    run_id: str
    project_id: str
    scenario_id: str
    scenario_how: str = ""  # tag | single | auto | default | manual
    created_at: str
    caption: str = ""
    media: list[MediaItem] = Field(default_factory=list)  # images the steps may see (for video: extracted frames)
    source_video: MediaItem | None = None  # original video when the material is a video
    params: dict[str, Any] = Field(default_factory=dict)  # voice_id, speed, mode
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    post_overrides: dict[str, str] = Field(default_factory=dict)  # platform -> final text
    fields: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    last_run_steps: dict[str, str] = Field(default_factory=dict)  # step id -> ran | cached | skipped
    posts: dict[str, PlatformPost] = Field(default_factory=dict)
    validation: Validation = Field(default_factory=Validation)
    decision: str = ""  # draft | await_confirm | publish
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    judge_cache: dict[str, list[str]] = Field(default_factory=dict)  # hash(secrets+texts) -> findings
    publish_results: list[dict[str, Any]] = Field(default_factory=list)
    # manual review of flagged content: {"by": user, "at": ts, "key": hash of the secret fields reviewed}
    review: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)  # append-only journal of decisions
    # new | scenario_selected | generating | ready | failed | draft | approved | cancelled
    status: str = "new"
    error: str | None = None


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class RunStore:
    def __init__(self, data_dir: Path):
        self.base = data_dir / "runs"
        self.base.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not run_id.replace("-", "").isalnum():
            raise ValueError(f"bad run id '{run_id}'")
        return self.base / run_id

    def save(self, state: RunState) -> Path:
        d = self.run_dir(state.run_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, run_id: str) -> RunState:
        path = self.run_dir(run_id) / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"run '{run_id}' not found")
        return RunState.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[str]:
        return sorted((p.name for p in self.base.iterdir() if (p / "state.json").exists()), reverse=True)
