"""Pick a scenario for the material. The user can always override it before generation."""

from __future__ import annotations

from pathlib import Path

from ..config.schema import Project
from ..llm.base import ContentPart, ImagePart, TextPart
from .services import Services


def scenario_from_tag(project: Project, caption: str) -> str | None:
    words = {w.lower() for w in caption.split()}
    for sc in project.scenarios.values():
        if sc.tag and sc.tag.lower() in words:
            return sc.id
    return None


async def select_scenario(project: Project, media: list[Path], caption: str, services: Services) -> tuple[str, str]:
    """-> (scenario id, how): how is tag | single | default | auto."""
    tagged = scenario_from_tag(project, caption)
    if tagged:
        return tagged, "tag"
    if len(project.scenarios) == 1:
        return next(iter(project.scenarios)), "single"
    if not project.scenario_selection.auto:
        return project.scenario_selection.default, "default"

    ids = list(project.scenarios)
    listing = "\n".join(f"- {sc.id}: {sc.title}. {sc.when}" for sc in project.scenarios.values())
    content: list[ContentPart] = [ImagePart(m) for m in media]
    content.append(TextPart(f"Подпись автора: {caption or '(нет)'}\n\nСценарии:\n{listing}"))
    schema = {
        "type": "object",
        "properties": {"scenario": {"type": "string", "enum": ids}},
        "required": ["scenario"],
        "additionalProperties": False,
    }
    try:
        data = await services.llm.generate_json(
            purpose=f"{project.id}.classify",
            role="classify",
            system="Выбери сценарий, который лучше всего подходит материалу. Если ни один явно не подходит — выбери обычный пост.",
            content=content,
            schema=schema,
        )
    except Exception:
        return project.scenario_selection.default, "default"
    chosen = data.get("scenario")
    if chosen in project.scenarios:
        return chosen, "auto"
    return project.scenario_selection.default, "default"
