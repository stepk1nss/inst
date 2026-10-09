"""Post-generation checks: secret leaks, uncertainty flags, platform limits."""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

from ..config.schema import CheckSpec, Project, Scenario
from ..llm.base import TextPart
from ..publishers.capabilities import CAPS
from .context import _strings, normalize, public_fields
from .services import Services
from .state import PlatformPost, RunState, Validation

_TAG_RE = re.compile(r"<[^>]+>")
_RU_LETTERS = "абвгде"
_LAT_LETTERS = "abcdef"
SHINGLE = 6


def public_texts(scenario: Scenario, state: RunState, posts: dict[str, PlatformPost]) -> dict[str, str]:
    """Every text that ends up on a platform without reveal_secrets: the post texts
    and the texts spoken in the voiceover (the audio goes into public videos)."""
    out: dict[str, str] = {}
    spoken = {s.input for s in scenario.steps if s.use == "tts" and s.input}
    for name, value in public_fields(scenario, state.fields).items():
        if name in spoken:
            joined = "\n".join(_strings(value))
            if joined:
                out[f"voiceover:{name}"] = joined
    for pname, post in posts.items():
        if not post.reveal_secrets:
            out[f"post:{pname}"] = "\n".join(t for t in (post.title or "", html.unescape(_TAG_RE.sub("", post.text))) if t)
    return out


def _contains_phrase(secret: str, text: str) -> bool:
    s, t = normalize(secret), normalize(text)
    if not s:
        return False
    words = s.split()
    if len(words) >= SHINGLE:
        return any(" ".join(words[i : i + SHINGLE]) in t for i in range(len(words) - SHINGLE + 1))
    return len(s) >= 4 and re.search(rf"(?<!\w){re.escape(s)}(?!\w)", t) is not None


_KEYWORDS = r"(ответ\w*|вариант\w*|правильн\w*|верн\w*)"


def _mentions_index(index: int, text: str) -> bool:
    """'ответ 2', 'правильный вариант №2', 'ответ — Б.' Letters are matched only
    right after the keyword and followed by punctuation/end, because Russian
    'а', 'в' are also ordinary words ('ответ в Telegram')."""
    if index < 1:
        return False
    if re.search(rf"{_KEYWORDS}( \w+){{0,2}} (№ )?{index}(?!\w)", normalize(text)):
        return True
    if index <= len(_RU_LETTERS):
        letters = f"[{_RU_LETTERS[index - 1]}{_LAT_LETTERS[index - 1]}]"
        pattern = rf"{_KEYWORDS}\s*[:—–-]?\s*[«\"(]?{letters}(?=[).,!?;»\"]|\s*$)"
        return re.search(pattern, text.lower().replace("ё", "е")) is not None
    return False


def deterministic_leaks(chk: CheckSpec, state: RunState, texts: dict[str, str]) -> list[str]:
    """Rule-based leak detection.

    * long secrets (explanations): any 6-word fragment copied into a public text;
    * the correct option: a leak only when it is singled out — naming it among
      the other options ("red or blue car — who yields?") is part of the question;
    * "answer 2" / "вариант Б" style references to the correct index.
    """
    leaks: list[str] = []
    index = state.fields.get(chk.index_field) if chk.index_field else None
    options = state.fields.get(chk.options_field) if chk.options_field else None
    options = [str(o) for o in options] if isinstance(options, list) else []
    option_norms = {normalize(o) for o in options}
    correct_option = options[index - 1] if isinstance(index, int) and 1 <= index <= len(options) else None
    other_options = [o for o in options if o != correct_option]

    secret_values: list[tuple[str, str]] = []
    for name in chk.secrets:
        for s in _strings(state.fields.get(name)):
            secret_values.append((name, s))
    if correct_option is not None:
        secret_values.append((f"{chk.options_field}[{index}]", correct_option))

    for where, text in texts.items():
        reported: set[str] = set()
        for name, value in secret_values:
            if not _contains_phrase(value, text) or normalize(value) in reported:
                continue
            if normalize(value) in option_norms and any(_contains_phrase(o, text) for o in other_options):
                continue  # all options listed, the correct one is not singled out
            reported.add(normalize(value))
            leaks.append(f"{where}: содержит значение '{name}'")
        if isinstance(index, int) and _mentions_index(index, text):
            leaks.append(f"{where}: называет номер правильного варианта")
    return leaks


