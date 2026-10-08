"""Stable media identities and bounded, atomic download publication."""

import asyncio
import hashlib
import os
from pathlib import Path
import re
import uuid

import errors


def platform_name(source):
    platform = str(source).split("_", 1)[0]
    if platform not in {"youtube", "netease", "bilibili"}:
        raise ValueError("Unsupported audio source")
    return platform


def cache_key(source, source_id, part=0):
    platform = platform_name(source)
    identity = str(source_id)
    suffix = f":p{int(part) + 1}" if platform == "bilibili" else ""
    return f"v2:{platform}:{identity}{suffix}"


def media_filename(source, source_id, extension, part=0):
    platform = platform_name(source)
    identity = str(source_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity):
        identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9]{1,12}", str(extension)):
        raise ValueError("Invalid media extension")
    suffix = f"-p{int(part) + 1}" if platform == "bilibili" else ""
    return f"{platform}-{identity}{suffix}.{extension}"


def publish_download(staged_path, download_path, filename):
    """Never overwrite an orphan that an older persisted queue may still use."""
    destination = Path(download_path) / filename
    if destination.exists():
        destination = destination.with_name(
            f"{destination.stem}-{uuid.uuid4().hex[:12]}{destination.suffix}"
        )
    os.replace(staged_path, destination)
    return str(destination)


class DownloadBudget:
    """Keep an unknown-size download bounded as its actual byte count grows."""

    def __init__(self, limit, reserve, loop=None):
        self.limit = int(limit)
        self._reserve = reserve
        self._loop = loop
        self._approved = 0

    async def reserve(self, size):
        size = int(size)
        if size > self.limit:
            raise errors.StorageFull("音频文件库")
        if size > self._approved:
            await self._reserve(size)
            self._approved = size

    def progress(self, progress):
        size = int(progress.get("downloaded_bytes") or 0)
        if size > self.limit:
            raise errors.StorageFull("音频文件库")
        # yt-dlp reports after writing a buffer; its caller fixes that buffer at
        # 64 KiB. A rejected block is removed with the private staging directory.
        wanted = size
        if wanted > self._approved:
            if self._loop is None:
                raise RuntimeError("Download budget needs its owning event loop")
            asyncio.run_coroutine_threadsafe(self.reserve(wanted), self._loop).result()


async def finish_thread_on_cancel(function):
    """A cancelled caller must not delete a directory while yt-dlp writes it."""
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
