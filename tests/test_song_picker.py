"""Offline UI regressions: load the real module with fake Discord transport."""
import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace as NS


ROOT = pathlib.Path(__file__).parents[1]


class Item:
    def __init__(self, **kwargs):
        self.disabled = False
        self.value = None
        self.__dict__.update(kwargs)


class View:
    def __init__(self, *, timeout=600):
        self.timeout = timeout
        self.children = []
        self.finished = False
    def add_item(self, item): self.children.append(item)
    def clear_items(self): self.children.clear()
    def stop(self): self.finished = True
    def is_finished(self): return self.finished


class Modal(View):
    def __init__(self, **kwargs):
        super().__init__(timeout=kwargs.get("timeout", 600))
        self.title = kwargs["title"]


fake_discord = types.ModuleType("discord")
fake_discord.ui = NS(View=View, Modal=Modal, InputText=Item, Select=Item, Button=Item)
fake_discord.SelectOption = Item
fake_discord.Embed = Item
fake_discord.ButtonStyle = NS(secondary=2, primary=1, danger=4)
fake_discord.Interaction = object
previous_discord = sys.modules.get("discord")
sys.modules["discord"] = fake_discord
spec = importlib.util.spec_from_file_location("_song_picker_under_test", ROOT / "zeta_bot" / "song_picker.py")
picker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = picker
try:
    spec.loader.exec_module(picker)
finally:
    if previous_discord is None:
        del sys.modules["discord"]
    else:
        sys.modules["discord"] = previous_discord


class Response:
    def __init__(self):
        self.done = False
        self.events = []
        self.defer_gate = None
    def is_done(self): return self.done
    async def defer(self):
        if self.done: raise AssertionError("double acknowledgment")
        self.events.append("defer")
        self.done = True
        if self.defer_gate is not None:
            await self.defer_gate.wait()
    async def send_message(self, text, **kwargs):
        if self.done: raise AssertionError("double acknowledgment")
        self.done = True
        self.events.append(("message", text, kwargs))
    async def send_modal(self, modal):
        if self.done: raise AssertionError("a Modal cannot follow defer")
        self.done = True
        self.events.append(("modal", modal))


class Followup:
    def __init__(self): self.messages = []
    async def send(self, text, **kwargs): self.messages.append((text, kwargs))


class Message:
    def __init__(self): self.edits = []
    async def edit(self, **kwargs): self.edits.append(kwargs)


def interaction(token=None, *, user=42, message=None):
    return NS(user=NS(id=user), response=Response(), followup=Followup(),
              data={"values": [token]} if token is not None else {}, message=message)


class Helpers(unittest.TestCase):
    def test_normalized_all_word_search(self):
        songs = [picker.SongChoice("a", "ＬＯＶＥ　ＳＯＮＧ", "陶喆"),
                 picker.SongChoice("b", "love song", "其他歌手")]
        self.assertEqual(picker.filter_choices(songs, "ＴＡＯ"), [])
        self.assertEqual(picker.filter_choices(songs, "LoVe 陶喆"), [songs[0]])
        self.assertEqual(picker.filter_choices(songs, "love missing"), [])
        self.assertEqual(picker.filter_choices(songs, "  "), songs)

    def test_duration_missing_does_not_filter_song(self):
        song = picker.SongChoice("a", "未提供时长", duration=None)
        self.assertEqual(picker.filter_choices([song], "时长"), [song])
        self.assertEqual(picker.format_duration(None), "时长未知")
        self.assertEqual(picker.format_duration(float("nan")), "时长未知")
        self.assertEqual(picker.format_duration(-1), "时长未知")
        self.assertEqual(picker.format_duration(65.9), "1:05")
        self.assertEqual(picker.format_duration(3661), "1:01:01")

    def test_bounded_description_for_untrusted_metadata(self):
        song = picker.SongChoice("a", "歌" * 400, "歌手" * 400, 1e300)
        self.assertLessEqual(len(picker.choice_description(song)), 100)
        self.assertLessEqual(len(picker.compact_text(song.title, 100)), 100)
        self.assertEqual(picker.compact_text("a", 0), "")


class PickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.songs = [picker.SongChoice(str(i), f"歌名{i}", f"歌手{i % 3}",
                                        None if i == 0 else 180 + i, payload={"source_index": i + 1})
                      for i in range(35)]
        self.played = []
        self.checked = []
        self.permission = True
        async def play(entry, event):
            self.assertTrue(event.response.is_done())
            self.played.append((entry, event))
        async def check(event, operation):
            self.checked.append((event.user.id, operation, event.response.is_done()))
            return self.permission
        self.view = picker.SongPickerView(42, lambda: self.songs, play, check=check)
        self.message = Message()
        self.view.bind_message(self.message)
        await self.view.prepare()

    def token(self): return next(iter(self.view._tokens))

    async def test_page_bounds_and_discord_component_limits(self):
        self.assertEqual(self.view.timeout, 600)
        self.assertEqual(self.view.page_count, 3)
        select = self.view.children[0]
        self.assertEqual(len(select.options), 15)
        self.assertTrue(all(len(x.label) <= 100 and len(x.description) <= 100 and len(x.value) <= 100
                            for x in select.options))
        self.assertEqual(len([x for x in self.view.children if x.row == 1]), 5)
        await self.view._next_callback(interaction())
        await self.view._next_callback(interaction())
        self.assertEqual(self.view.page, 2)
        self.assertEqual(len(self.view.children[0].options), 5)

    async def test_selection_acks_before_auth_and_uses_exact_snapshot(self):
        expected = self.songs[0]
        event = interaction(self.token())
        await self.view._select_callback(event)
        self.assertEqual(event.response.events[0], "defer")
        self.assertEqual(self.checked[-1], (42, "play", True))
        self.assertIs(self.played[0][0], expected)
        self.assertIs(self.played[0][1], event)
        self.assertFalse(self.view.closed)

    async def test_old_page_token_cannot_play_new_page_slot(self):
        old_token = self.token()
        await self.view._next_callback(interaction())
        event = interaction(old_token)
        await self.view._select_callback(event)
        self.assertEqual(self.played, [])
        self.assertTrue(event.response.is_done())
        self.assertIn("列表已经更新", event.response.events[0][1])

    async def test_old_filter_token_and_completed_play_token_expire(self):
        old_token = self.token()
        await self.view.apply_search(interaction(), "歌手1")
        await self.view._select_callback(interaction(old_token))
        self.assertEqual(self.played, [])
        current = self.token()
        await self.view._select_callback(interaction(current))
        await self.view._select_callback(interaction(current))
        self.assertEqual(len(self.played), 1)

    async def test_malformed_token_never_calls_play(self):
        event = interaction()
        event.data = {"values": [["unhashable"]]}
        await self.view._select_callback(event)
        self.assertEqual(self.played, [])

    async def test_owner_permission_and_modal_permission_recheck(self):
        await self.view._select_callback(interaction(self.token(), user=99))
        self.assertEqual(self.checked, [])
        self.assertEqual(self.played, [])
        modal_event = interaction()
        await self.view._search_callback(modal_event)
        modal = modal_event.response.events[-1][1]
        self.assertIsInstance(modal, picker.SongSearchModal)
        self.permission = False
        modal.keyword.value = "歌手1"
        submit = interaction()
        await modal.callback(submit)
        self.assertEqual(self.view.query, "")
        self.assertEqual(self.checked[-1], (42, "list", True))
        self.assertFalse(self.view.busy)

    async def test_modal_edits_bound_message_without_interaction_message(self):
        event = interaction()
        await self.view._search_callback(event)
        modal = event.response.events[-1][1]
        modal.keyword.value = "歌手1"
        await modal.callback(interaction(message=None))
        self.assertEqual(self.view.query, "歌手1")
        self.assertEqual(len(self.view.filtered_entries), 12)
        self.assertTrue(self.message.edits)

    async def test_double_click_and_navigation_are_blocked_during_play(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def slow_play(entry, event):
            self.played.append(entry)
            started.set()
            await release.wait()
        self.view.on_play = slow_play
        old_token = self.token()
        task = asyncio.create_task(self.view._select_callback(interaction(old_token)))
        await started.wait()
        await self.view._select_callback(interaction(old_token))
        await self.view._next_callback(interaction())
        await self.view._close_callback(interaction())
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.view.page, 0)
        self.assertFalse(self.view.closed)
        release.set()
        await task
        self.assertFalse(self.view.busy)

    async def test_busy_is_reserved_even_while_initial_defer_is_waiting(self):
        first = interaction(self.token())
        gate = asyncio.Event()
        first.response.defer_gate = gate
        task = asyncio.create_task(self.view._select_callback(first))
        await asyncio.sleep(0)
        self.assertTrue(self.view.busy)
        await self.view._select_callback(interaction(self.token()))
        self.assertEqual(self.played, [])
        gate.set()
        await task
        self.assertEqual(len(self.played), 1)

    async def test_play_failure_is_safe_and_retryable(self):
        async def failing(entry, event):
            raise RuntimeError("secret-token https://private.example/raw")
        self.view.on_play = failing
        event = interaction(self.token())
        await self.view._select_callback(event)
        self.assertFalse(self.view.closed)
        self.assertFalse(self.view.busy)
        self.assertNotIn("secret", str(event.followup.messages))
        self.assertNotIn("private", self.view.make_embed().description)
        async def retry(entry, event): self.played.append(entry)
        self.view.on_play = retry
        await self.view._select_callback(interaction(self.token()))
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.view.last_status, "")

    async def test_cancelled_error_propagates_and_releases_busy(self):
        async def cancelled(entry, event): raise asyncio.CancelledError()
        self.view.on_play = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.view._select_callback(interaction(self.token()))
        self.assertFalse(self.view.busy)
        self.assertFalse(self.view.closed)

    async def test_cancelled_permission_check_propagates(self):
        async def check(event, operation): raise asyncio.CancelledError()
        self.view.check = check
        with self.assertRaises(asyncio.CancelledError):
            await self.view._select_callback(interaction(self.token()))
        self.assertFalse(self.view.busy)

    async def test_add_all_ignores_search_and_handoff_never_edits_progress(self):
        all_received = []
        async def add_all(entries, event):
            all_received.extend(entries)
            self.view.stop()
            await self.message.edit(content="批量进度由导入器负责")
            return {"handoff": True}
        self.view.on_add_all = add_all
        await self.view.apply_search(interaction(), "歌手1")
        edit_count = len(self.message.edits)
        await self.view._add_all_callback(interaction())
        self.assertEqual(all_received, self.songs)
        self.assertEqual(len(self.message.edits), edit_count + 1)
        self.assertEqual(self.message.edits[-1]["content"], "批量进度由导入器负责")
        self.assertTrue(self.view.closed)
        self.assertFalse(self.view.busy)

    async def test_message_status_remains_open_and_only_explicit_close_closes(self):
        async def play(entry, event): return {"status": "playing", "message": "已经播放", "close": False}
        self.view.on_play = play
        await self.view._select_callback(interaction(self.token()))
        self.assertEqual(self.view.last_status, "已经播放")
        self.assertFalse(self.view.closed)
        async def close(entry, event): return {"close": True}
        self.view.on_play = close
        await self.view._select_callback(interaction(self.token()))
        self.assertTrue(self.view.closed)
        self.assertTrue(all(child.disabled for child in self.view.children))

    async def test_range_can_open_modal_and_releases_busy(self):
        async def advanced(entries, event):
            self.assertFalse(event.response.is_done())
            self.assertTrue(self.view.busy)
            await event.response.send_modal(Modal(title="高级范围"))
        self.view.on_range = advanced
        event = interaction()
        await self.view._range_callback(event)
        self.assertEqual(event.response.events[0][0], "modal")
        self.assertFalse(self.view.busy)

    async def test_dynamic_refresh_invalidates_old_tokens_and_clamps_page(self):
        await self.view._next_callback(interaction())
        await self.view._next_callback(interaction())
        old_token = self.token()
        self.songs[:] = self.songs[:1]
        await self.view.refresh()
        self.assertEqual(self.view.page, 0)
        self.assertEqual(len(self.view.entries), 1)
        await self.view._select_callback(interaction(old_token))
        self.assertEqual(self.played, [])

    async def test_async_provider_empty_results_and_timeout(self):
        async def provider(): return []
        self.view.entry_provider = provider
        await self.view.refresh()
        self.assertTrue(self.view.children[0].disabled)
        self.assertEqual(self.view.page_count, 1)
        await self.view.on_timeout()
        self.assertTrue(self.view.closed)
        self.assertEqual(self.view._tokens, {})

    async def test_public_busy_can_reserve_an_advanced_modal_submission(self):
        self.view.busy = True
        await self.view._select_callback(interaction(self.token()))
        self.assertEqual(self.played, [])
        self.assertTrue(await self.view.authorize(interaction(), "play"))
        self.view.busy = False

    async def test_permission_failure_has_safe_feedback(self):
        async def check(event, operation): raise RuntimeError("credentials")
        self.view.check = check
        event = interaction(self.token())
        await self.view._select_callback(event)
        self.assertEqual(self.played, [])
        self.assertFalse(self.view.busy)
        self.assertNotIn("credentials", str(event.followup.messages))


if __name__ == "__main__":
    unittest.main()
