"""Console chat simulator: the same bot logic, no Telegram, no token.

    python -m contentbot chat
    > media data/demo/ticket.png [подпись]
    > 1            (press button #1 of the last keyboard)
    > любой текст  (reply to the bot)
    > /drafts
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from ..settings import Settings
from .app import build_pipeline
from .controller import BotController
from .ui import Button, Keyboard

CHAT_ID = USER_ID = 1


class ConsoleUI:
    def __init__(self) -> None:
        self.buttons: dict[int, tuple[int, Button]] = {}  # number -> (message id, button)
        self.next_id = 1

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _print(self, title: str, text: str, kb: Keyboard | None, message_id: int) -> None:
        print(f"\n── {title} #{message_id} " + "─" * 30)
        print(text)
        if kb:
            self.buttons = {}
            n = 1
            for row in kb:
                cells = []
                for b in row:
                    self.buttons[n] = (message_id, b)
                    cells.append(f"[{n}] {b.text}")
                    n += 1
                print("   " + "   ".join(cells))

    async def send_text(self, chat_id, text, kb=None, html=False):
        mid = self._new_id()
        self._print("бот", text, kb, mid)
        return mid

    async def edit_text(self, chat_id, message_id, text, kb=None, html=False):
        self._print("бот (изменено)", text, kb, message_id)

    async def send_photo(self, chat_id, path, caption=None):
        mid = self._new_id()
        print(f"\n── фото #{mid}: {path} {caption or ''}")
        return mid

    async def send_video(self, chat_id, path, caption=None):
        mid = self._new_id()
        print(f"\n── видео #{mid}: {path} {caption or ''}")
        return mid

    async def send_voice(self, chat_id, path, caption=None):
        mid = self._new_id()
        print(f"\n── голосовое #{mid}: {path} {caption or ''}")
        return mid

    send_audio = send_voice

    async def answer_callback(self, callback_id, text=None):
        if text:
            print(f"   (всплывашка: {text})")


async def run_console(settings: Settings, mock_fixtures: dict | None = None, media: list[str] | None = None) -> None:
    ui = ConsoleUI()
    controller = BotController(build_pipeline(settings, mock_fixtures), ui, {USER_ID}, settings.data_dir)
    print(f"Симулятор чата · LLM: {settings.llm_provider} · TTS: {settings.tts_provider}. "
          "Команды: media <путь> [подпись] · <номер кнопки> · /help · выход: Ctrl+D")
    if media:
        await controller.handle_media(CHAT_ID, USER_ID, [Path(m) for m in media], "")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = (await loop.run_in_executor(None, input, "\n> ")).strip()
        except EOFError:
            print()
            return
        if not line:
            continue
        if line.isdigit() and int(line) in ui.buttons:
            message_id, button = ui.buttons[int(line)]
            await controller.handle_callback(CHAT_ID, USER_ID, "cb", button.data, message_id)
        elif line.startswith("media "):
            parts = shlex.split(line)[1:]
            paths = [p for p in parts if Path(p).exists()]
            caption = " ".join(p for p in parts if p not in paths)
            await controller.handle_media(CHAT_ID, USER_ID, [Path(p) for p in paths], caption)
        elif line.startswith("/"):
            cmd, _, args = line.partition(" ")
            await controller.handle_command(CHAT_ID, USER_ID, cmd, args)
        else:
            await controller.handle_text(CHAT_ID, USER_ID, line)
