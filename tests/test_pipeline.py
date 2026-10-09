import asyncio

import pytest

from contentbot.core.modes import decide
from contentbot.core.state import Validation

from .conftest import HAS_FFMPEG, make_pipeline


def _llm_purposes(pipe):
    return [c.purpose for c in pipe.services.llm.calls]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_voice_change_does_not_regenerate_texts(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))
    state = asyncio.run(pipe.generate(state))
    texts_before = {p: post.text for p, post in state.posts.items()}
    audio_before, video_before = state.artifacts["voice_audio"], state.artifacts["video"]
    llm_before, tts_before = len(pipe.services.llm.calls), pipe.services.tts.calls

    state = asyncio.run(pipe.set_voice(state, "pedal"))

    assert state.status == "ready", state.error
    assert len(pipe.services.llm.calls) == llm_before  # not a single LLM call (judge is cached too)
    assert pipe.services.tts.calls == tts_before + 1
    assert state.last_run_steps == {"extract": "cached", "solve": "cached", "teaser": "cached", "tts": "ran", "video": "ran"}
    assert state.artifacts["voice_audio"] != audio_before
    assert state.artifacts["video"] != video_before
    assert {p: post.text for p, post in state.posts.items()} == texts_before


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_video_is_vertical_1080x1920(tmp_path, demo_image, demo_fixtures):
    import subprocess

    pipe = make_pipeline(tmp_path, demo_fixtures, full_size=True)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    video = pipe.store.run_dir(state.run_id) / state.artifacts["video"]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "1080,1920"
    assert state.posts["instagram"].media_kind == "video"
    assert state.posts["telegram"].media_kind == "photo"


def test_editing_voiceover_reruns_only_tts_and_video(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    n = len(pipe.services.llm.calls)
    state = asyncio.run(pipe.edit_field(state, "voiceover", "Новый текст озвучки без ответа. Кто уступает?"))
    assert state.fields["voiceover"].startswith("Новый текст")
    assert [s for s, v in state.last_run_steps.items() if v == "cached"] == ["extract", "solve", "teaser"]
    # the judge re-checks because the spoken text changed; no generate step ran
    assert all(p.endswith(".judge") for p in _llm_purposes(pipe)[n:])


def test_manual_post_edit_does_not_call_llm_generate(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    n = len(pipe.services.llm.calls)
    state = asyncio.run(pipe.edit_post(state, "tiktok", "Свой текст #пдд"))
    assert state.posts["tiktok"].text == "Свой текст #пдд"
    assert not any(p.endswith((".extract", ".solve", ".teaser")) for p in _llm_purposes(pipe)[n:])


def test_regenerate_teaser_keeps_solution(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    state = asyncio.run(pipe.regenerate_step(state, "teaser"))
    assert state.last_run_steps["extract"] == "cached"
    assert state.last_run_steps["solve"] == "cached"
    assert state.last_run_steps["teaser"] == "ran"


def test_scenario_auto_and_manual_override(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image]))
    assert (state.scenario_id, state.scenario_how) == ("pdd_ticket", "auto")
    state = pipe.set_scenario(state, "simple")
    state = asyncio.run(pipe.generate(state))
    assert state.status == "ready", state.error
    assert state.scenario_how == "manual"
    assert "correct" not in state.fields


def test_scenario_by_caption_tag(tmp_path, demo_image):
    pipe = make_pipeline(tmp_path, {"classify": {"scenario": "simple"}})
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], caption="вот билет #пдд"))
    assert (state.scenario_id, state.scenario_how) == ("pdd_ticket", "tag")


def test_second_project_works_without_core_changes(tmp_path, demo_image):
    fixtures = {
        "describe": {"subject": "Кофейня", "facts": ["Открылась в 2020"], "address": "Невский проспект, 1", "hours": "10:00–22:00"},
    }
    pipe = make_pipeline(tmp_path, fixtures)
    state = asyncio.run(pipe.create_run("spb", [demo_image], caption="Кофейня на Невский проспект, 1", scenario_id="venue"))
    state = asyncio.run(pipe.generate(state))
    assert state.status == "ready", state.error
    assert set(state.posts) == {"telegram", "instagram", "tiktok"}
    assert state.fields["address"] == "Невский проспект, 1"  # present in caption -> kept
    assert state.fields["hours"] == ""  # not in caption -> blanked, never invented
    assert any("hours" in w for w in state.steps["describe"].warnings)


def test_modes():
    ok, flagged, broken = Validation(), Validation(block_auto=["x"]), Validation(errors=["e"])
    assert decide("draft", ok) == "draft"
    assert decide("confirm", ok) == "await_confirm"
    assert decide("auto", ok) == "publish"
    assert decide("auto", flagged) == "await_confirm"
    assert decide("auto", broken) == "await_confirm"


def test_draft_mode_from_params(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket", params={"mode": "draft"}))
    state = asyncio.run(pipe.generate(state))
    assert state.decision == "draft"


def test_dry_run_publish_writes_outbox(tmp_path, demo_image, demo_fixtures):
    from contentbot.publishers.base import publish_posts

    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    run_dir = pipe.store.run_dir(state.run_id)
    results = asyncio.run(publish_posts(state.posts, run_dir))
    if HAS_FFMPEG:
        assert all(r.ok and r.status == "dry_run" for r in results)
        assert (run_dir / "outbox" / "telegram.json").exists()
    else:  # without ffmpeg YouTube has no video and fails on its own, others still go
        assert {r.platform for r in results if r.ok} >= {"telegram"}
