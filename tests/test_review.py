"""Option (a): an uncertain answer locks confirmation until a person reviews it."""

import asyncio

import pytest

from contentbot.core.pipeline import PipelineError

from .test_bot_flow import CHAT, OWNER, Harness, h  # noqa: F401  (fixture)


@pytest.fixture
def uncertain(demo_fixtures):
    fx = dict(demo_fixtures)
    fx["solve"] = {**fx["solve"], "uncertain": True}
    return fx


def _buttons(h, run_id):
    card = h.ui.messages[h.bot.session(CHAT).cards[run_id]]
    return {b.data.split(":")[0]: b for row in card.kb for b in row}, card


def test_confirm_locked_and_review_button_shown(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    st = h.to_preview(demo_image)
    buttons, card = _buttons(h, st.run_id)
    assert "ok" not in buttons
    assert buttons["rv"].text == "✅ Я проверил ответ"
    assert "rs" in buttons and "e" in buttons and "r" in buttons  # can still fix / regenerate
    assert "Нужна проверка" in card.text
    assert not h.pipe.can_confirm(st)
    with pytest.raises(PipelineError):
        asyncio.run(h.pipe.approve(st))
    # a forged/old "ok" callback is refused too
    h.run(h.bot.handle_callback(CHAT, OWNER, "cb", f"ok:{st.run_id}", card.id))
    assert h.state().status == "ready"


def test_review_then_manual_confirm_is_journaled(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    st = h.to_preview(demo_image)
    h.click("rv:")
    st = h.state()
    assert st.review["by"] == str(OWNER)
    assert st.events[-1]["type"] == "review_confirmed" and st.events[-1]["by"] == str(OWNER)
    buttons, card = _buttons(h, st.run_id)
    assert "ok" in buttons and "rv" not in buttons
    assert "проверено вручную" in card.text
    h.click("ok:")
    st = h.state()
    assert st.status == "approved"
    approved = st.events[-1]
    assert approved["type"] == "approved" and approved["manual_review"] is True
    assert approved["reviewed_by"] == str(OWNER) and approved["by"] == str(OWNER)
    # the journal is persisted with the run
    assert '"review_confirmed"' in (h.pipe.store.run_dir(st.run_id) / "state.json").read_text(encoding="utf-8")


def test_auto_mode_stops_and_waits_for_review(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    h.send_media([demo_image])
    h.click("p:", "zazerkalye")
    st = h.state()
    st.params["mode"] = "auto"
    h.pipe.store.save(st)
    h.click("s:", "pdd_ticket")
    st = h.state()
    assert st.status == "ready" and st.decision == "await_confirm"
    assert not any(e["type"] == "approved" for e in st.events)
    h.click("rv:")
    assert h.state().status == "ready"  # review alone never publishes, even in auto
    h.click("ok:")
    assert h.state().status == "approved"


def test_review_resets_when_answer_changes(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    st = h.to_preview(demo_image)
    h.click("rv:")
    st = asyncio.run(h.pipe.edit_field(h.state(), "correct", "Синий автомобиль"))
    assert st.review is None
    assert any(e["type"] == "review_reset" for e in st.events)
    assert not h.pipe.can_confirm(st)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_voice_change_keeps_review(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    h.to_preview(demo_image)
    h.click("rv:")
    h.click("v:")
    h.click("vc:", "pedal")
    st = h.state()
    assert st.review is not None and h.pipe.can_confirm(st)


def test_recheck_reruns_only_the_solving_step(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    h.to_preview(demo_image)
    n = len(h.pipe.services.llm.calls)
    h.click("rs:")
    purposes = [c.purpose for c in h.pipe.services.llm.calls[n:]]
    assert "pdd_ticket.solve" in purposes
    assert not any(p.endswith((".extract", ".teaser")) for p in purposes)


def test_certain_answer_needs_no_review(h, demo_image):
    st = h.to_preview(demo_image)
    buttons, _ = _buttons(h, st.run_id)
    assert "ok" in buttons and "rv" not in buttons
    with pytest.raises(PipelineError):
        h.pipe.mark_reviewed(st, by=OWNER)


def test_manual_edit_of_answer_post_resets_review(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    st = h.to_preview(demo_image)
    h.click("rv:")
    assert h.pipe.can_confirm(h.state())

    # the user rewrites the Telegram post (the one that reveals the answer)
    h.click("e:")
    h.click("ep:", "telegram")
    h.say("Ответ: Синий", html_text="Ответ: <tg-spoiler>Синий автомобиль</tg-spoiler>")

    st = h.state()
    assert st.review is None
    assert not h.pipe.can_confirm(st)
    kinds = [e["type"] for e in st.events]
    assert kinds[-2:] == ["answer_changed", "review_reset"]
    change = st.events[-2]
    assert change["by"] == str(OWNER) and change["platform"] == "telegram"
    assert "Синий автомобиль" in change["new"]
    buttons, card = _buttons(h, st.run_id)
    assert "ok" not in buttons and buttons["rv"].text == "✅ Я проверил ответ"
    assert "Нужна проверка" in card.text

    # review again -> confirm becomes available -> approve
    h.click("rv:")
    buttons, _ = _buttons(h, st.run_id)
    assert "ok" in buttons
    h.click("ok:")
    st = h.state()
    assert st.status == "approved" and st.events[-1]["manual_review"] is True


def test_editing_answer_field_is_journaled(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    h.to_preview(demo_image)
    h.click("rv:")
    st = asyncio.run(h.pipe.edit_field(h.state(), "correct", "Синий автомобиль", by=OWNER))
    change = next(e for e in st.events if e["type"] == "answer_changed")
    assert change["field"] == "correct" and change["old"] == "Красный автомобиль" and change["by"] == str(OWNER)
    assert st.review is None


def test_editing_public_post_keeps_review(tmp_path, demo_image, uncertain):
    h = Harness(tmp_path, uncertain)
    h.to_preview(demo_image)
    h.click("rv:")
    h.click("t:", "instagram")
    h.click("e:")
    h.click("ep:", "instagram")
    h.say("Новый текст для Instagram без ответа")
    st = h.state()
    assert st.review is not None and h.pipe.can_confirm(st)
    assert not any(e["type"] == "answer_changed" for e in st.events)