async def judge_leaks(services: Services, scenario: Scenario, chk: CheckSpec, state: RunState, texts: dict[str, str]) -> list[str]:
    """Cheap model answers only yes/no; its output never flows into any text."""
    secret_block = "\n".join(f"{n}: {state.fields.get(n)}" for n in chk.secrets)
    public_block = "\n\n".join(f"[{k}]\n{v}" for k, v in texts.items())
    schema = {
        "type": "object",
        "properties": {
            "leak": {"type": "boolean"},
            "where": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["leak", "where", "reason"],
        "additionalProperties": False,
    }
    system = (
        "Ты проверяешь, не раскрывают ли публичные тексты секретную информацию (например, правильный ответ). "
        "Утечка — это прямое указание ответа или явный намёк, по которому ответ однозначно угадывается. "
        "Вопрос без подсказок утечкой не является."
    )
    data: dict[str, Any] = await services.llm.generate_json(
        purpose=f"{scenario.id}.judge",
        role="judge",
        system=system,
        content=[TextPart(f"СЕКРЕТ:\n{secret_block}\n\nПУБЛИЧНЫЕ ТЕКСТЫ:\n{public_block}")],
        schema=schema,
    )
    if data.get("leak"):
        where = ", ".join(data.get("where") or []) or "?"
        return [f"судья: возможный намёк на ответ ({where}): {data.get('reason', '')}"]
    return []


async def validate(project: Project, scenario: Scenario, state: RunState, posts: dict[str, PlatformPost], services: Services) -> Validation:
    v = Validation()
    texts = public_texts(scenario, state, posts)

    for chk in scenario.checks:
        if chk.type == "no_secret_leak":
            leaks = deterministic_leaks(chk, state, texts)
            if not leaks and chk.judge:
                key = hashlib.sha256(
                    json.dumps([chk.secrets, [state.fields.get(n) for n in chk.secrets], texts], ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()[:16]
                if key in state.judge_cache:
                    leaks = state.judge_cache[key]
                else:
                    try:
                        leaks = await judge_leaks(services, scenario, chk, state, texts)
                        state.judge_cache[key] = leaks
                    except Exception as e:  # judge failure must not silently pass
                        v.block_auto.append(f"проверка утечки моделью не выполнена: {e}")
            for leak in leaks:
                v.block_auto.append(f"возможная утечка секрета — {leak}")
        elif chk.type == "block_auto_if":
            if chk.field and state.fields.get(chk.field) == chk.equals:
                v.block_auto.append(chk.message or f"{chk.field} = {chk.equals}")

    for pname, post in posts.items():
        caps = CAPS[pname]
        v.errors += [f"{pname}: {e}" for e in post.errors]
        v.warnings += [f"{pname}: {w}" for w in post.warnings]
        length = len(html.unescape(_TAG_RE.sub("", post.text))) if post.html else len(post.text)
        if length > caps.text_limit:
            v.errors.append(f"{pname}: текст {length} символов, лимит {caps.text_limit}")
        if caps.title_limit and post.title and len(post.title) > caps.title_limit:
            v.errors.append(f"{pname}: заголовок {len(post.title)} символов, лимит {caps.title_limit}")
        if caps.title_limit and not post.title:
            v.errors.append(f"{pname}: нет заголовка")
        if not post.text.strip():
            v.errors.append(f"{pname}: пустой текст")
        if post.media_kind is None:
            v.errors.append(f"{pname}: нет медиа для публикации")

    v.warnings += project.warnings
    return v
