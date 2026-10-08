"""Offline lifecycle tests for the actual persistent-panel manager."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock


class HTTPException(Exception):
    pass


class NotFound(HTTPException):
    pass


class Forbidden(HTTPException):
    pass


class FakeView:
    created = []
    def __init__(self, runtime, guild):
        self.guild = guild
        self.stopped = False
        self.refreshed = 0
        self.timeout = None
        self.created.append(self)
    def stop(self): self.stopped = True
    def is_finished(self): return self.stopped
    def refresh_state(self): self.refreshed += 1
    def make_embed(self): return NS(to_dict=lambda: {"description": self.guild.status})
    def to_components(self): return [{"type": 1, "components": [{"type": 2, "custom_id": "persistent"}]}]


def load_manager():
    path = Path(__file__).resolve().parents[1] / "zeta_bot" / "control_panel.py"
    names = ("discord", "zeta_bot", "_control_panel_under_test")
    previous = {name: sys.modules.get(name) for name in names}
    discord = types.ModuleType("discord")
    discord.NotFound, discord.Forbidden, discord.HTTPException = NotFound, Forbidden, HTTPException
    package = types.ModuleType("zeta_bot")
    package.music_controls = NS(PersistentMusicView=FakeView)
    spec = importlib.util.spec_from_file_location("_control_panel_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.update({"discord": discord, "zeta_bot": package, spec.name: module})
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None: sys.modules.pop(name, None)
            else: sys.modules[name] = old


MANAGER = load_manager()


class Guild:
    def __init__(self, guild_id=1):
        self.id, self.status = guild_id, "idle"
        self.metadata = {}
        self.saves = 0
        self.save_error = None
    def get_id(self): return self.id
    def get_name(self): return "test guild"
    def get_control_panel(self): return dict(self.metadata)
    def set_control_panel(self, channel_id=None, message_id=None):
        self.saves += 1
        if self.save_error is not None: raise self.save_error
        self.metadata = {"channel_id": channel_id, "message_id": message_id} if channel_id and message_id else {}


class Message:
    def __init__(self, channel, message_id, author=9, webhook_id=None):
        self.channel, self.id = channel, message_id
        self.author, self.webhook_id = NS(id=author), webhook_id
        self.edits = []
        self.edit_error = None
    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.edit_error is not None: raise self.edit_error
        return self


class Channel:
    def __init__(self, channel_id=10, guild_id=1):
        self.id, self.guild = channel_id, NS(id=guild_id)
        self.messages = {}
        self.sends, self.fetches = [], []
        self.send_error = self.fetch_error = None
        self.send_gate = None
    async def send(self, **kwargs):
        self.sends.append(kwargs)
        if self.send_gate is not None: await self.send_gate.wait()
        if self.send_error is not None: raise self.send_error
        message = Message(self, 100 + len(self.sends))
        self.messages[message.id] = message
        return message
    async def fetch_message(self, message_id):
        self.fetches.append(message_id)
        if self.fetch_error is not None: raise self.fetch_error
        if message_id not in self.messages: raise NotFound("private token must never be logged")
        return self.messages[message_id]


class Bot:
    def __init__(self, channel):
        self.user = NS(id=9)
        self.guilds = [channel.guild]
        self.channels = {channel.id: channel}
        self.remote_channels = dict(self.channels)
        self.added, self.fetched = [], []
        self.fetch_error = None
    def get_channel(self, channel_id): return self.channels.get(channel_id)
    async def fetch_channel(self, channel_id):
        self.fetched.append(channel_id)
        if self.fetch_error is not None: raise self.fetch_error
        if channel_id not in self.remote_channels: raise NotFound("private")
        return self.remote_channels[channel_id]
    def add_view(self, view, message_id): self.added.append((view, message_id))


class ControlPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeView.created = []
        self.guild, self.channel = Guild(), Channel()
        self.bot = Bot(self.channel)
        self.ctx = NS(channel=self.channel)
        self.runtime = NS(bot=self.bot, console=NS(rp=AsyncMock()), audio_lib_main=object(),
                          guild_lib=NS(check_by_guild_obj=AsyncMock(), get_guild=lambda ctx: self.guild))

    def existing(self, channel=None, author=9, webhook_id=None):
        channel = channel or self.channel
        message = Message(channel, 50, author, webhook_id)
        channel.messages[50] = message
        self.guild.metadata = {"channel_id": channel.id, "message_id": 50}
        return message

    async def update(self, **kwargs):
        return await MANAGER.update(self.runtime, self.guild, **kwargs)

    async def test_concurrent_creation_sends_one_ordinary_message(self):
        self.channel.send_gate = asyncio.Event()
        first = asyncio.create_task(self.update(ctx=self.ctx, create=True))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.update(ctx=self.ctx, create=True))
        await asyncio.sleep(0)
        self.channel.send_gate.set()
        left, right = await asyncio.gather(first, second)
        self.assertIs(left, right)
        self.assertEqual(len(self.channel.sends), 1)
        self.assertEqual(self.guild.metadata, {"channel_id": 10, "message_id": left.id})
        self.assertEqual(len(FakeView.created), 1)
        self.assertEqual(left.webhook_id, None)

    async def test_existing_panel_is_reused_when_new_play_originates_elsewhere(self):
        message = self.existing()
        other = Channel(11)
        result = await self.update(ctx=NS(channel=other), create=True)
        self.assertIs(result, message)
        self.assertEqual(len(message.edits), 1)
        self.assertFalse(other.sends)
        self.assertFalse(self.channel.sends)

    async def test_unchanged_state_skips_edit_and_reuses_view(self):
        message = self.existing()
        await self.update()
        await self.update()
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(len(self.channel.fetches), 1)
        self.assertEqual(len(FakeView.created), 1)
        self.guild.status = "playing changed song"
        await self.update()
        self.assertEqual(len(message.edits), 2)
        self.assertEqual(len(FakeView.created), 1)
        self.assertFalse(self.bot.added)

    async def test_refresh_without_metadata_never_sends(self):
        self.assertIsNone(await self.update(ctx=self.ctx))
        self.assertFalse(self.channel.sends)
        self.assertFalse(FakeView.created)

    async def test_deleted_panel_clears_metadata_without_implicit_replacement(self):
        self.existing()
        self.channel.messages.clear()
        self.assertIsNone(await self.update(ctx=self.ctx))
        self.assertEqual(self.guild.metadata, {})
        self.assertFalse(self.channel.sends)
        self.assertTrue(FakeView.created[0].stopped)

    async def test_deleted_panel_is_replaced_once_when_creation_was_requested(self):
        self.existing()
        self.channel.messages.clear()
        message = await self.update(ctx=self.ctx, create=True)
        self.assertIsNotNone(message)
        self.assertEqual(len(self.channel.sends), 1)
        self.assertTrue(FakeView.created[0].stopped)
        self.assertFalse(FakeView.created[-1].stopped)

    async def test_explicit_create_checks_cached_message_even_when_state_is_unchanged(self):
        original = await self.update(ctx=self.ctx, create=True)
        old_view = FakeView.created[0]
        self.channel.messages.pop(original.id)
        replacement = await self.update(ctx=self.ctx, create=True)
        self.assertNotEqual(original.id, replacement.id)
        self.assertEqual(len(self.channel.sends), 2)
        self.assertTrue(old_view.stopped)

    async def test_explicit_create_rechecks_existing_but_does_not_edit_unchanged_state(self):
        original = await self.update(ctx=self.ctx, create=True)
        self.assertIs(await self.update(ctx=self.ctx, create=True), original)
        self.assertEqual(self.channel.fetches, [original.id])
        self.assertFalse(original.edits)
        self.assertEqual(len(self.channel.sends), 1)

    async def test_deleted_during_edit_can_recreate_once(self):
        old = self.existing()
        old.edit_error = NotFound("private")
        message = await self.update(ctx=self.ctx, create=True)
        self.assertIsNot(message, old)
        self.assertEqual(len(old.edits), 1)
        self.assertEqual(len(self.channel.sends), 1)

    async def test_forbidden_fetch_preserves_metadata_and_does_not_send(self):
        self.existing()
        self.channel.fetch_error = Forbidden("SECRET_URL_TOKEN")
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))
        self.assertEqual(self.guild.metadata["message_id"], 50)
        self.assertFalse(self.channel.sends)
        self.assertEqual(len(self.channel.fetches), 1)
        self.assertNotIn("SECRET_URL_TOKEN", str(self.runtime.console.rp.call_args_list))

    async def test_failed_edit_does_not_cache_fingerprint_or_retry_in_same_call(self):
        message = self.existing()
        message.edit_error = HTTPException("private")
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))
        self.assertEqual(len(message.edits), 1)
        self.assertFalse(self.channel.sends)
        message.edit_error = None
        await self.update()
        self.assertEqual(len(message.edits), 2)

    async def test_failed_send_is_not_retried_in_same_call(self):
        self.channel.send_error = Forbidden("private")
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))
        self.assertEqual(len(self.channel.sends), 1)
        self.assertEqual(self.guild.metadata, {})

    async def test_metadata_save_failure_does_not_duplicate_a_successful_send(self):
        self.guild.save_error = OSError("disk")
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))
        self.assertEqual(len(self.channel.sends), 1)
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))
        self.assertEqual(len(self.channel.sends), 1)
        self.guild.save_error = None
        message = await self.update(ctx=self.ctx, create=True)
        self.assertIsNotNone(message)
        self.assertEqual(len(self.channel.sends), 1)
        self.assertEqual(self.guild.metadata["message_id"], message.id)

    async def test_wrong_author_and_webhook_messages_are_not_edited(self):
        for author, webhook in ((12, None), (9, 123)):
            with self.subTest(author=author, webhook=webhook):
                message = self.existing(author=author, webhook_id=webhook)
                self.assertIsNone(await self.update())
                self.assertFalse(message.edits)
                self.assertEqual(self.guild.metadata, {})

    async def test_channel_from_another_guild_is_never_fetched_or_sent(self):
        other = Channel(12, guild_id=2)
        self.bot.channels[12] = other
        self.guild.metadata = {"channel_id": 12, "message_id": 50}
        self.assertIsNone(await self.update(ctx=NS(channel=other), create=True))
        self.assertFalse(other.fetches)
        self.assertFalse(other.sends)
        self.assertFalse(self.guild.metadata)

    async def test_uncached_thread_uses_fetch_channel_and_same_guild_validation(self):
        message = self.existing()
        self.bot.channels.clear()
        self.assertIs(await self.update(), message)
        self.assertEqual(self.bot.fetched, [10])
        self.assertEqual(len(message.edits), 1)

    async def test_restore_registers_existing_panel_once_and_never_sends(self):
        message = self.existing()
        await MANAGER.restore(self.runtime)
        await MANAGER.restore(self.runtime)
        self.assertEqual(len(self.bot.added), 1)
        self.assertEqual(self.bot.added[0][1], message.id)
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(len(FakeView.created), 1)
        self.assertFalse(self.channel.sends)
        self.assertEqual(self.runtime.guild_lib.check_by_guild_obj.await_count, 2)

    async def test_restore_without_metadata_registers_nothing(self):
        await MANAGER.restore(self.runtime)
        self.assertFalse(self.bot.added)
        self.assertFalse(self.channel.sends)
        self.assertFalse(FakeView.created)

    async def test_restore_guild_load_failure_is_logged_safely_and_does_not_send(self):
        self.runtime.guild_lib.check_by_guild_obj.side_effect = OSError("SECRET")
        await MANAGER.restore(self.runtime)
        self.runtime.console.rp.assert_awaited_once()
        self.assertNotIn("SECRET", str(self.runtime.console.rp.call_args))
        self.assertFalse(self.channel.sends)

    async def test_restore_does_not_register_wrong_author_or_deleted_message(self):
        self.existing(author=99)
        await MANAGER.restore(self.runtime)
        self.assertFalse(self.bot.added)
        self.assertFalse(self.guild.metadata)
        self.existing()
        self.channel.messages.clear()
        await MANAGER.restore(self.runtime)
        self.assertFalse(self.bot.added)
        self.assertFalse(self.channel.sends)

    async def test_external_metadata_change_stops_old_cached_view(self):
        old = self.existing()
        await self.update()
        view = FakeView.created[0]
        replacement = Message(self.channel, 60)
        self.channel.messages[60] = replacement
        self.guild.metadata["message_id"] = 60
        await self.update()
        self.assertTrue(view.stopped)
        self.assertEqual(len(FakeView.created), 2)
        self.assertEqual(len(old.edits), 1)
        self.assertEqual(len(replacement.edits), 1)

    async def test_cancellation_releases_panel_lock_without_swallowing_cancel(self):
        self.channel.send_gate = asyncio.Event()
        task = asyncio.create_task(self.update(ctx=self.ctx, create=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.runtime._control_panel_states[1].lock.locked())

    async def test_broken_logging_does_not_escape_into_playback(self):
        self.existing()
        self.channel.fetch_error = HTTPException("private")
        self.runtime.console.rp.side_effect = OSError("log disk")
        self.assertIsNone(await self.update(ctx=self.ctx, create=True))


if __name__ == "__main__":
    unittest.main()
