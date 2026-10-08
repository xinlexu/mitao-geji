"""Persistent, shared music controls; message ownership lives in control_panel."""
from __future__ import annotations

import math

import discord

from zeta_bot import playlist_browser
from zeta_bot.song_picker import compact_text, format_duration


MODE_NAMES = ("顺序播放", "单曲循环", "列表循环", "随机播放", "随机循环")


def _same_id(first, second):
    return first is not None and second is not None and str(first) == str(second)


def _volume(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 100.0
    return max(0.0, min(200.0, number)) if math.isfinite(number) else 100.0


class PersistentMusicView(discord.ui.View):
    """A persistent View registered by guild/message ID after bot startup."""

    def __init__(self, runtime, current_guild):
        super().__init__(timeout=None)
        self.runtime = runtime
        self.current_guild = current_guild
        self._busy = False
        self.buttons = {}
        for action, label, row, style in (
            ("toggle", "暂停", 0, discord.ButtonStyle.primary),
            ("next", "下一首", 0, discord.ButtonStyle.secondary),
            ("search", "搜索选歌", 0, discord.ButtonStyle.secondary),
            ("volume_down", "音量 -10", 1, discord.ButtonStyle.secondary),
            ("volume_up", "音量 +10", 1, discord.ButtonStyle.secondary),
            ("mode", "循环模式", 1, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, row=row, style=style,
                                       custom_id=f"zeta_music_{action}")

            async def callback(interaction, chosen_action=action):
                await self.dispatch_action(interaction, chosen_action)

            button.callback = callback
            self.buttons[action] = button
            self.add_item(button)
        self.refresh_state()

    def _discord_guild(self):
        guild = self.runtime.bot.get_guild(self.current_guild.get_id())
        return guild or getattr(self.current_guild, "_guild", None)

    def _voice(self):
        guild = self._discord_guild()
        return getattr(guild, "voice_client", None)

    def _valid_voice(self, voice):
        return (isinstance(voice, self.runtime.lavalink_backend.LavalinkVoiceClient)
                and voice.is_connected())

    def _snapshot(self):
        voice = self._voice()
        connected = self._valid_voice(voice)
        active = connected and self.runtime._voice_has_track(voice)
        stopping = connected and voice.is_stopping()
        paused = active and voice.is_paused()
        queue = self.current_guild.get_playlist()
        mode = self.current_guild.get_play_mode()
        if mode not in range(len(MODE_NAMES)):
            mode = 0
        return {
            "voice": voice, "connected": connected, "active": active,
            "stopping": stopping, "paused": paused, "count": len(queue),
            "head": queue.get_audio(0), "mode": mode,
            "volume": _volume(self.current_guild.get_voice_volume()),
        }

    def refresh_state(self):
        state = self._snapshot()
        self.buttons["toggle"].label = (
            "正在切歌…" if state["stopping"] else
            "继续" if state["paused"] or not state["active"] else "暂停"
        )
        self.buttons["toggle"].disabled = state["stopping"]
        self.buttons["next"].disabled = state["stopping"] or not state["active"]
        self.buttons["volume_down"].disabled = state["volume"] <= 0
        self.buttons["volume_up"].disabled = state["volume"] >= 200
        self.buttons["mode"].label = f"模式：{MODE_NAMES[state['mode']]}"

    def make_embed(self):
        state = self._snapshot()
        if not state["connected"]:
            status = "未连接语音 · 请先加入语音频道并点歌"
        elif state["stopping"]:
            status = "正在切换歌曲"
        elif state["paused"]:
            status = "已暂停 · 点“继续”恢复播放"
        elif state["active"]:
            status = "正在播放"
        elif state["count"]:
            status = "等待播放 · 点“继续”播放队列"
        else:
            status = "播放列表为空 · 点歌后可在此控制"
        embed = discord.Embed(title="音乐控制面板", description=status)
        head = state["head"]
        title = compact_text(head.get_title(), 600) if head is not None else "暂无歌曲"
        duration = compact_text(format_duration(head.get_duration()), 50) if head is not None else "—"
        embed.add_field(name="当前歌曲" if state["active"] else "待播歌曲",
                        value=title or "未命名歌曲", inline=False)
        embed.add_field(name="时长", value=duration, inline=True)
        count_label = f"{state['count']} 首" + ("（含当前歌曲）" if state["active"] else "")
        embed.add_field(name="队列", value=count_label, inline=True)
        embed.add_field(name="音量", value=f"{state['volume']:g}%", inline=True)
        embed.add_field(name="播放模式", value=MODE_NAMES[state["mode"]], inline=True)
        channel = getattr(state["voice"], "channel", None)
        channel_name = compact_text(getattr(channel, "name", None), 100) or "语音频道"
        embed.add_field(name="语音", value=channel_name if state["connected"] else "未连接", inline=True)
        embed.set_footer(text="同一语音频道内可使用；搜索选歌会打开个人菜单。")
        return embed

    async def _notify(self, interaction, message):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

    async def _log_error(self, interaction, error):
        try:
            await self.runtime.console.rp(
                f"音乐控制面板操作失败（{type(error).__name__}）", getattr(interaction, "guild", None),
            )
        except Exception:
            pass

    def _panel_matches(self, interaction):
        guild = getattr(interaction, "guild", None)
        if guild is None or not _same_id(guild.id, self.current_guild.get_id()):
            return False
        panel = self.current_guild.get_control_panel() or {}
        return _same_id(getattr(getattr(interaction, "message", None), "id", None),
                        panel.get("message_id"))

    def _allowed(self, ctx, operation):
        return (str(ctx.user.id) == str(self.runtime.setting.value("owner"))
                or bool(self.runtime.member_lib.allow(ctx.user.id, operation)))

    def _voice_unchanged(self, ctx, voice, generation):
        return (ctx.guild.voice_client is voice and voice.is_connected()
                and voice._generation == generation)

    async def _locked_action(self, interaction, action):
        """Return (private feedback, open search); caller holds playback lock."""
        ctx = discord.ApplicationContext(self.runtime.bot, interaction)
        self.runtime.member_lib.check(ctx)
        voice = ctx.guild.voice_client
        if not self._valid_voice(voice):
            return "机器人尚未连接语音，请先加入语音频道并点歌。", False
        actor_channel = getattr(getattr(ctx.user, "voice", None), "channel", None)
        if not _same_id(getattr(actor_channel, "id", None), getattr(voice.channel, "id", None)):
            return "请先进入机器人所在的语音频道，再使用控制面板。", False
        active = self.runtime._voice_has_track(voice)
        paused = voice.is_paused()
        operation = ("pause" if active and not paused else "resume") if action == "toggle" else {
            "next": "skip", "search": "list", "volume_down": "volume",
            "volume_up": "volume", "mode": "skip",
        }.get(action)
        if operation is None or not self._allowed(ctx, operation):
            return "你目前没有执行此操作的权限。", False
        if action == "search":
            return None, True
        if voice.is_stopping():
            return "正在切换歌曲，请稍候再操作。", False
        if action == "toggle":
            if active:
                generation = voice._generation
                await voice._patch_player({"paused": not paused})
                if (not self._voice_unchanged(ctx, voice, generation)
                        or not self.runtime._voice_has_track(voice) or voice.is_stopping()):
                    return "歌曲状态刚刚变化，请查看面板后再操作。", False
                voice._paused = not paused
            else:
                head = self.current_guild.get_playlist().get_audio(0)
                if head is None:
                    return "播放列表为空，请先点歌。", False
                if not self._allowed(ctx, "play"):
                    return "你目前没有播放歌曲的权限。", False
                await self.runtime.play_audio(ctx, head)
        elif action == "next":
            if not active:
                return "目前没有正在播放的歌曲，可点“继续”播放队列。", False
            # Original play_next owns consumption, history and cache release.
            voice.stop()
            return "正在切换到下一首。", False
        elif action in ("volume_down", "volume_up"):
            delta = -10 if action == "volume_down" else 10
            volume = max(0.0, min(200.0, _volume(self.current_guild.get_voice_volume()) + delta))
            generation = voice._generation
            await voice._patch_player({"volume": round(volume)})
            if not self._voice_unchanged(ctx, voice, generation):
                return "语音状态刚刚变化，请稍后再调整音量。", False
            # source.volume reads this same ratio; writing its setter would
            # schedule a second, unconfirmed PATCH, including while paused.
            voice._volume_ratio = volume / 100.0
            self.current_guild.set_voice_volume(volume)
        elif action == "mode":
            mode = (self.current_guild.get_play_mode() + 1) % len(MODE_NAMES)
            if not self.current_guild.set_play_mode(mode):
                return "播放模式暂时无法修改，请稍后重试。", False
        return None, False

    async def dispatch_action(self, interaction, action):
        # Reserve synchronously, including while Discord ACK or the remote PATCH
        # is pending, so a double-click cannot queue an immediate inverse toggle.
        if self._busy:
            await self._notify(interaction, "正在处理上一项操作，请稍候。")
            return
        self._busy = True
        try:
            await self._dispatch_action(interaction, action)
        finally:
            self._busy = False

    async def _dispatch_action(self, interaction, action):
        valid_panel = False
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            async with self.current_guild.get_playback_lock():
                if not self._panel_matches(interaction):
                    feedback, search = "这个控制面板已更新，请使用频道中的最新面板。", False
                else:
                    valid_panel = True
                    feedback, search = await self._locked_action(interaction, action)
            if feedback:
                await self._notify(interaction, feedback)
            if search:
                await playlist_browser.open_queue_picker(self.runtime, interaction)
        except Exception as error:
            await self._log_error(interaction, error)
            await self._notify(interaction, "这次操作未能完成，请稍后重试。")
        # Never hold the playback lock over the independent panel-message lock.
        # CancelledError bypasses this path and remains visible to task cleanup.
        if valid_panel:
            try:
                self.refresh_state()
                await self.runtime.refresh_control_panel(self.current_guild)
            except Exception as error:
                await self._log_error(interaction, error)
                await self._notify(interaction, "面板暂时无法刷新，请稍后再试。")
