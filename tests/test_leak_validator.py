import asyncio

from contentbot.config.schema import CheckSpec
from contentbot.core.state import RunState
from contentbot.core.validate import _mentions_index, deterministic_leaks

from .conftest import make_pipeline

CHK = CheckSpec(type="no_secret_leak", secrets=["correct", "explanation"], options_field="options", index_field="correct_index")


def _state(**fields):
    base = {
        "options": ["Красный автомобиль", "Синий автомобиль", "Тот, кто едет быстрее"],
        "correct": "Красный автомобиль",
        "correct_index": 1,
        "explanation": "На перекрёстке равнозначных дорог действует правило помехи справа, красный пропускает синего.",
    }
    base.update(fields)
    return RunState(run_id="x", project_id="p", scenario_id="s", created_at="now", fields=base)


def test_clean_teaser_passes():
    assert deterministic_leaks(CHK, _state(), {"post:ig": "Кто уступает? Пиши вариант в комментариях, ответ в Telegram"}) == []


def test_singled_out_answer_is_a_leak():
    leaks = deterministic_leaks(CHK, _state(), {"post:ig": "Конечно, уступает красный автомобиль!"})
    assert leaks and "post:ig" in leaks[0]


def test_listing_all_options_is_not_a_leak():
    text = "Красный автомобиль или синий автомобиль — кто уступает?"
    assert deterministic_leaks(CHK, _state(), {"post:ig": text}) == []


def test_copied_explanation_is_a_leak():
    text = "Подсказка: действует правило помехи справа, красный пропускает синего"
    assert deterministic_leaks(CHK, _state(), {"post:tt": text})


def test_index_mentions():
    assert _mentions_index(1, "Правильный ответ 1")
    assert _mentions_index(1, "верный вариант №1!")
    assert _mentions_index(2, "Ответ — Б.")
    assert not _mentions_index(1, "Пиши свой вариант в комментариях, а правильный ответ в Telegram")
    assert not _mentions_index(3, "ответ в нашем Telegram")
    assert not _mentions_index(2, "Ответ 12 минут назад")


def test_leak_forces_manual_confirmation_even_in_auto(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["teaser"] = {**fx["teaser"], "voiceover": "Тут всё просто: уступает красный автомобиль, помеха справа!"}
    pipe = make_pipeline(tmp_path, fx)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket", params={"mode": "auto"}))
    state = asyncio.run(pipe.generate(state))
    assert any("утечка" in b for b in state.validation.block_auto)
    assert state.decision == "await_confirm"


def test_uncertain_answer_blocks_auto(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["solve"] = {**fx["solve"], "uncertain": True}
    pipe = make_pipeline(tmp_path, fx)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket", params={"mode": "auto"}))
    state = asyncio.run(pipe.generate(state))
    assert state.decision == "await_confirm"
    assert any("не уверена" in r for r in state.validation.needs_review)


def test_judge_flag_blocks_auto(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["judge"] = {"leak": True, "where": ["post:instagram"], "reason": "намёк на помеху справа"}
    pipe = make_pipeline(tmp_path, fx)
    state = asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket", params={"mode": "auto"}))
    state = asyncio.run(pipe.generate(state))
    assert any("судья" in b for b in state.validation.block_auto)
    assert state.decision == "await_confirm"
