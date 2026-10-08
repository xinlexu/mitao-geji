"""Owner-scoped song menus, with no dependency on the bot's command module.

Callbacks receive the actual interaction, and ``on_play`` receives the exact
SongChoice that was displayed. Callback results may contain ``message`` (user
feedback), ``status`` (caller metadata), ``close=True`` or ``handoff=True``.
``handoff`` leaves the original message entirely to the caller. True also closes
the menu; False/None keep it open. Permission checks should return a bool.
"""
from __future__ import annotations

import inspect
import math
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import discord


@dataclass(frozen=True)
class SongChoice:
    key: str
    title: str
    subtitle: str = ""
    duration: float | int | None = None
    payload: Any = None


def compact_text(value: Any, limit: int) -> str:
    """A single line suitable for bounded Discord labels and descriptions."""
    if limit <= 0:
        return ""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def normalize_search(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def filter_choices(entries: Iterable[SongChoice], query: str) -> list[SongChoice]:
    terms = normalize_search(query).split()
    return [entry for entry in entries
            if all(term in normalize_search(f"{entry.title or ''} {entry.subtitle or ''}")
                   for term in terms)]


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "时长未知"
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "时长未知"
    if not math.isfinite(value) or value < 0:
        return "时长未知"
    whole = int(value)
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def choice_description(entry: SongChoice) -> str:
    duration = compact_text(format_duration(entry.duration), 32)
    subtitle = compact_text(entry.subtitle, 100 - len(duration) - 3)
    return f"{subtitle} · {duration}" if subtitle else duration


EntryProvider = Callable[[], Sequence[SongChoice] | Awaitable[Sequence[SongChoice]]]
PlayCallback = Callable[[SongChoice, discord.Interaction], Awaitable[Any]]
BatchCallback = Callable[[tuple[SongChoice, ...], discord.Interaction], Awaitable[Any]]
PermissionCheck = Callable[[discord.Interaction, str], Awaitable[bool]]


class SongSearchModal(discord.ui.Modal):
    def __init__(self, picker: "SongPickerView"):
        super().__init__(title="搜索歌名", timeout=600)
        self.picker = picker
        self.keyword = discord.ui.InputText(
            label="歌名或歌手（多个关键词用空格分隔）",
            placeholder="例如：陶喆 爱", value=picker.query or None,
            required=False, max_length=200,
        )
        self.add_item(self.keyword)

    async def callback(self, interaction: discord.Interaction):
        await self.picker.apply_search(interaction, self.keyword.value or "")


class SongPickerView(discord.ui.View):
    """Call ``await prepare()`` before sending, then ``bind_message(message)``.

    ``entry_provider`` may be sync or async. ``on_add_all`` receives every entry,
    including entries outside the active search. ``on_range`` is deliberately
    called without deferring, so it can respond with a Modal; that callback must
    acknowledge promptly. Its Modal must call ``authorize`` on submission too.
    Other callbacks receive an already-deferred interaction.
    """

    def __init__(
        self, owner_id: int, entry_provider: EntryProvider,
        on_play: PlayCallback, check: PermissionCheck | None = None,
        on_add_all: BatchCallback | None = None,
        on_range: BatchCallback | None = None,
        title: str = "选择歌曲", page_size: int = 15, timeout: float = 600,
    ):
        if not 10 <= page_size <= 20:
            raise ValueError("page_size must be between 10 and 20")
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.entry_provider = entry_provider
        self.on_play = on_play
        self.check = check
        self.on_add_all = on_add_all
        self.on_range = on_range
        self.title = compact_text(title, 256) or "选择歌曲"
        self.page_size = page_size
        self.page = 0
        self.query = ""
        self.last_status = ""
        self.last_result: Any = None
        self.message = None
        self._entries: tuple[SongChoice, ...] = ()
        self._filtered: tuple[SongChoice, ...] = ()
        self._tokens: dict[str, SongChoice] = {}
        self._nonce = ""
        self._busy = False
        self._closed = False
        self._handed_off = False

    @property
    def entries(self) -> tuple[SongChoice, ...]:
        return self._entries

    @property
    def busy(self) -> bool:
        return self._busy

    @busy.setter
    def busy(self, value: bool):
        # Advanced-range Modals use the same synchronous reservation as buttons.
        self._busy = bool(value)

    @property
    def closed(self) -> bool:
        return self._closed or self._handed_off or self.is_finished()

    @property
    def filtered_entries(self) -> tuple[SongChoice, ...]:
        return self._filtered

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self._filtered) / self.page_size))

    def bind_message(self, message):
        self.message = message
        return self

    async def prepare(self):
        await self.refresh()
        return self

    async def refresh(self):
        """Refresh data and invalidate all previously displayed option tokens."""
        entries = self.entry_provider()
        if inspect.isawaitable(entries):
            entries = await entries
        result = tuple(entries)
        if any(not isinstance(entry, SongChoice) for entry in result):
            raise TypeError("entry_provider must return SongChoice instances")
        self._entries = result
        self._filtered = tuple(filter_choices(result, self.query))
        self.page = max(0, min(self.page, self.page_count - 1))
        self._rebuild()

    def make_embed(self) -> discord.Embed:
        description = "在下方按歌名选一首，会立即播放；其他歌曲保留。"
        if self.query:
            description += f"\n搜索：{compact_text(self.query, 200)}"
        description += f"\n共 {len(self._entries)} 首"
        if self.query:
            description += f"，匹配 {len(self._filtered)} 首"
        description += f" · 第 {self.page + 1}/{self.page_count} 页"
        if not self._filtered:
            description += "\n没有匹配歌曲，可修改关键词或清除搜索。"
        if self.on_add_all is not None:
            description += "\n“添加整张歌单”会添加全部歌曲，不受搜索条件影响。"
        if self.last_status:
            description += f"\n\n{compact_text(self.last_status, 500)}"
        return discord.Embed(title=self.title, description=description)

    def _button(self, label, callback, *, row=1, disabled=False, style=None):
        button = discord.ui.Button(
            label=label, row=row, disabled=disabled,
            style=style or discord.ButtonStyle.secondary,
        )
        button.callback = callback
        self.add_item(button)

    def _rebuild(self):
        self.clear_items()
        self._nonce = secrets.token_hex(8)
        self._tokens = {}
        start = self.page * self.page_size
        visible = self._filtered[start:start + self.page_size]
        options = []
        for index, entry in enumerate(visible):
            token = f"{self._nonce}:{index}"
            self._tokens[token] = entry
            options.append(discord.SelectOption(
                label=compact_text(entry.title, 100) or "未命名歌曲",
                description=choice_description(entry), value=token,
            ))
        select = discord.ui.Select(
            placeholder="选择歌名，立即播放", row=0, min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="没有匹配歌曲", value="empty")],
            disabled=not options,
        )
        select.callback = self._select_callback
        self.add_item(select)
        self._button("搜索歌名", self._search_callback)
        self._button("清除搜索" if self.query else "全部歌曲", self._clear_callback,
                     disabled=not self.query)
        self._button("上一页", self._previous_callback, disabled=self.page == 0)
        self._button("下一页", self._next_callback, disabled=self.page + 1 >= self.page_count)
        self._button("关闭", self._close_callback, style=discord.ButtonStyle.danger)
        if self.on_add_all is not None:
            self._button("添加整张歌单", self._add_all_callback, row=2,
                         disabled=not self._entries, style=discord.ButtonStyle.primary)
        if self.on_range is not None:
            self._button("高级范围", self._range_callback, row=2, disabled=not self._entries)

    async def _notify(self, interaction, message: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            # An expired interaction cannot prevent queue or menu cleanup.
            pass

    async def authorize(self, interaction, operation: str = "play") -> bool:
        """Recheck the actual actor; usable by a caller's advanced-range Modal."""
        if getattr(getattr(interaction, "user", None), "id", None) != self.owner_id:
            await self._notify(interaction, "这是其他人的选歌界面，请重新打开自己的选歌菜单。")
            return False
        if self._closed or self._handed_off or self.is_finished():
            await self._notify(interaction, "这个选歌界面已关闭，请重新打开。")
            return False
        if self.check is not None:
            try:
                allowed = await self.check(interaction, operation)
            except Exception:
                await self._notify(interaction, "暂时无法检查权限，请稍后重试。")
                return False
            if not allowed:
                await self._notify(interaction, "你目前没有执行此操作的权限。")
                return False
        return True

    async def _begin(self, interaction, operation, *, defer=True) -> bool:
        # Reserve before the first await: concurrent dispatches cannot both act.
        if self._busy:
            await self._notify(interaction, "正在处理上一首歌曲，请稍候。")
            return False
        self._busy = True
        try:
            if defer and not interaction.response.is_done():
                await interaction.response.defer()
            if not await self.authorize(interaction, operation):
                self._busy = False
                return False
            return True
        except BaseException:
            self._busy = False
            raise

    async def _edit(self, interaction=None):
        if self._handed_off:
            return
        message = self.message or getattr(interaction, "message", None)
        if message is None:
            return
        try:
            await message.edit(embed=self.make_embed(), view=self)
        except Exception:
            # Message deletion, permissions or network failure do not undo play.
            pass

    async def _refresh_and_edit(self, interaction):
        if self._handed_off or self.is_finished():
            return
        try:
            await self.refresh()
        except Exception:
            self.last_status = "歌曲列表暂时无法刷新，请稍后重新打开。"
            self._rebuild()
        await self._edit(interaction)

    async def _handle_result(self, result, interaction):
        self.last_result = result
        if isinstance(result, Mapping):
            if result.get("handoff") is True:
                self._handed_off = True
                self.stop()
                return
            if result.get("message"):
                self.last_status = compact_text(result["message"], 500)
            close = result.get("close") is True
        else:
            close = result is True
        # A callback may stop early before awaiting a long import it owns.
        if self.is_finished():
            return
        if close:
            await self._finish(interaction)
        else:
            await self._refresh_and_edit(interaction)

    async def _run_action(self, interaction, callback, *, operation="play", defer=True):
        if not await self._begin(interaction, operation, defer=defer):
            return
        try:
            if operation == "play":
                self.last_status = ""
            result = await callback()
            await self._handle_result(result, interaction)
        except Exception:
            if not self.is_finished() and not self._handed_off:
                self.last_status = "这次操作没有完成，请稍后重试或选择其他歌曲。"
                await self._notify(interaction, self.last_status)
                await self._refresh_and_edit(interaction)
        finally:
            self._busy = False

    async def _select_callback(self, interaction):
        values = (getattr(interaction, "data", None) or {}).get("values", [])
        token = values[0] if isinstance(values, list) and len(values) == 1 else None
        entry = self._tokens.get(token) if isinstance(token, str) else None
        if entry is None:
            await self._notify(interaction, "列表已经更新，请在当前页面重新选择歌曲。")
            return
        await self._run_action(interaction, lambda: self.on_play(entry, interaction))

    async def _search_callback(self, interaction):
        if not await self._begin(interaction, "list", defer=False):
            return
        try:
            await interaction.response.send_modal(SongSearchModal(self))
        except Exception:
            await self._notify(interaction, "暂时无法打开搜索，请稍后重试。")
        finally:
            self._busy = False

    async def apply_search(self, interaction, query: str):
        async def apply():
            self.query = compact_text(query, 200)
            self.page = 0
            self.last_status = ""
        await self._run_action(interaction, apply, operation="list")

    async def _clear_callback(self, interaction):
        await self.apply_search(interaction, "")

    async def _previous_callback(self, interaction):
        async def previous():
            self.page = max(0, self.page - 1)
        await self._run_action(interaction, previous, operation="list")

    async def _next_callback(self, interaction):
        async def following():
            self.page = min(self.page_count - 1, self.page + 1)
        await self._run_action(interaction, following, operation="list")

    async def _add_all_callback(self, interaction):
        await self._run_action(interaction, lambda: self.on_add_all(self._entries, interaction))

    async def _range_callback(self, interaction):
        await self._run_action(interaction, lambda: self.on_range(self._entries, interaction), defer=False)

    async def _finish(self, interaction=None):
        self._closed = True
        self._tokens.clear()
        for child in self.children:
            child.disabled = True
        self.stop()
        await self._edit(interaction)

    async def _close_callback(self, interaction):
        # Owner may always close their own menu, even after losing play rights.
        if self._busy:
            await self._notify(interaction, "正在处理歌曲，请完成后再关闭。")
            return
        self._busy = True
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            if getattr(getattr(interaction, "user", None), "id", None) != self.owner_id:
                await self._notify(interaction, "这是其他人的选歌界面。")
                return
            await self._finish(interaction)
        finally:
            self._busy = False

    async def on_timeout(self):
        if self._handed_off or self._closed:
            return
        self.last_status = "选歌界面已过期，请重新打开。"
        await self._finish()
