from typing import *
import aiohttp
import html
from pathlib import Path
import tempfile
from bilibili_api import video, Credential, sync, select_client
from bilibili_api import search as bilibili_search

import errors
import utils

from zeta_bot import (
    console,
    audio,
    media_cache
)

# https://bili.moyu.moe/#/examples/video

# 设定请求库
select_client("curl_cffi")

SESSDATA = ""
BILI_JCT = ""
BUVID3 = ""

# FFMPEG 路径，查看：http://ffmpeg.org/
FFMPEG_PATH = "./zeta_bot/bin/ffmpeg"

# 控制台设置
console = console.Console()

level = "哔哩哔哩模块"

# 2026-11-24 服务器实测：B站音频 CDN 对不带 Range 的请求返回 HTTP 567（只换 UA、加 Origin/Accept 无效），
# 加上 Range: bytes=0- 后返回 206 和整首音频（Content-Range 0-末尾），Content-Length 校验保持有效。
# 其余为浏览器常规请求头。
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "*/*",
    "Range": "bytes=0-",
}


def _audio_url_candidates(audio_info: dict) -> list:
    """同一次 playurl 返回的主地址和备用地址；Akamai 海外节点在服务器实测中返回 403，排在最后。"""
    urls = [audio_info.get("baseUrl") or audio_info.get("base_url")]
    urls += list(audio_info.get("backupUrl") or audio_info.get("backup_url") or [])
    unique = []
    for url in urls:
        if url and url not in unique:
            unique.append(url)
    return sorted(unique, key=lambda url: "akamaized.net" in url.split("?", 1)[0])


async def get_info(bvid) -> dict:
    """
    返回视频信息

    :param bvid: 目标视频BV号
    :return:
    """
    await console.rp(f"开始提取信息：{bvid}", f"[{level}]")

    # 实例化 Credential 类
    credential = Credential(sessdata=SESSDATA, bili_jct=BILI_JCT, buvid3=BUVID3)
    # 实例化 Video 类
    v = video.Video(bvid=bvid, credential=credential)
    # 获取视频信息
    info_dict = await v.get_info()

    video_id = info_dict["bvid"]
    video_title = info_dict["title"]
    await console.rp(f"信息提取完毕：{video_title} [{video_id}]", f"[{level}]")

    return info_dict


async def get_filesize(info_dict: dict, num_p=0) -> Union[int, None]:
    headers = dict(DOWNLOAD_HEADERS)

    bvid = info_dict["bvid"]
    # 实例化 Credential 类
    credential = Credential(sessdata=SESSDATA, bili_jct=BILI_JCT, buvid3=BUVID3)
    # 实例化 Video 类
    v = video.Video(bvid=bvid, credential=credential)

    # 获取视频下载链接
    url = await v.get_download_url(num_p)

    # 音频轨链接（主地址被拒绝时依次尝试同一响应中的备用地址）
    candidates = _audio_url_candidates(url["dash"]["audio"][0])

    async with aiohttp.ClientSession() as sess:
        for index, audio_url in enumerate(candidates):
            try:
                async with sess.get(audio_url, headers=headers) as resp:
                    resp.raise_for_status()
                    length = resp.headers.get('content-length')
                    return int(length) if length is not None else None
            except aiohttp.ClientResponseError:
                if index == len(candidates) - 1:
                    raise

    return None


# TODO 检查下载报错代码，是否和异步并发有关
# aiohttp.http_exceptions.ContentLengthError: 400, message:
#   Not enough data to satisfy content length header.
# aiohttp.client_exceptions.ClientPayloadError: Response payload is not completed: <ContentLengthError: 400, message='Not enough data to satisfy content length header.'>

