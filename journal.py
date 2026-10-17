"""Durable manifests for replay jobs that survive a process restart."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


VERSION = 1


def _key(payload: dict[str, Any]) -> str:
    target = payload["target"]
    raw = "|".join(
        str(payload.get(name, ""))
        for name in ("digest", "signature", "replay")
    ) + "|" + "|".join(
        str(target.get(name, ""))
        for name in ("bot_id", "group_id", "user_id")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save(root: Path, job: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for target in [job, *job.subscribers]:
        payload = {
            "version": VERSION,
            "digest": job.digest,
            "signature": job.signature,
            "replay": str(job.replay),
            "target": {
                "bot_id": str(getattr(target.bot, "self_id", "")),
                "group_id": target.group_id,
                "user_id": target.user_id,
                "filename": target.filename,
            },
        }
        path = root / f"{_key(payload)}.json"
        descriptor, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def remove(root: Path, job: Any) -> None:
    for target in [job, *job.subscribers]:
        payload = {
            "digest": job.digest,
            "signature": job.signature,
            "replay": str(job.replay),
            "target": {
                "bot_id": str(getattr(target.bot, "self_id", "")),
                "group_id": target.group_id,
                "user_id": target.user_id,
            },
        }
        (root / f"{_key(payload)}.json").unlink(missing_ok=True)


def load(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not root.is_dir():
        return []
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != VERSION:
                raise ValueError("unsupported journal version")
            if not isinstance(payload.get("digest"), str):
                raise ValueError("missing digest")
            if not isinstance(payload.get("signature"), str):
                raise ValueError("missing signature")
            if not isinstance(payload.get("replay"), str):
                raise ValueError("missing replay path")
            target = payload.get("target")
            if not isinstance(target, dict) or not target.get("bot_id"):
                raise ValueError("missing target")
            result.append((path, payload))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
    return result
