"""M2: the main user flow through the bot controller (no Telegram, no network).

photo/video -> project -> scenario -> generation -> preview -> voice/text edits -> confirm
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from contentbot.bot.controller import BotController
from contentbot.bot.ui import Button

from .conftest import HAS_FFMPEG, ROOT, make_pipeline

OWNER = 42
CHAT = 100


@dataclass
class Msg:
    id: int
    kind: str  # text | photo | video | voice | audio
    text: str = ""
    kb: list[list[Button]] | None = None
    path: Path | None = None
    html: bool = False
    history: list[str] = field(default_factory=list)


class FakeUI:
    def __init__(self) -> None:
        self.messages: dict[int, Msg] = {}
        self.log: list[Msg] = []
        self.callback_answers: list[str | None] = []
        self._id = 0

    def _add(self, msg: Msg) -> int:
        self.messages[msg.id] = msg
        self.log.append(msg)
        return msg.id

    def _next(self) -> int:
        self._id += 1
        return self._id

    async def send_text(self, chat_id, text, kb=None, html=False):
        return self._add(Msg(self._next(), "text", text, kb, html=html))

    async def edit_text(self, chat_id, message_id, text, kb=None, html=False):
        m = self.messages[message_id]
        m.history.append(m.text)
        m.text, m.kb, m.html = text, kb, html

    async def send_photo(self, chat_id, path, caption=None):
        return self._add(Msg(self._next(), "photo", caption or "", path=path))

    async def send_video(self, chat_id, path, caption=None):
        return self._add(Msg(self._next(), "video", caption or "", path=path))

    async def send_voice(self, chat_id, path, caption=None):
        return self._add(Msg(self._next(), "voice", caption or "", path=path))

    async def send_audio(self, chat_id, path, caption=None):
        return self._add(Msg(self._next(), "audio", caption or "", path=path))

    async def answer_callback(self, callback_id, text=None):
        self.callback_answers.append(text)

    # helpers for tests
    def last(self, kind: str = "text") -> Msg:
        return next(m for m in reversed(self.log) if m.kind == kind)

    def with_button(self, prefix: str) -> tuple[Msg, Button]:
        for m in reversed(self.log):
            for row in m.kb or []:
                for b in row:
                    if b.data.startswith(prefix):
                        return m, b
        raise AssertionError(f"no button '{prefix}' in {[m.text[:40] for m in self.log[-5:]]}")

    def count(self, kind: str) -> int:
        return sum(1 for m in self.log if m.kind == kind)


class Harness:
    def __init__(self, tmp_path: Path, fixtures: dict):
        self.pipe = make_pipeline(tmp_path, fixtures)
        self.ui = FakeUI()
        self.bot = BotController(self.pipe, self.ui, {OWNER}, tmp_path / "data")

    def run(self, coro):
        return asyncio.run(coro)

    def send_media(self, paths, caption=""):
        self.run(self.bot.handle_media(CHAT, OWNER, list(paths), caption))

    def click(self, prefix: str, contains: str | None = None):
        for m in reversed(self.ui.log):
            for row in m.kb or []:
                for b in row:
                    if b.data.startswith(prefix) and (contains is None or contains in b.data or contains in b.text):
                        self.run(self.bot.handle_callback(CHAT, OWNER, "cb", b.data, m.id))
                        return
        raise AssertionError(f"no button {prefix} {contains}")

    def say(self, text: str, html_text: str | None = None):
        self.run(self.bot.handle_text(CHAT, OWNER, text, html_text))

    def state(self):
        return self.pipe.store.load(self.pipe.store.list()[0])

    def llm_purposes(self):
        return [c.purpose for c in self.pipe.services.llm.calls]

    def to_preview(self, image, caption=""):
        self.send_media([image], caption)
        self.click("p:", "zazerkalye")
        self.click("s:", "pdd_ticket")
        return self.state()


@pytest.fixture
def h(tmp_path, demo_fixtures):
    return Harness(tmp_path, demo_fixtures)


def test_unknown_user_is_refused_and_told_their_id(h, demo_image):
    h.run(h.bot.handle_media(CHAT, 777, [demo_image], ""))
    assert "777" in h.ui.last().text and "Нет доступа" in h.ui.last().text
    assert h.pipe.store.list() == []


def test_main_flow_to_preview_publishes_nothing(h, demo_image):
    h.send_media([demo_image])
    assert h.ui.last().text == "Выберите проект:"
    h.click("p:", "zazerkalye")
    # scenario offered BEFORE generation, auto-detected one first
    _, first = h.ui.with_button("s:")
    assert first.data.endswith(":pdd_ticket") and "(авто)" in first.text
    assert not any(p.endswith(".extract") for p in h.llm_purposes())  # nothing generated yet

    h.click("s:", "pdd_ticket")
    st = h.state()
    assert st.status == "ready" and st.decision == "await_confirm"
    assert st.params["mode"] == "confirm"
    card = h.ui.last()
    assert "Зазеркалье" in card.text and "Билет ПДД" in card.text
    assert {b.data.split(":")[0] for row in card.kb for b in row} >= {"t", "v", "e", "ok", "d", "c"}
    if HAS_FFMPEG:
        assert h.ui.last("video").path.suffix == ".mp4"
        assert h.ui.last("voice").path.suffix == ".ogg"
    run_dir = h.pipe.store.run_dir(st.run_id)
    assert not (run_dir / "outbox").exists()  # nothing "published" before confirmation


def test_tabs_show_answer_only_in_telegram(h, demo_image):
    h.to_preview(demo_image)
    card = h.ui.last()
    assert "<tg-spoiler>Красный автомобиль</tg-spoiler>" in card.text  # telegram tab first
    h.click("t:", "instagram")
    assert "Instagram" in card.text and "Красный автомобиль" not in card.text
    h.click("t:", "youtube")
    assert "private" in card.text and "Красный автомобиль" not in card.text


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_voice_change_does_not_call_claude(h, demo_image):
    st = h.to_preview(demo_image)
    llm_before, tts_before = len(h.pipe.services.llm.calls), h.pipe.services.tts.calls
    voices_before, videos_before = h.ui.count("voice"), h.ui.count("video")

    h.click("v:")
    h.click("vc:", "pedal")

    st = h.state()
    assert st.params["voice_id"] == "pedal"
    assert len(h.pipe.services.llm.calls) == llm_before
    assert h.pipe.services.tts.calls == tts_before + 1
    assert h.ui.count("voice") == voices_before + 1 and h.ui.count("video") == videos_before + 1
    assert "Педаль" in h.ui.last().text


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_voice_sample_is_cached(h, demo_image):
    h.to_preview(demo_image)
    h.click("v:")
    tts0 = h.pipe.services.tts.calls
    h.click("vs:", "pedal")
    h.click("vs:", "pedal")
    assert h.pipe.services.tts.calls == tts0 + 1
    assert "Педаль" in h.ui.last("voice").text


def test_manual_text_edit_skips_analysis(h, demo_image):
    h.to_preview(demo_image)
    h.click("t:", "instagram")
    h.click("e:")
    h.click("ep:", "instagram")
    n = len(h.pipe.services.llm.calls)
    h.say("Мой текст для Instagram 🚗")
    st = h.state()
    assert st.posts["instagram"].text == "Мой текст для Instagram 🚗"
    new = h.llm_purposes()[n:]
    assert not any(p.endswith((".extract", ".solve", ".teaser")) for p in new)
    assert all(not c.images for c in h.pipe.services.llm.calls[n:])


def test_telegram_edit_keeps_formatting_via_html(h, demo_image):
    h.to_preview(demo_image)
    h.click("e:")
    h.click("ep:", "telegram")
    h.say("Ответ: 1", html_text="Ответ: <tg-spoiler>1</tg-spoiler>")
    assert h.state().posts["telegram"].text == "Ответ: <tg-spoiler>1</tg-spoiler>"


def test_voiceover_edit_reruns_only_tts(h, demo_image):
    h.to_preview(demo_image)
    h.click("e:")
    h.click("ef:", "voiceover")
    n = len(h.pipe.services.llm.calls)
    h.say("Перекрёсток без знаков. Кто уступает? Ответ — в Телеграме.")
    st = h.state()
    assert st.fields["voiceover"].startswith("Перекрёсток без знаков")
    assert st.last_run_steps["extract"] == st.last_run_steps["solve"] == st.last_run_steps["teaser"] == "cached"
    assert all(not c.images for c in h.pipe.services.llm.calls[n:])


def test_ai_rewrite_sees_no_image_and_no_answer(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["pdd_ticket.teaser.revise"] = {**fx["teaser"], "teaser": "Короче: кто уступает? 😏"}
    h = Harness(tmp_path, fx)
    h.to_preview(demo_image)
    h.click("e:")
    h.click("ai:")
    n = len(h.pipe.services.llm.calls)
    h.say("Короче и смешнее")
    calls = h.pipe.services.llm.calls[n:]
    revise = next(c for c in calls if c.purpose == "pdd_ticket.teaser.revise")
    assert revise.images == []
    for key in ("correct", "explanation", "rule_ref", "correct_index"):
        assert f'"{key}"' not in revise.text_input
    assert "Короче и смешнее" in revise.text_input
    assert not any(c.purpose.endswith((".extract", ".solve")) for c in calls)
    st = h.state()
    assert st.fields["teaser"] == "Короче: кто уступает? 😏"
    assert "Короче: кто уступает?" in st.posts["telegram"].text


def test_confirm_writes_outbox_only_after_click(h, demo_image):
    st = h.to_preview(demo_image)
    run_dir = h.pipe.store.run_dir(st.run_id)
    assert not (run_dir / "outbox").exists()
    h.click("ok:")
    st = h.state()
    assert st.status == "approved"
    report = next(m for m in reversed(h.ui.log) if m.text.startswith("✅ Подтверждено"))
    assert "не подключена" in report.text
    if HAS_FFMPEG:
        assert {r["platform"] for r in st.publish_results if r["ok"]} == {"telegram", "instagram", "tiktok", "youtube"}
        assert (run_dir / "outbox" / "telegram.json").exists()
    # closed for edits
    h.click("t:", "telegram")
    card = h.ui.messages[h.bot.session(CHAT).cards[st.run_id]]
    assert all(b.data.startswith("t:") for row in card.kb for b in row)


def test_confirm_refused_when_post_has_errors(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["teaser"] = {**fx["teaser"], "platform_texts": {**fx["teaser"]["platform_texts"], "youtube": {"title": "x" * 150, "text": "t", "hashtags": []}}}
    h = Harness(tmp_path, fx)
    h.to_preview(demo_image)
    assert any("заголовок" in e for e in h.state().validation.errors)
    h.click("ok:")
    assert h.state().status == "ready"
    assert "Нельзя подтвердить" in h.ui.last().text


def test_draft_and_reopen(h, demo_image):
    st = h.to_preview(demo_image)
    h.click("d:")
    assert h.state().status == "draft"
    h.run(h.bot.handle_command(CHAT, OWNER, "/drafts"))
    h.click("o:", st.run_id)
    assert "черновик" in h.ui.last().text


def test_cancel(h, demo_image):
    h.to_preview(demo_image)
    h.click("c:")
    assert h.state().status == "cancelled"


def test_change_scenario_after_preview(h, demo_image):
    h.to_preview(demo_image)
    h.click("sc:")
    h.click("s:", "simple")
    st = h.state()
    assert st.scenario_id == "simple" and st.status == "ready"
    assert "correct" not in st.fields


def test_project_and_scenario_by_caption_tags(h, demo_image):
    h.send_media([demo_image], "#зазеркалье #пдд")
    st = h.state()
    assert (st.project_id, st.scenario_id, st.scenario_how) == ("zazerkalye", "pdd_ticket", "tag")
    assert st.status == "ready"  # no buttons needed


def test_recent_project_is_offered_first(h, demo_image):
    h.to_preview(demo_image)
    h.send_media([demo_image])
    _, first = h.ui.with_button("p:")
    assert first.data.endswith(":zazerkalye") and "⭐" in first.text


def test_auto_mode_still_waits_when_check_fires(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["solve"] = {**fx["solve"], "uncertain": True}
    h = Harness(tmp_path, fx)
    h.send_media([demo_image])
    # force auto mode for this run through the pipeline params
    st = h.state() if h.pipe.store.list() else None
    assert st is None
    h.click("p:", "zazerkalye")
    st = h.state()
    st.params["mode"] = "auto"
    h.pipe.store.save(st)
    h.click("s:", "pdd_ticket")
    st = h.state()
    assert st.decision == "await_confirm" and st.status == "ready"


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_video_material_flow(h, tmp_path, demo_fixtures):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=500:duration=2", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video)],
        check=True,
    )
    h.send_media([video])
    h.click("p:", "zazerkalye")
    h.click("s:", "pdd_ticket")
    st = h.state()
    assert st.status == "ready", st.error
    assert st.source_video is not None and len(st.media) == 2  # frames for analysis
    assert all(p.media_kind == "video" for p in st.posts.values())
    extract = next(c for c in h.pipe.services.llm.calls if c.purpose.endswith(".extract"))
    assert len(extract.images) == 2 and all(i.endswith(".jpg") for i in extract.images)


def test_bot_module_works_without_aiogram_import():
    """The controller must not depend on aiogram (only the Telegram transport does)."""
    for name in ("controller.py", "render.py", "ui.py", "console.py", "app.py"):
        assert "aiogram" not in (ROOT / "src" / "contentbot" / "bot" / name).read_text(encoding="utf-8")


def test_cli_bot_without_token_exits_cleanly(monkeypatch, capsys):
    from contentbot.cli import main

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(ROOT)
    assert main(["bot"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_telegram_adapter_builds_without_network(tmp_path, demo_fixtures):
    pytest.importorskip("aiogram")
    from aiogram import Bot

    from contentbot.bot.telegram import TelegramUI, _markup, build_dispatcher

    bot = Bot("123456:" + "A" * 35)  # syntactically valid fake token, no requests are made
    pipe = make_pipeline(tmp_path, demo_fixtures)
    controller = BotController(pipe, TelegramUI(bot), {OWNER}, tmp_path / "data")
    dp = build_dispatcher(bot, controller, tmp_path / "inbox")
    assert len(dp.message.handlers) == 3 and len(dp.callback_query.handlers) == 1
    markup = _markup([[Button("a", "t:x:telegram")], []])
    assert markup.inline_keyboard[0][0].callback_data == "t:x:telegram"
    assert _markup(None) is None
    asyncio.run(bot.session.close())


def test_callback_data_fits_telegram_limit(h, demo_image):
    h.to_preview(demo_image)
    h.click("v:")
    for m in h.ui.log:
        for row in m.kb or []:
            for b in row:
                assert len(b.data.encode()) <= 64, b.data
