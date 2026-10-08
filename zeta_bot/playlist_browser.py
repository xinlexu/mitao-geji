"""Connect song-name menus to the existing download and playback lifecycle."""
from __future__ import annotations

import secrets

import discord

from zeta_bot import song_choices, song_picker


def _snapshot_list(value):
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


async def notify(interaction, message):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        # Expired webhooks and deleted messages must not interrupt queue cleanup.
        pass


async def _report_error(runtime, interaction, operation, error):
    try:
        await runtime.console.rp(f"{operation}（{type(error).__name__}）", interaction.guild)
    except Exception:
        pass


async def permission_check(runtime, interaction, operation):
    if interaction.guild is None:
        return False
    ctx = discord.ApplicationContext(runtime.bot, interaction)
    runtime.member_lib.check(ctx)
    return (str(ctx.user.id) == str(runtime.setting.value("owner"))
            or runtime.member_lib.allow(ctx.user.id, operation))


async def ensure_voice(runtime, ctx):
    """Never move an existing music session to a different member's channel."""
    await runtime.guild_lib.check(ctx, runtime.audio_lib_main)
    current_guild = runtime.guild_lib.get_guild(ctx)
    async with current_guild.get_playback_lock():
        channel = getattr(getattr(ctx.user, "voice", None), "channel", None)
        if channel is None:
            return {"status": "voice_required", "message": "请先进入语音频道，再点选歌曲。"}
        voice = ctx.guild.voice_client
        if voice is not None:
            if not isinstance(voice, runtime.lavalink_backend.LavalinkVoiceClient):
                return {"status": "voice_required", "message": "语音连接暂不可用，请重新加入语音频道。"}
            if getattr(voice.channel, "id", None) != channel.id:
                return {"status": "voice_required", "message": "请先进入机器人所在的语音频道，再点选歌曲。"}
        else:
            await runtime.join_callback(ctx, command_call=False)
            voice = ctx.guild.voice_client
        if voice is None or not voice.is_connected():
            return {"status": "voice_required", "message": "语音连接正在准备，请稍后再点一次。"}
    return None


async def play_queue_choice(runtime, choice, interaction):
    ctx = discord.ApplicationContext(runtime.bot, interaction)
    error = await ensure_voice(runtime, ctx)
    if error:
        return error
    return await runtime.play_chosen_audio(ctx, choice.payload, from_queue=True)


async def open_queue_picker(runtime, interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, invisible=False)
    if not await permission_check(runtime, interaction, "list"):
        await notify(interaction, "你目前没有查看播放列表的权限。")
        return
    ctx = discord.ApplicationContext(runtime.bot, interaction)
    await runtime.guild_lib.check(ctx, runtime.audio_lib_main)
    queue = runtime.guild_lib.get_guild(ctx).get_playlist()
    view = song_picker.SongPickerView(
        interaction.user.id, lambda: song_choices.queue_choices(queue),
        lambda choice, clicked: play_queue_choice(runtime, choice, clicked),
        check=lambda clicked, operation: permission_check(runtime, clicked, operation),
        title="在播放列表中找歌",
    )
    await view.prepare()
    message = await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True, wait=True)
    view.bind_message(message)


class QueuePickerActions(discord.ui.View):
    """Keep a direct route to song names on the completed import message."""
    def __init__(self, runtime):
        super().__init__(timeout=600)
        self.runtime = runtime
        button = discord.ui.Button(label="选歌 / 搜索歌名", style=discord.ButtonStyle.primary)
        button.callback = self.open_picker
        self.add_item(button)

    async def open_picker(self, interaction):
        try:
            await open_queue_picker(self.runtime, interaction)
        except Exception as error:
            await _report_error(self.runtime, interaction, "打开歌名选择失败", error)
            await notify(interaction, "暂时无法打开选歌菜单，请稍后重试。")


