"""Real playback callbacks must publish one final, unlocked settled UI state."""
from __future__ import annotations

import asyncio
import unittest

import test_queue_fixes as fixtures


class PanelTransitionTests(unittest.IsolatedAsyncioTestCase):
    audio = fixtures.QueueFixTests.audio
    enqueue = fixtures.QueueFixTests.enqueue
    ended = fixtures.QueueFixTests.ended
    drain = fixtures.QueueFixTests.drain
    titles = fixtures.QueueFixTests.titles
    asyncTearDown = fixtures.QueueFixTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.QueueFixTests.asyncSetUp(self)
        self.panel_updates = []
        self.notifications = []

        async def render_panel(*args, **kwargs):
            self.panel_updates.append(self.snapshot())

        self.ns['control_panel'].update = render_panel
        original_play = self.vc.play_zeta_audio

        async def observed_play(*args, **kwargs):
            # Use the actual public keyword while keeping this suite independent
            # of the shared core fixture's namespace/observer wiring.
            kwargs['state_changed'] = self.settled
            return await original_play(*args, **kwargs)

        self.vc.play_zeta_audio = observed_play

    def snapshot(self):
        return dict(stopping=self.vc.is_stopping(), active=self.vc.has_active_track(),
                    paused=self.vc.is_paused(), playing=self.vc.is_playing(), queue=self.titles(),
                    pending=self.vc._pending_stop_generation, advancing=self.vc._advancing_generation,
                    finish_locked=self.vc._finish_lock.locked(),
                    operation_locked=self.vc._operation_lock.locked(),
                    queue_locked=self.guild.get_playback_lock().locked())

    async def settled(self):
        self.notifications.append(self.snapshot())
        await self.ns['_refresh_playback_view'](self.guild)

    def assert_settled(self, queue, *, active):
        self.assertTrue(self.notifications, 'No final state notification was delivered')
        for state in self.notifications:
            self.assertFalse(state['stopping'])
            self.assertIsNone(state['pending'])
            self.assertIsNone(state['advancing'])
            self.assertFalse(state['finish_locked'])
            self.assertFalse(state['operation_locked'])
            self.assertFalse(state['queue_locked'])
        self.assertTrue(self.panel_updates)
        self.assertEqual(self.panel_updates[-1]['queue'], queue)
        self.assertEqual(self.panel_updates[-1]['active'], active)
        self.assertFalse(self.panel_updates[-1]['stopping'])

    async def test_natural_end_renders_next_song_after_advancing_is_cleared(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        self.panel_updates.clear()
        await self.ended(self.a)
        self.assertTrue(any(state['stopping'] for state in self.panel_updates),
                        'Fixture must exercise the intermediate switching render')
        self.assert_settled(['B'], active=True)
        self.assertTrue(self.panel_updates[-1]['playing'])

    async def test_explicit_stop_last_song_finishes_without_stuck_controls(self):
        await self.enqueue(self.a)
        self.vc.stop()
        await self.drain()
        self.assert_settled([], active=False)
        self.assertFalse(self.panel_updates[-1]['paused'])

    async def test_explicit_stop_notifies_only_after_residual_pending_marker_is_cleared(self):
        await self.enqueue(self.a)
        previous = self.vc._after_callback

        async def pending_callback(ctx, **kwargs):
            await previous(ctx, **kwargs)
            self.vc._pending_stop_generation = kwargs['generation']

        self.vc._after_callback = pending_callback
        self.vc.stop()
        await self.drain()
        self.assert_settled([], active=False)

    async def test_clear_callback_publishes_final_empty_state(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)
        await self.ns['clear_callback'](self.ctx)
        await self.drain()
        self.assert_settled([], active=False)

    async def test_next_song_load_failure_clears_guards_and_keeps_retryable_queue(self):
        await self.enqueue(self.a)
        await self.enqueue(self.b)

        async def fail_load(path):
            raise RuntimeError('offline load failure')

        self.vc.manager.load_local_track = fail_load
        with self.assertLogs('queue-tests', level='ERROR'):
            await self.ended(self.a)
        self.assert_settled(['B'], active=False)

    async def test_callback_failure_still_notifies_after_releasing_locks(self):
        await self.enqueue(self.a)

        async def fail_callback(ctx, **kwargs):
            raise RuntimeError('offline callback failure')

        self.vc._after_callback = fail_callback
        with self.assertLogs('queue-tests', level='ERROR'):
            await self.ended(self.a)
        self.assert_settled(['A'], active=False)

    async def test_missing_callback_still_notifies_settled_state(self):
        await self.enqueue(self.a)
        self.vc._after_callback = None
        await self.ended(self.a)
        self.assert_settled(['A'], active=False)

    async def test_observer_failure_does_not_escape_or_undo_progress(self):
        await self.enqueue(self.a)

        async def fail_observer():
            raise RuntimeError('offline UI failure')

        self.vc._state_changed = fail_observer
        with self.assertLogs('queue-tests', level='ERROR'):
            await self.ended(self.a)
        self.assertEqual(self.titles(), [])
        self.assertFalse(self.vc.is_stopping())
        self.assertFalse(self.vc._finish_lock.locked())

    async def test_observer_cancellation_propagates_after_state_cleanup(self):
        await self.enqueue(self.a)

        async def cancel_observer():
            raise asyncio.CancelledError()

        self.vc._state_changed = cancel_observer
        with self.assertRaises(asyncio.CancelledError):
            await self.ended(self.a)
        self.assertEqual(self.titles(), [])
        self.assertFalse(self.vc.is_stopping())
        self.assertFalse(self.vc._finish_lock.locked())

    async def test_callback_cancellation_notifies_and_still_propagates(self):
        await self.enqueue(self.a)

        async def cancel_callback(ctx, **kwargs):
            raise asyncio.CancelledError()

        self.vc._after_callback = cancel_callback
        with self.assertRaises(asyncio.CancelledError):
            await self.ended(self.a)
        self.assert_settled(['A'], active=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
