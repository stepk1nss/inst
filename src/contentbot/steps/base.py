"""Step plugin interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config.schema import Project, Scenario, StepSpec
from ..core.context import StepView
from ..core.services import Services
from ..core.state import RunState


class StepError(Exception):
    pass


@dataclass
class StepContext:
    project: Project
    scenario: Scenario
    step: StepSpec
    view: StepView
    state: RunState  # read params / artifacts only; fields must be read via `view`
    services: Services
    run_dir: Path


@dataclass
class StepResult:
    fields: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path relative to run dir
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False


class Step(Protocol):
    name: str

    def cache_inputs(self, ctx: StepContext) -> dict[str, Any]:
        """Everything (besides the view and step spec) that changes this step's output."""
        ...

    async def run(self, ctx: StepContext) -> StepResult: ...
