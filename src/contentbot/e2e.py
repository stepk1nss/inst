"""M2.5: end-to-end check on real AI providers for a quiz-type scenario.

    python -m contentbot e2e --project <id> --scenario <quiz scenario> --media ticket.jpg

Runs the full pipeline (no publishing), then verifies:
  * the answer never appears in public texts (teaser, voiceover, non-reveal platforms);
  * the reveal platform (Telegram) has all options, the answer under a spoiler, the explanation;
  * an uncertain answer is never "ready to publish";
  * the public text steps never received secret fields or the image.
Writes data/runs/<id>/e2e_report.md. Secrets from the environment are redacted
from everything written or printed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config.schema import Scenario
from .core.context import normalize
from .core.pipeline import Pipeline
from .core.state import RunState

SECRET_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ELEVENLABS_API_KEY", "TELEGRAM_BOT_TOKEN",
    "META_APP_SECRET", "TIKTOK_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET", "R2_SECRET_ACCESS_KEY",
    "TOKEN_ENCRYPTION_KEY",
)


def redact(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, f"<{name}:redacted>")
    return text


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _singled_out(option: str, others: list[str], text: str) -> bool:
    """True if the option text appears in `text` without the other options next to it."""
    t = normalize(text)
    if normalize(option) not in t:
        return False
    return not any(normalize(o) in t for o in others if normalize(o))


def run_checks(pipeline: Pipeline, state: RunState) -> list[Check]:
    project = pipeline.registry.project(state.project_id)
    sc: Scenario = project.scenarios[state.scenario_id]
    f = state.fields
    checks: list[Check] = []
    options = [str(o) for o in f.get("options") or []]
    idx = f.get("correct_index")
    correct_option = options[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(options) else None
    others = [o for o in options if o != correct_option]
    correct = str(f.get("correct") or "")

    # 1. isolation: what public steps actually received
    secrets = sc.secret_fields()
    for step in sc.steps:
        rec = state.steps.get(step.id)
        if rec is None or step.sees.secrets or step.use != "generate":
            continue
        leaked = secrets & set(rec.seen_fields)
        checks.append(Check(f"изоляция: шаг «{step.id}» не получил секретных полей", not leaked, ", ".join(sorted(leaked))))
    teaser_step = next((s for s in sc.steps if s.use == "generate" and not s.sees.secrets and any(sc.fields[p].type == "platform_texts" for p in s.produces)), None)
    if teaser_step and teaser_step.id in state.steps:
        checks.append(Check(f"изоляция: шаг «{teaser_step.id}» не получил изображение", not state.steps[teaser_step.id].seen_media))

    # 2. answer absent from every public text
    spoken = next((s.input for s in sc.steps if s.use == "tts" and s.input), None)
    targets: dict[str, str] = {}
    if f.get("teaser"):
        targets["тизер"] = str(f["teaser"])
    if spoken and f.get(spoken):
        targets["озвучка"] = str(f[spoken])
    for pname, post in state.posts.items():
        if not post.reveal_secrets:
            targets[pname] = f"{post.title or ''}\n{post.text}"
    for where, text in targets.items():
        problems = []
        if correct_option and _singled_out(correct_option, others, text):
            problems.append(f"назван правильный вариант «{correct_option}»")
        if correct and correct != correct_option and len(normalize(correct)) >= 4 and normalize(correct) in normalize(text):
            problems.append("содержит текст ответа")
        if isinstance(idx, int) and re.search(rf"(ответ\w*|вариант\w*|правильн\w*)\D{{0,12}}\b{idx}\b", text.lower()):
            problems.append(f"упоминает номер {idx}")
        checks.append(Check(f"ответа нет: {where}", not problems, "; ".join(problems)))
    leak_flags = [b for b in state.validation.block_auto if "утечк" in b or "судья" in b]
    checks.append(Check("проверка утечки конвейера (правила + судья) чистая", not leak_flags, "; ".join(leak_flags)))

    # 3. reveal platform content
    for pname, post in state.posts.items():
        if not post.reveal_secrets:
            continue
        text = post.text
        missing = [o for o in options if normalize(o) not in normalize(re.sub(r"<[^>]+>", " ", text))]
        checks.append(Check(f"{pname}: все варианты ответа на месте", bool(options) and not missing, ", ".join(missing)))
        spoilers = re.findall(r"<tg-spoiler>(.*?)</tg-spoiler>", text, flags=re.S)
        checks.append(Check(f"{pname}: правильный ответ под спойлером", any(correct and normalize(correct) in normalize(s) for s in spoilers)))
        expl = normalize(str(f.get("explanation") or ""))[:60]
        checks.append(Check(f"{pname}: есть объяснение", bool(expl) and expl in normalize(text)))
        if f.get("rule_ref"):
            checks.append(Check(f"{pname}: есть пункт правил", normalize(str(f["rule_ref"])) in normalize(text)))

    # 4. uncertainty
    if f.get("uncertain") is True:
        # "not ready" means it cannot be confirmed until a person reviews it
        blocked = state.decision != "publish" and not pipeline.can_confirm(state) and bool(state.validation.needs_review)
        checks.append(Check(
            "неуверенный ответ не считается готовым к публикации",
            blocked,
            "" if blocked else f"решение={state.decision}, но пост можно подтвердить без ручной проверки",
        ))
    else:
        checks.append(Check("модель уверена в ответе (uncertain=false)", True))
    return checks


def _q(text: object) -> str:
    return "\n".join("> " + line for line in str(text).splitlines()) or "> (пусто)"


def write_report(pipeline: Pipeline, state: RunState, checks: list[Check], providers: str) -> Path:
    run_dir = pipeline.store.run_dir(state.run_id)
    f = state.fields
    lines = [
        f"# E2E-прогон {state.run_id}",
        "",
        f"Провайдеры: {providers}. Статус: **{state.status}**, решение: **{state.decision}**.",
        "",
        "## Результаты проверок",
        "",
        *[f"- {'✅' if c.ok else '❌'} {c.name}" + (f" — {c.detail}" if c.detail and not c.ok else "") for c in checks],
        "",
        "## Распознано",
        "",
        "**Вопрос**", "", _q(f.get("question", "")), "",
        "**Варианты**", "", *[f"{i}. {o}" for i, o in enumerate(f.get("options") or [], 1)], "",
        "## Решение (секретно)",
        "",
        f"**Правильный ответ:** {f.get('correct')} (вариант {f.get('correct_index')})", "",
        f"**Пункт правил:** {f.get('rule_ref', '—')}", "",
        f"**Модель не уверена:** {f.get('uncertain')}", "",
        "**Объяснение**", "", _q(f.get("explanation", "")), "",
        "## Тексты",
        "",
        "**Тизер**", "", _q(f.get("teaser", "")), "",
        "**Озвучка**", "", _q(f.get("voiceover", "")), "",
    ]
    for pname, post in state.posts.items():
        lines += [f"**{pname}**" + (" (с раскрытием)" if post.reveal_secrets else ""), ""]
        if post.title:
            lines += [f"Заголовок: {post.title}", ""]
        lines += ["```", post.text, "```", ""]
    lines += [
        "## Медиа",
        "",
        f"- Озвучка: `{state.artifacts.get('voice_audio', '—')}`",
        f"- Видео: `{state.artifacts.get('video', '—')}`",
        "",
        "## Проверки конвейера",
        "",
        *[f"- ⛔ {x}" for x in state.validation.errors],
        *[f"- ⚠️ {x}" for x in state.validation.block_auto],
        *[f"- {x}" for x in state.validation.warnings],
        "",
        "## Вызовы LLM",
        "",
        *[f"- {c['purpose']}: {c['model']}, токены {c['in']}/{c['out']}" for c in state.llm_calls],
    ]
    path = run_dir / "e2e_report.md"
    path.write_text(redact("\n".join(lines)), encoding="utf-8")
    return path
