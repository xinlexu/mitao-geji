"""Offline regression tests: execute production definitions, never live services."""
import ast
import asyncio
from collections import Counter
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import typing
import unittest
import uuid

SOURCE = Path(__file__).resolve().parent.parent


def definitions(path, names=None, namespace=None):
    tree = ast.parse((SOURCE / path).read_text(encoding='utf-8-sig'))
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (names is None or node.name in names)]
    env = dict(vars(typing))
    env.update(namespace or {})
    exec(compile(tree, str(SOURCE / path), 'exec'), env)
    return SimpleNamespace(**env)


class Offline:
    def __init__(self, root):
        self.root = Path(root)
        self.errors = definitions('errors.py')
        self.utils = definitions('utils.py', {'json_save', 'json_load', 'legal_name', 'convert_byte', 'convert_duration_to_str', 'DoubleLinkedNode', 'DoubleLinkedListDict', 'double_linked_list_dict_decoder'}, {'os': os, 'json': json, 'tempfile': tempfile, 'errors': self.errors})
        self.utils.PrintType = SimpleNamespace(WARNING=1, ERROR=2, CAUTION=3)
        class Console:
            async def rp(self, *args, **kwargs):
                pass
        self.console = Console()
        self.decorator = definitions('zeta_bot/decorator.py', {'check_initialized'}, {'errors': self.errors})
        self.audio = definitions('zeta_bot/audio.py', namespace={'utils': self.utils})
        self.media_cache = definitions('zeta_bot/media_cache.py', namespace={'asyncio': asyncio, 'hashlib': hashlib, 'os': os, 'Path': Path, 're': re, 'uuid': uuid, 'errors': self.errors})
        self.payloads = {}
        self.download_calls = []
        self.ydl_options = []
        self.started = None
        self.proceed = None
        owner = self
        class DownloadError(Exception):
            pass
        class FakeYDL:
            def __init__(self, options):
                self.options = options
                owner.ydl_options.append(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                cookie = self.options.get('cookiefile')
                if cookie:
                    Path(cookie).write_text('# Request private update\n', encoding='utf-8')
            def download(self, urls):
                owner.download_calls.append(urls[0])
                if owner.started is not None:
                    owner.started.set()
                    if not owner.proceed.wait(5):
                        raise RuntimeError('Offline test synchronization timed out')
                payload = owner.payloads.get(urls[0], urls[0].encode())
                path = Path(self.options['outtmpl'])
                path.write_bytes(payload)
                for hook in self.options.get('progress_hooks', []):
                    hook({'status': 'finished', 'downloaded_bytes': len(payload)})
                return 0
            def extract_info(self, url, download=False):
                info = {'id': 'A', 'title': 'Offline song', 'duration': 1, 'ext': 'webm'}
                if url.startswith('ytsearch'):
                    return {'entries': [info]}
                return info
        self.ytdlp = definitions('zeta_bot/ytdlp.py', namespace={
            'asyncio': asyncio, 'copy': copy, 'contextmanager': contextmanager, 'os': os, 'Path': Path, 'tempfile': tempfile,
            'YoutubeDL': FakeYDL, 'DownloadError': DownloadError, 'errors': self.errors, 'utils': self.utils,
            'console': self.console, 'audio': self.audio, 'media_cache': self.media_cache, 'level': 'OFFLINE',
            'NETEASE_COOKIE_FILE': str(self.root/'netease-missing.txt'), 'YOUTUBE_COOKIE_FILE': str(self.root/'youtube-missing.txt'),
            'COMBINED_COOKIE_FILE': str(self.root/'combined-missing.txt'),
        })
        self.bili_status = 200
        self.bili_payload = b'fake-audio'
        self.bili_type = 'audio/mp4'
        self.bili_length = True
        class ClientResponseError(Exception):
            pass
        class ClientPayloadError(Exception):
            pass
        class Content:
            def __init__(self, data):
                self.data = data
            async def read(self, size):
                result, self.data = self.data[:size], self.data[size:]
                return result
        class Response:
            def __init__(self):
                self.headers = {'content-type': owner.bili_type}
                if owner.bili_length:
                    self.headers['content-length'] = str(len(owner.bili_payload))
                self.content = Content(owner.bili_payload)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def raise_for_status(self):
                if owner.bili_status >= 400:
                    raise ClientResponseError(str(owner.bili_status))
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, *args, **kwargs):
                return Response()
        class Video:
            def __init__(self, *args, **kwargs):
                pass
            async def get_download_url(self, part):
                return {'dash': {'audio': [{'baseUrl': 'OFFLINE'}]}}
        self.aiohttp = SimpleNamespace(ClientSession=Session, ClientResponseError=ClientResponseError, ClientPayloadError=ClientPayloadError)
        self.bilibili = definitions('zeta_bot/bilibili.py', {'get_filesize', 'audio_download'}, {
            'Path': Path, 'tempfile': tempfile, 'audio': self.audio, 'utils': self.utils,
            'console': self.console, 'level': 'OFFLINE', 'media_cache': self.media_cache, 'errors': self.errors,
            'Credential': lambda **kwargs: None, 'SESSDATA': '', 'BILI_JCT': '', 'BUVID3': '',
            'video': SimpleNamespace(Video=Video), 'aiohttp': self.aiohttp,
        })
        self.fm = definitions('zeta_bot/file_management.py', namespace={
            'asyncio': asyncio, 'Counter': Counter, 'hashlib': hashlib, 'os': os, 'Path': Path, 'shutil': shutil,
            'time': time, 'errors': self.errors, 'utils': self.utils, 'decorator': self.decorator,
            'console': self.console, 'audio': self.audio, 'bilibili': self.bilibili, 'ytdlp': self.ytdlp,
            'media_cache': self.media_cache,
        })

    async def library(self, capacity=1000, name='test'):
        root = self.root/name/'downloads'
        root.mkdir(parents=True, exist_ok=True)
        data = self.root/name/'data'
        data.mkdir(exist_ok=True)
        lib = self.fm.AudioFileLibrary(str(root), str(data/'cache.json'), name, capacity)
        await lib.initialize()
        return lib

    async def put(self, lib, key, size, source='youtube_single', title=None):
        path = Path(lib._root)/(key+'.media')
        path.write_bytes(b'x'*size)
        item = self.audio.Audio(title or key, source, key, str(path), 1)
        await lib._append_audio(item, cache_key=self.media_cache.cache_key(source, key))
        return item

    @staticmethod
    def info(identity='A', title='Same title'):
        return {'id': identity, 'title': title, 'ext': 'webm', 'duration': 1}


class CacheFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cache-regression-')
        self.addCleanup(self.temp.cleanup)
        self.env = Offline(self.temp.name)

    async def test_same_title_has_distinct_id_paths_and_correct_bytes(self):
        lib = await self.env.library()
        one = await lib.download_ytdlp('first', self.env.info('A'), 'youtube_single')
        two = await lib.download_ytdlp('second', self.env.info('B'), 'youtube_single')
        self.assertNotEqual(one.get_path(), two.get_path())
        self.assertEqual(Path(one.get_path()).read_bytes(), b'first')
        self.assertEqual(Path(two.get_path()).read_bytes(), b'second')
        self.assertEqual(lib.get_used_storage_size(), 11)
        self.assertEqual(one.get_source_id(), 'A')
        lib.release_pending_audio(one)
        lib.release_pending_audio(two)

    async def test_bilibili_parts_have_distinct_keys_keep_public_bv(self):
        lib = await self.env.library()
        info = {'bvid': 'BVTEST', 'title': 'Series', 'pages': [{'part': 'P1', 'duration': 1}, {'part': 'P2', 'duration': 2}]}
        one = await lib.download_bilibili(info, 'bilibili_p', 0)
        two = await lib.download_bilibili(info, 'bilibili_p', 1)
        default = await lib.download_bilibili(info, 'bilibili_single', 0)
        self.assertNotEqual(one.get_path(), two.get_path())
        self.assertEqual(one.get_source_id(), two.get_source_id())
        self.assertIs(default, one)
        self.assertEqual(len(lib), 2)
        for item in (one, two, default):
            lib.release_pending_audio(item)

    async def test_same_id_single_download_and_reference_counted_leases(self):
        lib = await self.env.library()
        one, two = await asyncio.gather(*[lib.download_ytdlp('same', self.env.info(), 'youtube_single') for _ in range(2)])
        self.assertIs(one, two)
        self.assertEqual(self.env.download_calls, ['same'])
        self.assertEqual(lib.get_used_storage_size(), 4)
        lib.release_pending_audio(one)
        self.assertTrue(lib.using(two))
        lib.release_pending_audio(two)
        self.assertFalse(lib.using(two))

    async def test_cancel_one_waiter_does_not_cancel_shared_download_or_leak_lock(self):
        lib = await self.env.library()
        self.env.started = threading.Event()
        self.env.proceed = threading.Event()
        one = asyncio.create_task(lib.download_ytdlp('same', self.env.info(), 'youtube_single'))
        two = asyncio.create_task(lib.download_ytdlp('same', self.env.info(), 'youtube_single'))
        for _ in range(200):
            if self.env.started.is_set():
                break
            await asyncio.sleep(.005)
        self.assertTrue(self.env.started.is_set())
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        self.env.proceed.set()
        result = await two
        self.assertEqual(self.env.download_calls, ['same'])
        lib.release_pending_audio(result)
        self.assertFalse(lib.using(result))

    async def test_eviction_scans_beyond_queued_file(self):
        lib = await self.env.library(capacity=20)
        queued = await self.env.put(lib, 'queued', 10)
        unused = await self.env.put(lib, 'unused', 10)
        lib.lock_audio('1', queued)
        await lib._download_space_check(5)
        self.assertTrue(Path(queued.get_path()).exists())
        self.assertFalse(Path(unused.get_path()).exists())

    async def test_approx_size_never_deletes_locked_complete_file(self):
        lib = await self.env.library()
        item = await self.env.put(lib, 'locked', 10)
        lib.lock_audio('1_NOW_PLAYING', item)
        key = self.env.media_cache.cache_key('youtube_single', 'locked')
        same = await lib._download_file_exist_check(key, 11)
        self.assertIs(same, item)
        self.assertTrue(Path(item.get_path()).exists())
        with self.assertRaises(PermissionError):
            await lib._remove_audio(key)

    async def test_full_startup_preserves_unloaded_guild_queue(self):
        lib = await self.env.library(capacity=10)
        item = await self.env.put(lib, 'old', 10)
        guild = Path(lib._path).parent/'guilds'/'123'
        guild.mkdir(parents=True)
        self.env.utils.json_save(str(guild/'123.json'), {'playlist': {'playlist': [item.encode()]}})
        cold = self.env.fm.AudioFileLibrary(lib._root, lib._path, 'cold', 10)
        await cold.initialize()
        self.assertTrue(cold._initialized)
        self.assertTrue(cold.using(item))
        self.assertTrue(Path(item.get_path()).exists())
        with self.assertRaises(self.env.errors.StorageFull):
            await cold._download_space_check(1)
        cold.lock_audio('123', item)
        cold.unlock_audio('123', item)
        await cold._download_space_check(1)
        self.assertFalse(Path(item.get_path()).exists())

    async def test_unknown_size_is_bounded_and_partial_files_are_removed(self):
        lib = await self.env.library(capacity=10)
        with self.assertRaises(self.env.errors.StorageFull):
            await lib.download_ytdlp('x'*20, self.env.info(), 'youtube_single')
        self.assertEqual(list(Path(lib._root).iterdir()), [])
        self.assertEqual(len(lib), 0)
        self.assertEqual(lib.get_used_storage_size(), 0)

    async def test_unknown_size_can_evict_as_it_grows(self):
        lib = await self.env.library(capacity=15)
        old = await self.env.put(lib, 'old', 10)
        result = await lib.download_ytdlp('new1234567', self.env.info(), 'youtube_single')
        self.assertFalse(Path(old.get_path()).exists())
        self.assertEqual(lib.get_used_storage_size(), 10)
        lib.release_pending_audio(result)

    async def test_bilibili_http_error_and_html_never_enter_cache(self):
        lib = await self.env.library()
        info = {'bvid': 'BVTEST', 'title': 'T', 'pages': [{'duration': 1}]}
        self.env.bili_status = 403
        with self.assertRaises(self.env.aiohttp.ClientResponseError):
            await lib.download_bilibili(info, 'bilibili_single')
        self.env.bili_status = 200
        self.env.bili_type = 'text/html'
        with self.assertRaises(self.env.aiohttp.ClientPayloadError):
            await lib.download_bilibili(info, 'bilibili_single')
        self.assertEqual(len(lib), 0)
        self.assertEqual(list(Path(lib._root).iterdir()), [])

    async def test_bilibili_chunked_audio_without_content_length_is_valid(self):
        lib = await self.env.library()
        self.env.bili_length = False
        info = {'bvid': 'BVTEST', 'title': 'T', 'pages': [{'duration': 1}]}
        item = await lib.download_bilibili(info, 'bilibili_single')
        self.assertEqual(Path(item.get_path()).read_bytes(), self.env.bili_payload)
        lib.release_pending_audio(item)

    async def test_missing_file_record_is_removed_without_recursion(self):
        lib = await self.env.library(capacity=10)
        item = await self.env.put(lib, 'missing', 10)
        Path(item.get_path()).unlink()
        self.assertTrue(await lib._delete_least_used_file())
        await lib._download_space_check(1)
        self.assertEqual(len(lib), 0)
        self.assertEqual(lib.get_used_storage_size(), 0)

    async def test_legacy_ambiguous_paths_are_preserved_but_not_reused(self):
        lib = await self.env.library()
        path = Path(lib._root)/'old-title.webm'
        path.write_bytes(b'old')
        one = self.env.audio.Audio('title', 'youtube_single', 'A', str(path), 1)
        two = self.env.audio.Audio('title', 'youtube_single', 'B', str(path), 1)
        self.env.utils.json_save(lib._path, [{'key': 'A', 'item': one}, {'key': 'B', 'item': two}])
        cold = self.env.fm.AudioFileLibrary(lib._root, lib._path, 'cold', 1000)
        await cold.initialize()
        self.assertTrue(all(node['key'].startswith('legacy:') for node in cold.encode()))
        self.assertEqual(cold.get_used_storage_size(), 3)
        new = await cold.download_ytdlp('correct', self.env.info('B'), 'youtube_single')
        self.assertNotEqual(new.get_path(), str(path))
        self.assertEqual(path.read_bytes(), b'old')
        cold.release_pending_audio(new)

    async def test_damaged_index_does_not_delete_media(self):
        lib = await self.env.library()
        path = Path(lib._root)/'retained.webm'
        path.write_bytes(b'old queue audio')
        Path(lib._path).write_text('{broken', encoding='utf-8')
        cold = self.env.fm.AudioFileLibrary(lib._root, lib._path, 'cold', 1000)
        await cold.initialize()
        self.assertTrue(path.exists())
        self.assertEqual(len(list(Path(lib._path).parent.glob('cache.json.corrupt-*'))), 1)

    async def test_even_one_legacy_title_record_is_not_certified_as_id_cache(self):
        lib = await self.env.library()
        path = Path(lib._root)/'old-title.webm'
        path.write_bytes(b'uncertain old bytes')
        old = self.env.audio.Audio('title', 'youtube_single', 'A', str(path), 1)
        self.env.utils.json_save(lib._path, [{'key': 'A', 'item': old}])
        cold = self.env.fm.AudioFileLibrary(lib._root, lib._path, 'cold', 1000)
        await cold.initialize()
        self.assertTrue(cold.encode()[0]['key'].startswith('legacy:'))
        fresh = await cold.download_ytdlp('verified', self.env.info('A'), 'youtube_single')
        self.assertNotEqual(fresh.get_path(), old.get_path())
        self.assertEqual(path.read_bytes(), b'uncertain old bytes')
        cold.release_pending_audio(fresh)

    async def test_v2_cache_survives_restart_without_second_download(self):
        lib = await self.env.library()
        first = await lib.download_ytdlp('verified', self.env.info('A'), 'youtube_single')
        lib.release_pending_audio(first)
        cold = self.env.fm.AudioFileLibrary(lib._root, lib._path, 'cold', 1000)
        await cold.initialize()
        again = await cold.download_ytdlp('verified', self.env.info('A'), 'youtube_single')
        self.assertEqual(again.get_path(), first.get_path())
        self.assertEqual(self.env.download_calls, ['verified'])
        cold.release_pending_audio(again)

    async def test_low_disk_headroom_aborts_and_cleans_staging(self):
        lib = await self.env.library()
        lib._new_download.__func__.__globals__['shutil'] = SimpleNamespace(
            copy2=shutil.copy2,
            disk_usage=lambda path: SimpleNamespace(free=32*1024*1024+5),
        )
        with self.assertRaises(self.env.errors.StorageFull):
            await lib.download_ytdlp('0123456789', self.env.info(), 'youtube_single')
        self.assertEqual(list(Path(lib._root).iterdir()), [])
        self.assertEqual(len(lib), 0)

    async def test_existing_orphan_is_never_overwritten_on_publication(self):
        lib = await self.env.library()
        filename = self.env.media_cache.media_filename('youtube_single', 'A', 'webm')
        orphan = Path(lib._root)/filename
        orphan.write_bytes(b'old queue reference')
        fresh = await lib.download_ytdlp('new bytes', self.env.info('A'), 'youtube_single')
        self.assertNotEqual(fresh.get_path(), str(orphan))
        self.assertEqual(orphan.read_bytes(), b'old queue reference')
        self.assertEqual(Path(fresh.get_path()).read_bytes(), b'new bytes')
        lib.release_pending_audio(fresh)

    async def test_cookie_snapshots_are_distinct_cleaned_and_never_write_source(self):
        cookie = self.env.root/'synthetic-cookie.txt'
        content = '# Netscape HTTP Cookie File\n.example.invalid\tTRUE\t/\tFALSE\t0\tTEST\tSYNTHETIC\n'
        cookie.write_text(content, encoding='utf-8')
        self.env.ytdlp._cookie_snapshot.__wrapped__.__globals__['YOUTUBE_COOKIE_FILE'] = str(cookie)
        with self.env.ytdlp._youtube_dl({}) as one:
            with self.env.ytdlp._youtube_dl({}) as two:
                first, second = one.options['cookiefile'], two.options['cookiefile']
                self.assertNotEqual(first, second)
                self.assertNotEqual(first, str(cookie))
        self.assertEqual(cookie.read_text(encoding='utf-8'), content)
        self.assertFalse(Path(first).exists())
        self.assertFalse(Path(second).exists())

    async def test_youtube_policy_copies_nested_options_without_mutating_caller(self):
        options = {'quiet': False, 'format': 'bestaudio', 'extractor_args': {
            'youtube': {'skip': ['hls']}, 'netease': {'custom': ['unchanged']},
        }}
        before = copy.deepcopy(options)
        with self.env.ytdlp._youtube_dl(options) as ydl:
            self.assertEqual(ydl.options['extractor_args']['youtube']['player_client'], ['default', 'web_embedded'])
            self.assertFalse(ydl.options['quiet'])
            self.assertEqual(ydl.options['format'], 'bestaudio')
            self.assertEqual(ydl.options['extractor_args']['netease'], {'custom': ['unchanged']})
            ydl.options['extractor_args']['youtube']['skip'].append('dash')
        self.assertEqual(options, before)

    async def test_explicit_youtube_client_is_preserved_including_empty_selection(self):
        for clients in (['mweb'], []):
            options = {'extractor_args': {'youtube': {'player_client': clients}}}
            before = copy.deepcopy(options)
            with self.env.ytdlp._youtube_dl(options) as ydl:
                self.assertEqual(ydl.options['extractor_args']['youtube']['player_client'], clients)
                self.assertIsNot(ydl.options['extractor_args']['youtube']['player_client'], clients)
            self.assertEqual(options, before)

    async def test_metadata_search_and_download_share_the_central_youtube_policy(self):
        lib = await self.env.library()
        await self.env.ytdlp.get_info('https://www.youtube.com/watch?v=OFFLINE')
        await self.env.ytdlp.youtube_search('Offline search', query_num=1)
        item = await lib.download_ytdlp('offline bytes', self.env.info(), 'youtube_single')
        lib.release_pending_audio(item)
        self.assertEqual(len(self.env.ydl_options), 3)
        for options in self.env.ydl_options:
            self.assertEqual(options['extractor_args']['youtube']['player_client'], ['default', 'web_embedded'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
