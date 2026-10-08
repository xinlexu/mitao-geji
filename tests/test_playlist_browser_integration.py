"""Offline contracts between the real browser, picker and original import engine."""
from __future__ import annotations

import ast
import asyncio
import builtins
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import test_song_picker as ui


SOURCE = Path(__file__).resolve().parents[1] / "zeta_bot"


class ApplicationContext:
    def __init__(self, bot, interaction):
        self.bot, self.interaction = bot, interaction
        self.guild, self.user = interaction.guild, interaction.user


discord = types.ModuleType("discord")
discord.__dict__.update(ui.fake_discord.__dict__)
discord.ApplicationContext = ApplicationContext
discord.HTTPException = type("HTTPException", (Exception,), {})


def load_modules():
    names = ("discord", "zeta_bot", "zeta_bot.song_picker", "zeta_bot.song_choices", "_playlist_browser_test")
    previous = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("zeta_bot")
    package.song_picker = ui.picker
    sys.modules.update({"discord": discord, "zeta_bot": package, "zeta_bot.song_picker": ui.picker})
    try:
        spec = importlib.util.spec_from_file_location("zeta_bot.song_choices", SOURCE / "song_choices.py")
        choices = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = choices
        spec.loader.exec_module(choices)
        package.song_choices = choices
        spec = importlib.util.spec_from_file_location("_playlist_browser_test", SOURCE / "playlist_browser.py")
        browser = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = browser
        spec.loader.exec_module(browser)
        return browser
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


BROWSER = load_modules()


