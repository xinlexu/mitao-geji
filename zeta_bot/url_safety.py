"""Supported media link parsing and bounded, public-only short-link requests."""
import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urljoin


URL_DOMAINS = {
    "bilibili.com": "bilibili_url", "b23.tv": "bilibili_short_url",
    "youtube.com": "youtube_url", "youtu.be": "youtube_short_url",
    "music.163.com": "netease_url", "163cn.tv": "netease_short_url",
}


def supported_url_source(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port not in (None, 80, 443):
            return None
        host = (parsed.hostname or "").lower()
    except (ValueError, AttributeError):
        return None
    for domain, source in URL_DOMAINS.items():
        if host == domain or ("short" not in source and host.endswith("." + domain)):
            return source
    return None


def extract_url_candidate(text):
    # Parse the whole URL: a known name in userinfo or query is not a hostname.
    found = re.search(r"https?://[^\s<>]+", str(text), flags=re.IGNORECASE)
    if found:
        return found.group(0).rstrip("，。；！）】}>.,)")
    domains = "|".join(re.escape(domain) for domain in URL_DOMAINS)
    found = re.search(r"(?<![\w@.])(?:[\w-]+\.)*(?:" + domains + r")[^\s<>]*", str(text), flags=re.IGNORECASE)
    if found:
        return "https://" + found.group(0).rstrip("，。；！）】}>.,)")
    return None


def check_url_source(url):
    candidate = extract_url_candidate(url)
    if candidate is not None:
        return supported_url_source(candidate)
    if re.search(r"BV[0-9a-zA-Z]{10}", str(url)) is not None:
        return "bilibili_bvid"
    return None


def get_url_from_str(input_str, url_type):
    if url_type == "bilibili_bvid":
        found = re.search(r"BV[0-9a-zA-Z]{10}", str(input_str))
        return found.group(0) if found else None
    candidate = extract_url_candidate(input_str)
    if candidate is not None and supported_url_source(candidate) == url_type:
        return candidate
    return None


def get_legal_netease_url(input_str):
    found = re.search(r"(?:song|playlist)\?id=\d+", str(input_str))
    return "https://music.163.com/#/" + found.group(0) if found else None


async def get_redirect_url(url):
    import aiohttp

    class PublicResolver(aiohttp.abc.AbstractResolver):
        def __init__(self):
            self._resolver = aiohttp.resolver.DefaultResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            addresses = await self._resolver.resolve(host, port, family)
            if not addresses or any(not ipaddress.ip_address(item["host"]).is_global for item in addresses):
                raise ValueError("短链接指向了不允许访问的地址")
            return addresses

        async def close(self):
            await self._resolver.close()

    current = str(url)
    if supported_url_source(current) is None:
        raise ValueError("链接不是受支持的网站")
    resolver = PublicResolver()
    try:
        timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=8)
        connector = aiohttp.TCPConnector(resolver=resolver)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as session:
            async with asyncio.timeout(20):
                for _ in range(6):
                    if supported_url_source(current) is None:
                        raise ValueError("短链接跳转到了不支持的网站")
                    async with session.get(current, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise ValueError("短链接缺少跳转地址")
                            current = urljoin(current, location)
                            continue
                        response.raise_for_status()
                        return current
                raise ValueError("短链接跳转次数过多")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ValueError("短链接暂时无法访问，请使用完整链接重试") from exc
    finally:
        await resolver.close()
