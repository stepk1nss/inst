"""Bot logic, independent of Telegram.

Flow: media -> project -> scenario -> generation -> preview -> voice/text edits
-> confirm. Nothing is published before confirmation; in M2 confirmation
writes a dry-run outbox (real publishing arrives in M3/M4).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from ..core.pipeline import Pipeline, PipelineError
from ..core.state import RunState
from ..media.audio import to_ogg_opus
from . import render
from .ui import ChatUI

log = logging.getLogger(__name__)

SAMPLE_TEXT = "Привет! Так звучит этот голос в ваших роликах."


@dataclass
class Intake:
    id: str
    chat_id: int
    paths: list[Path]
    caption: str


@dataclass
class PendingInput:
    kind: str  # post | field | revise
    run_id: str
    target: str  # platform | field name | step id


@dataclass
class ChatSession:
    pending: PendingInput | None = None
    cards: dict[str, int] = field(default_factory=dict)  # run_id -> card message id


class BotController:
    def __init__(self, pipeline: Pipeline, ui: ChatUI, owner_ids: set[int], data_dir: Path):
        self.pipeline = pipeline
        self.ui = ui
        self.owner_ids = owner_ids
        self.data_dir = data_dir
        self.intakes: dict[str, Intake] = {}
        self.sessions: dict[int, ChatSession] = {}
        self.tabs: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.prefs_path = data_dir / "bot_prefs.json"
        self.recent: dict[str, list[str]] = self._load_prefs()

    # ------------------------------------------------------------------ helpers

    @property
    def registry(self):
        return self.pipeline.registry

    def _load_prefs(self) -> dict[str, list[str]]:
        try:
            return json.loads(self.prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _remember_project(self, chat_id: int, project_id: str) -> None:
        lst = [project_id] + [p for p in self.recent.get(str(chat_id), []) if p != project_id]
        self.recent[str(chat_id)] = lst[:10]
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(json.dumps(self.recent, ensure_ascii=False), encoding="utf-8")

    def session(self, chat_id: int) -> ChatSession:
        return self.sessions.setdefault(chat_id, ChatSession())

    def allowed(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    async def _deny(self, chat_id: int, user_id: int) -> None:
        await self.ui.send_text(
            chat_id,
            f"Нет доступа. Ваш Telegram ID: <code>{user_id}</code>\nДобавьте его в OWNER_TELEGRAM_IDS и перезапустите бота.",
            html=True,
        )

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self.locks.setdefault(run_id, asyncio.Lock())

    def _project_by_tag(self, caption: str) -> str | None:
        words = {w.lower() for w in caption.split()}
        for p in self.registry.projects.values():
            if p.tag and p.tag.lower() in words:
                return p.id
        # a scenario tag also identifies its project when it is unique across projects
        hits = [p.id for p in self.registry.projects.values() for s in p.scenarios.values() if s.tag and s.tag.lower() in words]
        return hits[0] if len(set(hits)) == 1 else None

    def _tab(self, state: RunState) -> str:
        tab = self.tabs.get(state.run_id)
        if tab not in state.posts:
            tab = next(iter(state.posts), "telegram")
        return tab

    def _spoken_field(self, state: RunState) -> str | None:
        sc = self.registry.project(state.project_id).scenarios[state.scenario_id]
        return next((s.input for s in sc.steps if s.use == "tts" and s.input), None)

    def _run_dir(self, state: RunState) -> Path:
        return self.pipeline.store.run_dir(state.run_id)

    # ------------------------------------------------------------------ entry points

    async def handle_media(self, chat_id: int, user_id: int, paths: list[Path], caption: str = "") -> None:
        if not self.allowed(user_id):
            await self._deny(chat_id, user_id)
            return
        intake = Intake(id=secrets.token_hex(4), chat_id=chat_id, paths=paths, caption=caption or "")
        self.intakes[intake.id] = intake
        self.session(chat_id).pending = None
        tagged = self._project_by_tag(intake.caption)
        if tagged:
            await self._start_project(chat_id, intake, tagged)
            return
        if not self.registry.projects:
            await self.ui.send_text(chat_id, "Нет ни одного загруженного проекта. Проверьте конфигурацию: python -m contentbot check")
            return
        await self.ui.send_text(
            chat_id,
            "Выберите проект:",
            render.projects_keyboard(self.registry, intake.id, self.recent.get(str(chat_id), [])),
        )

    async def handle_text(self, chat_id: int, user_id: int, text: str, html_text: str | None = None) -> None:
        if not self.allowed(user_id):
            await self._deny(chat_id, user_id)
            return
        pending = self.session(chat_id).pending
        if pending is None:
            await self.ui.send_text(chat_id, "Пришлите фото или видео для нового поста. /help — подсказка.")
            return
        self.session(chat_id).pending = None
        try:
            state = self.pipeline.store.load(pending.run_id)
        except FileNotFoundError:
            await self.ui.send_text(chat_id, "Пост не найден.")
            return
        if state.status in ("approved", "cancelled"):
            await self.ui.send_text(chat_id, "Этот пост уже закрыт для правок.")
            return
        async with self._lock(state.run_id):
            before = dict(state.artifacts)
            progress = await self.ui.send_text(chat_id, "⏳ Применяю правку…")
            try:
                if pending.kind == "post":
                    post = state.posts.get(pending.target)
                    value = (html_text if html_text is not None else html.escape(text, quote=False)) if post and post.html else text
                    state = await self.pipeline.edit_post(state, pending.target, value)
                elif pending.kind == "field":
                    state = await self.pipeline.edit_field(state, pending.target, text)
                elif pending.kind == "revise":
                    state = await self.pipeline.revise_step(state, pending.target, text)
            except PipelineError as e:
                await self.ui.edit_text(chat_id, progress, f"⛔ {e}")
                return
            await self.ui.edit_text(chat_id, progress, "✅ Правка применена" if state.status == "ready" else f"⛔ {state.error}")
            await self._send_update(chat_id, state, before)

    async def handle_command(self, chat_id: int, user_id: int, command: str, args: str = "") -> None:
        if not self.allowed(user_id):
            await self._deny(chat_id, user_id)
            return
        cmd = command.lstrip("/").split("@", 1)[0].lower()
        if cmd in ("start", "help"):
            await self.ui.send_text(chat_id, render.HELP)
        elif cmd == "projects":
            lines = [f"{p.label} — {', '.join(s.title for s in p.scenarios.values())}" for p in self.registry.projects.values()]
            lines += [f"⛔ {name}: ошибка конфигурации" for name in self.registry.errors]
            await self.ui.send_text(chat_id, "\n".join(lines) or "Проектов нет")
        elif cmd == "cancel":
            self.session(chat_id).pending = None
            await self.ui.send_text(chat_id, "Ок, ввод отменён.")
        elif cmd == "drafts":
            await self._list_drafts(chat_id)
        else:
            await self.ui.send_text(chat_id, "Неизвестная команда. /help")

    async def handle_callback(self, chat_id: int, user_id: int, callback_id: str, data: str, message_id: int) -> None:
        if not self.allowed(user_id):
            await self.ui.answer_callback(callback_id, "Нет доступа")
            return
        action, _, rest = data.partition(":")
        try:
            if action == "p":
                intake_id, _, project_id = rest.partition(":")
                await self.ui.answer_callback(callback_id)
                intake = self.intakes.get(intake_id)
                if intake is None:
                    await self.ui.edit_text(chat_id, message_id, "Материал устарел, пришлите его ещё раз.")
                    return
                await self.ui.edit_text(chat_id, message_id, f"Проект: {self.registry.project(project_id).label}")
                await self._start_project(chat_id, intake, project_id)
                return
            if action == "x":
                self.intakes.pop(rest, None)
                await self.ui.answer_callback(callback_id)
                await self.ui.edit_text(chat_id, message_id, "Отменено.")
                return
            await self._run_callback(chat_id, callback_id, action, rest, message_id)
        except PipelineError as e:
            await self.ui.send_text(chat_id, f"⛔ {e}")
        except Exception as e:  # never kill the bot on one bad update
            log.exception("callback failed: %s", data)
            await self.ui.send_text(chat_id, f"⛔ Внутренняя ошибка: {e}")

    # ------------------------------------------------------------------ run callbacks

    async def _run_callback(self, chat_id: int, callback_id: str, action: str, rest: str, message_id: int) -> None:
        run_id, _, arg = rest.partition(":")
        state = self.pipeline.store.load(run_id)
        project = self.registry.project(state.project_id)
        lock = self._lock(run_id)
        if action == "dx":  # dismiss an auxiliary menu
            await self.ui.answer_callback(callback_id)
            await self.ui.edit_text(chat_id, message_id, "Ок, без изменений.")
            return
        read_only = action in ("t", "e", "v", "bk", "vs", "o", "sc")
        if lock.locked() and not read_only:
            await self.ui.answer_callback(callback_id, "⏳ Подождите, идёт обработка")
            return
        if state.status in ("approved", "cancelled") and action not in ("t", "o"):
            await self.ui.answer_callback(callback_id, "Пост уже закрыт")
            return
        await self.ui.answer_callback(callback_id)

        if action == "s":  # scenario chosen -> generate
            async with lock:
                state = self.pipeline.set_scenario(state, arg)
                await self.ui.edit_text(chat_id, message_id, f"Сценарий: {project.scenarios[arg].title}")
                await self._generate(chat_id, state)
        elif action == "sc":  # offer scenarios again
            await self.ui.send_text(
                chat_id, "Выберите сценарий:", render.scenarios_keyboard(project, run_id, state.scenario_id, "current", cancel_data=f"dx:{run_id}")
            )
        elif action == "g":  # retry generation
            async with lock:
                await self._generate(chat_id, state)
        elif action == "t":
            self.tabs[run_id] = arg
            await self._refresh_card(chat_id, state, message_id)
        elif action == "e":
            await self.ui.edit_text(
                chat_id, message_id, render.preview_card(project, state, self._tab(state)),
                render.edit_keyboard(state, self._tab(state), self._spoken_field(state), self.pipeline.main_text_step(state) is not None),
                html=True,
            )
        elif action == "v":
            if not project.voice.enabled or not project.voice.allowed:
                await self.ui.send_text(chat_id, "В этом проекте озвучка выключена.")
                return
            await self.ui.edit_text(
                chat_id, message_id, render.preview_card(project, state, self._tab(state)),
                render.voice_keyboard(project, state), html=True,
            )
        elif action == "bk":
            await self._refresh_card(chat_id, state, message_id)
        elif action == "vs":
            await self._send_voice_sample(chat_id, state, arg)
        elif action == "vc":
            if arg == (state.params.get("voice_id") or project.voice.default):
                await self.ui.send_text(chat_id, "Этот голос уже выбран.")
                return
            async with lock:
                before = dict(state.artifacts)
                progress = await self.ui.send_text(chat_id, "⏳ Переозвучиваю…")
                state = await self.pipeline.set_voice(state, arg)
                await self.ui.edit_text(chat_id, progress, f"🎙 Голос: {project.voice.get(arg).name}")
                await self._send_update(chat_id, state, before)
        elif action in ("ep", "ef"):
            kind = "post" if action == "ep" else "field"
            self.session(chat_id).pending = PendingInput(kind, run_id, arg)
            what = render.CAPS[arg].title if kind == "post" else "текста озвучки"
            current = state.posts[arg].text if kind == "post" else str(state.fields.get(arg, ""))
            await self.ui.send_text(
                chat_id,
                f"Пришлите новый текст для <b>{render.e(what)}</b> одним сообщением (/cancel — отмена).\n\nСейчас:\n"
                + (current if kind == "post" and state.posts[arg].html else render.e(current)),
                html=True,
            )
        elif action == "ai":
            step_id = self.pipeline.main_text_step(state)
            if step_id is None:
                await self.ui.send_text(chat_id, "В этом сценарии нечего переписывать.")
                return
            self.session(chat_id).pending = PendingInput("revise", run_id, step_id)
            await self.ui.send_text(
                chat_id, "Что изменить? Например: «короче и смешнее», «убери эмодзи», «другой призыв к действию». (/cancel — отмена)"
            )
        elif action == "r":
            step_id = self.pipeline.main_text_step(state)
            async with lock:
                before = dict(state.artifacts)
                progress = await self.ui.send_text(chat_id, "⏳ Пишу тексты заново…")
                state = await self.pipeline.regenerate_step(state, step_id)
                await self.ui.edit_text(chat_id, progress, "✅ Готово" if state.status == "ready" else f"⛔ {state.error}")
                await self._send_update(chat_id, state, before)
        elif action == "ok":
            async with lock:
                if state.validation.errors:
                    await self.ui.send_text(chat_id, "⛔ Нельзя подтвердить, пока есть ошибки:\n" + "\n".join(state.validation.errors))
                    return
                state = await self.pipeline.approve(state)
                await self._report_approved(chat_id, state)
                await self._refresh_card(chat_id, state)
        elif action == "d":
            state = self.pipeline.save_draft(state)
            await self._refresh_card(chat_id, state, message_id)
            await self.ui.send_text(chat_id, "💾 Сохранено в черновики. Открыть позже: /drafts")
        elif action == "c":
            state = self.pipeline.cancel(state)
            if message_id in self.session(chat_id).cards.values():
                await self._refresh_card(chat_id, state, message_id)
            else:
                await self.ui.edit_text(chat_id, message_id, "Отменено.")
        elif action == "o":
            await self._send_preview(chat_id, state)

    # ------------------------------------------------------------------ flow steps

    async def _start_project(self, chat_id: int, intake: Intake, project_id: str) -> None:
        project = self.registry.project(project_id)
        self._remember_project(chat_id, project_id)
        progress = await self.ui.send_text(chat_id, "🔎 Смотрю материал…")
        state = await self.pipeline.create_run(project_id, intake.paths, intake.caption, params={"chat_id": chat_id})
        self.intakes.pop(intake.id, None)
        if state.scenario_how in ("tag", "single"):
            await self.ui.edit_text(chat_id, progress, f"Сценарий: {project.scenarios[state.scenario_id].title}")
            async with self._lock(state.run_id):
                await self._generate(chat_id, state)
            return
        await self.ui.edit_text(
            chat_id,
            progress,
            f"Сценарий для {project.label}:",
            render.scenarios_keyboard(project, state.run_id, state.scenario_id, state.scenario_how),
        )

    async def _generate(self, chat_id: int, state: RunState) -> None:
        progress = await self.ui.send_text(chat_id, "⏳ Генерирую: анализ → тексты → озвучка → видео…")
        state = await self.pipeline.generate(state)
        if state.status != "ready":
            await self.ui.edit_text(
                chat_id,
                progress,
                f"⛔ Не получилось: {state.error}",
                [[render.Button("🔁 Повторить", f"g:{state.run_id}"), render.Button("🔄 Сценарий", f"sc:{state.run_id}")]],
            )
            return
        await self.ui.edit_text(chat_id, progress, "✅ Готово — превью ниже")
        await self._send_preview(chat_id, state)
        if state.decision == "publish":  # auto mode and every check passed
            state = await self.pipeline.approve(state)
            await self._report_approved(chat_id, state, auto=True)
            await self._refresh_card(chat_id, state)

    async def _send_media(self, chat_id: int, state: RunState) -> None:
        run_dir = self._run_dir(state)
        video = state.artifacts.get("video") or (state.source_video.path if state.source_video else None)
        caption = "Превью"
        if video:
            await self.ui.send_video(chat_id, run_dir / video, caption)
        elif state.media:
            await self.ui.send_photo(chat_id, run_dir / state.media[0].path, caption)

    async def _send_voice(self, chat_id: int, state: RunState) -> None:
        audio = state.artifacts.get("voice_audio")
        if not audio:
            return
        path = self._run_dir(state) / audio
        project = self.registry.project(state.project_id)
        voice = project.voice.get(state.params.get("voice_id") or project.voice.default or "")
        caption = f"🎙 {voice.name}" if voice else "🎙 Озвучка"
        if self.pipeline.services.ffmpeg:
            await self.ui.send_voice(chat_id, await to_ogg_opus(path), caption)
        else:
            await self.ui.send_audio(chat_id, path, caption)

    async def _send_card(self, chat_id: int, state: RunState) -> None:
        project = self.registry.project(state.project_id)
        sess = self.session(chat_id)
        old = sess.cards.get(state.run_id)
        if old is not None:  # retire the previous card so only one has live buttons
            await self.ui.edit_text(chat_id, old, "↪️ Обновлённое превью ниже")
        tab = self._tab(state)
        msg = await self.ui.send_text(
            chat_id,
            render.preview_card(project, state, tab),
            render.preview_keyboard(state, tab, self.pipeline.main_text_step(state) is not None),
            html=True,
        )
        sess.cards[state.run_id] = msg

    async def _send_preview(self, chat_id: int, state: RunState) -> None:
        await self._send_media(chat_id, state)
        await self._send_voice(chat_id, state)
        await self._send_card(chat_id, state)

    async def _send_update(self, chat_id: int, state: RunState, artifacts_before: dict[str, str]) -> None:
        """After an edit: resend only what changed, then a fresh card."""
        if state.artifacts.get("video") != artifacts_before.get("video"):
            await self._send_media(chat_id, state)
        if state.artifacts.get("voice_audio") != artifacts_before.get("voice_audio"):
            await self._send_voice(chat_id, state)
        await self._send_card(chat_id, state)

    async def _refresh_card(self, chat_id: int, state: RunState, message_id: int | None = None) -> None:
        project = self.registry.project(state.project_id)
        message_id = message_id or self.session(chat_id).cards.get(state.run_id)
        if message_id is None:
            await self._send_card(chat_id, state)
            return
        tab = self._tab(state)
        await self.ui.edit_text(
            chat_id,
            message_id,
            render.preview_card(project, state, tab),
            render.preview_keyboard(state, tab, self.pipeline.main_text_step(state) is not None),
            html=True,
        )

    async def _send_voice_sample(self, chat_id: int, state: RunState, voice_id: str) -> None:
        services = self.pipeline.services
        project = self.registry.project(state.project_id)
        voice = project.voice.get(voice_id)
        if voice is None or services.tts is None:
            await self.ui.send_text(chat_id, "Озвучка не подключена." if services.tts is None else "Голос не найден.")
            return
        provider_id = voice.provider_voice_id or services.system.tts.fallback_voice_id
        tag = hashlib.sha1(f"{services.tts.name}|{provider_id}|{services.system.tts.model}|{SAMPLE_TEXT}".encode()).hexdigest()[:10]
        path = self.data_dir / "voice_samples" / project.id / f"{voice.id}-{tag}.mp3"
        if not path.exists():
            await services.tts.synthesize(text=SAMPLE_TEXT, voice_id=provider_id, speed=project.voice.speed, out_path=path)
        caption = f"▶ {voice.name}" + ("" if voice.provider_voice_id else " (временный голос)")
        if services.ffmpeg:
            await self.ui.send_voice(chat_id, await to_ogg_opus(path), caption)
        else:
            await self.ui.send_audio(chat_id, path, caption)

    async def _report_approved(self, chat_id: int, state: RunState, auto: bool = False) -> None:
        lines = ["✅ Подтверждено автоматически (режим «Авто»)." if auto else "✅ Подтверждено."]
        lines.append("Реальная публикация в соцсети пока не подключена — пост сохранён и будет готов к отправке:")
        for r in state.publish_results:
            post = state.posts.get(r["platform"])
            note = ""
            if post and post.mode == "draft":
                note = " (в черновики TikTok)"
            elif post and r["platform"] == "youtube":
                note = f" ({post.visibility})"
            mark = "•" if r["ok"] else "⛔"
            lines.append(f"{mark} {render.CAPS[r['platform']].title}{note}" + ("" if r["ok"] else f": {r['error']}"))
        await self.ui.send_text(chat_id, "\n".join(lines))

    async def _list_drafts(self, chat_id: int) -> None:
        rows = []
        for run_id in self.pipeline.store.list()[:50]:
            try:
                st = self.pipeline.store.load(run_id)
            except Exception:
                continue
            if st.status not in ("draft", "ready") or st.project_id not in self.registry.projects:
                continue
            p = self.registry.project(st.project_id)
            sc = p.scenarios.get(st.scenario_id)
            label = f"{p.emoji} {sc.title if sc else st.scenario_id} · {render.STATUS_LABELS.get(st.status, st.status)} · {run_id[:15]}"
            rows.append([render.Button(label, f"o:{run_id}")])
            if len(rows) >= 10:
                break
        if not rows:
            await self.ui.send_text(chat_id, "Черновиков и ожидающих постов нет.")
            return
        await self.ui.send_text(chat_id, "Черновики и посты, ждущие подтверждения:", rows)
