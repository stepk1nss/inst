"""Pipeline engine: runs a scenario's steps with per-step caching.

Each step's cache key is a hash of everything it depends on (its spec, the
view the core built for it, and step-specific inputs such as the voice). When
a post parameter changes — e.g. the voice — only steps whose key changed run
again; the LLM steps are served from cache.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.loader import ConfigRegistry
from ..config.schema import Project, Scenario
from ..media.image import normalize_image, sha256_file
from ..media.video import VideoError, extract_frames, is_video
from ..steps import STEP_TYPES
from ..steps.base import StepContext, StepError
from ..steps.generate import revise_generate
from .classify import select_scenario
from ..llm.base import LLMError
from ..publishers.base import publish_posts
from .compose import compose_posts
from .context import SecretIsolationError, build_view
from .modes import can_confirm, decide, review_pending
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
        await self._ingest(state, run_dir, media_paths)

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

    async def _ingest(self, state: RunState, run_dir: Path, media_paths: list[Path]) -> None:
        if not media_paths:
            raise PipelineError("нет материала: пришлите фото или видео")
        img = self.services.system.image
        videos = [p for p in media_paths if is_video(p)]
        if videos:
            if not self.services.ffmpeg:
                raise PipelineError("видео не поддерживается без FFmpeg")
            src = videos[0]
            rel = f"media/source_video{src.suffix.lower()}"
            (run_dir / "media").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, run_dir / rel)
            state.source_video = MediaItem(path=rel, sha256=sha256_file(run_dir / rel), kind="video")
            try:
                frames = await extract_frames(run_dir / rel, run_dir / "media" / "frames")
            except VideoError as e:
                raise PipelineError(f"не удалось прочитать видео: {e}") from e
            sources = frames
            if len(media_paths) > 1:
                state.params["ingest_note"] = "в посте используется только первое видео"
        else:
            sources = media_paths
        for i, src in enumerate(sources):
            rel = f"media/source_{i}.jpg"
            try:
                normalize_image(src, run_dir / rel, img.max_side, img.jpeg_quality)
            except OSError as e:
                raise PipelineError(f"не удалось открыть изображение {src.name}: {e}") from e
            state.media.append(MediaItem(path=rel, sha256=sha256_file(run_dir / rel)))

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
            if state.review is not None and not self.is_reviewed(state):
                # what the person checked has changed (e.g. the answer was regenerated)
                state.review = None
                self._event(state, "review_reset", reason="проверенные данные изменились")
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

    async def edit_field(self, state: RunState, field: str, value: Any, by: str | int = "user") -> RunState:
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        if field not in scenario.fields:
            raise PipelineError(f"нет поля '{field}' в сценарии '{scenario.id}'")
        if scenario.fields[field].secret and state.fields.get(field) != value:
            self._event(state, "answer_changed", by=str(by), field=field, old=state.fields.get(field), new=value)
        state.field_overrides[field] = value
        return await self.generate(state)

    async def edit_post(self, state: RunState, platform: str, text: str, by: str | int = "user") -> RunState:
        if platform not in state.posts:
            raise PipelineError(f"площадка '{platform}' не включена в этом посте")
        if platform in self._reveal_platforms(state) and state.posts[platform].text != text:
            # this post shows the answer: a manual edit may change it, so it needs a fresh review
            self._event(state, "answer_changed", by=str(by), platform=platform, old=state.posts[platform].text, new=text)
        state.post_overrides[platform] = text
        return await self.generate(state)

    async def regenerate_step(self, state: RunState, step_id: str) -> RunState:
        """Force a step (and everything depending on it) to run again."""
        state.steps.pop(step_id, None)
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        for name in scenario.step(step_id).produces:
            state.field_overrides.pop(name, None)
        return await self.generate(state)


    async def revise_step(self, state: RunState, step_id: str, instruction: str) -> RunState:
        """Rewrite a generate step's output by the author's instruction.

        The material is NOT analysed again: the model gets only the step's
        allowed input fields (secrets stay filtered) and its current output.
        The result is stored as overrides; downstream steps re-run as needed.
        """
        project = self.registry.project(state.project_id)
        scenario = project.scenarios[state.scenario_id]
        step = scenario.step(step_id)
        if step.use != "generate":
            raise PipelineError(f"шаг '{step_id}' нельзя переписать текстом")
        if not instruction.strip():
            raise PipelineError("пустое указание для правки")
        run_dir = self.store.run_dir(state.run_id)
        view = build_view(scenario, step, state, run_dir)
        view.media = []  # no re-analysis of the image
        ctx = StepContext(project, scenario, step, view, state, self.services, run_dir)
        current = {name: state.fields.get(name) for name in step.produces}
        llm_calls_before = len(self.services.llm.calls)
        try:
            data = await revise_generate(ctx, current, instruction)
        except (StepError, LLMError) as e:
            raise PipelineError(f"правка не удалась: {e}") from e
        except SecretIsolationError as e:
            raise PipelineError(f"НАРУШЕНИЕ ИЗОЛЯЦИИ СЕКРЕТОВ: {e}") from e
        finally:
            for call in self.services.llm.calls[llm_calls_before:]:
                state.llm_calls.append({"purpose": call.purpose, "model": call.model, "in": call.input_tokens, "out": call.output_tokens})
        for name, value in data.items():
            state.field_overrides[name] = value
        return await self.generate(state)

    def main_text_step(self, state: RunState) -> str | None:
        """The step that writes the platform texts (target of 'rewrite' / 'regenerate')."""
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        candidates = [s for s in scenario.steps if s.use == "generate"]
        for s in candidates:
            if any(scenario.fields[f].type == "platform_texts" for f in s.produces):
                return s.id
        return candidates[-1].id if candidates else None

    # ------------------------------------------------------------------ decisions

    # ------------------------------------------------------------------ manual review

    def _reveal_platforms(self, state: RunState) -> set[str]:
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        return {p for p, pd in scenario.platform_defaults.items() if pd.reveal_secrets}

    def review_key(self, state: RunState) -> str:
        """Fingerprint of what a reviewer vouches for: the secret fields (e.g. the answer)
        and any manual text of the platforms that reveal them (e.g. the Telegram answer post)."""
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        reveal_overrides = {p: t for p, t in sorted(state.post_overrides.items()) if p in self._reveal_platforms(state)}
        return _hash({
            "secrets": {name: state.fields.get(name) for name in sorted(scenario.secret_fields())},
            "reveal_overrides": reveal_overrides,
        })

    def is_reviewed(self, state: RunState) -> bool:
        return state.review is not None and state.review.get("key") == self.review_key(state)

    def can_confirm(self, state: RunState) -> bool:
        return state.status in ("ready", "draft") and can_confirm(state.validation, self.is_reviewed(state))

    def flagged_step(self, state: RunState) -> str | None:
        """The step that produced the field which triggered a review (to re-run it)."""
        scenario = self.registry.project(state.project_id).scenarios[state.scenario_id]
        fields = [c.field for c in scenario.checks if c.type == "require_review_if" and c.field]
        for step in scenario.steps:
            if any(f in step.produces for f in fields):
                return step.id
        return None

    def mark_reviewed(self, state: RunState, by: str | int) -> RunState:
        if state.status not in ("ready", "draft"):
            raise PipelineError("пост не ждёт проверки")
        if not state.validation.needs_review:
            raise PipelineError("этот пост не требует ручной проверки")
        state.review = {"by": str(by), "at": _now(), "key": self.review_key(state)}
        self._event(state, "review_confirmed", by=str(by), reasons=list(state.validation.needs_review))
        self.store.save(state)
        return state

    @staticmethod
    def _event(state: RunState, kind: str, **data: Any) -> None:
        state.events.append({"at": _now(), "type": kind, **data})

    # ------------------------------------------------------------------ confirm

    async def approve(self, state: RunState, platforms: list[str] | None = None, by: str | int = "user") -> RunState:
        """User confirmation. Real publishing is not connected yet: posts go to a dry-run outbox."""
        if state.status not in ("ready", "draft"):
            raise PipelineError("пост ещё не готов")
        if state.validation.errors:
            raise PipelineError("есть ошибки, публикация невозможна:\n" + "\n".join(state.validation.errors))
        if review_pending(state.validation, self.is_reviewed(state)):
            raise PipelineError("сначала нужна ручная проверка: " + "; ".join(state.validation.needs_review))
        results = await publish_posts(state.posts, self.store.run_dir(state.run_id), platforms)
        state.publish_results = [r.__dict__ for r in results]
        state.status = "approved"
        self._event(
            state, "approved", by=str(by),
            manual_review=bool(state.validation.needs_review),
            reviewed_by=state.review["by"] if state.review else None,
            platforms=[r.platform for r in results if r.ok],
            dry_run=True,
        )
        self.store.save(state)
        return state

    def save_draft(self, state: RunState, by: str | int = "user") -> RunState:
        if state.status not in ("ready", "draft"):
            raise PipelineError("черновик можно сохранить только у готового поста")
        state.status = "draft"
        self._event(state, "draft", by=str(by))
        self.store.save(state)
        return state

    def cancel(self, state: RunState, by: str | int = "user") -> RunState:
        state.status = "cancelled"
        self._event(state, "cancelled", by=str(by))
        self.store.save(state)
        return state


def summarize_llm_usage(state: RunState) -> dict[str, int]:
    return {"calls": len(state.llm_calls), "in": sum(c["in"] for c in state.llm_calls), "out": sum(c["out"] for c in state.llm_calls)}