def add_queue_selector(runtime, menu):
    """The visible page carries object references, never mutable queue indexes."""
    for child in tuple(menu.children):
        if getattr(child, "custom_id", None) == "queue_play_choice":
            menu.remove_item(child)
    entries = song_choices.queue_choices(menu.playlist)
    visible = entries[menu.page_num * 10:menu.page_num * 10 + 10]
    nonce = secrets.token_hex(8)
    menu._choice_version = nonce
    tokens = {f"{nonce}:{index}": entry for index, entry in enumerate(visible)}
    options = [discord.SelectOption(
        label=song_picker.compact_text(entry.title, 100) or "未命名歌曲",
        description=song_picker.choice_description(entry), value=token,
    ) for token, entry in tokens.items()]
    select = discord.ui.Select(
        custom_id="queue_play_choice", placeholder="点选本页歌名，立即播放（其余歌曲保留）",
        options=options or [discord.SelectOption(label="播放列表为空", value="empty")],
        disabled=not options, min_values=1, max_values=1, row=0,
    )

    async def choose(interaction):
        if getattr(menu, "_picker_busy", False):
            await notify(interaction, "正在处理上一首歌曲，请稍候。")
            return
        values = (interaction.data or {}).get("values", [])
        token = values[0] if isinstance(values, list) and len(values) == 1 else None
        entry = tokens.get(token) if isinstance(token, str) else None
        if entry is None or menu._choice_version != nonce:
            await notify(interaction, "播放列表已更新，请在当前页面重新选择歌曲。")
            return
        menu._picker_busy = True
        try:
            await interaction.response.defer()
            # Also check here: direct callbacks and delayed dispatches use the actor.
            if not await permission_check(runtime, interaction, "play"):
                await notify(interaction, "你目前没有播放歌曲的权限。")
                return
            result = await play_queue_choice(runtime, entry, interaction)
            await notify(interaction, result["message"])
            await menu._deferred_refresh_menu(interaction)
        except Exception as error:
            await _report_error(runtime, interaction, "按歌名切换失败", error)
            await notify(interaction, "这次切换没有完成，请刷新列表后重试。")
        finally:
            menu._picker_busy = False

    select.callback = choose
    menu.add_item(select)


class ImportRangeModal(discord.ui.Modal):
    def __init__(self, browser):
        super().__init__(title="按范围添加歌曲", timeout=600)
        self.browser = browser
        total = browser.engine._entry_count()
        self.selection = discord.ui.InputText(
            label=f"共 {total} 项，每批最多 {browser.runtime.PLAYLIST_IMPORT_LIMIT} 首"[:45],
            placeholder="例如：1-100,125,150-200",
            max_length=browser.runtime.PLAYLIST_SELECTION_TEXT_LIMIT,
        )
        self.add_item(self.selection)

    async def callback(self, interaction):
        view = self.browser.view
        if view.busy:
            await notify(interaction, "正在处理歌曲，请稍候再提交。")
            return
        view.busy = True
        try:
            await interaction.response.defer()
            if not await view.authorize(interaction, "play"):
                return
            try:
                text = (self.selection.value or "").replace("，", ",").replace(" ", "")
                selected = self.browser.runtime.parse_episode_selection(text, self.browser.engine._entry_count())
            except ValueError as error:
                await notify(interaction, str(error))
                return
            result = await self.browser.start_import(selected, interaction)
            if not result.get("handoff"):
                await notify(interaction, result.get("message", "暂时无法添加，请稍后重试。"))
        except Exception as error:
            await _report_error(self.browser.runtime, interaction, "按范围添加失败", error)
            await notify(interaction, "本次添加未能完成，请稍后重试；已经入队的歌曲会保留。")
        finally:
            view.busy = False