async def audio_download(info_dict: dict, download_path: str, download_type="bilibili_single", num_p=0, budget=None) -> audio.Audio:
    bvid = info_dict["bvid"]
    credential = Credential(sessdata=SESSDATA, bili_jct=BILI_JCT, buvid3=BUVID3)
    v = video.Video(bvid=bvid, credential=credential)
    title = info_dict["pages"][num_p]["part"] if download_type == "bilibili_p" else info_dict["title"]
    duration = int(info_dict["pages"][num_p].get("duration") or 0)
    filename = media_cache.media_filename(download_type, bvid, "m4a", num_p)
    download_info = await v.get_download_url(num_p)
    candidates = _audio_url_candidates(download_info["dash"]["audio"][0])
    headers = dict(DOWNLOAD_HEADERS)
    await console.rp(f"开始下载：{title}", f"[{level}]")
    process = 0
    try:
        with tempfile.TemporaryDirectory(prefix=".cache-work-", dir=download_path) as temporary:
            if budget is not None:
                budget.staging_path = temporary
            staged_path = Path(temporary) / filename
            async with aiohttp.ClientSession() as sess:
                for index, audio_url in enumerate(candidates):
                    try:
                        async with sess.get(audio_url, headers=headers) as resp:
                            resp.raise_for_status()
                            length = resp.headers.get("content-length")
                            expected = int(length) if length is not None else None
                            content_type = resp.headers.get("content-type", "").lower()
                            if content_type.startswith("text/") or "json" in content_type:
                                raise aiohttp.ClientPayloadError("CDN returned a non-audio response")
                            if budget is not None and expected is not None and expected > budget.limit:
                                raise errors.StorageFull("音频文件库")
                            with open(staged_path, "wb") as output:
                                while True:
                                    chunk = await resp.content.read(65536)
                                    if not chunk:
                                        break
                                    if process == 0 and chunk.lstrip().lower().startswith((b"<html", b"<!doctype html")):
                                        raise aiohttp.ClientPayloadError("CDN returned an HTML response")
                                    if budget is not None:
                                        await budget.reserve(process + len(chunk))
                                    output.write(chunk)
                                    process += len(chunk)
                            if not process or (expected is not None and process != expected):
                                raise aiohttp.ClientPayloadError("Incomplete audio response")
                        break
                    except aiohttp.ClientResponseError:
                        # 只在节点直接拒绝（尚未写入任何音频字节）时改用同一响应里的下一个地址
                        if process or index == len(candidates) - 1:
                            raise
                        await console.rp(f"音频节点拒绝请求，改用备用地址（{index + 2}/{len(candidates)}）", f"[{level}]")
            if budget is not None:
                await budget.reserve(process)
            path = media_cache.publish_download(staged_path, download_path, filename)
    finally:
        if budget is not None:
            budget.staging_path = None
    # Keep source_id as the public BV identifier. The cache index separately
    # carries the P number, and existing queue serialization remains compatible.
    new_audio = audio.Audio(title, download_type, bvid, path, duration)
    if info_dict.get("pic"):
        new_audio.set_cover_url(info_dict["pic"])
    size = utils.convert_byte(process)
    await console.rp(f"下载完成：{title} [{bvid}] P{num_p + 1}，{size[0]} {size[1]}", f"[{level}]")
    return new_audio


async def search(query, query_num=5) -> list:
    """
    搜索哔哩哔哩的视频，最大返回20个结果（一页）
    """
    query = query.strip()

    if query_num > 20:
        query_num = 20

    await console.rp(f"开始搜索：{query}", f"[{level}]")

    info_dict = await bilibili_search.search_by_type(query, search_type=bilibili_search.SearchObjectType.VIDEO)

    result = []
    log_message = f"搜索 {query} 结果为："
    counter = 1

    id_header = "https://www.bilibili.com/video/"

    for item in info_dict["result"]:
        if counter > query_num:
            break

        title = html.unescape(item["title"])
        title = title.replace("<em class=\"keyword\">", "")
        title = title.replace("</em>", "")

        duration = utils.convert_str_to_duration(item["duration"])

        result.append(
            {
                "title": title,
                "id": id_header + item["bvid"],
                "duration": duration,
                "duration_str": utils.convert_duration_to_str(duration),
            }
        )

        log_message += f"\n{counter}. {item['bvid']}：{title} [{item['duration']}]"
        counter += 1

    await console.rp(log_message, f"[{level}]")

    return result
