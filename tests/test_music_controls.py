"""Persistent music controls tested with the real module and fake transports."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

SOURCE = Path(__file__).parents[1] / "zeta_bot"


class Item:
    def __init__(self, **kwargs):
        self.disabled = False
        self.__dict__.update(kwargs)


class View:
    def __init__(self, *, timeout=600):
        self.timeout, self.children = timeout, []
    def add_item(self, item): self.children.append(item)


class Embed(Item):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fields = []
    def add_field(self, **kwargs): self.fields.append(kwargs)
    def set_footer(self, **kwargs): self.footer = kwargs


class Context:
    def __init__(self, bot, interaction):
        self.bot, self.interaction = bot, interaction
        self.user, self.guild = interaction.user, interaction.guild


def load_module():
    names = ("discord", "zeta_bot", "zeta_bot.playlist_browser", "zeta_bot.song_picker", "_music_controls_test")
    previous = {name: sys.modules.get(name) for name in names}
    fake_discord = types.ModuleType("discord")
    fake_discord.ui = NS(View=View, Button=Item)
    fake_discord.ButtonStyle = NS(primary=1, secondary=2)
    fake_discord.Embed = Embed
    fake_discord.ApplicationContext = Context
    package = types.ModuleType("zeta_bot")
    browser = types.ModuleType("zeta_bot.playlist_browser")
    browser.open_queue_picker = AsyncMock()
    picker = types.ModuleType("zeta_bot.song_picker")
    picker.compact_text = lambda value, limit: str(value or "")[:limit]
    picker.format_duration = lambda value: "时长未知" if value is None else str(value)
    package.playlist_browser = browser
    sys.modules.update({"discord": fake_discord, "zeta_bot": package,
                        "zeta_bot.playlist_browser": browser, "zeta_bot.song_picker": picker})
    try:
        spec = importlib.util.spec_from_file_location("_music_controls_test", SOURCE / "music_controls.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


CONTROLS = load_module()


class Voice:
    def __init__(self):
        self.channel = NS(id=50, name="听歌频道")
        self._paused, self._generation, self._volume_ratio = False, 7, 1.0
        self.active, self.stopping, self.connected = True, False, True
        self.stops = 0
        self._patch_player = AsyncMock()
    def is_connected(self): return self.connected
    def is_paused(self): return self.active and self._paused
    def is_stopping(self): return self.stopping
    def stop(self):
        self.stops += 1
        self.stopping = True
    @property
    def source(self): return NS(volume=self._volume_ratio)


class Queue:
    def __init__(self):
        self.items = [NS(get_title=lambda: "今日歌名", get_duration=lambda: 200)]
    def __len__(self): return len(self.items)
    def get_audio(self, index): return self.items[index] if index < len(self.items) else None


class CurrentGuild:
    def __init__(self, guild):
        self._guild = guild
        self.lock = asyncio.Lock()
        self.queue, self.mode, self.volume = Queue(), 0, 100.0
        self.metadata = {"message_id": 900}
    def get_id(self): return self._guild.id
    def get_control_panel(self): return self.metadata
    def get_playback_lock(self): return self.lock
    def get_playlist(self): return self.queue
    def get_voice_volume(self): return self.volume
    def set_voice_volume(self, value): self.volume = value
    def get_play_mode(self): return self.mode
    def set_play_mode(self, mode): self.mode = mode; return True


class Response:
    def __init__(self): self.done, self.events = False, []
    def is_done(self): return self.done
    async def defer(self): self.done = True; self.events.append("defer")
    async def send_message(self, message, **kwargs):
        self.done = True
        self.events.append((message, kwargs))


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = Voice()
        self.guild = NS(id=5, voice_client=self.voice)
        self.current = CurrentGuild(self.guild)
        self.user = NS(id=42, voice=NS(channel=self.voice.channel))
        self.allowed, self.checked, self.operations = True, [], []
        def check(ctx):
            self.assertTrue(self.current.lock.locked())
            self.checked.append(ctx.user.id)
        def allow(user, operation):
            self.assertTrue(self.current.lock.locked())
            self.operations.append((user, operation))
            return self.allowed if isinstance(self.allowed, bool) else operation in self.allowed
        async def refresh(current):
            self.assertIs(current, self.current)
            self.assertFalse(self.current.lock.locked())
        self.runtime = NS(bot=NS(get_guild=lambda guild_id: self.guild),
                          lavalink_backend=NS(LavalinkVoiceClient=Voice),
                          _voice_has_track=lambda voice: voice.active,
                          member_lib=NS(check=check, allow=allow),
                          setting=NS(value=lambda name: "1"),
                          console=NS(rp=AsyncMock()), play_audio=AsyncMock(),
                          refresh_control_panel=AsyncMock(side_effect=refresh))
        self.view = CONTROLS.PersistentMusicView(self.runtime, self.current)
        CONTROLS.playlist_browser.open_queue_picker.reset_mock()
        CONTROLS.playlist_browser.open_queue_picker.side_effect = None

    def interaction(self):
        return NS(guild=self.guild, user=self.user, message=NS(id=900),
                  response=Response(), followup=NS(send=AsyncMock()))

    async def click(self, action):
        interaction = self.interaction()
        await self.view.buttons[action].callback(interaction)
        return interaction

    async def test_persistent_ids_survive_new_view_construction(self):
        other = CONTROLS.PersistentMusicView(self.runtime, self.current)
        expected = {"zeta_music_toggle", "zeta_music_next", "zeta_music_search",
                    "zeta_music_volume_down", "zeta_music_volume_up", "zeta_music_mode"}
        self.assertIsNone(self.view.timeout)
        self.assertEqual({item.custom_id for item in self.view.children}, expected)
        self.assertEqual({item.custom_id for item in other.children}, expected)

    async def test_pause_and_resume_ack_first_and_commit_after_patch(self):
        async def patch(payload):
            self.assertTrue(self.current.lock.locked())
            self.assertEqual(self.voice._paused, not payload["paused"])
        self.voice._patch_player.side_effect = patch
        event = await self.click("toggle")
        self.assertEqual(event.response.events, ["defer"])
        self.assertTrue(self.voice._paused)
        self.assertEqual(self.view.buttons["toggle"].label, "继续")
        await self.click("toggle")
        self.assertFalse(self.voice._paused)
        self.assertEqual(self.operations, [(42, "pause"), (42, "resume")])
        event.followup.send.assert_not_awaited()

    async def test_ack_does_not_wait_for_playback_lock(self):
        event = self.interaction()
        await self.current.lock.acquire()
        task = asyncio.create_task(self.view.dispatch_action(event, "toggle"))
        await asyncio.sleep(0)
        self.assertTrue(event.response.is_done())
        self.assertEqual(self.checked, [])
        self.current.lock.release()
        await task

    async def test_double_click_during_patch_cannot_enqueue_inverse_toggle(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def patch(payload):
            started.set()
            await release.wait()
        self.voice._patch_player.side_effect = patch
        first = asyncio.create_task(self.click("toggle"))
        await started.wait()
        second = await self.click("toggle")
        search = await self.click("search")
        self.assertIn("正在处理", second.response.events[0][0])
        self.assertIn("正在处理", search.response.events[0][0])
        self.assertEqual(self.voice._patch_player.await_count, 1)
        CONTROLS.playlist_browser.open_queue_picker.assert_not_awaited()
        release.set()
        await first
        self.assertTrue(self.voice._paused)
        self.assertFalse(self.view._busy)

    async def test_failed_remote_pause_does_not_change_local_state(self):
        self.voice._patch_player.side_effect = RuntimeError("secret token")
        event = await self.click("toggle")
        self.assertFalse(self.voice._paused)
        self.assertIn("未能完成", event.followup.send.call_args.args[0])
        self.assertNotIn("secret", str(event.followup.send.call_args))

    async def test_natural_end_during_patch_cannot_write_stale_pause_state(self):
        async def patch(payload):
            self.voice.active = False
            self.voice.stopping = True
        self.voice._patch_player.side_effect = patch
        event = await self.click("toggle")
        self.assertFalse(self.voice._paused)
        self.assertIn("状态刚刚变化", event.followup.send.call_args.args[0])

    async def test_paused_volume_updates_remote_ratio_and_guild_once(self):
        self.voice._paused = True
        await self.click("volume_up")
        self.voice._patch_player.assert_awaited_once_with({"volume": 110})
        self.assertEqual(self.current.volume, 110)
        self.assertAlmostEqual(self.voice.source.volume, 1.1)
        self.assertTrue(self.voice._paused)

    async def test_volume_failure_keeps_previous_values(self):
        self.voice._patch_player.side_effect = RuntimeError("offline")
        await self.click("volume_down")
        self.assertEqual(self.current.volume, 100)
        self.assertEqual(self.voice.source.volume, 1)

    async def test_volume_clamps_and_disables_boundary_buttons(self):
        self.current.volume = 195
        await self.click("volume_up")
        self.assertEqual(self.current.volume, 200)
        self.assertTrue(self.view.buttons["volume_up"].disabled)
        self.current.volume = 5
        await self.click("volume_down")
        self.assertEqual(self.current.volume, 0)
        self.assertTrue(self.view.buttons["volume_down"].disabled)

    async def test_skip_stops_once_without_removing_queued_audio(self):
        original = self.current.queue.items[:]
        first = await self.click("next")
        second = await self.click("next")
        self.assertEqual(self.voice.stops, 1)
        self.assertEqual(self.current.queue.items, original)
        self.assertIn("正在切换", first.followup.send.call_args.args[0])
        self.assertIn("正在切换", second.followup.send.call_args.args[0])
        self.assertEqual(self.view.buttons["toggle"].label, "正在切歌…")

    async def test_cycle_modes_uses_skip_permission_and_wraps(self):
        for expected in (1, 2, 3, 4, 0):
            await self.click("mode")
            self.assertEqual(self.current.mode, expected)
        self.assertTrue(all(operation == "skip" for _, operation in self.operations))

    async def test_shared_controls_use_actual_actor_and_deny_existing_restrictions(self):
        self.allowed = False
        self.user.id = 77
        await self.click("next")
        self.assertEqual(self.checked, [77])
        self.assertEqual(self.operations, [(77, "skip")])
        self.assertEqual(self.voice.stops, 0)

    async def test_owner_exempts_permissions_but_not_voice_membership(self):
        self.allowed = False
        self.user.id = 1
        await self.click("toggle")
        self.assertTrue(self.voice._paused)
        self.user.voice.channel = NS(id=99)
        event = await self.click("toggle")
        self.assertTrue(self.voice._paused)
        self.assertIn("进入机器人所在", event.followup.send.call_args.args[0])

    async def test_wrong_guild_or_old_panel_never_checks_or_mutates_members(self):
        event = self.interaction()
        event.guild = NS(id=8, voice_client=self.voice)
        await self.view.dispatch_action(event, "toggle")
        event = self.interaction()
        event.message.id = 901
        await self.view.dispatch_action(event, "toggle")
        self.assertEqual(self.checked, [])
        self.voice._patch_player.assert_not_awaited()
        self.runtime.refresh_control_panel.assert_not_awaited()

    async def test_panel_metadata_is_rechecked_after_waiting_for_lock(self):
        event = self.interaction()
        await self.current.lock.acquire()
        task = asyncio.create_task(self.view.dispatch_action(event, "next"))
        await asyncio.sleep(0)
        self.current.metadata["message_id"] = 999
        self.current.lock.release()
        await task
        self.assertEqual(self.voice.stops, 0)

    async def test_disconnected_controls_do_not_rejoin(self):
        self.guild.voice_client = None
        event = await self.click("toggle")
        self.runtime.play_audio.assert_not_awaited()
        self.assertIn("先加入语音", event.followup.send.call_args.args[0])

    async def test_idle_queue_resume_requires_resume_and_play_and_holds_lock(self):
        self.voice.active = False
        async def play(ctx, head):
            self.assertTrue(self.current.lock.locked())
            self.assertIs(ctx.user, self.user)
            self.assertIs(head, self.current.queue.items[0])
        self.runtime.play_audio.side_effect = play
        await self.click("toggle")
        self.assertEqual(self.operations, [(42, "resume"), (42, "play")])
        self.runtime.play_audio.assert_awaited_once()

    async def test_idle_queue_play_permission_is_rechecked(self):
        self.voice.active = False
        self.allowed = {"resume"}
        await self.click("toggle")
        self.runtime.play_audio.assert_not_awaited()

    async def test_search_opens_private_picker_only_after_releasing_lock(self):
        async def open_picker(runtime, interaction):
            self.assertFalse(self.current.lock.locked())
            self.assertTrue(interaction.response.is_done())
        CONTROLS.playlist_browser.open_queue_picker.side_effect = open_picker
        await self.click("search")
        self.assertEqual(self.operations, [(42, "list")])
        CONTROLS.playlist_browser.open_queue_picker.assert_awaited_once()

    async def test_embed_has_bounded_titles_and_honest_stopping_state(self):
        self.current.queue.items[0] = NS(get_title=lambda: "歌" * 10000, get_duration=lambda: None)
        self.voice.stopping = True
        embed = self.view.make_embed()
        self.assertEqual(embed.description, "正在切换歌曲")
        self.assertTrue(all(len(field["value"]) <= 1024 for field in embed.fields))
        self.assertIn("含当前歌曲", next(field["value"] for field in embed.fields if field["name"] == "队列"))

    async def test_remote_cancellation_propagates_and_unlocks_without_claiming_success(self):
        self.voice._patch_player.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.click("toggle")
        self.assertFalse(self.current.lock.locked())
        self.assertFalse(self.voice._paused)
        self.assertFalse(self.view._busy)
        self.runtime.refresh_control_panel.assert_not_awaited()

    async def test_failed_feedback_and_refresh_do_not_break_cleanup(self):
        self.voice._patch_player.side_effect = RuntimeError("private token")
        self.runtime.refresh_control_panel.side_effect = RuntimeError("message gone")
        event = self.interaction()
        event.followup.send.side_effect = RuntimeError("webhook expired")
        await self.view.dispatch_action(event, "toggle")
        self.assertFalse(self.current.lock.locked())
        self.assertFalse(self.voice._paused)


if __name__ == "__main__":
    unittest.main()
