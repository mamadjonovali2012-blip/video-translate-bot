# -*- coding: utf-8 -*-
"""Вспомогательные handlers: /start, /help (в /start), навигация."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from modules.subscription.service import (
    cfg_of, store, has_active, _door_keyboard, buy_flow, on_callback,
)

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await buy_flow(ctx, update.effective_chat.id, update.effective_user.id)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cfg = cfg_of(ctx)
    st = store(ctx)
    status = ("✅ Подписка активна" if has_active(cfg, st, user_id)
              else f"🔒 Нужна подписка ({cfg.price_rub} ₽/мес)")
    await update.effective_chat.send_message(
        "🎬 Привет! Я перевожу видео с любого языка на любой.\n"
        "Отправь видео — пришлю версию с озвучкой на нужном языке.\n\n"
        f"Статус: {status}\n\n"
        "Команды:\n"
        "/start — это меню\n"
        "/buy — подписка / оплата\n"
        "/help — справка",
        reply_markup=_door_keyboard(cfg, st, user_id),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = cfg_of(ctx)
    pay = ""
    if not cfg.payments_enabled:
        pay = f"\nОплата подписки — переводом на карту:\n`{cfg.pay_card}`\n(затем нажмите «Я оплатил(а)» в /buy)"
    await update.effective_chat.send_message(
        "📋 Как пользоваться:\n"
        f"1. Оформите подписку: /buy (200 ₽/мес, кроме paid-programme).{pay}\n"
        "2. Пришлите видеофайл.\n"
        "3. Выберите язык из списка.\n"
        "4. Через некоторое время получите переведённый ролик.\n\n"
        "Поддерживается широкий набор языков (whisper + edge-tts).",
        parse_mode="Markdown",
    )


async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await on_callback(update, ctx)