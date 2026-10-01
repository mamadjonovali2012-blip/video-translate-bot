"""Конфигурация бота: токен, цены, whitelist, настройки перевода.

Всё читается из окружения (переменные вида VT_*) с разумными значениями
по умолчанию. Секреты в git не хранятся — см. .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да", "y"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_list(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return list(default)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


@dataclass(frozen=True)
class SubscriptionConfig:
    """Параметры подписки 200 руб/мес."""

    price_rub: int = 200
    currency: str = "RUB"
    # Аккаунты, освобождённые от оплаты (paid programme). Числа вне кавычек.
    whitelist: list[int] = field(default_factory=lambda: [835648334, 8542930176])
    provider_token: str = ""
    # Платежи реально принимаются, только когда задан provider_token.
    payments_enabled: bool = False
    label_prefix: str = "vt"  # префикс метки платежа
    # Смягчение на время боевой эксплуатации без provider_token:
    # при включённом flag шлюз «настоящей» оплаты отключается (см. README).
    allow_unverified: bool = False
    # Период оплаты в днях; при 0 подписка действует бессрочно (whitelist).
    duration_days: int = 30
    # Карта для переводов в демо-режиме (когда провайдер не настроен).
    pay_card: str = "2202 2061 6956 5376"


def load_subscription() -> SubscriptionConfig:
    provider = os.getenv("VT_PROVIDER_TOKEN", "").strip()
    return SubscriptionConfig(
        price_rub=_int("VT_PRICE_RUB", 200),
        currency=os.getenv("VT_CURRENCY", "RUB"),
        whitelist=_int_list("VT_WHITELIST", [835648334, 8542930176]),
        provider_token=provider,
        payments_enabled=bool(provider) and not _bool("VT_DISABLE_PAYMENTS", False),
        label_prefix=os.getenv("VT_LABEL_PREFIX", "vt"),
        allow_unverified=_bool("VT_ALLOW_UNVERIFIED", False),
        duration_days=_int("VT_DURATION_DAYS", 30),
        pay_card=os.getenv("VT_PAY_CARD", "2202 2061 6956 5376"),
    )


@dataclass(frozen=True)
class TranslateConfig:
    """Параметры пайплайна STT -> перевод -> TTS -> ffmpeg."""

    model: str = "base"  # скорость/качество компромисс, см. README
    device: str = "auto"
    compute_type: str = "auto"
    tts_voice: str = "ru-RU-DmitryNeural"
    ffmpeg_bin: str = "ffmpeg"
    duration_ms: int = 3000_000  # 50 мин
    max_size_mb: int = 700
    # Лимиты TTS-хвоста: если нарезка сказанного длиннее кадра/паузы, конец строки
    # обрезается, чтобы голос не залезал на следующую фразу. Переключаемо.
    tts_clip_to_breath: bool = True
    min_duration_sec: float = 0.7  # короче не озвучиваем (шум/клики)
    out_codec: str = "aac"
    out_bitrate: str = "128k"
    workers: int = 1  # конкурентных задач перевода


def load_translate() -> TranslateConfig:
    device = os.getenv("VT_DEVICE", "auto").strip().lower()
    compute = os.getenv("VT_COMPUTE", "auto").strip().lower()
    resample = _bool("VT_RESAMPLE", True)  # зарезервировано под аудиомикс
    cfg = TranslateConfig(
        model=os.getenv("VT_WHISPER_MODEL", "base"),
        device=device,
        compute_type=compute,
        tts_voice=os.getenv("VT_TTS_VOICE", "ru-RU-DmitryNeural"),
        ffmpeg_bin=os.getenv("VT_FFMPEG", "ffmpeg"),
        duration_ms=_int("VT_MAX_DURATION_MS", 3000_000),
        max_size_mb=_int("VT_MAX_SIZE_MB", 700),
        tts_clip_to_breath=_bool("VT_TTS_CLIP_BREATH", True),
        min_duration_sec=float(os.getenv("VT_MIN_DURATION_SEC", "0.7")),
        out_codec=os.getenv("VT_OUT_CODEC", "aac"),
        out_bitrate=os.getenv("VT_OUT_BITRATE", "128k"),
        workers=_int("VT_WORKERS", 1),
    )
    _ = resample  # оставлено под будущий микс с оригиналом
    return cfg


@dataclass(frozen=True)
class BotConfig:
    token: str
    long_polling: bool = True
    webhook_secret: str = ""


def load_bot() -> BotConfig:
    return BotConfig(
        token=os.getenv("VT_BOT_TOKEN", "").strip(),
        long_polling=_bool("VT_POLLING", True),
        webhook_secret=os.getenv("VT_WEBHOOK_SECRET", "").strip(),
    )