"""Offline regressions from real AST-extracted handlers; all external I/O is fake."""
import ast
import asyncio
import copy
import json
import pathlib
import re
import tracemalloc
import unittest
from types import SimpleNamespace as NS

SOURCE = pathlib.Path(__file__).parents[1]


class Embed:
    def __init__(self, **kwargs):
        self.data = dict(kwargs)
        self.data.setdefault("fields", [])
    @property
    def description(self): return self.data.get("description")
    @description.setter
    def description(self, value): self.data["description"] = value
    @property
    def fields(self): return self.data["fields"]
    def clear_fields(self): self.data["fields"] = []
    def add_field(self, **kwargs): self.fields.append(kwargs)
    def remove_field(self, index): self.fields.pop(index)
    def set_author(self, **kwargs): self.data["author"] = kwargs
    def set_footer(self, **kwargs): self.data["footer"] = kwargs
    def to_dict(self): return copy.deepcopy(self.data)
    @classmethod
    def from_dict(cls, data): return cls(**data)


class View:
    def clear_items(self): self.children = []
    def remove_item(self, item): self.children.remove(item)
    def stop(self): self.stopped = True


class Response:
    def __init__(self): self.calls = []
    async def defer(self, **kwargs): self.calls.append(("defer", kwargs))
    async def send_message(self, message, **kwargs): self.calls.append(("send", message))


class Context:
    def __init__(self, bot, interaction):
        self.user = interaction.user
        self.guild = interaction.guild
        self.interaction = interaction


async def noop(*args, **kwargs): pass


def load_nodes(path, names, env):
    tree = ast.parse((SOURCE / path).read_text(encoding="utf-8"))
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + selected, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), env)


def environment():
    style = NS(grey=0, green=1, red=2, primary=3)
    discord = NS(Embed=Embed, ApplicationContext=Context, ButtonStyle=style,
                 ui=NS(button=lambda **kwargs: lambda func: func))
    env = dict(discord=discord, View=View, re=re, asyncio=asyncio,
               PLAYLIST_IMPORT_LIMIT=500, PLAYLIST_SELECTION_TEXT_LIMIT=256,
               playlist_imports={}, bot=object(), console=NS(rp=noop),
               utils=NS(markdown_escape=lambda text: text.replace("*", "\\*")),
               errors=NS(StorageFull=type("StorageFull", (Exception,), {})))
    load_nodes("zeta_bot/core.py", {
        "bounded_embed", "parse_episode_selection", "component_command_check",
        "PersonalMusicView", "PlaylistMenu", "EpisodeSelectMenu", "SearchedAudioSelectionMenu",
        "media_error_summary", "media_error_stops_batch",
    }, env)
    return env


def interaction(user=111):
    return NS(user=NS(id=user), guild=NS(id=1, voice_client=None),
              data={"custom_id": "button_next_audio"}, response=Response())


