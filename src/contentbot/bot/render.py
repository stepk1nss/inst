"""Texts and keyboards of the bot (Telegram HTML)."""

from __future__ import annotations

import html

from ..config.loader import ConfigRegistry
from ..config.schema import Project
from ..core.state import RunState
from ..publishers.capabilities import CAPS
from .ui import Button, Keyboard

TAB_LABELS = {"telegram": "TG", "instagram": "IG", "tiktok": "TT", "youtube": "YT"}
MODE_LABELS = {"draft": "Черновик", "confirm": "Подтверждение", "auto": "Авто"}
STATUS_LABELS = {
    "ready": "ждёт подтверждения",
    "draft": "черновик",
    "approved": "подтверждён",
    "cancelled": "отменён",
    "failed": "ошибка",
}
CARD_LIMIT = 4000  # Telegram message limit is 4096


def e(text: object) -> str:
    return html.escape(str(text), quote=False)


def projects_keyboard(registry: ConfigRegistry, intake_id: str, recent: list[str]) -> Keyboard:
    order = [p for p in recent if p in registry.projects] + [p for p in registry.projects if p not in recent]
    rows: Keyboard = []
    for i, pid in enumerate(order):
        p = registry.projects[pid]
        mark = " ⭐" if i == 0 and recent else ""
        rows.append([Button(f"{p.label}{mark}", f"p:{intake_id}:{pid}")])
    rows.append([Button("❌ Отмена", f"x:{intake_id}")])
    return rows


def scenarios_keyboard(project: Project, run_id: str, suggested: str, how: str, cancel_data: str | None = None) -> Keyboard:
    rows: Keyboard = []
    order = [suggested] + [s for s in project.scenarios if s != suggested]
    for sid in order:
        sc = project.scenarios[sid]
        label = f"✅ {sc.title} (авто)" if sid == suggested and how == "auto" else sc.title
        rows.append([Button(label, f"s:{run_id}:{sid}")])
    rows.append([Button("❌ Отмена", cancel_data or f"c:{run_id}")])
    return rows


def preview_card(project: Project, state: RunState, tab: str) -> str:
    sc = project.scenarios[state.scenario_id]
    v = state.validation
    voice_id = state.params.get("voice_id") or project.voice.default
    voice = project.voice.get(voice_id) if voice_id else None
    lines = [
        f"<b>{e(project.label)} · {e(sc.title)}</b>",
        f"Голос: {e(voice.name if voice else 'выкл.')} · Режим: {e(MODE_LABELS.get(state.params.get('mode', 'confirm'), '?'))}"
        f" · Статус: {e(STATUS_LABELS.get(state.status, state.status))}",
    ]
    if state.error:
        lines.append(f"⛔ <b>Ошибка:</b> {e(state.error)}")
    for err in v.errors:
        lines.append(f"⛔ {e(err)}")
    for b in v.block_auto:
        lines.append(f"⚠️ {e(b)}")
    for r in v.needs_review:
        if state.review:
            lines.append(f"🔎 {e(r)} — <b>проверено вручную</b>")
        else:
            lines.append(f"🔎 <b>Нужна проверка:</b> {e(r)}. Подтверждение недоступно до проверки.")
    if state.params.get("ingest_note"):
        lines.append(f"ℹ️ {e(state.params['ingest_note'])}")

    post = state.posts.get(tab)
    if post:
        caps = CAPS[tab]
        where = f"{caps.title} → {post.connection}"
        kind = {"photo": "фото", "video": "видео", None: "нет медиа"}[post.media_kind]
        extra = []
        if post.mode == "draft":
            extra.append("в черновики TikTok")
        if tab == "youtube":
            extra.append(post.visibility)
        if post.reveal_secrets:
            extra.append("с ответом")
        header = f"📣 <b>{e(where)}</b> · {kind}" + (f" · {e(', '.join(extra))}" if extra else "")
        body = post.text if post.html else e(post.text)  # telegram posts are already safe HTML
        title = f"<b>{e(post.title)}</b>\n" if post.title else ""
        lines += ["", header, title + body]
        for w in post.warnings:
            lines.append(f"<i>· {e(w)}</i>")
    text = "\n".join(lines)
    if len(text) > CARD_LIMIT:
        text = text[: CARD_LIMIT - 20] + "\n…(обрезано)"
    return text


def preview_keyboard(
    state: RunState, tab: str, can_revise: bool, review_pending: bool = False, can_recheck: bool = False
) -> Keyboard:
    rid = state.run_id
    tabs = [Button(("• " if p == tab else "") + TAB_LABELS.get(p, p), f"t:{rid}:{p}") for p in state.posts]
    if state.status in ("approved", "cancelled"):
        return [tabs]
    rows: Keyboard = [tabs]
    rows.append([Button("🎙 Голос", f"v:{rid}"), Button("✏️ Текст", f"e:{rid}")])
    regen = [Button("🔁 Тексты заново", f"r:{rid}")] if can_revise else []
    rows.append(regen + [Button("🔄 Сценарий", f"sc:{rid}")])
    if review_pending:
        # confirm is locked: the person must review first (or fix / re-check)
        label = state.validation.review_button or "✅ Я проверил"
        rows.append([Button(label, f"rv:{rid}")] + ([Button("🔁 Перепроверить", f"rs:{rid}")] if can_recheck else []))
        rows.append([Button("💾 Черновик", f"d:{rid}"), Button("❌", f"c:{rid}")])
    else:
        rows.append([Button("✅ Подтвердить", f"ok:{rid}"), Button("💾 Черновик", f"d:{rid}"), Button("❌", f"c:{rid}")])
    return rows


def edit_keyboard(state: RunState, tab: str, spoken_field: str | None, can_revise: bool) -> Keyboard:
    rid = state.run_id
    rows: Keyboard = [[Button(f"✍️ Свой текст для {TAB_LABELS.get(tab, tab)}", f"ep:{rid}:{tab}")]]
    if spoken_field:
        rows.append([Button("🗣 Текст озвучки", f"ef:{rid}:{spoken_field}")])
    if can_revise:
        rows.append([Button("🤖 Переписать по указанию", f"ai:{rid}")])
    rows.append([Button("↩️ Назад", f"bk:{rid}")])
    return rows


def voice_keyboard(project: Project, state: RunState) -> Keyboard:
    rid = state.run_id
    current = state.params.get("voice_id") or project.voice.default
    rows: Keyboard = []
    for v in project.voice.allowed:
        mark = "✓ " if v.id == current else ""
        rows.append([Button(f"▶ {v.name}", f"vs:{rid}:{v.id}"), Button(f"{mark}Выбрать", f"vc:{rid}:{v.id}")])
    rows.append([Button("↩️ Назад", f"bk:{rid}")])
    return rows


HELP = (
    "Пришлите фото или видео (можно с подписью).\n"
    "Дальше: проект → сценарий → превью → голос/текст → ✅ Подтвердить.\n\n"
    "Подпись может содержать #тег проекта или сценария — тогда шаги выбора пропускаются.\n"
    "Команды: /projects — проекты, /drafts — черновики и ожидающие посты, /cancel — отменить ввод текста.\n\n"
    "Реальная публикация в соцсети пока не подключена: подтверждённый пост сохраняется для публикации."
)