def core_class(name, methods, namespace, base="object"):
    tree = ast.parse((SOURCE / "core.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    body = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods]
    for node in body:
        node.decorator_list = []
    node = ast.ClassDef(name=name, bases=[ast.Name(id=base, ctx=ast.Load())], keywords=[], body=body, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(SOURCE / "core.py"), "exec"), namespace)
    return namespace[name]


class Voice:
    def __init__(self, channel):
        self.channel = channel
    def is_connected(self):
        return True


class Engine(ui.View):
    def __init__(self, ctx, source, info, *args, **kwargs):
        super().__init__()
        self.ctx, self.source, self.info_dict = ctx, source, info
        self.finish = False
        self.original_msg = None
        self._download_selected = AsyncMock(return_value={"audio": None, "message": "unavailable"})
        self._begin_import = AsyncMock()
    def _entry_count(self):
        return len(self.info_dict.get("entries", []))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = NS(id=100)
        self.guild = NS(id=9, voice_client=Voice(self.channel))
        self.actor = NS(id=42, voice=NS(channel=self.channel))
        self.interaction = ui.interaction(user=42)
        self.interaction.user, self.interaction.guild = self.actor, self.guild
        self.lock = asyncio.Lock()
        self.current = NS(get_playback_lock=lambda: self.lock, get_playlist=lambda: "queue")
        self.runtime = NS(
            bot=object(), guild_lib=NS(check=AsyncMock(), get_guild=lambda ctx: self.current),
            audio_lib_main=NS(), lavalink_backend=NS(LavalinkVoiceClient=Voice),
            join_callback=AsyncMock(), play_chosen_audio=AsyncMock(return_value={"status": "playing", "message": "done"}),
            EpisodeSelectMenu=Engine, playlist_imports={}, PLAYLIST_IMPORT_LIMIT=500,
            PLAYLIST_SELECTION_TEXT_LIMIT=1000, member_lib=NS(check=lambda ctx: None, allow=lambda user, op: True),
            setting=NS(value=lambda key: "1"), console=NS(rp=AsyncMock()),
            parse_episode_selection=lambda text, total: [1],
        )
        self.ctx = ApplicationContext(self.runtime.bot, self.interaction)

    def browser(self, info=None):
        return BROWSER.ImportedPlaylistBrowser(self.runtime, self.ctx, "youtube_playlist", info or {
            "entries": [{"id": "one", "title": "first"}, None, {"id": "three", "title": "third"}],
        })

    async def test_download_hands_exact_audio_lease_and_current_actor_to_playback(self):
        view = self.browser()
        target = object()
        view.engine._download_selected.return_value = {"audio": target}
        result = await view.play(view.choices[1], self.interaction)
        view.engine._download_selected.assert_awaited_once_with(3)
        args, kwargs = self.runtime.play_chosen_audio.call_args
        self.assertIs(args[0].user, self.actor)
        self.assertIs(args[1], target)
        self.assertEqual(kwargs, {"pending_lease": True})
        self.assertEqual(result["status"], "playing")

    async def test_queue_choice_borrows_audio_without_claiming_a_download_lease(self):
        target = object()
        await BROWSER.play_queue_choice(self.runtime, ui.picker.SongChoice("id", "song", payload=target), self.interaction)
        args, kwargs = self.runtime.play_chosen_audio.call_args
        self.assertIs(args[1], target)
        self.assertEqual(kwargs, {"from_queue": True})

    async def test_user_outside_voice_cannot_download_or_mutate_queue(self):
        view = self.browser()
        self.actor.voice = None
        result = await view.play(view.choices[0], self.interaction)
        self.assertEqual(result["status"], "voice_required")
        view.engine._download_selected.assert_not_awaited()
        self.runtime.play_chosen_audio.assert_not_awaited()

    async def test_other_channel_cannot_move_existing_music_session(self):
        self.actor.voice.channel = NS(id=101)
        result = await BROWSER.ensure_voice(self.runtime, self.ctx)
        self.assertEqual(result["status"], "voice_required")
        self.runtime.join_callback.assert_not_awaited()

    async def test_disconnected_bot_joins_once_while_queue_lock_is_held(self):
        self.guild.voice_client = None
        async def join(ctx, command_call):
            self.assertTrue(self.lock.locked())
            self.assertFalse(command_call)
            self.guild.voice_client = Voice(self.channel)
        self.runtime.join_callback.side_effect = join
        self.assertIsNone(await BROWSER.ensure_voice(self.runtime, self.ctx))
        self.runtime.join_callback.assert_awaited_once()
        self.assertFalse(self.lock.locked())

    async def test_browser_freezes_flat_order_before_original_metadata_changes(self):
        info = {"entries": [{"id": "first", "title": "first"}, None, {"id": "third", "title": "third"}]}
        view = self.browser(info)
        info["entries"].reverse()
        self.assertEqual(view.choices[1].payload, 3)
        self.assertEqual(view.engine.info_dict["entries"][2]["id"], "third")

    async def test_permission_check_reads_clicked_member_and_requested_operation(self):
        checked = []
        self.runtime.member_lib.allow = lambda user, operation: checked.append((user, operation)) or False
        self.assertFalse(await BROWSER.permission_check(self.runtime, self.interaction, "skip"))
        self.assertEqual(checked, [(42, "skip")])

    async def test_old_engine_constructor_tolerates_missing_youtube_thumbnail_metadata(self):
        namespace = {"discord": discord, "PersonalMusicView": ui.View, "builtins": builtins, "list": object(),
                     "utils": NS(ctime_str=lambda: "now"), "guild_lib": self.runtime.guild_lib}
        cls = core_class("EpisodeSelectMenu", {"__init__"}, namespace, base="PersonalMusicView")
        cls.refresh_pages = lambda self: None
        for thumbnails in (None, [None, {}, {"url": "https://example.invalid/image.png"}]):
            with self.subTest(thumbnails=thumbnails):
                menu = cls(self.ctx, "youtube_playlist", {"entries": [{"id": "x"}], "thumbnails": thumbnails}, [""], timeout=600)
                self.assertEqual(menu.source, "youtube_playlist")

    async def test_collection_entry_uses_normalizer_before_legacy_formatting(self):
        opened = []
        class Browser:
            def __init__(self, *args):
                opened.append(args)
            async def init_eos(self, *args, **kwargs):
                pass
        namespace = {"discord": discord, "bot": self.runtime.bot, "sys": sys, "__name__": __name__,
                     "playlist_browser": NS(ImportedPlaylistBrowser=Browser),
                     "embed_eos": AsyncMock(), "utils": NS(convert_duration_to_str=lambda x: str(x), make_playlist_page=lambda *a, **k: [""])}
        cls = core_class("CheckCollectionMenu", {"button_confirm_callback"}, namespace)
        menu = cls()
        menu.finish = False
        menu.source = "bilibili_collection"
        menu.info_dict = {"ugc_season": {"title": "合集", "sections": [{"episodes": [None, {"bvid": "BVx", "title": None}]}]}}
        menu.clear_items, menu.stop = lambda: None, lambda: None
        self.interaction.message = ui.Message()
        await menu.button_confirm_callback(None, self.interaction)
        self.assertEqual(len(opened), 1)

    async def test_real_old_engine_accepts_predeferred_modal_and_fallback_message(self):
        registry, processed = {}, []
        namespace = {"discord": discord, "bot": self.runtime.bot, "playlist_imports": registry}
        cls = core_class("EpisodeSelectMenu", {"_begin_import"}, namespace)
        engine = cls()
        engine.finish = False
        engine.children = []
        engine.clear_items = lambda: None
        engine.stop = lambda: None
        async def process(selected):
            processed.append(selected)
            self.assertIs(registry[9], engine)
        engine.play_select = process
        engine.original_msg = ui.Message()
        self.interaction.message = None
        await self.interaction.response.defer()
        await engine._begin_import([3], self.interaction)
        self.assertEqual(self.interaction.response.events, ["defer"])
        self.assertEqual(processed, [[3]])
        self.assertFalse(registry)

    async def test_range_modal_general_failure_reports_safely_and_releases_reservation(self):
        browser = self.browser()
        browser.start_import = AsyncMock(side_effect=RuntimeError("private-token"))
        modal = BROWSER.ImportRangeModal(browser)
        modal.selection.value = "1"
        await modal.callback(self.interaction)
        self.assertFalse(browser.view.busy)
        self.assertFalse(browser.view.closed)
        self.assertEqual(self.interaction.response.events[0], "defer")
        self.assertIn("未能完成", self.interaction.followup.messages[0][0])
        self.assertNotIn("private-token", str(self.interaction.followup.messages))
        self.assertNotIn("private-token", str(self.runtime.console.rp.call_args))

    async def test_range_invalid_input_keeps_picker_available(self):
        browser = self.browser()
        def invalid(text, total): raise ValueError("请填写有效范围")
        self.runtime.parse_episode_selection = invalid
        modal = BROWSER.ImportRangeModal(browser)
        modal.selection.value = "wrong"
        await modal.callback(self.interaction)
        self.assertFalse(browser.view.busy)
        self.assertFalse(browser.view.closed)
        browser.engine._begin_import.assert_not_awaited()
        self.assertEqual(self.interaction.followup.messages[0][0], "请填写有效范围")

    async def test_range_cancellation_propagates_and_releases_reservation(self):
        browser = self.browser()
        browser.start_import = AsyncMock(side_effect=asyncio.CancelledError())
        modal = BROWSER.ImportRangeModal(browser)
        modal.selection.value = "1"
        with self.assertRaises(asyncio.CancelledError):
            await modal.callback(self.interaction)
        self.assertFalse(browser.view.busy)
        self.runtime.console.rp.assert_not_awaited()

    async def test_import_stops_picker_before_long_handoff_without_overwriting_message(self):
        browser = self.browser()
        message = ui.Message()
        browser.view.bind_message(message)
        async def begin(selected, interaction):
            self.assertTrue(browser.view.closed)
            self.assertIs(browser.engine.original_msg, message)
            await message.edit(content="导入器的进度")
        browser.engine._begin_import.side_effect = begin
        result = await browser.start_import([1], self.interaction)
        self.assertEqual(result, {"handoff": True})
        self.assertEqual(message.edits, [{"content": "导入器的进度"}])

    async def test_import_failure_preserves_progress_and_attaches_usable_queue_actions(self):
        browser = self.browser()
        message = ui.Message()
        browser.view.bind_message(message)
        async def begin(selected, interaction):
            await message.edit(content="成功 2 首；失败 1 首")
            raise RuntimeError("private-token")
        browser.engine._begin_import.side_effect = begin
        await self.interaction.response.defer()
        result = await browser.start_import([1], self.interaction)
        self.assertTrue(result["handoff"])
        self.assertEqual(result["status"], "import_error")
        self.assertTrue(browser.view.closed)
        self.assertEqual(message.edits[0], {"content": "成功 2 首；失败 1 首"})
        self.assertEqual(set(message.edits[1]), {"view"})
        self.assertIsInstance(message.edits[1]["view"], BROWSER.QueuePickerActions)
        self.assertIn("已经入队的歌曲会保留", self.interaction.followup.messages[0][0])
        self.assertNotIn("private-token", str(self.interaction.followup.messages))

    async def test_import_cancellation_is_not_hidden(self):
        browser = self.browser()
        message = ui.Message()
        browser.view.bind_message(message)
        browser.engine._begin_import.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await browser.start_import([1], self.interaction)
        self.assertEqual(message.edits, [])
        self.runtime.console.rp.assert_not_awaited()

    async def test_completed_import_actions_report_open_failure_after_ack(self):
        self.runtime.guild_lib.check.side_effect = RuntimeError("private-token")
        actions = BROWSER.QueuePickerActions(self.runtime)
        # Real Response.defer accepts the ephemeral and invisible flags.
        async def defer(**kwargs):
            self.interaction.response.done = True
            self.interaction.response.events.append("defer")
        self.interaction.response.defer = defer
        await actions.open_picker(self.interaction)
        self.assertEqual(self.interaction.response.events, ["defer"])
        self.assertFalse(actions.is_finished())
        self.assertIn("暂时无法打开", self.interaction.followup.messages[0][0])
        self.assertNotIn("private-token", str(self.interaction.followup.messages))

    async def test_feedback_failure_does_not_raise_but_cancellation_does(self):
        self.interaction.response.send_message = AsyncMock(side_effect=RuntimeError("deleted"))
        await BROWSER.notify(self.interaction, "retry")
        self.interaction.response.send_message = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await BROWSER.notify(self.interaction, "retry")


if __name__ == "__main__":
    unittest.main()
