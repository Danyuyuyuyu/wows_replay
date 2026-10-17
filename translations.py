"""为其他插件提供客户端语言资源；共用 replay 上游与 Git 下载锁。"""

import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time

from . import config, data_service, updater
from .tools import fetch_renderer_data as fetcher

_sync_lock = asyncio.Lock()
_data_service = data_service.DataService(updater)


def _read_state(path):
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict):
            return {}
        for key in ("checked_at", "retry_at"):
            value = row.get(key, 0)
            if type(value) not in {int, float} or not 0 <= value < float("inf"):
                return {}
        return row
    except (OSError, ValueError):
        return {}


def _save_state(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(row, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _local_source(language, cache, languages):
    choices = []
    for base, cached in ((config.data_dir(), False), (cache, True)):
        if not base.is_dir():
            continue
        for version in base.iterdir():
            if (
                not config.VERSION_DIR_RE.fullmatch(version.name)
                or not version.is_dir()
            ):
                continue
            if cached:
                ready = fetcher.translations_ready(version, languages)
            else:
                try:
                    for code in languages:
                        fetcher._read_translation_mo(
                            version / "translations" / code / "LC_MESSAGES/global.mo"
                        )
                    ready = True
                except (OSError, ValueError):
                    ready = False
            if ready:
                choices.append(
                    (
                        config.version_key(version.name),
                        cached,
                        version / "translations",
                        version.name,
                    )
                )
    if not choices:
        return None
    _, _, root, build = max(choices)
    return root, build


def _fetch_languages(src, cache, build, languages, proxy):
    fetcher.ensure_clone(src, proxy)
    return fetcher.materialize_translations(src, cache, build, languages)


async def sync_latest(language, *, force=False):
    """Return a verified local source; check upstream on the replay check interval.

    Failure/cooldown reports retain the previous local source. Language-only caches
    are outside renderer DATA_DIR, so a partial data set cannot masquerade as a
    complete renderer installation.
    """
    if not re.fullmatch(r"[a-z]{2}(?:_[a-z]{2})?", language):
        raise ValueError("客户端语言代码无效")
    languages = tuple(sorted({language, "en"}))
    cache = config.WORK_DIR / "translations" / language
    state_path = cache / "sync.json"
    async with _sync_lock:
        local = await _data_service.await_thread(_local_source, language, cache, languages)
        state = await _data_service.await_thread(_read_state, state_path)
        result = {
            "source": local,
            "status": "ready",
            "checked_at": state.get("checked_at", 0),
            "upstream_build": state.get("upstream_build"),
        }
        if (
            not config.is_enabled()
            or not config.update_check_enabled()
            or not config.auto_install_data()
        ):
            return dict(result, status="disabled")
        now = time.time()
        if not force and state.get("retry_at", 0) > now:
            return dict(
                result,
                status="cooldown",
                error=state.get("error", "ResourceUnavailable"),
            )
        interval = config.update_check_interval_hours() * 3600
        if (
            not force
            and now - state.get("checked_at", 0) < interval
            and local is not None
        ):
            return result
        try:
            versions = await updater.upstream_versions(use_cache=not force)
            valid = [
                name
                for name in versions or []
                if isinstance(name, str) and config.VERSION_DIR_RE.fullmatch(name)
            ]
            if not valid:
                raise RuntimeError("上游客户端版本列表暂不可用")
            latest = max(valid, key=config.version_key)
            if local is None or config.version_key(local[1]) < config.version_key(
                latest
            ):
                async with _data_service.source_lock():
                    await _data_service.run_git_operation(
                        _fetch_languages,
                        config.data_src_dir(),
                        cache,
                        latest,
                        languages,
                        _data_service.proxy(),
                    )
                local = await _data_service.await_thread(
                    _local_source, language, cache, languages
                )
                if local is None or config.version_key(local[1]) < config.version_key(
                    latest
                ):
                    raise RuntimeError("新版客户端语言资源未完整安装")
            state = {"checked_at": time.time(), "retry_at": 0, "upstream_build": latest}
            await _data_service.await_thread(_save_state, state_path, state)
            return {
                "source": local,
                "status": "ready",
                "checked_at": state["checked_at"],
                "upstream_build": latest,
            }
        except (Exception, SystemExit) as error:
            state.update(
                retry_at=time.time() + updater.INSTALL_FAILED_TTL,
                error=type(error).__name__,
            )
            await _data_service.await_thread(_save_state, state_path, state)
            return dict(result, status="unavailable", error=type(error).__name__)
