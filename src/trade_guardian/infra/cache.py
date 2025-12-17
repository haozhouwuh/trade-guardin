from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional


class JsonDailyCache:
    """
    Simple daily cache. Resets automatically each day by storing a date stamp.
    """
    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self._data = {"_date": self._today(), "items": {}}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {"_date": self._today(), "items": {}}

        if self._data.get("_date") != self._today():
            self._data = {"_date": self._today(), "items": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def get(self, key: str) -> Optional[dict]:
        return self._data.get("items", {}).get(key)

    def set(self, key: str, value: dict) -> None:
        self._data.setdefault("items", {})[key] = value
        self._save()
