"""One ordinary bot-owned playback panel per guild, restored without timers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import json
from types import SimpleNamespace

import discord

from zeta_bot import music_controls


@dataclass
class _PanelState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    guild: object = None
    view: object = None
    registered_id: int | None = None
    message: object = None
    fingerprint: str | None = None
    saved_ids: tuple[int, int] | None = None
    pending_metadata: tuple[int, int] | None = None


class _InvalidPanel(ValueError):
    pass


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _state(runtime, current_guild):
    states = getattr(runtime, "_control_panel_states", None)
    if states is None:
        states = {}
        runtime._control_panel_states = states
    guild_id = _positive_id(current_guild.get_id())
    if guild_id is None:
        raise _InvalidPanel()
    return states.setdefault(guild_id, _PanelState())


async def _log(runtime, current_guild, error):
    try:
        get_name = getattr(current_guild, "get_name", None)
        target = get_name() if callable(get_name) else getattr(current_guild, "name", "播放控制面板")
        await runtime.console.rp(
            f"播放控制面板暂时无法更新（{type(error).__name__}）",
            target,
        )
    except Exception:
        pass


def _stop_view(state):
    if state.view is not None:
        state.view.stop()
    state.view = None
    state.guild = None
    state.registered_id = None
    state.message = None
    state.fingerprint = None
    state.saved_ids = None


def _view(runtime, current_guild, state):
    if state.view is not None and (state.guild is not current_guild or state.view.is_finished()):
        _stop_view(state)
    if state.view is None:
        state.view = music_controls.PersistentMusicView(runtime, current_guild)
        state.guild = current_guild
    return state.view


async def _render(runtime, current_guild, state):
    view = _view(runtime, current_guild, state)
    result = view.refresh_state()
    if inspect.isawaitable(result):
        await result
    embed = view.make_embed()
    fingerprint = json.dumps(
        {"embed": embed.to_dict(), "components": view.to_components()},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return view, embed, fingerprint


def _metadata(current_guild):
    data = current_guild.get_control_panel()
    if not data:
        return None
    if not isinstance(data, dict):
        raise _InvalidPanel()
    channel_id, message_id = _positive_id(data.get("channel_id")), _positive_id(data.get("message_id"))
    if channel_id is None or message_id is None:
        raise _InvalidPanel()
    return channel_id, message_id


def _valid_channel(channel, current_guild):
    return (channel is not None
            and _positive_id(getattr(getattr(channel, "guild", None), "id", None)) == _positive_id(current_guild.get_id())
            and callable(getattr(channel, "send", None)))


def _valid_message(message, channel_id, message_id, runtime, current_guild):
    bot_id = _positive_id(getattr(getattr(runtime.bot, "user", None), "id", None))
    return (bot_id is not None
            and _positive_id(getattr(message, "id", None)) == message_id
            and _positive_id(getattr(getattr(message, "channel", None), "id", None)) == channel_id
            and _valid_channel(getattr(message, "channel", None), current_guild)
            and _positive_id(getattr(getattr(message, "author", None), "id", None)) == bot_id
            and getattr(message, "webhook_id", None) is None)


def _clear(current_guild, state):
    current_guild.set_control_panel()
    state.pending_metadata = None
    _stop_view(state)


def _remember(state, message, ids, fingerprint):
    state.message = message
    state.saved_ids = ids
    state.fingerprint = fingerprint
    # Discord stores the same persistent view when send/edit succeeds.
    state.registered_id = ids[1]


async def _send(runtime, current_guild, state, ctx):
    channel = getattr(ctx, "channel", None)
    if not _valid_channel(channel, current_guild):
        return None
    view, embed, fingerprint = await _render(runtime, current_guild, state)
    message = await channel.send(embed=embed, view=view)
    ids = (_positive_id(channel.id), _positive_id(getattr(message, "id", None)))
    if None in ids or not _valid_message(message, *ids, runtime, current_guild):
        raise _InvalidPanel()
    _remember(state, message, ids, fingerprint)
    # If persistence fails after Discord accepted the message, remember its IDs
    # in memory. A later refresh retries saving, never sends a duplicate panel.
    state.pending_metadata = ids
    current_guild.set_control_panel(channel_id=ids[0], message_id=ids[1])
    state.pending_metadata = None
    return message


async def _update_locked(runtime, current_guild, state, ctx, create, restoring=False):
    if state.pending_metadata is not None:
        ids = state.pending_metadata
        current_guild.set_control_panel(channel_id=ids[0], message_id=ids[1])
        state.pending_metadata = None
    try:
        ids = _metadata(current_guild)
    except _InvalidPanel:
        _clear(current_guild, state)
        ids = None
    if ids is None:
        if state.saved_ids is not None:
            _stop_view(state)
        return await _send(runtime, current_guild, state, ctx) if create else None

    channel_id, message_id = ids
    if ((state.saved_ids is not None and state.saved_ids != ids)
            or (state.registered_id is not None and state.registered_id != message_id)):
        _stop_view(state)
    view, embed, fingerprint = await _render(runtime, current_guild, state)
    if (not create and not restoring and state.saved_ids == ids and state.message is not None
            and state.fingerprint == fingerprint):
        return state.message
    try:
        channel = runtime.bot.get_channel(channel_id)
        if channel is None:
            channel = await runtime.bot.fetch_channel(channel_id)
        if not _valid_channel(channel, current_guild) or not callable(getattr(channel, "fetch_message", None)):
            raise _InvalidPanel()
        message = await channel.fetch_message(message_id)
        if not _valid_message(message, channel_id, message_id, runtime, current_guild):
            raise _InvalidPanel()
        if restoring and state.registered_id != message_id:
            runtime.bot.add_view(view, message_id=message_id)
            state.registered_id = message_id
        if state.saved_ids == ids and state.fingerprint == fingerprint:
            state.message = message
            return message
        updated = await message.edit(embed=embed, view=view)
        _remember(state, updated or message, ids, fingerprint)
        return state.message
    except (discord.NotFound, _InvalidPanel) as error:
        _clear(current_guild, state)
        if isinstance(error, _InvalidPanel):
            await _log(runtime, current_guild, error)
        return await _send(runtime, current_guild, state, ctx) if create else None


async def update(runtime, current_guild, ctx=None, create=False):
    """Update the saved message; only an explicit create call may send one."""
    try:
        state = _state(runtime, current_guild)
        async with state.lock:
            return await _update_locked(runtime, current_guild, state, ctx, create)
    except Exception as error:
        await _log(runtime, current_guild, error)
        return None


async def restore(runtime):
    """Register and refresh saved panels after ready; never send or join voice."""
    for discord_guild in tuple(runtime.bot.guilds):
        current_guild = None
        try:
            await runtime.guild_lib.check_by_guild_obj(discord_guild, runtime.audio_lib_main)
            # GuildLibrary already accepts the interaction-bearing context shape.
            context = SimpleNamespace(interaction=SimpleNamespace(guild=discord_guild))
            current_guild = runtime.guild_lib.get_guild(context)
            if current_guild is None:
                continue
            state = _state(runtime, current_guild)
            async with state.lock:
                await _update_locked(runtime, current_guild, state, None, False, restoring=True)
        except Exception as error:
            await _log(runtime, current_guild or discord_guild, error)
