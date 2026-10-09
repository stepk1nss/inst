"""Command line for dev/test: run the whole pipeline without the bot and without publishing.

    python -m contentbot check
    python -m contentbot demo
    python -m contentbot run --project <id> --media photo.jpg [--caption "..."] [--scenario <id>]
    python -m contentbot voice <run_id> <voice_id>
    python -m contentbot edit <run_id> --field voiceover="Новый текст"
    python -m contentbot edit <run_id> --post instagram="Свой текст"
    python -m contentbot regen <run_id> <step_id>
    python -m contentbot scenario <run_id> <scenario_id>
    python -m contentbot publish <run_id>          # dev: dry run only
    python -m contentbot chat [--mock file.yaml]   # the bot flow in the terminal, no Telegram
    python -m contentbot bot                       # Telegram bot (TELEGRAM_BOT_TOKEN)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

from .config.loader import ConfigError, load_all
from .core.pipeline import Pipeline, PipelineError
from .core.services import build_services, load_mock_fixtures
from .core.state import RunState, RunStore
from .preview import render_preview
from .publishers.base import publish_posts
from .settings import SettingsError, get_settings


def _print_state(pipeline: Pipeline, state: RunState) -> None:
    project = pipeline.registry.project(state.project_id)
    sc = project.scenarios[state.scenario_id]
    run_dir = pipeline.store.run_dir(state.run_id)
    print(f"\n{project.label} · {sc.title}  (сценарий: {state.scenario_how}, режим: {state.params.get('mode')})")
    print(f"run: {state.run_id}   статус: {state.status}   решение: {state.decision or '-'}")
    if state.error:
        print(f"ОШИБКА: {state.error}")
    if state.last_run_steps:
        print("шаги: " + ", ".join(f"{k}={v}" for k, v in state.last_run_steps.items()))
    v = state.validation
    for label, items in (("блокирует", v.errors), ("требует подтверждения", v.block_auto), ("предупреждение", v.warnings)):
        for item in items:
            print(f"  [{label}] {item}")
    for pname, post in state.posts.items():
        first = post.text.strip().splitlines()[0] if post.text.strip() else ""
        print(f"  {pname:<9} {post.media_kind or '-':<5} {'с ответом' if post.reveal_secrets else 'без секретов':<12} {first[:70]}")
    if state.status in ("ready", "failed"):
        preview = render_preview(project, state, run_dir)
        print(f"превью: {preview}")


async def _amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = load_all(settings.root)

    if args.cmd == "check":
        print(f"режим: {settings.mode} · LLM: {settings.llm_provider} · TTS: {settings.tts_provider} · FFmpeg: {'да' if settings.ffmpeg else 'нет'}")
        print(f"шаблоны: {', '.join(registry.templates)}")
        for p in registry.projects.values():
            print(f"✓ {p.label} [{p.id}]: сценарии {', '.join(p.scenarios)}; площадки {', '.join(p.enabled_platforms())}")
            for w in p.warnings:
                print(f"    ⚠ {w}")
        for d, err in registry.errors.items():
            print(f"✗ {d}:\n{err}")
        return 1 if registry.errors else 0

    if args.cmd == "projects":
        for p in registry.projects.values():
            print(f"{p.label} [{p.id}] — {', '.join(f'{s.title} ({s.id})' for s in p.scenarios.values())}")
        return 0

    if args.cmd in ("bot", "chat"):
        fixtures = load_mock_fixtures(Path(args.mock)) if args.mock and settings.llm_provider == "mock" else None
        if args.cmd == "chat":
            from .bot.console import run_console

            await run_console(settings, fixtures, args.media)
            return 0
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            print(
                "TELEGRAM_BOT_TOKEN не задан — Telegram-бот не запущен.\n"
                "Локальный конвейер работает и без него: `contentbot demo`, `contentbot run ...`, "
                "а весь сценарий бота можно пройти в симуляторе: `contentbot chat`."
            )
            return 2
        try:
            from .bot.telegram import run_bot
        except ImportError:
            print("Не установлен aiogram: pip install -e '.[bot]'")
            return 2
        from .bot.app import owner_ids_from_env

        if not owner_ids_from_env():
            print("OWNER_TELEGRAM_IDS пуст: бот будет отвечать всем «нет доступа» и показывать их ID. "
                  "Напишите боту, возьмите свой ID из ответа и добавьте в .env.")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        await run_bot(settings, fixtures)
        return 0

    if args.cmd == "e2e":
        return await _e2e(args, settings, registry)

    store = RunStore(settings.data_dir)
    mock_path: Path | None = Path(args.mock) if getattr(args, "mock", None) else None
    state: RunState | None = None
    if getattr(args, "run_id", None):
        state = store.load(args.run_id)
        if mock_path is None and state.params.get("dev_mock"):
            mock_path = Path(state.params["dev_mock"])
    demo_cfg: dict = {}
    if args.cmd == "demo":
        demo_file = settings.root / "examples" / "dev" / "demo.yaml"
        demo_cfg = yaml.safe_load(demo_file.read_text(encoding="utf-8")) or {}
        if mock_path is None and demo_cfg.get("mock"):
            mock_path = demo_file.parent / demo_cfg["mock"]

    services = build_services(settings, registry.system, load_mock_fixtures(mock_path) if settings.llm_provider == "mock" else None)
    pipeline = Pipeline(registry, services, store)

    if args.cmd in ("run", "demo"):
        if args.cmd == "demo":
            from .demo import make_demo_ticket

            image = demo_cfg.get("image", "generated_ticket")
            media = [make_demo_ticket(settings.data_dir / "demo" / "ticket.png") if image == "generated_ticket" else settings.root / image]
            project_id, caption, scenario = demo_cfg["project"], demo_cfg.get("caption") or "", demo_cfg.get("scenario")
        else:
            media = [Path(m) for m in args.media]
            project_id, caption, scenario = args.project, args.caption or "", args.scenario
        params = {}
        if getattr(args, "voice", None):
            params["voice_id"] = args.voice
        if getattr(args, "mode", None):
            params["mode"] = args.mode
        if mock_path is not None and settings.llm_provider == "mock":
            params["dev_mock"] = str(mock_path.resolve())
        state = await pipeline.create_run(project_id, media, caption, scenario, params)
        print(f"сценарий: {state.scenario_id} ({state.scenario_how})")
        state = await pipeline.generate(state)
    elif args.cmd == "voice":
        state = await pipeline.set_voice(state, args.voice, args.speed)
    elif args.cmd == "edit":
        for item in args.field or []:
            name, _, value = item.partition("=")
            state = await pipeline.edit_field(state, name, value)
        for item in args.post or []:
            name, _, value = item.partition("=")
            state = await pipeline.edit_post(state, name, value.replace("\\n", "\n"))
    elif args.cmd == "regen":
        state = await pipeline.regenerate_step(state, args.step)
    elif args.cmd == "scenario":
        state = await pipeline.generate(pipeline.set_scenario(state, args.scenario))
    elif args.cmd == "show":
        pass
    elif args.cmd == "publish":
        if not settings.dev:
            print("Реальная публикация появится на этапах M3/M4. Сейчас доступен только dry-run (CONTENTBOT_MODE=dev).")
            return 2
        if state.status != "ready":
            print("Пост не готов к публикации")
            return 2
        results = await publish_posts(state.posts, store.run_dir(state.run_id), args.platform)
        for r in results:
            print(f"  {r.platform:<9} {'✓' if r.ok else '✗'} {r.status} {r.url or r.error or ''}")
        return 0 if all(r.ok for r in results) else 1

    assert state is not None
    _print_state(pipeline, state)
    return 0 if state.status == "ready" else 1


async def _e2e(args: argparse.Namespace, settings, registry) -> int:
    from .bot.app import build_pipeline
    from .e2e import redact, run_checks, write_report

    real = settings.llm_provider == "anthropic" and settings.tts_provider == "elevenlabs"
    if not real and not args.allow_mock:
        print(
            "Реальный прогон требует ключей: ANTHROPIC_API_KEY и ELEVENLABS_API_KEY (в .env или окружении).\n"
            f"Сейчас: LLM={settings.llm_provider}, TTS={settings.tts_provider}. "
            "Самопроверка инструмента на заглушках: --allow-mock (это НЕ результат реального прогона)."
        )
        return 2
    if real:
        project = registry.project(args.project)
        voice = project.voice.get(args.voice or project.voice.default or "")
        if not (voice and voice.provider_voice_id) and not registry.system.tts.fallback_voice_id:
            print("Не задан голос ElevenLabs: укажите ID голоса проекта (например VOICE_BATRAKAN) или ELEVENLABS_FALLBACK_VOICE_ID.")
            return 2
    if not settings.ffmpeg:
        print("Нужен FFmpeg для озвучки и видео.")
        return 2
    fixtures = load_mock_fixtures(Path(args.mock)) if args.mock and not real else None
    pipeline = build_pipeline(settings, fixtures)
    params = {"voice_id": args.voice} if args.voice else {}
    state = await pipeline.create_run(args.project, [Path(m) for m in args.media], args.caption or "", args.scenario, params)
    print(f"сценарий: {state.scenario_id} ({state.scenario_how}); генерация…")
    state = await pipeline.generate(state)
    run_dir = pipeline.store.run_dir(state.run_id)
    providers = f"LLM={settings.llm_provider}, TTS={settings.tts_provider}" + ("" if real else " — ЗАГЛУШКИ, не реальный прогон")
    if state.status != "ready":
        print(redact(f"⛔ генерация не удалась: {state.error}"))
        return 1
    render_preview(pipeline.registry.project(state.project_id), state, run_dir)
    checks = run_checks(pipeline, state)
    report = write_report(pipeline, state, checks, providers)
    for c in checks:
        print(redact(f"{'✅' if c.ok else '❌'} {c.name}" + (f" — {c.detail}" if c.detail and not c.ok else "")))
    print(f"отчёт: {report}\nпревью: {run_dir / 'preview.html'}")
    return 0 if all(c.ok for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contentbot", description="Контент-конвейер: dev/test CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="проверить конфигурацию всех проектов")
    sub.add_parser("projects", help="список проектов и сценариев")

    x = sub.add_parser("e2e", help="M2.5: полный прогон на реальных AI с проверками секретности (без публикации)")
    x.add_argument("--project", required=True)
    x.add_argument("--scenario")
    x.add_argument("--media", required=True, nargs="+")
    x.add_argument("--caption")
    x.add_argument("--voice")
    x.add_argument("--allow-mock", action="store_true", help="самопроверка инструмента на заглушках")
    x.add_argument("--mock", help="файл mock-ответов (только с --allow-mock)")

    b = sub.add_parser("bot", help="запустить Telegram-бота (нужен TELEGRAM_BOT_TOKEN)")
    b.add_argument("--mock", help="файл с ответами mock-LLM (dev)")
    c = sub.add_parser("chat", help="симулятор бота в терминале (без Telegram)")
    c.add_argument("--mock", help="файл с ответами mock-LLM (dev)")
    c.add_argument("--media", nargs="*", help="сразу отправить материал")

    d = sub.add_parser("demo", help="демо-прогон по examples/dev/demo.yaml (без ключей)")
    d.add_argument("--mock", help="файл с ответами mock-LLM")
    d.add_argument("--voice")
    d.add_argument("--mode", choices=["draft", "confirm", "auto"])

    r = sub.add_parser("run", help="прогнать материал через проект")
    r.add_argument("--project", required=True)
    r.add_argument("--media", required=True, nargs="+")
    r.add_argument("--caption")
    r.add_argument("--scenario", help="сценарий вручную (иначе — автоопределение)")
    r.add_argument("--voice")
    r.add_argument("--mode", choices=["draft", "confirm", "auto"])
    r.add_argument("--mock", help="файл с ответами mock-LLM (dev)")

    for name, help_ in (("voice", "сменить голос (перезапускает только озвучку и видео)"), ("edit", "правка поля или текста площадки"),
                        ("regen", "перегенерировать шаг"), ("scenario", "сменить сценарий"), ("show", "показать пост"),
                        ("publish", "опубликовать (в dev — dry-run)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("run_id")
        p.add_argument("--mock")
        if name == "voice":
            p.add_argument("voice")
            p.add_argument("--speed", type=float)
        if name == "edit":
            p.add_argument("--field", action="append", help="name=value")
            p.add_argument("--post", action="append", help="platform=text")
        if name == "regen":
            p.add_argument("step")
        if name == "scenario":
            p.add_argument("scenario")
        if name == "publish":
            p.add_argument("--platform", action="append")

    args = ap.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except (ConfigError, SettingsError, PipelineError, FileNotFoundError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
