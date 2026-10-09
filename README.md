# contentbot — универсальный контент-конвейер

Одно ядро → проекты → сценарии → посты. Материал (фото) → анализ → генерация текстов → проверка секретности → озвучка → видеообёртка → превью → публикация в Telegram / Instagram / TikTok / YouTube.

Проектирование: [`docs/MVP_SPEC.md`](docs/MVP_SPEC.md) (финальная спецификация), [`docs/ARCHITECTURE_v3.md`](docs/ARCHITECTURE_v3.md), факты об API площадок — [`docs/AUDIT.md`](docs/AUDIT.md).

## Статус

| Этап | Что | Статус |
|---|---|---|
| M1 | Каркас: конфиги, движок шагов, секретные поля, dev-режим, CLI, HTML-превью, тесты | ✅ |
| M2 | Telegram-бот: фото/видео → проект → сценарий → превью → голос/текст → подтверждение | ✅ |
| M3 | Публикация Telegram + Instagram, журнал | следующий |
| M4 | TikTok (черновики) + YouTube (приватно) до аудита | — |
| M5 | `/new_project`, `/test` | — |

## Быстрый старт (без ключей API)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m contentbot check     # проверить конфиги всех проектов
.venv/bin/python -m contentbot demo      # демо-билет ПДД → превью (mock-LLM, mock-голос)
```

Нужен FFmpeg (для озвучки-заглушки и видео). Без FFmpeg конвейер тоже работает, но без аудио и видео.

`demo` выводит путь к `data/runs/<run_id>/preview.html` — откройте в браузере: медиа, озвучка, видео, тексты по площадкам, проверки и таблица «что видел каждый шаг».

## Telegram-бот (M2)

Без токена бота весь сценарий можно пройти в терминале:

```bash
.venv/bin/python -m contentbot chat --mock examples/dev/pdd_demo.mock.yaml --media data/demo/ticket.png
# цифра — нажать кнопку, любой текст — ответить боту, media <путь> [подпись] — новый материал
```

Запуск настоящего бота:

1. Создайте бота у [@BotFather](https://t.me/BotFather), токен — в `.env` как `TELEGRAM_BOT_TOKEN`.
2. `pip install -e ".[bot]"` и `.venv/bin/python -m contentbot bot`.
3. Напишите боту что угодно — он ответит «нет доступа» и покажет ваш Telegram ID. Добавьте его в `OWNER_TELEGRAM_IDS` и перезапустите.

Бот работает локально (long polling): сервер, домен и HTTPS для него не нужны. Без `ANTHROPIC_API_KEY` / `ELEVENLABS_API_KEY` он использует mock-провайдеры (для осмысленных mock-текстов: `contentbot bot --mock examples/dev/pdd_demo.mock.yaml`).

Сценарий в чате:

1. Фото, альбом или видео (до 20 МБ — ограничение Telegram для ботов). В подписи можно указать `#проект` / `#сценарий` — шаги выбора пропускаются.
2. Выбор проекта (последний — первым, ⭐).
3. Выбор сценария **до генерации**: автоопределённый — первым.
4. Превью: видео/фото, голосовое с озвучкой, карточка текстов с вкладками TG / IG / TT / YT, предупреждения проверок.
5. Правки: 🎙 голос (с образцами ▶; Claude не вызывается), ✏️ свой текст для площадки, 🗣 текст озвучки, 🤖 переписать по указанию (без повторного анализа изображения), 🔁 тексты заново, 🔄 сменить сценарий.
6. ✅ Подтвердить / 💾 Черновик (`/drafts`) / ❌. До подтверждения ничего не публикуется; в M2 подтверждение сохраняет пост в `outbox/` (реальная публикация — M3/M4).

## Режимы

| Переменная | Значения | По умолчанию |
|---|---|---|
| `CONTENTBOT_MODE` | `dev` — mock там, где нет ключа, публикация всегда dry-run; `prod` — только реальные провайдеры | `dev` |
| `CONTENTBOT_LLM` | `mock` / `anthropic` | `anthropic`, если есть `ANTHROPIC_API_KEY`, иначе `mock` |
| `CONTENTBOT_TTS` | `mock` / `elevenlabs` / `none` | `elevenlabs`, если есть `ELEVENLABS_API_KEY`, иначе `mock` |

Ключи — только в `.env` (шаблон: `.env.example`), в git он не попадает.

## Команды CLI

```bash
contentbot run --project <id> --media photo.jpg [--caption "..."] [--scenario <id>] [--voice <id>] [--mode draft|confirm|auto] [--mock file.yaml]
contentbot voice <run_id> <voice_id> [--speed 1.1]   # только озвучка и видео, тексты не перегенерируются
contentbot edit <run_id> --field voiceover="..."     # правка поля
contentbot edit <run_id> --post instagram="..."      # свой текст для площадки
contentbot regen <run_id> <step_id>                  # перегенерировать шаг (и зависимые)
contentbot scenario <run_id> <scenario_id>           # сменить сценарий
contentbot publish <run_id> [--platform telegram]    # в dev — dry-run в outbox/
```

## Как устроено

```
config/system.yaml          общие настройки: модели, рендер
templates/*.yaml            переиспользуемые сценарии (quiz_teaser, place, simple_post)
projects/<id>/project.yaml  проект: стиль, голоса, площадки, сценарии (extends + overrides)
projects/<id>/prompts/      материалы проекта (сейчас заглушки с пометкой PLACEHOLDER)
src/contentbot/
  config/    схема (Pydantic), загрузка, статические проверки секретности
  core/      движок, изоляция входов шагов, сборка постов, проверки, режимы
  steps/     шаги-плагины: generate, tts, render_video (+ точка расширения enrich)
  llm/       Claude (structured outputs) и mock
  tts/       ElevenLabs и mock
  media/     Pillow (нормализация, поля 4:5), FFmpeg (видеообёртка)
  publishers/ лимиты площадок, интерфейс публикации, dry-run
```

### Секретные поля

Поле с `secret: true` (например, правильный ответ) видят только шаги с `sees.secrets: true` и площадки с `reveal_secrets: true`. Это обеспечивает ядро:
- входные данные шага собирает ядро и вырезает секреты;
- всё, что произвёл шаг, видевший секреты, обязано быть секретным (иначе проект не загрузится);
- шаблон публичной площадки, ссылающийся на секрет, — ошибка загрузки;
- после генерации — проверка утечки в публичных текстах и озвучке (правила + модель-судья). При подозрении пост не публикуется автоматически.

### Голоса

ID голосов ElevenLabs задаются в `.env` (`VOICE_BATRAKAN=...`) или прямо в `project.yaml`. Пустой ID → временный голос `ELEVENLABS_FALLBACK_VOICE_ID`. Голос меняется для конкретного поста (`contentbot voice`), при этом тексты не перегенерируются.

### Новый проект

Создайте `projects/<id>/project.yaml` (пример — `projects/spb`), сценарии через `extends: <шаблон>`, затем `contentbot check`. Код ядра менять не нужно.

## Тесты

```bash
.venv/bin/python -m pytest -q
```
