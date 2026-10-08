from __future__ import unicode_literals
import asyncio
import copy
from contextlib import contextmanager
from typing import *
import os
from pathlib import Path
import tempfile
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from yt_dlp.extractor.neteasemusic import NetEaseMusicBaseIE

import errors
import utils
from zeta_bot import console, audio, media_cache

console = console.Console()
NETEASE_COOKIE_FILE = "./configs/netease-cookies.txt"
NetEaseMusicBaseIE._API_BASE = "https://music.163.com/api/"
YOUTUBE_COOKIE_FILE = "./configs/youtube-cookies.txt"
COMBINED_COOKIE_FILE = "./configs/.cookies-combined.txt"
level = "YT-DLP模块"


@contextmanager
def _cookie_snapshot():
    """yt-dlp may save its jar on close; give each request its own private file."""
    sources = [path for path in (NETEASE_COOKIE_FILE, YOUTUBE_COOKIE_FILE) if os.path.isfile(path)]
    if not sources:
        yield None
        return
    # Preserve the effective, already-working jar if it includes newer cookie
    # refreshes. Reinstalling a source cookie makes that source take precedence.
    if os.path.isfile(COMBINED_COOKIE_FILE) and os.path.getmtime(COMBINED_COOKIE_FILE) >= max(os.path.getmtime(path) for path in sources):
        sources = [COMBINED_COOKIE_FILE]
    fd, path = tempfile.mkstemp(prefix="zeta-cookie-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("# Netscape HTTP Cookie File\n")
            for source in sources:
                with open(source, "r", encoding="utf-8", errors="replace") as input_file:
                    output.write(input_file.read().rstrip("\n") + "\n")
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


@contextmanager
def _youtube_dl(options):
    with _cookie_snapshot() as cookiefile:
        options = dict(options)
        # The logged-in TV client can reject playback; retain the defaults and
        # add YouTube's embedded client (upstream yt-dlp issue #17389). Keep this
        # source-specific, and never override an explicitly selected client.
        extractor_args = copy.deepcopy(options.get("extractor_args") or {})
        youtube_args = extractor_args.setdefault("youtube", {})
        youtube_args.setdefault("player_client", ["default", "web_embedded"])
        options["extractor_args"] = extractor_args
        if cookiefile:
            options["cookiefile"] = cookiefile
        with YoutubeDL(options) as ydl:
            yield ydl


async def get_info(ytb_url):
    ydl_opts = {'format': 'exhigh/higher/standard/bestaudio/best', 'extract_flat': True, 'quiet': True}
    await console.rp(f"开始提取信息：{ytb_url}", f"[{level}]")
    def extract():
        with _youtube_dl(ydl_opts) as ydl:
            return ydl.extract_info(ytb_url, download=False)
    info_dict = await media_cache.finish_thread_on_cancel(extract)
    await console.rp(f"信息提取完毕：{info_dict['title']} [{info_dict['id']}]", f"[{level}]")
    return info_dict


def get_filesize(info_dict: dict) -> Union[int, None]:
    # An estimate can be useful for display, never for cache integrity decisions.
    size = info_dict.get("filesize") or info_dict.get("filesize_approx")
    try:
        return int(size) if size and int(size) > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


async def audio_download(youtube_url, info_dict, download_path, download_type="youtube_single", budget=None) -> audio.Audio:
    video_id = info_dict["id"]
    video_title = info_dict["title"]
    extension = info_dict["ext"]
    duration = info_dict.get("duration") or 0
    filename = media_cache.media_filename(download_type, video_id, extension)
    await console.rp(f"开始下载：{video_title}", f"[{level}]")
    try:
        with tempfile.TemporaryDirectory(prefix=".cache-work-", dir=download_path) as temporary:
            if budget is not None:
                budget.staging_path = temporary
            staged_path = Path(temporary) / filename
            options = {
                "format": "exhigh/higher/standard/bestaudio/best",
                "outtmpl": str(staged_path), "extract_flat": True, "quiet": True,
                "noplaylist": True, "buffersize": 65536, "noresizebuffer": True,
                "concurrent_fragment_downloads": 1,
            }
            if budget is not None:
                options["progress_hooks"] = [budget.progress]
                options["max_filesize"] = budget.limit
            def download():
                with _youtube_dl(options) as ydl:
                    return ydl.download([youtube_url])
            result = await media_cache.finish_thread_on_cancel(download)
            if result or not staged_path.is_file() or staged_path.stat().st_size == 0:
                if budget is not None and (get_filesize(info_dict) or 0) > budget.limit:
                    raise errors.StorageFull("音频文件库")
                raise DownloadError("下载未生成完整的音频文件")
            actual_size = staged_path.stat().st_size
            if budget is not None:
                await budget.reserve(actual_size)
            video_path = media_cache.publish_download(staged_path, download_path, filename)
    finally:
        if budget is not None:
            budget.staging_path = None
    new_audio = audio.Audio(video_title, download_type, video_id, video_path, duration)
    if info_dict.get("thumbnail"):
        new_audio.set_cover_url(info_dict["thumbnail"])
    size = utils.convert_byte(actual_size)
    await console.rp(f"下载完成：{video_title} [{video_id}]，{size[0]} {size[1]}", f"[{level}]")
    return new_audio


async def youtube_search(query, query_num=5) -> list:
    query = query.strip()
    if not query:
        return []
    options = {
        'format': 'exhigh/higher/standard/bestaudio/best', 'default_search': 'ytsearch',
        'extract_flat': True, 'quiet': True,
    }
    await console.rp(f"开始搜索：{query}", f"[{level}]")
    def search():
        with _youtube_dl(options) as ydl:
            return ydl.extract_info(f"ytsearch{query_num}:{query}", download=False)
    extracted = await media_cache.finish_thread_on_cancel(search)
    result = []
    for item in extracted.get("entries", []):
        if not item or len(result) >= query_num:
            continue
        duration = item.get("duration") or 0
        result.append({
            "title": item["title"], "id": "https://www.youtube.com/watch?v=" + item["id"],
            "duration": duration, "duration_str": utils.convert_duration_to_str(duration),
        })
    await console.rp(f"搜索完成：{query}，{len(result)}个结果", f"[{level}]")
    return result