def batch(env):
    obj = object.__new__(env["EpisodeSelectMenu"])
    obj.ctx = Context(None, interaction())
    obj.finish = False
    obj.source = "youtube_playlist"
    obj.info_dict = {"entries": [{"id": str(i)} for i in range(2089)]}
    obj.embed = Embed(title="A playlist", description="")
    obj.children = [NS(custom_id="button_cancel", label="取消", disabled=False)]
    obj.original_msg = object()
    obj.list_type = "YouTube播放列表"
    obj._cancel_requested = False
    return obj


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.env = environment()

    def test_range_validates_before_allocation(self):
        parse = self.env["parse_episode_selection"]
        tracemalloc.start()
        with self.assertRaises(ValueError): parse("1-999999999", 25)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, 100_000)
        self.assertEqual(parse("5-3,1", 25), [5, 4, 3, 1])
        self.assertEqual(len(parse("1-500", 2089)), 500)
        for invalid in ("0", "1-501", "1-400,1-400", "1--3", ",", "9" * 257):
            with self.assertRaises(ValueError, msg=invalid): parse(invalid, 2089)

    def test_embeds_obey_individual_and_total_limits(self):
        raw = Embed(title="t" * 500, description="d" * 7000)
        raw.set_author(name="a" * 600)
        raw.set_footer(text="f" * 3000)
        for _ in range(30): raw.add_field(name="n" * 500, value="v" * 2000)
        data = self.env["bounded_embed"](raw).to_dict()
        self.assertLessEqual(len(data["title"]), 256)
        self.assertLessEqual(len(data["description"]), 4096)
        self.assertLessEqual(len(data["fields"]), 25)
        total = sum(len(data.get(key, "")) for key in ("title", "description"))
        total += len(data.get("author", {}).get("name", "")) + len(data.get("footer", {}).get("text", ""))
        total += sum(len(f["name"]) + len(f["value"]) for f in data["fields"])
        self.assertLessEqual(total, 6000)

    async def test_500_failed_entries_complete_with_bounded_progress(self):
        obj = batch(self.env)
        async def failure(num): return {"audio": None, "message": "平台要求登录验证" * 30}
        obj._download_selected = failure
        descriptions = []
        async def send(*args, **kwargs):
            descriptions.append(kwargs["embed"].description)
            return obj.original_msg
        self.env["eos"] = send
        await obj.play_select(list(range(1, 501)))
        self.assertEqual((obj._processed, obj._failed), (500, 500))
        self.assertTrue(all(len(d) <= 4096 for d in descriptions))
        self.assertEqual(len(obj._recent_results), 5)
        self.assertIn("500/500", descriptions[-1])

    async def test_progress_http_failure_does_not_abort_downloads(self):
        obj = batch(self.env)
        async def failure(num): return {"audio": None, "message": "不可用"}
        obj._download_selected = failure
        async def send(*args, **kwargs): raise RuntimeError("simulated deleted Discord message")
        self.env["eos"] = send
        await obj.play_select(list(range(1, 101)))
        self.assertEqual(obj._processed, 100)
        self.assertTrue(obj._progress_error_logged)

    async def test_cancel_finishes_current_item_and_preserves_queue(self):
        obj = batch(self.env)
        queued = []
        async def download(num):
            if num == 2: obj._cancel_requested = True
            return {"audio": NS(get_title=lambda: "song", number=num)}
        async def enqueue(ctx, audio, **kwargs):
            self.assertFalse(kwargs["announce"])
            queued.append(audio.number)
        obj._download_selected = download
        self.env.update(enqueue_audio=enqueue, eos=noop)
        await obj.play_select([1, 2, 3, 4])
        self.assertEqual(queued, [1, 2])
        self.assertEqual(obj._processed, 2)
        self.assertIn("添加已停止", obj.embed.data["author"]["name"])

    async def test_systemic_login_failure_stops_batch_after_one_attempt(self):
        obj = batch(self.env)
        attempts = []
        async def download(num):
            attempts.append(num)
            return {"audio": None, "message": "平台要求登录验证", "stop_batch": True}
        obj._download_selected = download
        self.env["eos"] = noop
        await obj.play_select([1, 2, 3])
        self.assertEqual(attempts, [1])
        self.assertEqual(obj._processed, 1)
        self.assertIn("登录", obj._halt_reason)

    async def test_batch_uses_new_component_message_for_long_running_progress(self):
        obj = batch(self.env)
        current = interaction()
        current.message = NS(flags=NS(ephemeral=False))
        observed = []
        async def run(selected): observed.append(obj.original_msg)
        obj.play_select = run
        await obj._begin_import([1], current)
        self.assertIs(observed[0], current.message)

    async def test_batch_download_dispatch_preserves_each_source(self):
        obj = batch(self.env)
        calls = []
        async def bili(*args, **kwargs):
            calls.append((args, kwargs))
            return {"audio": "bili_audio"}
        async def info(ctx, url):
            calls.append(url)
            return {"info_dict": {"title": "track"}}
        async def download(ctx, url, data, kind):
            calls.append(kind)
            return {"audio": "remote_audio"}
        self.env.update(download_bilibili_audio=bili, get_ytdlp_info=info, download_ytdlp_audio=download)
        obj.source = "bilibili_p"
        await obj._download_selected(2)
        self.assertEqual(calls[-1][0][-2:], ("bilibili_p", 1))
        obj.source = "bilibili_collection"
        await obj._download_selected(3)
        self.assertEqual(calls[-1][1], {"num_option": 2})
        obj.source, obj.info_dict = "youtube_playlist", {"entries": [{"id": "test_id"}]}
        await obj._download_selected(1)
        self.assertEqual(calls[-2:], ["https://www.youtube.com/watch?v=test_id", "youtube_single"])
        obj.source, obj.info_dict = "netease_playlist", {"entries": [{"url": "https://music.163.com/song?id=1"}]}
        await obj._download_selected(1)
        self.assertEqual(calls[-2:], ["https://music.163.com/song?id=1", "netease_single"])

    async def test_repeated_click_and_parallel_batch_are_rejected(self):
        obj, other = batch(self.env), batch(self.env)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def run(selected):
            entered.set()
            await finish.wait()
        obj.play_select = run
        first = asyncio.create_task(obj._begin_import([1], interaction()))
        await entered.wait()
        repeat, parallel = interaction(), interaction()
        await obj._begin_import([1], repeat)
        await other._begin_import([1], parallel)
        self.assertIn("已处理", repeat.response.calls[0][1])
        self.assertIn("已有歌单", parallel.response.calls[0][1])
        self.assertFalse(other.finish)
        finish.set()
        await first
        self.assertFalse(self.env["playlist_imports"])
        self.assertTrue(obj.stopped)

    async def test_select_all_2089_is_explicitly_rejected_without_truncation(self):
        obj = batch(self.env)
        action = interaction()
        await obj.button_all_callback(NS(), action)
        self.assertIn("500", action.response.calls[0][1])
        self.assertIn("分批", action.response.calls[0][1])
        self.assertFalse(obj.finish)

    async def test_permissions_are_checked_for_actual_clicker(self):
        checked = []
        def allow(user, operation):
            checked.append((user, operation))
            return operation == "list"
        self.env.update(member_lib=NS(check=lambda ctx: None, allow=allow), setting=NS(value=lambda key: "999"))
        public = object.__new__(self.env["PlaylistMenu"])
        public.ctx = Context(None, interaction(111))
        clicker = interaction(222)
        self.assertFalse(await public.interaction_check(clicker))
        self.assertEqual(checked[-1], (222, "skip"))
        clicker.data["custom_id"] = "button_next_page"
        self.assertTrue(await public.interaction_check(clicker))
        self.assertEqual(checked[-1], (222, "list"))

    async def test_public_skip_executes_with_clicker_context(self):
        ids = []
        async def skip(ctx, **kwargs): ids.append(ctx.user.id)
        self.env["skip_callback"] = skip
        public = object.__new__(self.env["PlaylistMenu"])
        public.ctx = Context(None, interaction(111))
        public._deferred_refresh_menu = noop
        clicker = interaction(222)
        await public.button_next_audio_callback(NS(), clicker)
        self.assertEqual(ids, [222])
        self.assertEqual(clicker.response.calls[0][0], "defer")

    async def test_personal_menu_rejects_other_users_and_finished_selection(self):
        obj = batch(self.env)
        self.assertFalse(await obj.interaction_check(interaction(222)))
        obj.finish = True
        self.assertFalse(await obj.interaction_check(interaction(111)))

    async def test_search_acks_before_play_and_rejects_double_click(self):
        obj = object.__new__(self.env["SearchedAudioSelectionMenu"])
        obj.finish = False
        obj.children = []
        obj.original_msg = object()
        obj.resource_list = [{"id": "valid_test_track"}]
        clicker = interaction()
        calls = []
        async def play(ctx, link, **kwargs):
            self.assertEqual(clicker.response.calls[0][0], "defer")
            calls.append(link)
        self.env["play_callback"] = play
        await obj.play(0, clicker)
        await obj.play(0, interaction())
        self.assertEqual(calls, ["valid_test_track"])

    def test_error_summaries_do_not_echo_raw_exception(self):
        summarize = self.env["media_error_summary"]
        raw = "Sign in to confirm you're not a bot https://example.test/?token=secret"
        message = summarize(RuntimeError(raw))
        self.assertIn("登录", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("https", message)
        self.assertIn("风控", summarize(RuntimeError("HTTP Error 429")))
        self.assertIn("地区", summarize(RuntimeError("not available in your country")))
        self.assertTrue(self.env["media_error_stops_batch"](RuntimeError(raw)))
        self.assertFalse(self.env["media_error_stops_batch"](RuntimeError("private video")))


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(re=re, errors=NS(UserCancelled=type("UserCancelled", (Exception,), {})), time=NS(sleep=lambda _: None))
        load_nodes("zeta_bot/setting.py", {"Setting"}, self.env)
        self.setting = object.__new__(self.env["Setting"])
        self.setting._name = "test"
        self.setting._version = "1"
        self.setting._setting = {"name": "old"}
        self.setting._config = [{}, dict(id="name", name="name", description="", input_description="", type="str", dependent=None, regex=None, options=None, value="default")]
        self.setting.save = lambda: None

    def test_quoted_input_is_plain_text_not_eval(self):
        value = 'name "with quotes" and __import__("os")'
        self.env["input"] = lambda _: value
        self.setting.change_setting(1)
        self.assertEqual(self.setting._setting["name"], value)

    def test_reset_rebuilds_defaults_before_initialization(self):
        captured = []
        self.setting.initialize_setting = lambda: captured.append(dict(self.setting._setting))
        self.setting.reset_setting()
        self.assertEqual(captured, [{"config_name": "test", "version": "1", "name": "default"}])


class MemberTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.existing = False
        self.record = {}
        env = dict(decorator=NS(Singleton=lambda cls: cls),
                   os=NS(path=NS(exists=lambda path: self.existing)),
                   utils=NS(json_load=lambda path: copy.deepcopy(self.record),
                            json_save=lambda path, data: self.saved.update({path: copy.deepcopy(data)}),
                            ctime_str=lambda: "now"),
                   lang=NS(system_language="zh-CN"))
        load_nodes("zeta_bot/member.py", {"MemberLibrary"}, env)
        self.lib = object.__new__(env["MemberLibrary"])
        self.lib.root = "fake_members"
        self.lib.hashtag_file = {}
        self.lib.load_hashtag_file = lambda: None
        self.lib.save_hashtag_file = lambda: None

    def test_first_dm_contact_creates_member_without_guild_access(self):
        self.lib.check(NS(user=NS(id=1, name="test"), guild=None))
        self.assertEqual(self.saved["fake_members/1.json"]["guilds"], {})

    def test_existing_string_guild_key_preserves_language(self):
        self.existing = True
        self.record = {"name": "test", "guilds": {"10": {"nickname": "old", "language": "en-US"}}}
        self.lib.check(NS(user=NS(id=1, name="test", nick="new"), guild=NS(id=10)))
        guilds = self.saved["fake_members/1.json"]["guilds"]
        self.assertEqual(guilds, {"10": {"nickname": "new", "language": "en-US"}})


if __name__ == "__main__": unittest.main()
