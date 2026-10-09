"""Universal LLM step: fields in (as allowed by the core), fields out (dynamic schema)."""

from __future__ import annotations

import json
from typing import Any

from ..core.context import assert_no_secrets, normalize
from ..llm.base import ContentPart, ImagePart, TextPart
from ..llm.schema_builder import build_output_schema, platform_texts_targets
from ..publishers.capabilities import CAPS
from .base import StepContext, StepError, StepResult


def _project_fingerprint(ctx: StepContext) -> dict[str, Any]:
    p, sc = ctx.project, ctx.scenario
    return {
        "brand": p.brand_text,
        "knowledge": p.knowledge_text,
        "examples": p.examples,
        "language": p.language,
        "commercial": p.commercial,
        "scenario": [sc.when, sc.instructions, sc.knowledge_text],
        "platforms": {k: v.model_dump() for k, v in p.enabled_platforms().items()},
        "model": ctx.services.system.models.generate.model_dump(),
    }


def build_system_prompt(ctx: StepContext) -> str:
    p, sc, step = ctx.project, ctx.scenario, ctx.step
    parts = [
        f"Ты — контент-редактор проекта «{p.name}». Язык всех текстов: {p.language}.",
        "Отвечай строго в формате заданной JSON-схемы. Не выдумывай факты: если чего-то не знаешь — не пиши этого.",
    ]
    if p.brand_text:
        parts.append("## Голос бренда\n" + p.brand_text)
    if p.knowledge_text:
        parts.append("## Знания проекта\n" + p.knowledge_text)
    if sc.knowledge_text:
        parts.append("## Знания сценария\n" + sc.knowledge_text)
    if p.examples:
        parts.append("## Примеры удачных постов (ориентир по стилю)\n" + "\n---\n".join(p.examples))
    parts.append(f"## Сценарий: {sc.title}\n{sc.when}\n{sc.instructions}".strip())
    if step.instructions:
        parts.append("## Задача шага\n" + step.instructions.strip())

    field_lines = []
    for name in step.produces:
        spec = sc.fields[name]
        line = f"- {name}: {spec.desc}"
        if spec.source == "caption":
            line += " (ТОЛЬКО из подписи автора, дословно; если в подписи этого нет — пустая строка)"
        field_lines.append(line)
    parts.append("## Поля ответа\n" + "\n".join(field_lines))

    for name in step.produces:
        if sc.fields[name].type != "platform_texts":
            continue
        rules = []
        for pname in platform_texts_targets(p, sc, name):
            target, caps = p.platforms[pname], CAPS[pname]
            rule = f"- {pname}: {caps.prompt_rules}"
            if target.hashtags_fixed:
                rule += f" Хэштеги {' '.join(target.hashtags_fixed)} добавятся автоматически — не повторяй их."
            if target.cta:
                rule += f" Призыв к действию: {target.cta}."
            rules.append(rule)
        if rules:
            parts.append("## Правила площадок (тексты разные, не копируй один в другой)\n" + "\n".join(rules))
    if p.commercial:
        parts.append("Это рекламный контент: не скрывай рекламный характер, не давай недостоверных обещаний.")
    return "\n\n".join(parts)


def build_user_content(ctx: StepContext) -> tuple[list[ContentPart], str]:
    content: list[ContentPart] = [ImagePart(m) for m in ctx.view.media]
    lines = []
    if ctx.view.caption is not None:
        lines.append(f"Подпись автора к материалу: {ctx.view.caption or '(нет подписи)'}")
    if ctx.view.fields:
        lines.append("Входные данные:\n" + json.dumps(ctx.view.fields, ensure_ascii=False, indent=2))
    if not ctx.view.media:
        lines.append("(Материал не передаётся — работай только с входными данными.)")
    lines.append(f"Заполни поля: {', '.join(ctx.step.produces)}.")
    text = "\n\n".join(lines)
    content.append(TextPart(text))
    return content, text


def _caption_supports(value: str, caption: str) -> bool:
    v, c = normalize(value), normalize(caption)
    if not v:
        return True
    words = [w for w in v.split() if len(w) > 2 or w.isdigit()]
    if not words:
        return v in c
    found = sum(1 for w in words if w in c)
    return found / len(words) >= 0.7


class GenerateStep:
    name = "generate"

    def cache_inputs(self, ctx: StepContext) -> dict[str, Any]:
        return _project_fingerprint(ctx)

    async def run(self, ctx: StepContext) -> StepResult:
        schema = build_output_schema(ctx.step.produces, ctx.project, ctx.scenario)
        system = build_system_prompt(ctx)
        assert_no_secrets(ctx.scenario, ctx.step, ctx.view)
        content, _ = build_user_content(ctx)

        data = await ctx.services.llm.generate_json(
            purpose=f"{ctx.scenario.id}.{ctx.step.id}",
            role="generate",
            system=system,
            content=content,
            schema=schema,
        )
        missing = [k for k in ctx.step.produces if k not in data]
        if missing:
            raise StepError(f"model did not return fields: {', '.join(missing)}")

        result = StepResult(fields={k: data[k] for k in ctx.step.produces})
        caption = ctx.state.caption or ""
        for name in ctx.step.produces:
            spec = ctx.scenario.fields[name]
            value = result.fields[name]
            if spec.source == "caption" and isinstance(value, str) and value.strip():
                if not _caption_supports(value, caption):
                    result.fields[name] = ""
                    result.warnings.append(f"поле '{name}' не найдено в подписи — оставлено пустым (выдумывать запрещено)")
        return result


async def revise_generate(ctx: StepContext, current: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Rewrite this step's current output by the author's instruction.

    Uses the same allowed inputs as the step (the core already filtered
    secrets and removed media), plus the current output. Returns all fields
    of the step.
    """
    assert_no_secrets(ctx.scenario, ctx.step, ctx.view)
    schema = build_output_schema(ctx.step.produces, ctx.project, ctx.scenario)
    system = build_system_prompt(ctx) + (
        "\n\n## Правка\nАвтор просит изменить результат. Сохрани всё, что не затронуто указанием, "
        "и соблюдай те же правила (в том числе запреты из задачи шага)."
    )
    content, _ = build_user_content(ctx)
    content.append(
        TextPart(
            "Текущий результат:\n" + json.dumps(current, ensure_ascii=False, indent=2)
            + f"\n\nУказание автора: {instruction.strip()}"
        )
    )
    data = await ctx.services.llm.generate_json(
        purpose=f"{ctx.scenario.id}.{ctx.step.id}.revise",
        role="generate",
        system=system,
        content=content,
        schema=schema,
    )
    missing = [k for k in ctx.step.produces if k not in data]
    if missing:
        raise StepError(f"model did not return fields: {', '.join(missing)}")
    return {k: data[k] for k in ctx.step.produces}
