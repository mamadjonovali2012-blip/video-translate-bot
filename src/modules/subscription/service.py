"""Подписка 200 ₽/мес: проверка доступа, invoice, оплата, демо-режим.

Правило доступа:
  — paid-programme (whitelist) работают без оплаты (бессрочно);
  — остальные обязаны оплатить подписку 200 ₽/мес.

Приём платежей:
  — если задан VT_PROVIDER_TOKEN (BotFather -> Payments), выставляется
    реальный счёт (invoice) на 200 ₽ через Telegram.
  — если токена нет, включается демо-режим: кнопка «Я оплатил(а)»
    выдаёт 30 дней доступа. Позволяет протестировать бота без денег.
"""

from __future__ import annotations

import time

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update, error,
)
from telegram.ext import ContextTypes

from core.config import SubscriptionConfig
from storage.state import StateStore


def store(ctx: ContextTypes.DEFAULT_TYPE) -> StateStore:
    return ctx.bot_data["store"]


def cfg_of(ctx: ContextTypes.DEFAULT_TYPE) -> SubscriptionConfig:
    return ctx.bot_data["cfg"]


def is_whitelisted(cfg: SubscriptionConfig, user_id: int) -> bool:
    return user_id in cfg.whitelist


def has_active(cfg: SubscriptionConfig, st: StateStore, user_id: int) -> bool:
    if is_whitelisted(cfg, user_id):
        return True
    return st.is_subscribed(user_id)


def is_allowed(cfg: SubscriptionConfig, st: StateStore, user_id: int) -> bool:
    """Право пользоваться переводом."""
    return has_active(cfg, st, user_id)


# --------------------------------------------------------------------------
# Клавиатуры
# --------------------------------------------------------------------------

def _door_keyboard(cfg: SubscriptionConfig, st: StateStore,
                   user_id: int) -> InlineKeyboardMarkup:
    if has_active(cfg, st, user_id):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Отправить видео", callback_data="noop")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Оплатить {cfg.price_rub} ₽/мес",
                              callback_data="buy-subscription")],
    ])


# --------------------------------------------------------------------------
# Buy-логика (используется и из /buy, и из callback)
# --------------------------------------------------------------------------

async def _demo_purchase(ctx: ContextTypes.DEFAULT_TYPE,
                         chat_id: int, cfg: SubscriptionConfig) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Я оплатил(а) — выдать доступ",
                              callback_data="confirm-unverified")],
    ])
    await ctx.bot.send_message(
        chat_id,
        "Платёжный провайдер не настроен (VT_PROVIDER_TOKEN), поэтому "
        "приём онлайн-платежей выключен.\n\n"
        f"Стоимость подписки: **{cfg.price_rub} ₽/мес**.\n"
        "Оплатите переводом, затем нажмите кнопку — доступ на 30 дней.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def _real_purchase(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                         user_id: int, cfg: SubscriptionConfig) -> None:
    label = f"{cfg.label_prefix}-{user_id}-{int(time.time())}"
    prices = [LabeledPrice(f"Подписка {cfg.price_rub} ₽", cfg.price_rub * 100)]
    try:
        await ctx.bot.send_invoice(
            chat_id=chat_id,
            title="Перевод видео — 1 месяц",
            description=f"Подписка {cfg.price_rub} ₽/мес. Доступ сразу.",
            payload=label,
            provider_token=cfg.provider_token,
            currency=cfg.currency,
            prices=prices,
            need_name=False,
            need_email=False,
        )
    except error.TelegramError as e:
        await ctx.bot.send_message(
            chat_id, f"Не удалось выставить счёт: {e}")


async def buy_flow(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                   user_id: int) -> None:
    cfg = cfg_of(ctx)
    st = store(ctx)
    if is_whitelisted(cfg, user_id):
        await ctx.bot.send_message(
            chat_id, "✅ Вы в paid-programme — бессрочный доступ, "
                     "подписка не нужна.")
        return
    if has_active(cfg, st, user_id):
        await ctx.bot.send_message(
            chat_id, "✅ Подписка уже активна. Отправьте видео.")
        return
    if cfg.payments_enabled:
        await _real_purchase(ctx, chat_id, user_id, cfg)
    else:
        await _demo_purchase(ctx, chat_id, cfg)


# --------------------------------------------------------------------------
# Команды /start и /buy
# --------------------------------------------------------------------------

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


async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await buy_flow(ctx, update.effective_chat.id, update.effective_user.id)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "📋 Как пользоваться:\n"
        "1. Оформите подписку: /buy (200 ₽/мес, кроме paid-programme).\n"
        "2. Пришлите видеофайл.\n"
        "3. Выберите язык из списка.\n"
        "4. Через некоторое время получите переведённый ролик.\n\n"
        "Поддерживается широкий набор языков (whisper + edge-tts)."
    )


# --------------------------------------------------------------------------
# Callback-обработчик (покупка, подтверждение демо-оплаты)
# --------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    await q.answer()

    if data == "buy-subscription":
        await buy_flow(ctx, update.effective_chat.id, update.effective_user.id)
        return
    if data == "confirm-unverified":
        cfg = cfg_of(ctx)
        user_id = update.effective_user.id
        if is_whitelisted(cfg, user_id):
            await q.edit_message_text("Доступ уже есть (paid-programme).")
            return
        grant_days(store(ctx), user_id, cfg.duration_days)
        await q.edit_message_text(
            "✅ Доступ выдан на 30 дней. Отправьте видео для перевода.\n"
            "(Демо-режим: в бою оплата идёт через провайдера.)")
        return
    if data == "noop":
        return


def grant_days(st: StateStore, user_id: int, days: int) -> None:
    st.set_subscription(user_id, time.time() + days * 86400)


# --------------------------------------------------------------------------
# Обработка платежей Telegram
# --------------------------------------------------------------------------

async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    cfg = cfg_of(ctx)
    if not q.invoice_payload.startswith(f"{cfg.label_prefix}-"):
        await q.answer(ok=False, error_message="Некорректный счёт.")
        return
    await q.answer(ok=True)


async def payment_success(update: Update,
                          ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = cfg_of(ctx)
    grant_days(store(ctx), update.effective_user.id, cfg.duration_days)
    await update.effective_chat.send_message(
        "✅ Оплата получена! Подписка активна на "
        f"{cfg.duration_days} дней.\nОтправьте видео — переведу его.")