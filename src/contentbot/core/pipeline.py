"""Pipeline engine: runs a scenario's steps with per-step caching.

Each step's cache key is a hash of everything it depends on (its spec, the
view the core built for it, and step-specific inputs such as the voice). When
a post parameter changes — e.g. the voice — only steps whose key changed run
again; the LLM steps are served from cache.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.loader import ConfigRegistry
from ..config.schema import Project, Scenario
from ..media.image import normalize_image, sha256_file
from ..steps import STEP_TYPES
from ..steps.base import StepContext, StepError
from .classify import select_scenario
from .compose import compose_posts
from ..llm.base import LLMError
from .context import SecretIsolationError, build_view
from .modes import decide
from .services import Services
from .state import MediaItem, RunState, RunStore, StepRecord, new_run_id
from .validate import validate


class PipelineError(Exception):
    pass


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, registry: ConfigRegistry, services: Services, store: RunStore):
        self.registry = registry
        self.services = services
        self.store = store

    # ------------------------------------------------------------------ intake

    async def create_run(
        self,
        project_id: str,
        media_paths: list[Path],
        caption: str = "",
        scenario_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> RunState:
        """Store the material and pick the scenario (auto unless given). Does not generate."""
        project = self.registry.project(project_id)
        state = RunState(
            run_id=new_run_id(),
            project_id=project.id,
            scenario_id="",
            created_at=_now(),
            caption=caption.strip(),
            params=dict(params or {}),
        )
        run_dir = self.store.run_dir(state.run_id)
        img = self.services.system.image
        for i, src in enumerate(media_paths):
            rel = f"media/source_{i}.jpg"
            normalize_image(src, run_dir / rel, img.max_side, img.jpeg_quality)
            state.media.append(MediaItem(path=rel, sha256=sha256_file(run_dir / rel)))

        if scenario_id:
            if scenario_id not in project.scenarios:
                raise PipelineError(f"project '{project.id}' has no scenario '{scenario_id}'")
            state.scenario_id, state.scenario_how = scenario_id, "manual"
        else:
            state.scenario_id, state.scenario_how = await select_scenario(
                project, [run_dir / m.path for m in state.media], state.caption, self.services
            )
        state.params.setdefault("mode", project.publishing.mode)
        state.status = "scenario_selected"
        self.store.save(state)
        return state

    def set_scenario(self, state: RunState, scenario_id: str) -> RunState:
        project = self.registry.project(state.project_id)
        if scenario_id not in project.scenarios:
            raise PipelineError(f"project '{project.id}' has no scenario '{scenario_id}'")
        if scenario_id != state.scenario_id:
            state.scenario_id, state.scenario_how = scenario_id, "manual"
            state.fields, state.artifacts, state.steps, state.posts = {}, {}, {}, {}
            state.field_overrides, state.post_overrides = {}, {}
        self.store.save(state)
        return state

    # ------------------------------------------------------------------ generation

    async def generate(self, state: RunState) -> RunState:
        project = self.registry.project(state.project_id)
        scenario = project.scenarios[state.scenario_id]
        run_dir = self.store.run_dir(state.run_id)
        llm_calls_before = len(self.services.llm.calls)
        state.status, state.error = "generating", None
        state.fields, state.artifacts, state.last_run_steps = {}, {}, {}
        try:
            for step in scenario.steps:
                await self._run_step(project, scenario, step.id, state, run_dir)
            state.posts = compose_posts(project, scenario, state, run_dir)
            state.validation = await validate(project, scenario, state, state.posts, self.services)
            state.decision = decide(state.params.get("mode", project.publishing.mode), state.validation)
            state.status = "ready"
        except (StepError, PipelineError) as e:
            state.status, state.error = "failed", str(e)
        finally:
            for call in self.services.llm.calls[llm_calls_before:]:
                state.llm_calls.append(
                    {"purpose": call.purpose, "model": call.model, "in": call.input_tokens, "out": call.output_tokens}
                )
            self.store.save(state)
        return state

    async def _run_step(self, project: Project, scenario: Scenario, step_id: str, state: RunState, run_dir: Path) -> None:
        step = scenario.step(step_id)
        impl = STEP_TYPES[step.use]
        view = build_view(scenario, step, state, run_dir)
        ctx = StepContext(project, scenario, step, view, state, self.services, run_dir)
        key = _hash(
            {
                "step": step.model_dump(),
                "view": {"fields": view.fields, "media": [str(p) for p in view.media], "caption": view.caption},
                "media_sha": [m.sha256 for m in state.media] if step.sees.media else [],
                "extra": impl.cache_inputs(ctx),
            }
        )
        cached = state.steps.get(step.id)
        artifacts_ok = cached is not None and all((run_dir / p).exists() for p in cached.artifacts.values())
        if cached is not None and cached.key == key and artifacts_ok:
            record, status = cached, "cached"
        else:
            try:
                result = await impl.run(ctx)
            except (StepError, LLMError) as e:
                raise StepError(f"шаг '{step.id}': {e}") from e
            except SecretIsolationError as e:  # a bug, never a content problem: stop loudly
                raise StepError(f"НАРУШЕНИЕ ИЗОЛЯЦИИ СЕКРЕТОВ в шаге '{step.id}': {e}") from e
            unexpected = set(result.fields) - set(step.produces)
            if unexpected:
                raise StepError(f"шаг '{step.id}' вернул необъявленные поля: {', '.join(sorted(unexpected))}")
            record = StepRecord(
                key=key,
                fields=result.fields,
                artifacts=result.artifacts,
                warnings=result.warnings,
                seen_fields=sorted(view.fields),
                seen_media=bool(view.media),
                seen_caption=view.caption is not None,
                at=_now(),
            )
            state.steps[step.id] = record
            status = "skipped" if result.skipped else "ran"
        state.last_run_steps[step.id] = status
        state.fields.update(record.fields)
        for name in step.produces:
            if name in state.field_overrides:
                state.fields[name] = state.field_overrides[name]
        state.artifacts.update(record.artifacts)

    # ------------------------------------------------------------------ edits (re-run only what changed)

    async def set_voice(self, state: RunState, voice_id: str | None = None, speed: float | None = None) -> RunState:
        project = self.registry.project(state.project_id)
        if voice_id is not None:
            if project.voice.get(voice_id) is None:
                raise PipelineError(f"голос '{voice_id}' не задан в проекте '{project.id}'")
            state.params["voice_id"] = voice_id
        if speed is not None:
            state.params["speed"] = speed
        return await self.generate(state)

    async def edit_field(self, state: RunState, field: str, value: Any) -> RunState:
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        if field not in scenario.fields:
            raise PipelineError(f"нет поля '{field}' в сценарии '{scenario.id}'")
        state.field_overrides[field] = value
        return await self.generate(state)

    async def edit_post(self, state: RunState, platform: str, text: str) -> RunState:
        if platform not in state.posts:
            raise PipelineError(f"площадка '{platform}' не включена в этом посте")
        state.post_overrides[platform] = text
        return await self.generate(state)

    async def regenerate_step(self, state: RunState, step_id: str) -> RunState:
        """Force a step (and everything depending on it) to run again."""
        state.steps.pop(step_id, None)
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        for name in scenario.step(step_id).produces:
            state.field_overrides.pop(name, None)
        return await self.generate(state)


def summarize_llm_usage(state: RunState) -> dict[str, int]:
    return {"calls": len(state.llm_calls), "in": sum(c["in"] for c in state.llm_calls), "out": sum(c["out"] for c in state.llm_calls)}
