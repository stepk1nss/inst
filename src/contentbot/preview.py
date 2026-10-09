"""Dev preview: a static HTML page next to the run (data/runs/<id>/preview.html).

The Telegram bot preview (M2) shows the same information inside the chat.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .config.schema import Project
from .core.state import RunState
from .publishers.capabilities import CAPS

CSS = """
:root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e2e2dc;
        --ok:#1f7a4d; --warn:#9a6700; --err:#b42318; --accent:#3b5bdb; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#21211f; --ink:#ececea;
        --muted:#a3a39c; --line:#34342f; --ok:#4cc38a; --warn:#e0b44a; --err:#ff7b72; --accent:#8da2fb; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px } h2 { font-size:17px; margin:28px 0 10px }
.muted { color:var(--muted) } .grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
.card h3 { margin:0 0 6px; font-size:15px } .text { white-space:pre-wrap; word-wrap:break-word; }
.badge { display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; border:1px solid var(--line); margin-right:4px }
.secret { color:var(--warn) } .ok { color:var(--ok) } .err { color:var(--err) } .warn { color:var(--warn) }
.spoiler { background:var(--ink); color:transparent; border-radius:4px; cursor:pointer; transition:.2s }
.spoiler:hover, .spoiler:focus { background:transparent; color:inherit }
img, video { max-width:100%; border-radius:10px; display:block } video { max-height:520px }
table { width:100%; border-collapse:collapse; font-size:14px } td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top }
code { font-size:13px } ul { margin:6px 0; padding-left:20px }
"""


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _telegram_html(text: str) -> str:
    # post.text is already Telegram-safe HTML (values escaped at compose time)
    return text.replace("<tg-spoiler>", '<span class="spoiler" tabindex="0">').replace("</tg-spoiler>", "</span>")


def render_preview(project: Project, state: RunState, run_dir: Path) -> Path:
    sc = project.scenarios[state.scenario_id]
    secrets = sc.secret_fields()
    v = state.validation
    decision_label = {"draft": "Черновик", "await_confirm": "Ждёт подтверждения", "publish": "Будет опубликован автоматически"}
    out: list[str] = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Превью поста</title><style>{CSS}</style></head><body><main>",
        f"<h1>{_e(project.label)} · {_e(sc.title)}</h1>",
        f"<div class='muted'>run {_e(state.run_id)} · сценарий выбран: {_e(state.scenario_how)} · "
        f"режим: {_e(state.params.get('mode'))} · статус: {_e(state.status)} · "
        f"<b>{_e(decision_label.get(state.decision, state.decision))}</b></div>",
    ]
    if state.error:
        out.append(f"<p class='err'><b>Ошибка:</b> {_e(state.error)}</p>")

    # checks
    out.append("<h2>Проверки</h2><div class='card'>")
    if not (v.errors or v.block_auto or v.warnings):
        out.append("<span class='ok'>Всё в порядке</span>")
    for cls, title, items in (("err", "Блокирует публикацию", v.errors), ("err", "Нужна ручная проверка (подтверждение заблокировано)", v.needs_review),
                              ("warn", "Требует подтверждения", v.block_auto), ("muted", "Предупреждения", v.warnings)):
        if items:
            out.append(f"<b class='{cls}'>{title}</b><ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>")
    out.append("</div>")

    # media
    out.append("<h2>Медиа и озвучка</h2><div class='grid'>")
    for m in state.media:
        out.append(f"<div class='card'><h3>Исходник</h3><img src='{_e(m.path)}' alt='исходное изображение'></div>")
    if "video" in state.artifacts:
        out.append(f"<div class='card'><h3>Видеообёртка</h3><video controls src='{_e(state.artifacts['video'])}'></video></div>")
    if "voice_audio" in state.artifacts:
        voice = state.params.get("voice_id") or project.voice.default
        out.append(
            f"<div class='card'><h3>Озвучка · голос: {_e(voice)}</h3>"
            f"<audio controls src='{_e(state.artifacts['voice_audio'])}'></audio></div>"
        )
    out.append("</div>")

    # posts
    out.append("<h2>Публикации по площадкам</h2><div class='grid'>")
    for pname, post in state.posts.items():
        caps = CAPS[pname]
        badges = [f"<span class='badge'>{_e(post.format)}</span>", f"<span class='badge'>{_e(post.media_kind or 'нет медиа')}</span>"]
        badges.append("<span class='badge secret'>с ответом</span>" if post.reveal_secrets else "<span class='badge'>без секретов</span>")
        if post.mode == "draft":
            badges.append("<span class='badge'>черновик TikTok</span>")
        if pname == "youtube":
            badges.append(f"<span class='badge'>{_e(post.visibility)}</span>")
        body = _telegram_html(post.text) if post.html else _e(post.text)
        title = f"<p><b>{_e(post.title)}</b></p>" if post.title else ""
        problems = "".join(f"<li class='err'>{_e(e)}</li>" for e in post.errors) + "".join(
            f"<li class='warn'>{_e(w)}</li>" for w in post.warnings
        )
        out.append(
            f"<div class='card'><h3>{_e(caps.title)} <span class='muted'>→ {_e(post.connection)}</span></h3>"
            f"{''.join(badges)}{title}<div class='text'>{body}</div>"
            + (f"<ul>{problems}</ul>" if problems else "")
            + "</div>"
        )
    out.append("</div>")

    # isolation proof
    out.append("<h2>Изоляция данных: что видел каждый шаг</h2><div class='card'><table>")
    out.append("<tr><th>Шаг</th><th>Статус</th><th>Поля на входе</th><th>Медиа</th><th>Подпись</th></tr>")
    for step in sc.steps:
        rec = state.steps.get(step.id)
        if rec is None:
            continue
        seen = ", ".join(
            f"<span class='secret'>🔒{_e(f)}</span>" if f in secrets else _e(f) for f in rec.seen_fields
        ) or "—"
        out.append(
            f"<tr><td><code>{_e(step.id)}</code> <span class='muted'>{_e(step.use)}</span></td>"
            f"<td>{_e(state.last_run_steps.get(step.id, ''))}</td><td>{seen}</td>"
            f"<td>{'да' if rec.seen_media else 'нет'}</td><td>{'да' if rec.seen_caption else 'нет'}</td></tr>"
        )
    out.append("</table></div>")

    # fields
    out.append("<h2>Сгенерированные поля</h2><div class='card'><table>")
    for name, value in state.fields.items():
        mark = "<span class='secret'>🔒 секрет</span>" if name in secrets else ""
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        out.append(f"<tr><td><code>{_e(name)}</code> {mark}</td><td class='text'>{_e(shown)}</td></tr>")
    out.append("</table></div>")

    if state.llm_calls:
        out.append("<h2>Вызовы LLM</h2><div class='card'><table><tr><th>Назначение</th><th>Модель</th><th>Токены in/out</th></tr>")
        for c in state.llm_calls:
            out.append(f"<tr><td>{_e(c['purpose'])}</td><td>{_e(c['model'])}</td><td>{c['in']}/{c['out']}</td></tr>")
        out.append("</table></div>")

    out.append("</main></body></html>")
    path = run_dir / "preview.html"
    path.write_text("\n".join(out), encoding="utf-8")
    return path
