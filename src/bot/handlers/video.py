"""Приём видео: проверка подписки, выбор языка, перевод."""

from __future__ import annotations

import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from modules.subscription.service import cfg_of, store, is_allowed
from modules.translate.pipeline import detect_capabilities, translate_video

WORKDIR = Path(os.getenv("VT_WORKDIR", "./work"))


def _paid_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить подписку",
                              callback_data="buy-subscription")],
    ])


LANG_BUTTONS = [
    ("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English"),
    ("de", "🇩🇪 Deutsch"), ("fr", "🇫🇷 Français"),
    ("es", "🇪🇸 Español"), ("it", "🇮🇹 Italiano"),
    ("pt", "🇵🇹 Português"), ("zh", "🇨🇳 中文"),
    ("ja", "🇯🇵 日本語"), ("ko", "🇰🇷 한국어"),
    ("hi", "🇮🇳 हिन्दी"), ("ar", "🇸🇦 العربية"),
    ("tr", "🇹🇷 Türkçe"), ("uk", "🇺🇦 Українська"),
    ("pl", "🇵🇱 Polski"),
]


def _lang_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(LANG_BUTTONS), 2):
        pair = LANG_BUTTONS[i:i + 2]
        rows.append([InlineKeyboardButton(txt, callback_data=f"lang:{code}")
                     for code, txt in pair])
    return InlineKeyboardMarkup(rows)


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cfg = cfg_of(ctx)
    st = store(ctx)
    if not is_allowed(cfg, st, user_id):
        await update.effective_chat.send_message(
            f"🔒 Для перевода нужна подписка ({cfg.price_rub} ₽/мес).\n"
            "Оформить: /buy",
            reply_markup=_paid_keyboard(),
        )
        return

    caps = detect_capabilities()
    if not caps.full:
        await update.effective_chat.send_message(
            "⚠️ На сервере не установлены компоненты перевода ("
            + ", ".join(caps.missing())
            + "). Попросите администратора настроить проект (см. README).")
        return

    media = update.message.video or update.message.document
    if not media:
        await update.effective_chat.send_message("Пришлите видеофайлом.")
        return

    chat_dir = WORKDIR / str(update.effective_chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    src = chat_dir / "input.mp4"
    tg_file = await media.get_file()
    await tg_file.download_to_drive(src)

    ctx.user_data["pending_video"] = str(src)
    await update.effective_chat.send_message(
        "Отлично! На какой язык перевести? 👇",
        reply_markup=_lang_keyboard(),
    )


async def on_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("lang:"):
        return
    await q.answer()
    lang = data.split(":", 1)[1]
    src = ctx.user_data.get("pending_video")
    if not src or not Path(src).exists():
        await q.edit_message_text("Видео не найдено. Пришлите его снова.")
        return

    await q.edit_message_text(
        f"⏳ Перевожу на {lang}… это может занять несколько минут.")

    try:
        out = await translate_video(Path(src), lang, cfg_of(ctx), WORKDIR)
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка перевода: {e}")
        _rm(src)
        return

    try:
        with open(out, "rb") as f:
            await ctx.bot.send_video(
                chat_id=update.effective_chat.id, video=f,
                caption=f"✅ Перевод готов ({lang})")
    except Exception:
        with open(out, "rb") as f:
            await ctx.bot.send_document(
                chat_id=update.effective_chat.id, document=f,
                caption=f"✅ Перевод готов ({lang})")
    _rm(src)


def _rm(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except TypeError:
        Path(path).unlink() if Path(path).exists() else None