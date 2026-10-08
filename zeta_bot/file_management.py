from typing import *
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import time

import errors
import utils
from zeta_bot import decorator, console, audio, bilibili, ytdlp, media_cache

console = console.Console()


class AudioFileLibrary:
    def __init__(self, root: str, path: str, name: str = "音频文件管理模块", storage_capacity: int = 2097152):
        self._initialized = False
        self._root = root
        self._path = path
        self._using = {}
        self._storage_capacity = max(0, int(storage_capacity))
        self._name = name
        self._used_storage_size = 0
        self._dl_list = utils.DoubleLinkedListDict()
        self._download_lock = asyncio.Lock()
        self._inflight = {}
        self._active_budget = None
        self._saved_queue_guards = {}

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.realpath(path))

    def _inside_root(self, path):
        try:
            return os.path.commonpath([self._path_key(self._root), self._path_key(path)]) == self._path_key(self._root)
        except (ValueError, TypeError):
            return False

    async def initialize(self) -> None:
        # on_ready can run more than once; keep live locks and download tasks.
        if self._initialized:
            return
        os.makedirs(self._root, exist_ok=True)
        try:
            await self._load()
        except FileNotFoundError:
            await self._reset_library()
        except (errors.JSONFileError, KeyError, TypeError, ValueError):
            # Keep both the damaged index and every existing media file.
            if os.path.isfile(self._path):
                shutil.copy2(self._path, f"{self._path}.corrupt-{time.time_ns()}")
            await self._reset_library()
        self._load_saved_queue_guards()
        self._recount_storage()
        self._initialized = True
        await self.save()
        # Do not evict at startup: persisted queues must retain their files.
        # A later download can reclaim unlocked files even if currently over cap.
        await console.rp(f"{self._name}初始化完成", f"[{self._name}]")

    def _load_saved_queue_guards(self):
        guild_root = Path(self._path).parent / "guilds"
        if not guild_root.exists():
            return
        for guild_dir in guild_root.iterdir():
            if not guild_dir.is_dir() or not guild_dir.name.isdecimal():
                continue
            path = guild_dir / f"{guild_dir.name}.json"
            try:
                data = utils.json_load(str(path))
                paths = {
                    self._path_key(item["path"])
                    for item in data.get("playlist", {}).get("playlist", [])
                    if isinstance(item, dict) and item.get("path") and self._inside_root(item["path"])
                }
            except (OSError, errors.JSONFileError, TypeError, AttributeError):
                continue
            if paths:
                self._saved_queue_guards[guild_dir.name] = paths

    def print_info(self):
        print(f"{self._name}: {len(self)} 个缓存记录, {self._used_storage_size}/{self._storage_capacity} bytes")

    def __len__(self):
        return len(self._dl_list)

    def __contains__(self, item):
        return item in self._dl_list or any(node["item"].get_source_id() == item for node in self._dl_list.encode())

    async def save(self) -> None:
        utils.json_save(self._path, self)

    async def _load(self) -> None:
        loaded = utils.json_load(self._path)
        if not isinstance(loaded, list):
            raise ValueError("Invalid audio cache index")
        valid = []
        for node in loaded:
            try:
                item = audio.audio_decoder(node["item"])
                if not self._inside_root(item.get_path()) or not os.path.isfile(item.get_path()):
                    continue
                valid.append((node["key"], item))
            except (KeyError, TypeError, ValueError):
                continue
        rebuilt = utils.DoubleLinkedListDict()
        for old_key, item in valid:
            key = str(old_key)
            if not key.startswith("v2:") and not key.startswith("legacy:"):
                # Even a currently unique old title path may have collided in
                # the past. Preserve old queue paths, but never certify their
                # contents as an ID-based cache hit for a new request.
                key = self._legacy_key(item, old_key)
            rebuilt.append(item, key, force=True)
        self._dl_list = rebuilt

    def _legacy_key(self, item, old_key=""):
        identity = f"{old_key}\0{item.get_source()}\0{item.get_source_id()}\0{self._path_key(item.get_path())}"
        return "legacy:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def _reset_library(self) -> None:
        # Rebuild metadata without deleting downloads referenced by old queues.
        self._dl_list = utils.DoubleLinkedListDict()
        self._recount_storage()
        await self.save()

    def _recount_storage(self):
        total = 0
        active = getattr(self._active_budget, "staging_path", None)
        active = self._path_key(active) if active else None
        index_path = self._path_key(self._path)
        for directory, dirs, files in os.walk(self._root, followlinks=False):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(directory, name)) and self._path_key(os.path.join(directory, name)) != active]
            for name in files:
                path = os.path.join(directory, name)
                if os.path.islink(path) or self._path_key(path) == index_path:
                    continue
                try:
                    total += os.path.getsize(path)
                except FileNotFoundError:
                    pass
        self._used_storage_size = total

    def get_name(self):
        return self._name

    def get_storage_capacity(self):
        return self._storage_capacity

    def get_used_storage_size(self):
        return self._used_storage_size

    def storage_full(self):
        return self._used_storage_size >= self._storage_capacity

    def get_available_storage_size(self):
        return max(0, self._storage_capacity - self._used_storage_size)

    def get_used_storage_percentage(self, round_num=2):
        return round(self._used_storage_size * 100 / self._storage_capacity, round_num) if self._storage_capacity else 100.0

    def get_available_storage_percentage(self, round_num=2):
        return round(self.get_available_storage_size() * 100 / self._storage_capacity, round_num) if self._storage_capacity else 0.0

    def storage_will_full(self, new_file_size):
        return self._used_storage_size + new_file_size > self._storage_capacity

    @decorator.check_initialized
    def using(self, target):
        path = target if isinstance(target, str) else target.get_path()
        path = self._path_key(path)
        return path in self._using or any(path in paths for paths in self._saved_queue_guards.values())

    @decorator.check_initialized
    def now_playing(self, target):
        path = target if isinstance(target, str) else target.get_path()
        return any("NOW_PLAYING" in key for key in self._using.get(self._path_key(path), {}))

    @decorator.check_initialized
    def lock_audio(self, key, target_audio):
        key = str(key)
        # GuildPlaylist restores its entries synchronously, without await. Its
        # real reference counts replace the saved startup guard at first append.
        self._saved_queue_guards.pop(key, None)
        path = self._path_key(target_audio.get_path())
        holders = self._using.setdefault(path, {})
        holders[key] = holders.get(key, 0) + 1

    @decorator.check_initialized
    def unlock_audio(self, key, target_audio):
        path = self._path_key(target_audio.get_path())
        holders = self._using.get(path, {})
        key = str(key)
        if key in holders:
            holders[key] -= 1
            if holders[key] <= 0:
                holders.pop(key)
        if not holders:
            self._using.pop(path, None)

    def release_pending_audio(self, target_audio):
        if target_audio is not None:
            self.unlock_audio("PENDING_RETURN", target_audio)

    @decorator.check_initialized
    async def _append_audio(self, new_audio, repeat_file=False, cache_key=None):
        key = cache_key
        if key is None:
            for node in self._dl_list.encode():
                if node["item"] is new_audio:
                    key = node["key"]
                    break
        if key is None:
            key = self._legacy_key(new_audio)
        self._dl_list.append(new_audio, key, force=True)
        self._recount_storage()
        await self.save()

    @decorator.check_initialized
    async def _remove_audio(self, key):
        target = self._dl_list.key_get(key)
        if self.using(target):
            raise PermissionError("Audio is still referenced")
        path = target.get_path()
        if not self._inside_root(path):
            raise PermissionError("Audio is outside the cache directory")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        # Legacy same-title aliases must be removed together, counting the
        # physical file only once. No nonexistent entry can stall eviction.
        for node in list(self._dl_list.encode()):
            if self._path_key(node["item"].get_path()) == self._path_key(path):
                self._dl_list.key_remove(node["key"])
        self._recount_storage()
        await self.save()

    @decorator.check_initialized
    async def _delete_least_used_file(self, depth=0):
        for node in list(self._dl_list.encode())[depth:]:
            if self.using(node["item"]):
                continue
            try:
                await self._remove_audio(node["key"])
            except (PermissionError, KeyError):
                continue
            return True
        return False

    @decorator.check_initialized
    async def _download_space_check(self, new_file_size):
        size = max(0, int(new_file_size or 0))
        if size > self._storage_capacity:
            raise errors.StorageFull(self._name)
        self._recount_storage()
        while self.storage_will_full(size):
            if not await self._delete_least_used_file():
                raise errors.StorageFull(self._name)

    @decorator.check_initialized
    async def _download_file_exist_check(self, target_file_id, target_file_size=None):
        if target_file_id not in self._dl_list:
            return None
        existing = self._dl_list.key_get(target_file_id)
        try:
            size = os.path.getsize(existing.get_path())
        except FileNotFoundError:
            self._dl_list.key_remove(target_file_id)
            self._recount_storage()
            await self.save()
            return None
        if size <= 0:
            if not self.using(existing):
                await self._remove_audio(target_file_id)
            return None
        # Metadata sizes may be estimates or a different representation. Only
        # successfully published files enter the new index; do not delete an
        # in-use file because a later metadata request reports a larger size.
        await self._append_audio(existing, repeat_file=True, cache_key=target_file_id)
        return existing

    async def _shared_download(self, key, producer):
        entry = self._inflight.get(key)
        if entry is None:
            entry = {"waiters": 0, "audio": None, "producer_lock": False}
            async def run():
                async with self._download_lock:
                    result = await producer()
                    entry["audio"] = result
                    if result is not None and entry["waiters"]:
                        self.lock_audio("INFLIGHT:" + key, result)
                        entry["producer_lock"] = True
                    return result
            task = asyncio.create_task(run())
            entry["task"] = task
            self._inflight[key] = entry
            def finished(completed):
                if self._inflight.get(key) is entry:
                    self._inflight.pop(key, None)
                if not completed.cancelled():
                    completed.exception()  # retrieve failures if every waiter cancelled
            task.add_done_callback(finished)
        entry["waiters"] += 1
        try:
            result = await asyncio.shield(entry["task"])
            if result is not None:
                self.lock_audio("PENDING_RETURN", result)
            return result
        finally:
            entry["waiters"] -= 1
            if not entry["waiters"] and entry["producer_lock"]:
                self.unlock_audio("INFLIGHT:" + key, entry["audio"])
                entry["producer_lock"] = False

    async def _new_download(self, key, downloader):
        existing = await self._download_file_exist_check(key)
        if existing is not None:
            return existing
        async def reserve(size):
            await self._download_space_check(size)
            # Container fixups may temporarily require a second full file.
            # Keep that physical headroom as well as space for JSON and logs;
            # the logical cache budget still counts the final media only.
            headroom = 32 * 1024 * 1024
            if shutil.disk_usage(self._root).free < size + headroom:
                raise errors.StorageFull(self._name)
        budget = media_cache.DownloadBudget(self._storage_capacity, reserve, asyncio.get_running_loop())
        self._active_budget = budget
        try:
            if self._storage_capacity <= 0:
                raise errors.StorageFull(self._name)
            await budget.reserve(1)
            result = await downloader(budget)
            if result is None:
                return None
            # Downloaders reserve their actual size before atomic publication.
            self._recount_storage()
            if self._used_storage_size > self._storage_capacity:
                if not self.using(result):
                    os.remove(result.get_path())
                self._recount_storage()
                raise errors.StorageFull(self._name)
            await self._append_audio(result, cache_key=key)
            return result
        finally:
            self._active_budget = None
            self._recount_storage()

    @decorator.check_initialized
    async def download_bilibili(self, info_dict, download_type, num_option=0):
        key = media_cache.cache_key(download_type, info_dict["bvid"], num_option)
        async def producer():
            return await self._new_download(key, lambda budget: bilibili.audio_download(
                info_dict, self._root, download_type, num_option, budget=budget))
        return await self._shared_download(key, producer)

    @decorator.check_initialized
    async def download_ytdlp(self, url, info_dict, download_type):
        key = media_cache.cache_key(download_type, info_dict["id"])
        async def producer():
            return await self._new_download(key, lambda budget: ytdlp.audio_download(
                url, info_dict, self._root, download_type, budget=budget))
        return await self._shared_download(key, producer)

    def encode(self):
        return self._dl_list.encode()
