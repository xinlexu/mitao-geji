"""Offline queue/lifecycle regressions using production AST functions and fake I/O."""
from __future__ import annotations
import ast
import asyncio
import logging
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import *

SRC = Path(__file__).resolve().parents[1] / 'zeta_bot'


def load_defs(filename, names, ns, methods=None):
    tree = ast.parse((SRC / filename).read_text(encoding='utf-8-sig'))
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    if len(nodes) != len(names):
        raise AssertionError(f'Missing production definitions: {names}')
    for node in nodes:
        node.decorator_list = []
        if methods is not None and isinstance(node, ast.ClassDef):
            node.body = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods]
            for item in node.body:
                item.decorator_list = [d for d in item.decorator_list if isinstance(d, ast.Name) and d.id in ('staticmethod', 'classmethod')]
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(SRC / filename), 'exec'), ns)


async def noop(*args, **kwargs):
    return None


class VP:
    def __init__(self, client, channel):
        self.client, self.channel = client, channel
    def cleanup(self):
        self.channel.guild.voice_client = None


class FakeHTTPException(Exception):
    pass


class FakeMessage:
    async def edit(self, **kwargs):
        return self


class DeletedMessage:
    async def edit(self, **kwargs):
        raise FakeHTTPException('404 Unknown Message')


class FakeEmbed:
    def __init__(self):
        self.author = NS(name='正在播放 A')
    def set_author(self, **kwargs):
        self.author = NS(**kwargs)


async def embed(*args, **kwargs):
    return FakeMessage(), FakeEmbed()


class Manager:
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.applied = []
        self.gates = {}
        self.entered = {}
    async def load_local_track(self, path):
        name = path.name
        if name in self.gates:
            self.entered[name].set()
            await self.gates[name].wait()
        return {'encoded': name}
    async def update_player(self, guild, payload):
        self.applied.append(payload)
        return {'state': {'connected': True}}
    async def destroy_player(self, guild):
        pass
    def unregister_voice_client(self, guild):
        pass
    def gate(self, name):
        self.gates[name] = asyncio.Event()
        self.entered[name] = asyncio.Event()


class QueueFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zeta-queue-tests-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        ns = dict(globals())
        ns.update(discord=NS(VoiceProtocol=VP, Interaction=type('Interaction', (), {}), HTTPException=FakeHTTPException),
                  APP_ROOT=Path(self.temp.name), log=logging.getLogger('queue-tests'),
                  utils=NS(convert_duration_to_str=lambda t: str(t), markdown_escape=lambda t: t),
                  console=NS(rp=noop), icon=NS(url=lambda t: t), icon_lib=NS(files=lambda t: []),
                  embed_eos=embed, embed_respond=embed, embed_send=embed, orange=0, bot_name='fake',
                  control_panel=NS(update=noop))
        load_defs('audio.py', {'Audio'}, ns)
        ns['audio'] = NS(Audio=ns['Audio'])
        load_defs('playlist.py', {'Playlist'}, ns)
        ns['playlist'] = NS(Playlist=ns['Playlist'])
        load_defs('guild.py', {'GuildPlaylist'}, ns)
        load_defs('file_management.py', {'AudioFileLibrary'}, ns,
                  {'_path_key', 'lock_audio', 'unlock_audio', 'using', 'now_playing'})
        load_defs('lavalink_backend.py', {'LavalinkManager', '_VolumeProxy', 'LavalinkVoiceClient', '_field'}, ns)
        ns['lavalink_backend'] = NS(LavalinkVoiceClient=ns['LavalinkVoiceClient'])
        load_defs('core.py', {'_voice_has_track', 'refresh_control_panel', '_refresh_playback_view', 'enqueue_audio', 'play_audio', 'play_next',
                             'play_youtube', 'move_callback', 'leave_callback', 'pause_callback', 'resume_callback',
                             'skip_callback', 'clear_callback'}, ns)
        gns = dict(ns)
        load_defs('guild.py', {'Guild'}, gns, {'refresh_playing_message', 'get_play_mode', 'set_play_mode'})

        class Guild:
            refresh_playing_message = gns['Guild'].refresh_playing_message
            get_play_mode = gns['Guild'].get_play_mode
            set_play_mode = gns['Guild'].set_play_mode
            def __init__(self, library):
                self._playing_message = self._playing_embed = None
                self._play_mode = 0
                self._guild = 'fake'
                self._playback_lock = asyncio.Lock()
                self.saved = []
                self.playlist = ns['GuildPlaylist'](self, library)
            def get_name(self): return 'fake'
            def get_id(self): return 1
            def get_playlist(self): return self.playlist
            def get_voice_volume(self): return 100
            def get_playback_lock(self): return self._playback_lock
            async def refresh_list_view(self): pass
            def save(self):
                self.saved.append([a.get_title() for a in self.playlist._playlist])

        library = ns['AudioFileLibrary'].__new__(ns['AudioFileLibrary'])
        library._using = {}
        library._saved_queue_guards = {}
        self.pending_releases = []
        library.release_pending_audio = lambda a: self.pending_releases.append(a.get_title())
        guild = Guild(library)
        discord_guild = NS(id=1, voice_client=None, change_voice_state=noop)
        channel = NS(guild=discord_guild, id=1)
        client = NS(loop=asyncio.get_running_loop())
        vc = ns['LavalinkVoiceClient'](client, channel)
        vc.manager = Manager()
        vc._voice_ready.set()
        discord_guild.voice_client = vc
        ctx = NS(guild=discord_guild, user=NS(voice=NS(channel=channel)), author='tester')
        ns.update(audio_lib_main=library, guild_lib=NS(get_guild=lambda c: guild, check=noop))
        self.ns, self.lib, self.guild, self.ctx, self.vc = ns, library, guild, ctx, vc
        self.a, self.b, self.c = [self.audio(name) for name in 'ABC']

    def audio(self, name):
        path = Path(self.temp.name) / name
        path.write_bytes(b'Offline placeholder; no decoder is invoked.')
        return self.ns['Audio'](name, 'test', name, str(path), 60)

    def titles(self):
        return [a.get_title() for a in self.guild.playlist._playlist]

    async def enqueue(self, item):
        return await self.ns['enqueue_audio'](self.ctx, item)

    async def ended(self, item, reason='finished'):
        await self.ctx.guild.voice_client._handle_lavalink_event({
            'type': 'TrackEndEvent', 'reason': reason,
            'track': {'encoded': 'different encoded position', 'info': {'identifier': item.get_path()},
                      'userData': self.ctx.guild.voice_client._track_user_data()}})

    async def drain(self):
        for _ in range(8):
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                     and t.get_name().startswith('zeta-lavalink-')]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        self.fail('Playback tasks did not settle')

    async def asyncTearDown(self):
        await self.drain()

    async def test_concurrent_first_enqueue_starts_only_head(self):
        manager = self.vc.manager
        manager.gate('A')
        first = asyncio.create_task(self.enqueue(self.a))
        await asyncio.wait_for(manager.entered['A'].wait(), 2)
        second = asyncio.create_task(self.enqueue(self.b))
        await asyncio.sleep(0)
        self.assertEqual(self.titles(), ['A'])
        manager.gates['A'].set()
        await asyncio.gather(first, second)
        self.assertEqual(self.titles(), ['A', 'B'])
        self.assertEqual([p['track']['encoded'] for p in manager.applied], ['A'])
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['B'])
        self.assertEqual([p['track']['encoded'] for p in manager.applied], ['A', 'B'])
        self.assertNotIn(self.lib._path_key(self.a.get_path()), self.lib._using)
        self.assertCountEqual(self.pending_releases, ['A', 'B'])

    async def test_deleted_message_does_not_abort_enqueue(self):
        self.guild._playing_message = DeletedMessage()
        self.guild._playing_embed = FakeEmbed()
        await self.enqueue(self.a)
        self.assertEqual(self.titles(), ['A'])
        self.assertTrue(self.vc.is_playing())
        self.assertIsInstance(self.guild._playing_message, DeletedMessage)
        await self.ended(self.a)
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.lib._using, {})

    async def test_new_message_failure_does_not_abort_enqueue(self):
        async def fail(*args, **kwargs): raise FakeHTTPException('missing channel permission')
        self.ns['control_panel'].update = fail
        await self.enqueue(self.a)
        self.assertEqual(self.titles(), ['A'])
        self.assertTrue(self.vc.is_playing())
        await self.ended(self.a)
        self.assertEqual(self.lib._using, {})

    async def test_leave_releases_playback_but_preserves_queue_reference(self):
        await self.enqueue(self.a)
        await self.ns['leave_callback'](self.ctx)
        self.assertEqual(self.titles(), ['A'])
        self.assertEqual(self.lib._using[self.lib._path_key(self.a.get_path())], {'1': 1})
        replacement = self.ns['LavalinkVoiceClient'](self.vc.client, self.vc.channel)
        replacement.manager = Manager()
        replacement._voice_ready.set()
        self.ctx.guild.voice_client = replacement
        await self.ns['resume_callback'](self.ctx)
        await self.ended(self.a)
        self.assertEqual(self.lib._using, {})

    async def test_external_disconnect_releases_playback_reference(self):
        await self.enqueue(self.a)
        await self.vc.on_voice_state_update({'channel_id': None})
        self.assertEqual(self.lib._using[self.lib._path_key(self.a.get_path())], {'1': 1})
        self.assertEqual(self.titles(), ['A'])

    async def test_node_recovery_skip_cannot_overwrite_new_track(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        manager = self.vc.manager
        manager.gate('A')
        self.vc._resume_position_ms = 15000
        recovery = asyncio.create_task(self.vc._resume_current_after_node_reconnect())
        await asyncio.wait_for(manager.entered['A'].wait(), 2)
        self.vc.stop()
        manager.gates['A'].set()
        await recovery
        await self.drain()
        self.assertEqual(self.titles(), ['B'])
        self.assertEqual(manager.applied[-1]['track']['encoded'], 'B')
        self.assertEqual(self.vc._active_audio_path, Path(self.b.get_path()))
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['B'])
        await self.ended(self.b)
        self.assertEqual(self.titles(), [])

    async def test_offline_move_is_exact_and_persisted(self):
        self.ctx.guild.voice_client = None
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ns['move_callback'](self.ctx, 1, 2)
        self.assertEqual(self.titles(), ['B', 'A'])
        self.assertEqual(self.guild.saved[-1], ['B', 'A'])
        await self.ns['move_callback'](self.ctx, 20, 1)
        self.assertEqual(self.titles(), ['B', 'A'])

    async def test_active_move_to_front_preserves_other_songs(self):
        for a in (self.a, self.b, self.c): await self.enqueue(a)
        await self.ns['move_callback'](self.ctx, 3, 1)
        await self.drain()
        self.assertEqual(self.titles(), ['C', 'A', 'B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.c.get_path()))

    async def test_explicit_move_to_front_overrides_shuffle_for_that_transition(self):
        self.guild.set_play_mode(4)
        self.ns['random'] = NS(randrange=lambda n: n - 1)
        for a in (self.a, self.b, self.c): await self.enqueue(a)
        await self.ns['move_callback'](self.ctx, 3, 1)
        await self.drain()
        self.assertEqual(self.titles(), ['C', 'A', 'B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.c.get_path()))

    async def test_single_repeat_and_manual_skip_have_different_semantics(self):
        self.guild.set_play_mode(1)
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['A', 'B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))
        await self.ns['skip_callback'](self.ctx)
        await self.drain()
        self.assertEqual(self.titles(), ['B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.b.get_path()))

    async def test_list_repeat_requeues_finished_track(self):
        self.guild.set_play_mode(2)
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['B', 'A'])

    async def test_shuffle_selects_from_remaining_queue(self):
        self.guild.set_play_mode(3)
        self.ns['random'] = NS(randrange=lambda n: n - 1)
        for a in (self.a, self.b, self.c): await self.enqueue(a)
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['C', 'B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.c.get_path()))

    async def test_shuffle_repeat_preserves_finished_track(self):
        self.guild.set_play_mode(4)
        self.ns['random'] = NS(randrange=lambda n: n - 1)
        for a in (self.a, self.b, self.c): await self.enqueue(a)
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['A', 'B', 'C'])

    async def test_load_failure_does_not_repeat_broken_song(self):
        self.guild.set_play_mode(1)
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ended(self.a, reason='loadFailed')
        self.assertEqual(self.titles(), ['B'])

    async def test_pause_without_voice_and_empty_finish_are_safe(self):
        self.ctx.guild.voice_client = None
        await self.ns['pause_callback'](self.ctx, command_call=True)
        await self.ns['play_next'](self.ctx)

    async def test_start_failure_keeps_accepted_queue_and_releases_playback_lock(self):
        async def fail(path): raise RuntimeError('node unavailable')
        self.vc.manager.load_local_track = fail
        started = await self.enqueue(self.a)
        self.assertFalse(started)
        self.assertEqual(self.titles(), ['A'])
        self.assertEqual(self.lib._using[self.lib._path_key(self.a.get_path())], {'1': 1})
        self.assertEqual(self.pending_releases, ['A'])

    async def test_paused_skip_preserves_pause_for_next_song(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        self.vc.pause()
        await self.drain()
        await self.ns['skip_callback'](self.ctx)
        await self.drain()
        self.assertEqual(self.titles(), ['B'])
        self.assertTrue(self.vc.is_paused())
        self.assertTrue(self.vc.manager.applied[-1]['paused'])
        await self.ns['clear_callback'](self.ctx)
        await self.drain()
        await self.enqueue(self.c)
        self.assertTrue(self.vc.is_playing())

    async def test_normal_node_recovery_restores_position(self):
        await self.enqueue(self.a)
        self.vc._resume_position_ms = 15000
        await self.vc._resume_current_after_node_reconnect()
        self.assertEqual(self.vc.manager.applied[-1]['track']['encoded'], 'A')
        self.assertEqual(self.vc.manager.applied[-1]['position'], 15000)
        self.assertEqual(self.vc.manager.applied[-1]['track']['userData'], self.vc._track_user_data())
        self.assertEqual(self.titles(), ['A'])

    async def test_exception_then_delayed_end_does_not_skip_same_file_again(self):
        for a in (self.a, self.a, self.b): await self.enqueue(a)
        first_user_data = self.vc.manager.applied[0]['track']['userData']
        stale_track = {'info': {'identifier': self.a.get_path()}, 'userData': first_user_data}
        await self.vc._handle_lavalink_event({'type': 'TrackExceptionEvent', 'track': stale_track,
                                             'exception': {'message': 'fake decoder failure'}})
        self.assertEqual(self.titles(), ['A', 'B'])
        self.assertNotEqual(first_user_data, self.vc._track_user_data())
        await self.vc._handle_lavalink_event({'type': 'TrackEndEvent', 'reason': 'loadFailed', 'track': stale_track})
        self.assertEqual(self.titles(), ['A', 'B'])
        await self.ended(self.a)
        self.assertEqual(self.titles(), ['B'])

    async def test_stuck_then_delayed_end_does_not_skip_same_file_again(self):
        for a in (self.a, self.a, self.b): await self.enqueue(a)
        stale_track = {'info': {'identifier': self.a.get_path()}, 'userData': self.vc._track_user_data()}
        await self.vc._handle_lavalink_event({'type': 'TrackStuckEvent', 'track': stale_track, 'thresholdMs': 1000})
        self.assertEqual(self.titles(), ['A', 'B'])
        await self.vc._handle_lavalink_event({'type': 'TrackEndEvent', 'reason': 'finished', 'track': stale_track})
        self.assertEqual(self.titles(), ['A', 'B'])

    async def test_unmarked_old_event_cannot_complete_new_track(self):
        await self.enqueue(self.a)
        await self.vc._handle_lavalink_event({'type': 'TrackEndEvent', 'reason': 'finished',
                                             'track': {'info': {'identifier': self.a.get_path()}}})
        self.assertEqual(self.titles(), ['A'])
        self.assertTrue(self.vc.is_playing())

    async def test_slow_guild_events_do_not_block_shared_websocket_reader(self):
        manager = self.ns['LavalinkManager'].__new__(self.ns['LavalinkManager'])
        manager._voice_clients = {1: self.vc}
        started, release = asyncio.Event(), asyncio.Event()
        processed = []
        async def slow(event):
            if event['sequence'] == 1:
                started.set()
                await release.wait()
            processed.append(event['sequence'])
        self.vc._handle_lavalink_event = slow
        try:
            await asyncio.wait_for(manager._handle_websocket_payload({'op': 'event', 'guildId': '1', 'sequence': 1}), .5)
            await asyncio.wait_for(started.wait(), .5)
            await asyncio.wait_for(manager._handle_websocket_payload({'op': 'event', 'guildId': '1', 'sequence': 2}), .5)
            await asyncio.wait_for(manager._handle_websocket_payload({'op': 'playerUpdate', 'guildId': '1',
                                                                      'state': {'connected': True, 'position': 987}}), .5)
            self.assertEqual(self.vc._last_position_ms, 987)
            self.assertEqual(processed, [])
        finally:
            release.set()
        await self.drain()
        self.assertEqual(processed, [1, 2])

    async def test_enqueue_ahead_of_waiting_finish_does_not_restart_old_head(self):
        await self.enqueue(self.a)
        async with self.guild.get_playback_lock():
            enqueue_b = asyncio.create_task(self.enqueue(self.b))
            await asyncio.sleep(0)
            completion = asyncio.create_task(self.ended(self.a))
            await asyncio.sleep(0)
            self.assertTrue(self.vc.is_stopping())
        await asyncio.gather(enqueue_b, completion)
        self.assertEqual(self.titles(), ['B'])
        self.assertEqual([p['track']['encoded'] for p in self.vc.manager.applied], ['A', 'B'])

    async def test_leave_ahead_of_waiting_finish_invalidates_callback(self):
        await self.enqueue(self.a)
        async with self.guild.get_playback_lock():
            leaving = asyncio.create_task(self.ns['leave_callback'](self.ctx))
            await asyncio.sleep(0)
            completion = asyncio.create_task(self.ended(self.a))
            await asyncio.sleep(0)
        await asyncio.gather(leaving, completion)
        self.assertEqual(self.titles(), ['A'])
        self.assertEqual(self.lib._using[self.lib._path_key(self.a.get_path())], {'1': 1})

    async def test_cancel_waiting_enqueue_releases_pending_lease(self):
        async with self.guild.get_playback_lock():
            task = asyncio.create_task(self.enqueue(self.a))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.pending_releases, ['A'])
        self.assertEqual(self.titles(), [])

    async def test_clear_and_duplicate_stop_do_not_skip_later_enqueue(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ns['clear_callback'](self.ctx)
        self.vc.stop()
        await self.drain()
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.lib._using, {})
        await self.enqueue(self.c)
        self.assertTrue(self.vc.is_playing())
        self.assertEqual(self.titles(), ['C'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
