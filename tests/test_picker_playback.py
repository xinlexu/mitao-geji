"""Offline regressions for click-to-play without discarding queued songs."""
from __future__ import annotations
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_queue_fixes as queue_fixtures


class PickerPlaybackTests(unittest.IsolatedAsyncioTestCase):
    audio = queue_fixtures.QueueFixTests.audio
    enqueue = queue_fixtures.QueueFixTests.enqueue
    ended = queue_fixtures.QueueFixTests.ended
    drain = queue_fixtures.QueueFixTests.drain
    titles = queue_fixtures.QueueFixTests.titles
    asyncTearDown = queue_fixtures.QueueFixTests.asyncTearDown

    async def asyncSetUp(self):
        await queue_fixtures.QueueFixTests.asyncSetUp(self)
        queue_fixtures.load_defs('core.py', {'_picker_operation_allowed', 'play_chosen_audio', '_play_chosen_after_stop'}, self.ns)
        self.ctx.user.id = 111
        self.owner_id = '999'
        self.permissions = {'play': True, 'skip': True, 'resume': True}
        self.permission_calls = []
        def allow(user_id, operation):
            self.permission_calls.append((user_id, operation))
            return self.permissions.get(operation, False)
        self.ns['member_lib'] = SimpleNamespace(allow=allow)
        self.ns['setting'] = SimpleNamespace(value=lambda name: self.owner_id if name == 'owner' else None)
        self.x, self.y, self.n = [self.audio(name) for name in ('X', 'Y', 'N')]

    async def choose(self, target, **kwargs):
        return await self.ns['play_chosen_audio'](self.ctx, target, **kwargs)

    async def populate(self):
        for item in (self.a, self.x, self.b, self.y):
            await self.enqueue(item)

    async def test_active_choice_preserves_current_and_remaining_order(self):
        await self.populate()
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'switching')
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        self.assertEqual(self.guild.saved[-1], ['B', 'A', 'X', 'Y'])
        await self.drain()
        self.assertEqual(self.vc._active_audio_path, Path(self.b.get_path()))
        self.assertIsNone(self.vc._picker_switch_generation)
        await self.ended(self.b)
        self.assertEqual(self.titles(), ['A', 'X', 'Y'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))

    async def test_new_download_plays_now_without_consuming_old_head(self):
        await self.populate()
        before = len(self.pending_releases)
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'switching')
        self.assertEqual(self.titles(), ['N', 'A', 'X', 'B', 'Y'])
        self.assertEqual(self.pending_releases[before:], ['N'])
        await self.drain()
        self.assertEqual(self.vc._active_audio_path, Path(self.n.get_path()))
        await self.ended(self.n)
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))

    async def test_paused_choice_starts_selected_song_unpaused(self):
        await self.populate()
        self.vc.pause()
        await self.drain()
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'switching')
        await self.drain()
        self.assertTrue(self.vc.is_playing())
        self.assertFalse(self.vc.is_paused())
        self.assertFalse(self.vc.manager.applied[-1]['paused'])
        self.assertEqual(self.vc.manager.applied[-1]['track']['encoded'], 'B')

    async def test_disconnected_and_unready_choices_do_not_mutate_queue(self):
        await self.populate()
        self.ctx.guild.voice_client = None
        before = len(self.pending_releases)
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.pending_releases[before:], ['N'])
        self.ctx.guild.voice_client = self.vc
        self.vc._voice_ready.clear()
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])

    async def test_current_choice_does_not_restart_or_duplicate(self):
        await self.populate()
        generation = self.vc._generation
        releases = list(self.pending_releases)
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'already_playing')
        self.assertEqual(self.vc._generation, generation)
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.pending_releases, releases)
        self.vc.pause()
        await self.drain()
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'resumed')
        await self.drain()
        self.assertTrue(self.vc.is_playing())
        self.assertEqual(self.vc._generation, generation)

    async def test_deleted_choice_is_missing_even_if_same_file_is_readded(self):
        await self.populate()
        replacement = self.ns['Audio']('replacement B', 'test', 'B', self.b.get_path(), 60)
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.b, from_queue=True))
            await asyncio.sleep(0)
            self.guild.playlist.remove_audio(2)
            self.guild.playlist.append_audio(replacement)
        result = await choosing
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(self.titles(), ['A', 'X', 'Y', 'replacement B'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))

    async def test_moved_choice_is_relocated_by_identity_under_lock(self):
        await self.populate()
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.b, from_queue=True))
            await asyncio.sleep(0)
            self.guild.playlist.move_audio(2, 3)
        result = await choosing
        self.assertEqual(result['status'], 'switching')
        await self.drain()
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])

    async def test_busy_second_choice_preserves_first_and_releases_own_lease(self):
        await self.populate()
        first = await self.choose(self.b, from_queue=True)
        self.assertEqual(first['status'], 'switching')
        before = len(self.pending_releases)
        second = await self.choose(self.n, pending_lease=True)
        self.assertEqual(second['status'], 'busy')
        self.assertEqual(self.pending_releases[before:], ['N'])
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        await self.drain()
        self.assertEqual(self.vc._active_audio_path, Path(self.b.get_path()))

    async def test_idle_choice_starts_selected_head(self):
        self.guild.playlist.append_audio(self.a)
        self.guild.playlist.append_audio(self.b)
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'playing')
        self.assertEqual(self.titles(), ['B', 'A'])
        self.assertEqual(self.vc._active_audio_path, Path(self.b.get_path()))

    async def test_new_cache_hit_reuses_existing_queue_entry(self):
        await self.populate()
        cached = self.ns['Audio']('cached B', 'test', 'B', self.b.get_path(), 60)
        before = len(self.pending_releases)
        result = await self.choose(cached, pending_lease=True)
        self.assertEqual(result['status'], 'switching')
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        self.assertIs(self.guild.playlist.get_audio(0), self.b)
        self.assertEqual(self.pending_releases[before:], ['cached B'])
        await self.drain()

    async def test_full_queue_can_promote_existing_song_without_extra_slot(self):
        await self.populate()
        self.guild.playlist._limitation = 4
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'switching')
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        await self.drain()
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'full')
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])

    async def test_pending_lease_is_released_when_lock_wait_is_cancelled(self):
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.n, pending_lease=True))
            await asyncio.sleep(0)
            choosing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await choosing
        self.assertEqual(self.pending_releases, ['N'])
        self.assertEqual(self.titles(), [])

    async def test_voice_disconnect_invalidates_switch_callback_without_losing_songs(self):
        await self.populate()
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'switching')
        await self.vc.disconnect()
        self.assertIsNone(self.vc._picker_switch_generation)
        await self.drain()
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        self.assertFalse(any('NOW_PLAYING' in key for holders in self.lib._using.values() for key in holders))

    async def test_clear_before_pending_switch_finishes_empties_every_song(self):
        await self.populate()
        await self.choose(self.b, from_queue=True)
        await self.ns['clear_callback'](self.ctx)
        await self.drain()
        self.assertEqual(self.titles(), [])
        self.assertFalse(self.vc.is_playing())
        self.assertEqual(self.lib._using, {})
        self.assertIsNone(self.vc._picker_switch_generation)

    async def test_clear_ahead_of_waiting_switch_callback_empties_every_song(self):
        await self.populate()
        entered, release = asyncio.Event(), asyncio.Event()
        original_update = self.vc.manager.update_player
        async def controlled_update(guild, payload):
            if payload.get('track') == {'encoded': None}:
                entered.set()
                await release.wait()
            return await original_update(guild, payload)
        self.vc.manager.update_player = controlled_update
        await self.choose(self.b, from_queue=True)
        await asyncio.wait_for(entered.wait(), 2)
        # Clear waits first. Then stop completes, releases the old playback and
        # waits behind clear. The release wrapper must retain the picker marker.
        async with self.guild.get_playback_lock():
            clearing = asyncio.create_task(self.ns['clear_callback'](self.ctx))
            await asyncio.sleep(0)
            release.set()
            await asyncio.sleep(0)
            self.assertIsNotNone(self.vc._advancing_generation)
            self.assertEqual(self.vc._picker_switch_generation, self.vc._generation)
        await clearing
        await self.drain()
        self.assertEqual(self.titles(), [])
        self.assertFalse(self.vc.is_playing())
        self.assertIsNone(self.vc._picker_switch_generation)

    async def test_pending_switch_external_disconnect_cleans_marker(self):
        await self.populate()
        await self.choose(self.b, from_queue=True)
        await self.vc.on_voice_state_update({'channel_id': None})
        await self.drain()
        self.assertIsNone(self.vc._picker_switch_generation)
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])

    async def test_pending_switch_start_failure_cleans_marker_and_keeps_queue(self):
        await self.populate()
        async def fail(path): raise RuntimeError('fake switched-track failure')
        self.vc.manager.load_local_track = fail
        await self.choose(self.b, from_queue=True)
        await self.drain()
        self.assertIsNone(self.vc._picker_switch_generation)
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        self.assertFalse(self.vc.is_playing())

    async def test_initial_start_failure_preserves_selected_head_and_releases_lease(self):
        async def fail(path): raise RuntimeError('fake node failure')
        self.vc.manager.load_local_track = fail
        self.guild.playlist.append_audio(self.a)
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'playback_error')
        self.assertEqual(self.titles(), ['N', 'A'])
        self.assertEqual(self.pending_releases, ['N'])
        self.assertFalse(any('NOW_PLAYING' in key for holders in self.lib._using.values() for key in holders))

    async def test_play_only_member_cannot_interrupt_active_track(self):
        await self.populate()
        self.permissions['skip'] = False
        before = len(self.pending_releases)
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'forbidden')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))
        self.assertEqual(self.pending_releases[before:], ['N'])
        self.assertEqual(self.permission_calls, [(111, 'play'), (111, 'skip')])
        self.assertFalse(self.vc.is_stopping())

    async def test_owner_can_interrupt_without_group_skip_permission(self):
        await self.populate()
        self.ctx.user.id = 999
        self.permissions = {'play': False, 'skip': False, 'resume': False}
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'switching')
        await self.drain()
        self.assertEqual(self.titles(), ['B', 'A', 'X', 'Y'])
        self.assertEqual(self.permission_calls, [])

    async def test_idle_play_does_not_require_skip_permission(self):
        self.permissions = {'play': True, 'skip': False, 'resume': False}
        self.guild.playlist.append_audio(self.a)
        result = await self.choose(self.b, pending_lease=True)
        self.assertEqual(result['status'], 'playing')
        self.assertEqual(self.titles(), ['B', 'A'])
        self.assertEqual(self.permission_calls, [(111, 'play')])

    async def test_current_playing_song_does_not_require_skip_permission(self):
        await self.populate()
        self.permissions['skip'] = False
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'already_playing')
        self.assertEqual(self.permission_calls, [(111, 'play')])
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])

    async def test_current_paused_song_requires_resume_but_not_skip(self):
        await self.populate()
        self.vc.pause()
        await self.drain()
        self.permissions = {'play': True, 'skip': False, 'resume': False}
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'forbidden')
        self.assertTrue(self.vc.is_paused())
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.permissions['resume'] = True
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'resumed')
        await self.drain()
        self.assertTrue(self.vc.is_playing())
        self.assertEqual(self.permission_calls, [(111, 'play'), (111, 'resume'),
                                                (111, 'play'), (111, 'resume')])

    async def test_paused_new_selection_cannot_bypass_resume_permission(self):
        await self.populate()
        self.vc.pause()
        await self.drain()
        self.permissions = {'play': True, 'skip': True, 'resume': False}
        result = await self.choose(self.b, from_queue=True)
        self.assertEqual(result['status'], 'forbidden')
        self.assertTrue(self.vc.is_paused())
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])

    async def test_track_started_while_selection_waited_requires_skip_permission(self):
        self.permissions['skip'] = False
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.n, pending_lease=True))
            await asyncio.sleep(0)
            self.guild.playlist.append_audio(self.a)
            await self.ns['play_audio'](self.ctx, self.a)
        result = await choosing
        self.assertEqual(result['status'], 'forbidden')
        self.assertEqual(self.titles(), ['A'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))
        self.assertEqual(self.pending_releases, ['N'])

    async def test_permission_revoked_while_waiting_is_rechecked_inside_lock(self):
        await self.populate()
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.b, from_queue=True))
            await asyncio.sleep(0)
            self.permissions['skip'] = False
        result = await choosing
        self.assertEqual(result['status'], 'forbidden')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])

    async def test_actor_left_voice_while_waiting_does_not_switch_or_leak_lease(self):
        await self.populate()
        before = len(self.pending_releases)
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.n, pending_lease=True))
            await asyncio.sleep(0)
            self.ctx.user.voice = None
        result = await choosing
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.vc._active_audio_path, Path(self.a.get_path()))
        self.assertEqual(self.pending_releases[before:], ['N'])
        self.assertFalse(self.vc.is_stopping())

    async def test_actor_moved_voice_while_waiting_is_rechecked(self):
        await self.populate()
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.b, from_queue=True))
            await asyncio.sleep(0)
            self.ctx.user.voice.channel = SimpleNamespace(id=2)
        result = await choosing
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertFalse(self.vc.is_stopping())

    async def test_bot_moved_voice_while_waiting_is_rechecked(self):
        await self.populate()
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.b, from_queue=True))
            await asyncio.sleep(0)
            self.vc.channel = SimpleNamespace(id=2, guild=self.ctx.guild)
        result = await choosing
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertFalse(self.vc.is_stopping())

    async def test_owner_cannot_bypass_same_voice_channel_requirement(self):
        self.ctx.user.id = 999
        self.ctx.user.voice = None
        result = await self.choose(self.n, pending_lease=True)
        self.assertEqual(result['status'], 'voice_required')
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.pending_releases, ['N'])
        self.assertEqual(self.permission_calls, [])

    async def test_play_revoked_while_waiting_rejects_idle_play_and_releases_lease(self):
        async with self.guild.get_playback_lock():
            choosing = asyncio.create_task(self.choose(self.n, pending_lease=True))
            await asyncio.sleep(0)
            self.permissions['play'] = False
        result = await choosing
        self.assertEqual(result['status'], 'forbidden')
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.pending_releases, ['N'])
        self.assertEqual(self.permission_calls, [(111, 'play')])

    async def test_play_revoked_rejects_current_song_without_resuming(self):
        await self.populate()
        self.vc.pause()
        await self.drain()
        self.permissions['play'] = False
        result = await self.choose(self.a, from_queue=True)
        self.assertEqual(result['status'], 'forbidden')
        self.assertTrue(self.vc.is_paused())
        self.assertEqual(self.titles(), ['A', 'X', 'B', 'Y'])
        self.assertEqual(self.permission_calls, [(111, 'play')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
