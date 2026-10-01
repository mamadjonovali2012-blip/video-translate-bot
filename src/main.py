# -*- coding: utf-8 -*-
"""Точка входа бота: сборка приложения и запуск (polling или webhook).

Перед запуском установите зависимости и задайте переменные окружения
(см. .env.example / README). Минимум: VT_BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from telegram import Update, constants
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler,
    CommandHandler, MessageHandler, filters, PreCheckoutQueryHandler,
)

from bot.handlers import misc, video
from core.config import load_bot, load_subscription, load_translate
from modules.subscription.service import payment_success, pre_checkout
from storage.state import StateStore

WORKDIR = Path(__file__).resolve().parents[2] / "work"


def build_app() -> Application:
    bot_cfg = load_bot()
    sub_cfg = load_subscription()
    tr_cfg = load_translate()

    if not bot_cfg.token:
        raise SystemExit("VT_BOT_TOKEN не задан. См. .env.example.")

    store = StateStore(WORKDIR / "store.json")

    builder = ApplicationBuilder().token(bot_cfg.token)
    app = builder.build()

    app.bot_data["cfg"] = sub_cfg
    app.bot_data["tr_cfg"] = tr_cfg
    app.bot_data["store"] = store

    # --- handlers ---
    app.add_handler(CommandHandler("start", misc.cmd_start))
    app.add_handler(CommandHandler("buy", misc.cmd_buy))
    app.add_handler(CommandHandler("help", misc.cmd_help))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO, video.handle_video))
    app.add_handler(CallbackQueryHandler(video.on_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(misc.callback))

    # --- payments ---
    if sub_cfg.payments_enabled:
        app.add_handler(PreCheckoutQueryHandler(pre_checkout))
        app.add_handler(MessageHandler(
            filters.SuccessfulPayment, payment_success))

    return app


def main() -> None:
    bot_cfg = load_bot()
    app = build_app()

    if bot_cfg.webhook_secret:
        # Webhook-режим: задайте VT_WEBHOOK_URL вместе с VT_WEBHOOK_SECRET
        url = os.getenv("VT_WEBHOOK_URL", "")
        _run_webhook(app, url, bot_cfg.webhook_secret)
    else:
        print("Запуск в режиме long-polling. Для остановки — Ctrl+C")
        async def run() -> None:
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass
            await stop.wait()
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
        finally:
            asyncio.run(_shutdown(app))


def _run_webhook(app: Application, url: str, secret: str) -> None:
    import uvicorn  # noqa: PLC0415  (optional dep, см. requirements)
    from fastapi import FastAPI, Request  # noqa: PLC0415
    from telegram.ext import Application  # noqa: PLC0415

    asgi_app = FastAPI()

    @asgi_app.post("/webhook")
    async def webhook(request: Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token != secret:
            return {"ok": False}
        data = await request.json()
        await app.process_update(Update.de_json(data, app.bot))
        return {"ok": True}

    @asgi_app.on_event("startup")
    async def startup():
        await app.initialize()
        await app.start()
        await app.updater.set_webhook(url, secret_token=secret)

    @asgi_app.on_event("shutdown")
    async def shutdown():
        await app.stop()

    uvicorn.run(asgi_app, host="0.0.0.0", port=int(os.getenv("VT_PORT", "8443")))


async def _shutdown(app: Application) -> None:
    try:
        await app.stop()
        await app.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    main()