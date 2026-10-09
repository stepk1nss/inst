"""Secrecy guarantees: the answer can never reach the teaser generator."""

import asyncio

import pytest

from contentbot.config.loader import load_all
from contentbot.config.schema import Sees, StepSpec
from contentbot.core.context import SecretIsolationError, StepView, assert_no_secrets, build_view
from contentbot.core.state import RunState

from .conftest import copy_config_root, make_pipeline, write_project

SECRET_KEYS = ("correct", "correct_index", "explanation", "rule_ref", "uncertain")


def _quiz_project(**overrides):
    data = {
        "id": "quiz",
        "name": "Quiz",
        "platforms": {
            "telegram": {"connection": "tg"},
            "instagram": {"connection": "ig"},
        },
        "scenario_selection": {"default": "q"},
        "scenarios": [{"id": "q", "title": "Q", "extends": "quiz_teaser", **overrides}],
    }
    return data


def _load_errors(tmp_path, project):
    root = copy_config_root(tmp_path)
    write_project(root, project["id"], project)
    return load_all(root).errors.get(project["id"], "")


def test_valid_quiz_project_loads(tmp_path):
    assert _load_errors(tmp_path, _quiz_project()) == ""


def test_taint_rule_rejects_public_output_from_secret_step(tmp_path):
    proj = _quiz_project(overrides={"steps": {"solve": {"produces": ["correct", "correct_index", "explanation", "uncertain", "teaser"]}}})
    err = _load_errors(tmp_path, proj)
    assert "must be secret" in err and "teaser" in err


def test_public_step_cannot_list_secret_input(tmp_path):
    proj = _quiz_project(overrides={"steps": {"teaser": {"sees": {"fields": ["question", "correct"]}}}})
    err = _load_errors(tmp_path, proj)
    assert "sees secret field 'correct'" in err


def test_public_platform_template_cannot_use_secret(tmp_path):
    proj = _quiz_project(overrides={"platforms": {"instagram": {"template": "{{ teaser }} {{ correct }}"}}})
    err = _load_errors(tmp_path, proj)
    assert "instagram" in err and "secret field 'correct'" in err


def test_tts_of_secret_text_is_rejected(tmp_path):
    proj = _quiz_project(overrides={"steps": {"tts": {"input": "explanation"}}})
    assert "is secret" in _load_errors(tmp_path, proj)


def test_external_source_not_connected_yet(tmp_path):
    proj = _quiz_project(overrides={"fields": {"situation": {"source": "enrich.maps"}}})
    assert "not connected yet" in _load_errors(tmp_path, proj)


def test_view_drops_secrets_even_if_config_asks_for_all(registry):
    sc = registry.project("zazerkalye").scenarios["pdd_ticket"]
    state = RunState(run_id="x", project_id="zazerkalye", scenario_id="pdd_ticket", created_at="now",
                     fields={"question": "q", "situation": "s", "correct": "A", "explanation": "e", "rule_ref": "r"})
    step = StepSpec(id="t", use="generate", sees=Sees(fields="all"), produces=["teaser"])
    view = build_view(sc, step, state, run_dir=None)  # type: ignore[arg-type]
    assert set(view.fields) == {"question", "situation"}


def test_runtime_assertion_catches_secret_in_view(registry):
    sc = registry.project("zazerkalye").scenarios["pdd_ticket"]
    step = sc.step("teaser")
    with pytest.raises(SecretIsolationError):
        assert_no_secrets(sc, step, StepView(fields={"question": "q", "correct": "A"}))


def test_teaser_generator_never_receives_answer(tmp_path, demo_image, demo_fixtures):
    """End to end: inspect exactly what the mock LLM received for each step."""
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))
    state = asyncio.run(pipe.generate(state))
    assert state.status == "ready", state.error

    calls = {c.purpose: c for c in pipe.services.llm.calls}
    teaser = calls["pdd_ticket.teaser"]
    payload = teaser.text_input
    for key in SECRET_KEYS:
        assert f'"{key}"' not in payload
    assert state.fields["explanation"] not in payload
    assert state.fields["rule_ref"] not in payload
    assert teaser.images == []  # no image: the model could solve the ticket from it
    assert '"options"' not in payload  # options are withheld from the teaser too

    extract = calls["pdd_ticket.extract"]
    for key in SECRET_KEYS:
        assert f'"{key}"' not in extract.text_input

    # recorded proof in the run state
    assert state.steps["teaser"].seen_fields == ["question", "situation"]
    assert state.steps["teaser"].seen_media is False


def test_answer_only_in_telegram(tmp_path, demo_image, demo_fixtures):
    pipe = make_pipeline(tmp_path, demo_fixtures)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))
    state = asyncio.run(pipe.generate(state))
    tg = state.posts["telegram"].text
    assert "<tg-spoiler>Красный автомобиль</tg-spoiler>" in tg
    for opt in state.fields["options"]:
        assert opt in tg
    assert "13.11" in tg and state.fields["explanation"][:30] in tg
    for p in ("instagram", "tiktok", "youtube"):
        text = state.posts[p].text + (state.posts[p].title or "")
        assert "Красный автомобиль" not in text
        assert "13.11" not in text