class ImportedPlaylistBrowser:
    """The original import engine still owns retries, progress, limits and cancel."""
    def __init__(self, runtime, ctx, source, info_dict, list_type=None, list_title=None):
        self.runtime = runtime
        self.ctx = ctx
        # Freeze the flat entry order once: filtered choices retain original indexes.
        self.info_dict = dict(info_dict)
        if source in ("youtube_playlist", "netease_playlist"):
            self.info_dict["entries"] = _snapshot_list(info_dict.get("entries"))
        elif source == "bilibili_p":
            self.info_dict["pages"] = _snapshot_list(info_dict.get("pages"))
        elif source == "bilibili_collection":
            season = info_dict.get("ugc_season")
            season = dict(season) if isinstance(season, dict) else {}
            sections = _snapshot_list(season.get("sections"))
            first = dict(sections[0]) if sections and isinstance(sections[0], dict) else {}
            first["episodes"] = _snapshot_list(first.get("episodes"))
            season["sections"] = [first, *sections[1:]]
            self.info_dict["ugc_season"] = season
        self.choices = song_choices.imported_choices(source, self.info_dict)
        self.engine = runtime.EpisodeSelectMenu(
            ctx, source, self.info_dict, [""], list_type, list_title, timeout=600,
        )
        self.engine._name_picker_origin = True
        self.view = song_picker.SongPickerView(
            ctx.user.id, lambda: self.choices, self.play,
            check=lambda clicked, operation: permission_check(runtime, clicked, operation),
            on_add_all=self.add_all, on_range=self.open_range,
            title=list_title or list_type or "选择歌单中的歌曲",
        )

    async def init_eos(self, response=None, silent=False):
        await self.view.prepare()
        message = await self.runtime.eos(self.ctx, response, embed=self.view.make_embed(), view=self.view, silent=silent)
        self.view.bind_message(message)

    async def init_respond(self, ephemeral=False, silent=False):
        await self.view.prepare()
        message = await self.ctx.respond(embed=self.view.make_embed(), view=self.view, ephemeral=ephemeral, silent=silent)
        if isinstance(message, discord.Interaction):
            message = await message.original_response()
        self.view.bind_message(message)

    async def play(self, choice, interaction):
        ctx = discord.ApplicationContext(self.runtime.bot, interaction)
        error = await ensure_voice(self.runtime, ctx)
        if error:
            return error
        self.engine.ctx = ctx
        result = await self.engine._download_selected(choice.payload)
        target = result.get("audio")
        if target is None:
            return {"status": "download_error", "message": result.get("message") or "此曲暂时无法播放，请选择其他歌曲。"}
        # No await between receiving the lease and transferring it to its owner.
        return await self.runtime.play_chosen_audio(ctx, target, pending_lease=True)

    async def add_all(self, entries, interaction):
        if not self.choices:
            return {"status": "empty", "message": "歌单中暂时没有可添加的曲目。"}
        try:
            selected = self.runtime.parse_episode_selection(
                f"1-{self.engine._entry_count()}", self.engine._entry_count(),
            )
        except ValueError:
            return {"status": "too_many", "message": f"这张歌单每批最多添加 {self.runtime.PLAYLIST_IMPORT_LIMIT} 首，可用“高级范围”分批添加；单首仍可直接点歌名。"}
        return await self.start_import(selected, interaction)

    async def open_range(self, entries, interaction):
        await interaction.response.send_modal(ImportRangeModal(self))

    async def start_import(self, selected, interaction):
        ctx = discord.ApplicationContext(self.runtime.bot, interaction)
        error = await ensure_voice(self.runtime, ctx)
        if error:
            return error
        if interaction.guild.id in self.runtime.playlist_imports:
            return {"status": "busy", "message": "本服务器已有歌单正在添加，请等待完成或先停止原任务。"}
        self.engine.ctx = ctx
        self.engine.original_msg = self.view.message
        self.view.stop()
        try:
            await self.engine._begin_import(selected, interaction)
        except Exception as error:
            await _report_error(self.runtime, interaction, "歌单添加中断", error)
            await notify(interaction, "本次添加已中断，已经入队的歌曲会保留；可点“选歌 / 搜索歌名”继续播放，或重新打开歌单再试。")
            # The importer owns the message now. Preserve its counts and recent
            # results while replacing any stopped controls with a usable route.
            message = self.engine.original_msg or self.view.message
            if message is not None:
                try:
                    await message.edit(view=QueuePickerActions(self.runtime))
                except Exception:
                    pass
            return {"handoff": True, "status": "import_error"}
        return {"handoff": True}
