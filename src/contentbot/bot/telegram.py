"""Telegram transport (aiogram 3). Imported only by `contentbot bot`, so the
rest of the system works without aiogram and without a bot token."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..settings import Settings
from .app import build_pipeline, owner_ids_from_env
from .controller import BotController
from .ui import Keyboard

log = logging.getLogger(__name__)

# Bot API getFile limit on the public cloud API
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ALBUM_WAIT_SECONDS = 1.2


def _markup(kb: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not kb:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in kb if row]
    )


class TelegramUI:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_text(self, chat_id, text, kb=None, html=False):
        m = await self.bot.send_message(chat_id, text, reply_markup=_markup(kb), parse_mode="HTML" if html else None)
        return m.message_id

    async def edit_text(self, chat_id, message_id, text, kb=None, html=False):
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                reply_markup=_markup(kb), parse_mode="HTML" if html else None,
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                log.warning("edit failed: %s", e)

    async def send_photo(self, chat_id, path: Path, caption=None):
        return (await self.bot.send_photo(chat_id, FSInputFile(path), caption=caption)).message_id

    async def send_video(self, chat_id, path: Path, caption=None):
        return (await self.bot.send_video(chat_id, FSInputFile(path), caption=caption, supports_streaming=True)).message_id

    async def send_voice(self, chat_id, path: Path, caption=None):
        return (await self.bot.send_voice(chat_id, FSInputFile(path), caption=caption)).message_id

    async def send_audio(self, chat_id, path: Path, caption=None):
        return (await self.bot.send_audio(chat_id, FSInputFile(path), caption=caption)).message_id

    async def answer_callback(self, callback_id, text=None):
        try:
            await self.bot.answer_callback_query(callback_id, text=text)
        except TelegramBadRequest:
            pass  # too old to answer — harmless


def _media_of(message: Message):
    """-> (downloadable, suffix, size) or None."""
    if message.photo:
        p = message.photo[-1]
        return p, ".jpg", p.file_size or 0
    if message.video:
        return message.video, ".mp4", message.video.file_size or 0
    if message.document and (message.document.mime_type or "").split("/")[0] in ("image", "video"):
        name = message.document.file_name or "file"
        suffix = Path(name).suffix or (".mp4" if message.document.mime_type.startswith("video") else ".jpg")
        return message.document, suffix, message.document.file_size or 0
    return None


def build_dispatcher(bot: Bot, controller: BotController, inbox: Path) -> Dispatcher:
    dp = Dispatcher()
    albums: dict[str, list[Message]] = {}

    async def download(messages: list[Message]) -> list[Path] | str:
        folder = inbox / secrets.token_hex(6)
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, m in enumerate(messages):
            media = _media_of(m)
            if media is None:
                continue
            file, suffix, size = media
            if size > MAX_DOWNLOAD_BYTES:
                return "Файл больше 20 МБ — Telegram не даёт боту скачать его. Сожмите видео или пришлите короче."
            dst = folder / f"{i}{suffix}"
            await bot.download(file, destination=dst)
            paths.append(dst)
        return paths

    async def process(messages: list[Message]) -> None:
        first = messages[0]
        caption = next((m.caption for m in messages if m.caption), "") or ""
        result = await download(messages)
        if isinstance(result, str):
            await first.answer(result)
            return
        if not result:
            await first.answer("Пришлите фото или видео.")
            return
        await controller.handle_media(first.chat.id, first.from_user.id, result, caption)

    async def flush_album(group_id: str) -> None:
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        messages = albums.pop(group_id, [])
        if messages:
            await process(sorted(messages, key=lambda m: m.message_id))

    @dp.message(F.photo | F.video | F.document)
    async def on_media(message: Message) -> None:
        if not controller.allowed(message.from_user.id):
            await controller.handle_media(message.chat.id, message.from_user.id, [], "")
            return
        if message.media_group_id:
            group = albums.setdefault(message.media_group_id, [])
            group.append(message)
            if len(group) == 1:
                asyncio.create_task(flush_album(message.media_group_id))
            return
        await process([message])

    @dp.message(F.text.startswith("/"))
    async def on_command(message: Message) -> None:
        cmd, _, args = message.text.partition(" ")
        await controller.handle_command(message.chat.id, message.from_user.id, cmd, args)

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        await controller.handle_text(message.chat.id, message.from_user.id, message.text, message.html_text)

    @dp.callback_query()
    async def on_callback(cq: CallbackQuery) -> None:
        if cq.message is None or cq.data is None:
            await cq.answer()
            return
        await controller.handle_callback(cq.message.chat.id, cq.from_user.id, cq.id, cq.data, cq.message.message_id)

    return dp


async def run_bot(settings: Settings, mock_fixtures: dict | None = None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    owners = owner_ids_from_env()
    pipeline = build_pipeline(settings, mock_fixtures)
    bot = Bot(token)
    controller = BotController(pipeline, TelegramUI(bot), owners, settings.data_dir)
    dp = build_dispatcher(bot, controller, settings.data_dir / "inbox")
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Как пользоваться"),
            BotCommand(command="projects", description="Проекты и сценарии"),
            BotCommand(command="drafts", description="Черновики и ожидающие посты"),
            BotCommand(command="cancel", description="Отменить ввод текста"),
        ]
    )
    log.info("bot started: mode=%s llm=%s tts=%s owners=%s", settings.mode, settings.llm_provider, settings.tts_provider, sorted(owners))
    await dp.start_polling(bot)
