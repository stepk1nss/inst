"""The M2.5 checker must catch what it claims to catch."""

import asyncio

from contentbot.e2e import redact, run_checks

from .conftest import make_pipeline


def _run(tmp_path, demo_image, fixtures):
    pipe = make_pipeline(tmp_path, fixtures)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    return {c.name: c for c in run_checks(pipe, state)}


def test_clean_run_passes(tmp_path, demo_image, demo_fixtures):
    checks = _run(tmp_path, demo_image, demo_fixtures)
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]


def test_answer_in_instagram_is_caught(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    texts = dict(fx["teaser"]["platform_texts"])
    texts["instagram"] = {"text": "Ответ простой: Красный автомобиль уступает!", "hashtags": []}
    fx["teaser"] = {**fx["teaser"], "platform_texts": texts}
    checks = _run(tmp_path, demo_image, fx)
    assert not checks["ответа нет: instagram"].ok
    assert checks["ответа нет: tiktok"].ok


def test_missing_option_in_telegram_is_caught(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["extract"] = {**fx["extract"], "options": ["Красный автомобиль", "Синий автомобиль", "Тот, кто едет быстрее"]}
    pipe = make_pipeline(tmp_path, fx)
    state = asyncio.run(pipe.generate(asyncio.run(pipe.create_run("zazerkalye", [demo_image], scenario_id="pdd_ticket"))))
    state.posts["telegram"].text = state.posts["telegram"].text.replace("Тот, кто едет быстрее", "")
    checks = {c.name: c for c in run_checks(pipe, state)}
    assert not checks["telegram: все варианты ответа на месте"].ok


def test_uncertain_answer_must_not_be_confirmable(tmp_path, demo_image, demo_fixtures):
    fx = dict(demo_fixtures)
    fx["solve"] = {**fx["solve"], "uncertain": True}
    checks = _run(tmp_path, demo_image, fx)
    assert checks["неуверенный ответ не считается готовым к публикации"].ok


def test_redact(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-123")
    assert redact("key=sk-ant-secret-value-123") == "key=<ANTHROPIC_API_KEY:redacted>"
