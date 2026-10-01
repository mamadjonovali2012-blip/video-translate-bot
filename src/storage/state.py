"""Простое JSON-хранилище с атомарной записью (совместимо с потоком задач)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class JsonStore:
    """Потокобезопасное JSON-хранилище с блокировкой и атомарной записью."""

    def __init__(self, path: Path, default: dict | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = dict(default or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._save_now()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self._data = loaded
        except (json.JSONDecodeError, OSError):
            # повреждённый/пустой файл: начинаем с чистого
            self._data = {}
            self._save_now()

    def _save_now(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _save(self) -> None:
        with self._lock:
            self._save_now()

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def update(self, key: str, **fields) -> None:
        with self._lock:
            node = self._data.setdefault(key, {})
            if isinstance(node, dict):
                node.update(fields)
            else:
                node = fields
                self._data[key] = node
            self._save()

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())


class StateStore:
    """Бизнес-состояние: подписки и задачи по users."""

    def __init__(self, path: Path):
        self._db = JsonStore(path, {
            "users": {},
            "tasks": {},
        })

    # ---------- users ----------
    def is_whitelisted(self, user_id: int, whitelist: list[int]) -> bool:
        return user_id in whitelist

    def is_subscribed(self, user_id: int, now: float | None = None) -> bool:
        """По whitelist или оплаченной (активной) подписке."""
        now = float(now) if now else time.time()
        u = self._db.get("users", {}).get(str(user_id), {})
        expires = u.get("expires_at")
        if isinstance(expires, (int, float)) and expires > now:
            return True
        # whitelist-пользователи не хранятся здесь: решается в subscription.py
        return False

    def set_subscription(self, user_id: int, expires_at: float) -> None:
        self._db.set("users", {**self._db.get("users", {}),
                               str(user_id): {
                                   "expires_at": expires_at,
                                   "activated_at": time.time(),
                               }})

    def user(self, user_id: int) -> dict:
        return self._db.get("users", {}).get(str(user_id), {})

    # ---------- tasks (метаданные выполнения) ----------
    def new_task(self, task_id: str, **meta) -> None:
        self._db.update("tasks", **{task_id: {
            **meta,
            "created_at": time.time(),
            "status": "queued",
        }})

    def task(self, task_id: str) -> dict:
        return self._db.get("tasks", {}).get(task_id, {})

    def set_task(self, task_id: str, **fields) -> None:
        self._db.update("tasks", **{task_id: {
            **self.task(task_id),
            **fields,
        }})