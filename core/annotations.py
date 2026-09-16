"""
annotations.py
User-added notes/tags on individual run results, keyed by result_id.

Kept as a small sidecar JSON file rather than mutating the results JSONL —
those files are append-only and written by the scheduler process, so
editing a line in place risks corruption under concurrent writes. This
store is only ever touched by the web process, in response to a user
action, so a single JSON file with an in-process lock is plenty.
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import List, Optional


class AnnotationStore:

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)

    def get(self, result_id: str) -> dict:
        return self._load().get(result_id) or {"notes": "", "tags": [], "updated_at": None}

    def get_all(self) -> dict:
        return self._load()

    def set(self, result_id: str, notes: str = "", tags: Optional[List[str]] = None) -> dict:
        notes = (notes or "").strip()
        tags  = sorted({t.strip() for t in (tags or []) if t.strip()})

        with self._lock:
            data = self._load()
            if not notes and not tags:
                data.pop(result_id, None)  # clearing both removes the entry
            else:
                data[result_id] = {
                    "notes":      notes,
                    "tags":       tags,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            self._save(data)
            return data.get(result_id, {"notes": "", "tags": [], "updated_at": None})

    def all_tags(self) -> List[str]:
        tags = set()
        for entry in self._load().values():
            tags.update(entry.get("tags", []))
        return sorted(tags)
