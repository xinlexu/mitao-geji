import ast
import asyncio
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('safe_media_urls_test', SOURCE / 'zeta_bot/url_safety.py')
urls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(urls)
tree = ast.parse((SOURCE / 'utils.py').read_text(encoding='utf-8'))
namespace = dict(vars(typing), os=os, json=json, tempfile=tempfile,
                 errors=types.SimpleNamespace(KeyAlreadyExists=KeyError))
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
         and n.name in {'json_save', 'DoubleLinkedNode', 'DoubleLinkedListDict'}]
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'utils.py', 'exec'), namespace)


class PersistenceAndLinks(unittest.TestCase):
    def test_atomic_save_preserves_previous_data_on_encode_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            path.write_text('{"queue":["A","B"]}', encoding='utf-8')
            previous = path.read_bytes()
            class Bad:
                def encode(self):
                    raise ValueError('injected serializer failure')
            with self.assertRaises(ValueError):
                namespace['json_save'](path, Bad())
            self.assertEqual(path.read_bytes(), previous)

    def test_atomic_save_preserves_previous_data_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            path.write_text('{"queue":["A"]}', encoding='utf-8')
            previous = path.read_bytes()
            with patch.object(os, 'replace', side_effect=OSError('injected disk failure')):
                with self.assertRaises(OSError):
                    namespace['json_save'](path, {'queue': ['B']})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['queue.json'])
            namespace['json_save'](path, {'queue': ['B', '中文']})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'queue': ['B', '中文']})

    def test_linked_list_insert_remove_preserves_index_and_key_views(self):
        linked = namespace['DoubleLinkedListDict']()
        linked.append('A', 'a')
        linked.append('C', 'c')
        linked.index_insert(1, 'B', 'b')
        self.assertEqual([linked.index_get(i) for i in range(len(linked))], ['A', 'B', 'C'])
        linked.index_remove(1)
        self.assertNotIn('b', linked)
        self.assertEqual([linked.index_get(i) for i in range(len(linked))], ['A', 'C'])
        linked.index_remove(1)
        linked.index_remove(0)
        self.assertEqual(len(linked), 0)
        self.assertNotIn('a', linked)
        self.assertNotIn('c', linked)
        with self.assertRaises(IndexError):
            linked.index_remove(0)

    def test_supported_urls_keep_existing_formats(self):
        cases = {
            'https://www.youtube.com/watch?v=abc&list=xyz': 'youtube_url',
            'https://m.youtube.com/watch?v=abc': 'youtube_url',
            'https://youtu.be/abc?t=12': 'youtube_short_url',
            '分享 https://www.bilibili.com/video/BV1234567890 。': 'bilibili_url',
            'www.youtube.com/watch?v=abc': 'youtube_url',
            'https://music.163.com/#/song?id=454828887': 'netease_url',
            'https://163cn.tv/example': 'netease_short_url',
            'BV1234567890': 'bilibili_bvid',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(urls.check_url_source(value), expected)
                self.assertIsNotNone(urls.get_url_from_str(value, expected))
        self.assertIsNone(urls.get_legal_netease_url('https://music.163.com/song?id=bad'))

    def test_fake_hosts_userinfo_and_ports_are_rejected(self):
        for value in ['https://youtu.be@127.0.0.1:443/example',
                      'https://youtube.com.evil.invalid/x',
                      'https://notyoutube.com/x',
                      'https://evil.invalid/?next=https://youtube.com/x',
                      'https://musicX163.com/song?id=1',
                      'https://youtube.com:2333/x',
                      'https://user:password@youtube.com/x']:
            with self.subTest(value=value):
                self.assertIsNone(urls.check_url_source(value))


class FakeHttp:
    class ClientError(Exception):
        pass

    def __init__(self, responses, addresses=None, delay=0):
        self.responses = list(responses)
        self.addresses = addresses or [{'host': '93.184.216.34'}]
        self.requests = []
        self.delay = delay
        self.close_count = 0
        owner = self

        class DefaultResolver:
            async def resolve(self, host, port=0, family=socket.AF_INET):
                return owner.addresses
            async def close(self):
                owner.close_count += 1

        class Response:
            def __init__(self, status, headers):
                self.status, self.headers = status, headers
            async def __aenter__(self):
                await asyncio.sleep(owner.delay)
                return self
            async def __aexit__(self, *args):
                pass
            def raise_for_status(self):
                if self.status >= 400:
                    raise FakeHttp.ClientError('injected http error')

        class Request:
            def __init__(self, response, resolver, url):
                self.response, self.resolver, self.url = response, resolver, url
            async def __aenter__(self):
                await self.resolver.resolve(urlsplit(self.url).hostname, 443)
                return await self.response.__aenter__()
            async def __aexit__(self, *args):
                return await self.response.__aexit__(*args)

        class Session:
            def __init__(self, **kwargs):
                owner.session_options = kwargs
                self.resolver = kwargs['connector'].resolver
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, url, **kwargs):
                owner.requests.append(url)
                assert kwargs['allow_redirects'] is False
                status, headers = owner.responses.pop(0)
                return Request(Response(status, headers), self.resolver, url)

        self.module = types.SimpleNamespace(
            abc=types.SimpleNamespace(AbstractResolver=object),
            resolver=types.SimpleNamespace(DefaultResolver=DefaultResolver),
            ClientTimeout=lambda **kwargs: kwargs,
            TCPConnector=lambda **kwargs: types.SimpleNamespace(**kwargs),
            ClientSession=Session, ClientError=self.ClientError)


class Redirects(unittest.IsolatedAsyncioTestCase):
    async def test_valid_redirect_is_async_bounded_and_keeps_parameters(self):
        fake = FakeHttp([(302, {'Location': 'https://www.youtube.com/watch?v=abc&t=12'}), (200, {})], delay=.03)
        ticks = []
        async def heartbeat():
            for _ in range(5):
                await asyncio.sleep(.005)
                ticks.append(1)
        with patch.dict(sys.modules, {'aiohttp': fake.module}):
            result, _ = await asyncio.gather(urls.get_redirect_url('https://youtu.be/abc?t=12'), heartbeat())
        self.assertEqual(result, 'https://www.youtube.com/watch?v=abc&t=12')
        self.assertEqual(len(ticks), 5)
        self.assertEqual(fake.session_options['timeout']['total'], 15)
        self.assertFalse(fake.session_options['trust_env'])
        self.assertEqual(fake.close_count, 1)

    async def test_unapproved_redirect_is_not_requested(self):
        fake = FakeHttp([(302, {'Location': 'https://127.0.0.1/private'})])
        with patch.dict(sys.modules, {'aiohttp': fake.module}):
            with self.assertRaises(ValueError):
                await urls.get_redirect_url('https://youtu.be/abc')
        self.assertEqual(fake.requests, ['https://youtu.be/abc'])

    async def test_dns_private_and_loopback_addresses_are_rejected(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1']:
            fake = FakeHttp([(200, {})], addresses=[{'host': address}])
            with patch.dict(sys.modules, {'aiohttp': fake.module}):
                with self.assertRaises(ValueError):
                    await urls.get_redirect_url('https://youtu.be/abc')

    async def test_redirect_cycle_is_bounded(self):
        fake = FakeHttp([(302, {'Location': '/abc'})] * 6)
        with patch.dict(sys.modules, {'aiohttp': fake.module}):
            with self.assertRaises(ValueError):
                await urls.get_redirect_url('https://youtu.be/abc')
        self.assertEqual(len(fake.requests), 6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
