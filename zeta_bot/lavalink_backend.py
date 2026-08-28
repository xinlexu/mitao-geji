from __future__ import annotations

import asyncio
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import discord

APP_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = APP_ROOT / "logs" / "lavalink-backend.log"
BACKEND_VERSION = "2026-08-28.1"

LAVALINK_HOST = os.environ.get("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.environ.get("LAVALINK_PASSWORD", "")
NODE_READY_TIMEOUT_SECONDS = float(os.environ.get("LAVALINK_NODE_READY_TIMEOUT", "45"))
VOICE_READY_TIMEOUT_SECONDS = float(os.environ.get("LAVALINK_VOICE_READY_TIMEOUT", "45"))


def _build_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("zeta.lavalink_backend")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s "
                "[%(threadName)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    return logger


log = _build_logger()


class LavalinkRequestError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"Lavalink {method} {path} failed: HTTP {status}: {body[:1000]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class LavalinkManager:
    def __init__(self, bot: discord.Client) -> None:
        if bot.user is None:
            raise RuntimeError("Discord bot user is not ready")
        if not LAVALINK_PASSWORD:
            raise RuntimeError("LAVALINK_PASSWORD is missing from the service environment")

        self.bot = bot
        self.base_url = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"
        self.ws_url = f"ws://{LAVALINK_HOST}:{LAVALINK_PORT}/v4/websocket"
        self.session_id: Optional[str] = None
        self.ready = asyncio.Event()
        self._closing = False
        self._http: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task[None]] = None
        self._voice_clients: dict[int, LavalinkVoiceClient] = {}
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._http is None or self._http.closed:
                timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
                connector = aiohttp.TCPConnector(
                    force_close=False,
                    enable_cleanup_closed=True,
                    limit=20,
                )
                self._http = aiohttp.ClientSession(timeout=timeout, connector=connector)

            if self._ws_task is None or self._ws_task.done():
                self._closing = False
                self._ws_task = self.bot.loop.create_task(
                    self._websocket_loop(),
                    name="zeta-lavalink-websocket",
                )

        await self.wait_until_ready(NODE_READY_TIMEOUT_SECONDS)

    async def wait_until_ready(self, timeout: float) -> None:
        await asyncio.wait_for(self.ready.wait(), timeout=timeout)

    async def close(self) -> None:
        self._closing = True
        self.ready.clear()
        self.session_id = None

        for voice_client in list(self._voice_clients.values()):
            try:
                await voice_client._on_manager_closing()
            except Exception:
                log.exception("Failed closing voice client guild=%s", voice_client.guild_id)

        task = self._ws_task
        self._ws_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    def register_voice_client(self, voice_client: "LavalinkVoiceClient") -> None:
        self._voice_clients[voice_client.guild_id] = voice_client

    def unregister_voice_client(self, guild_id: int) -> None:
        self._voice_clients.pop(int(guild_id), None)

    async def load_local_track(self, path: Path) -> dict[str, Any]:
        payload = await self.request(
            "GET",
            "/v4/loadtracks",
            params={"identifier": str(path)},
        )

        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected Lavalink load response: {payload!r}")

        load_type = str(payload.get("loadType", ""))
        data = payload.get("data")

        if load_type == "track" and isinstance(data, dict):
            track = data
        elif load_type == "search" and isinstance(data, list) and data:
            track = data[0]
        elif load_type == "playlist" and isinstance(data, dict):
            tracks = data.get("tracks")
            track = tracks[0] if isinstance(tracks, list) and tracks else None
        elif load_type == "error":
            raise RuntimeError(f"Lavalink failed loading {path}: {data!r}")
        else:
            track = None

        if not isinstance(track, dict) or not track.get("encoded"):
            raise RuntimeError(
                f"Lavalink returned no playable local track for {path}; "
                f"loadType={load_type!r}, data={data!r}"
            )

        return track

    async def update_player(
        self,
        guild_id: int,
        payload: dict[str, Any],
        *,
        no_replace: bool = False,
    ) -> Any:
        session_id = self.session_id
        if session_id is None:
            raise RuntimeError("Lavalink node session is not ready")

        return await self.request(
            "PATCH",
            f"/v4/sessions/{session_id}/players/{int(guild_id)}",
            params={"noReplace": "true" if no_replace else "false"},
            json_body=payload,
        )

    async def get_player(self, guild_id: int) -> Optional[dict[str, Any]]:
        session_id = self.session_id
        if session_id is None:
            return None

        try:
            payload = await self.request(
                "GET",
                f"/v4/sessions/{session_id}/players/{int(guild_id)}",
            )
        except LavalinkRequestError as exc:
            if exc.status == 404:
                return None
            raise

        return payload if isinstance(payload, dict) else None

    async def destroy_player(self, guild_id: int) -> None:
        session_id = self.session_id
        if session_id is None:
            return

        try:
            await self.request(
                "DELETE",
                f"/v4/sessions/{session_id}/players/{int(guild_id)}",
            )
        except LavalinkRequestError as exc:
            if exc.status != 404:
                raise

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        if self._http is None or self._http.closed:
            raise RuntimeError("Lavalink HTTP session is closed")

        headers = {"Authorization": LAVALINK_PASSWORD}
        url = self.base_url + path

        async with self._http.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
        ) as response:
            text = await response.text()

            if response.status == 204:
                return None
            if response.status < 200 or response.status >= 300:
                raise LavalinkRequestError(method, path, response.status, text)
            if not text:
                return None

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def _websocket_loop(self) -> None:
        delay = 1.0

        while not self._closing:
            try:
                await self._run_one_websocket()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ready.clear()
                self.session_id = None
                log.exception("Lavalink websocket disconnected; retrying in %.1fs", delay)
                for voice_client in list(self._voice_clients.values()):
                    voice_client._on_node_unavailable()
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)

    async def _run_one_websocket(self) -> None:
        if self._http is None or self._http.closed:
            raise RuntimeError("Lavalink HTTP session is closed")
        if self.bot.user is None:
            raise RuntimeError("Discord bot user is not ready")

        headers = {
            "Authorization": LAVALINK_PASSWORD,
            "User-Id": str(self.bot.user.id),
            "Client-Name": f"Zeta-Lavalink-Backend/{BACKEND_VERSION}",
        }

        log.info("Connecting Lavalink websocket url=%s", self.ws_url)
        async with self._http.ws_connect(
            self.ws_url,
            headers=headers,
            heartbeat=20.0,
            autoping=True,
        ) as websocket:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    await self._handle_websocket_payload(payload)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise RuntimeError(
                        f"Lavalink websocket closed type={message.type} "
                        f"exception={websocket.exception()!r}"
                    )

        if not self._closing:
            raise RuntimeError("Lavalink websocket ended unexpectedly")

    async def _handle_websocket_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")

        if op == "ready":
            self.session_id = str(payload["sessionId"])
            self.ready.set()
            log.info(
                "Lavalink ready session=%s resumed=%s",
                self.session_id,
                payload.get("resumed"),
            )
            for voice_client in list(self._voice_clients.values()):
                self.bot.loop.create_task(
                    voice_client._on_node_ready(),
                    name=f"zeta-lavalink-node-ready-{voice_client.guild_id}",
                )
            return

        if op == "playerUpdate":
            guild_id = int(payload["guildId"])
            voice_client = self._voice_clients.get(guild_id)
            if voice_client is not None:
                await voice_client._handle_player_update(payload.get("state") or {})
            return

        if op == "event":
            guild_id = int(payload["guildId"])
            voice_client = self._voice_clients.get(guild_id)
            if voice_client is not None:
                await voice_client._handle_lavalink_event(payload)
            return

        if op == "stats":
            return

        log.warning("Unknown Lavalink websocket op=%r payload=%r", op, payload)


class _VolumeProxy:
    def __init__(self, voice_client: "LavalinkVoiceClient") -> None:
        self._voice_client = voice_client

    @property
    def volume(self) -> float:
        return self._voice_client._volume_ratio

    @volume.setter
    def volume(self, value: float) -> None:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            raise TypeError("volume must be numeric") from None

        self._voice_client._set_volume_ratio(ratio)


class LavalinkVoiceClient(discord.VoiceProtocol):
    """Pycord VoiceProtocol whose audio connection is owned by Lavalink."""

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self.guild_id = int(channel.guild.id)
        self.manager: Optional[LavalinkManager] = None
        self._destroyed = False
        self._disconnecting = False
        self._voice_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self._playing = False
        self._paused = False
        self._volume_ratio = 1.0
        self._generation = 0
        self._active_encoded_track: Optional[str] = None
        self._active_audio_path: Optional[Path] = None
        self._active_ctx: Any = None
        self._after_callback: Optional[Callable[[Any], Awaitable[None]]] = None
        self._voice_state: dict[str, str] = {}
        self._last_position_ms = 0
        self._resume_after_node_ready = False
        self._resume_position_ms = 0
        self.source = _VolumeProxy(self)

    @property
    def guild(self) -> discord.Guild:
        return self.channel.guild

    async def connect(self, *, timeout: float, reconnect: bool) -> None:
        self._destroyed = False
        self._disconnecting = False
        self._voice_ready.clear()

        try:
            self.manager = await initialize(self.client)
            self.manager.register_voice_client(self)

            await self.guild.change_voice_state(
                channel=self.channel,
                self_mute=False,
                self_deaf=True,
            )

            await self._wait_until_voice_ready(
                min(float(timeout), VOICE_READY_TIMEOUT_SECONDS)
            )
        except BaseException:
            # Pycord only guarantees automatic cleanup for a timeout raised by
            # VoiceProtocol.connect(). Clean up every failed path here so the
            # guild is never left with a cached, unusable voice client.
            self._disconnecting = True
            self._voice_ready.clear()
            self._playing = False
            self._paused = False
            self._clear_active_callback()

            if self.manager is not None:
                self.manager.unregister_voice_client(self.guild_id)
                try:
                    await self.manager.destroy_player(self.guild_id)
                except Exception:
                    log.exception(
                        "Failed destroying player after connect failure guild=%s",
                        self.guild_id,
                    )

            try:
                await self.guild.change_voice_state(channel=None)
            except Exception:
                log.exception(
                    "Failed clearing Discord voice state after connect failure guild=%s",
                    self.guild_id,
                )

            self._voice_state.clear()
            self.cleanup()
            self._destroyed = True
            self._disconnecting = False
            raise

        log.info(
            "Voice connected guild=%s channel=%s",
            self.guild_id,
            getattr(self.channel, "id", None),
        )

    async def on_voice_server_update(self, data: Any) -> None:
        endpoint = _field(data, "endpoint")
        token = _field(data, "token")

        if endpoint is None or token is None:
            self._voice_state.pop("endpoint", None)
            self._voice_state.pop("token", None)
            self._voice_ready.clear()
            return

        self._voice_state["endpoint"] = str(endpoint)
        self._voice_state["token"] = str(token)
        await self._dispatch_voice_state()

    async def on_voice_state_update(self, data: Any) -> None:
        channel_id = _field(data, "channel_id")
        session_id = _field(data, "session_id")

        if channel_id is None:
            self._voice_state.clear()
            self._voice_ready.clear()
            self._generation += 1
            self._playing = False
            self._paused = False
            self._clear_active_callback()
            if not self._disconnecting:
                if self.manager is not None:
                    try:
                        await self.manager.destroy_player(self.guild_id)
                    except Exception:
                        log.exception(
                            "Failed destroying externally disconnected player guild=%s",
                            self.guild_id,
                        )
                    self.manager.unregister_voice_client(self.guild_id)
                self.cleanup()
                self._destroyed = True
            return

        channel_id_int = int(channel_id)
        new_channel = self.client.get_channel(channel_id_int)
        if new_channel is not None:
            self.channel = new_channel

        if session_id is None:
            self._voice_state.pop("sessionId", None)
            self._voice_ready.clear()
            return

        self._voice_state["sessionId"] = str(session_id)
        self._voice_state["channelId"] = str(channel_id)
        await self._dispatch_voice_state()

    async def disconnect(self, *, force: bool = False) -> None:
        if self._disconnecting or self._destroyed:
            return

        self._disconnecting = True
        self._generation += 1
        self._playing = False
        self._paused = False
        self._clear_active_callback()

        try:
            await self.guild.change_voice_state(channel=None)

            if self.manager is not None:
                try:
                    await self.manager.destroy_player(self.guild_id)
                except Exception:
                    log.exception("Failed destroying Lavalink player guild=%s", self.guild_id)
                self.manager.unregister_voice_client(self.guild_id)
        finally:
            self._voice_state.clear()
            self._voice_ready.clear()
            self.cleanup()
            self._disconnecting = False
            self._destroyed = True

        log.info("Voice disconnected guild=%s", self.guild_id)

    async def move_to(self, channel: discord.abc.Connectable, *, timeout: float = 30.0) -> None:
        if channel is None:
            await self.disconnect(force=True)
            return
        if getattr(self.channel, "id", None) == channel.id:
            return

        self.channel = channel
        self._voice_ready.clear()
        await self.guild.change_voice_state(
            channel=channel,
            self_mute=False,
            self_deaf=True,
        )
        await self._wait_until_voice_ready(float(timeout))

    def is_connected(self) -> bool:
        return not self._destroyed and self._voice_ready.is_set()

    def is_playing(self) -> bool:
        return self._playing and not self._paused

    def is_paused(self) -> bool:
        return self._playing and self._paused

    def pause(self) -> None:
        if not self._playing or self._paused:
            return
        self._paused = True
        self._spawn(self._patch_player({"paused": True}), "pause")

    def resume(self) -> None:
        if not self._playing or not self._paused:
            return
        self._paused = False
        self._spawn(self._patch_player({"paused": False}), "resume")

    def stop(self) -> None:
        if not self._playing and self._after_callback is None:
            return

        expected_generation = self._generation
        preserve_pause = self._paused
        self._playing = False
        self._spawn(
            self._stop_and_advance(expected_generation, preserve_pause),
            "stop-and-advance",
        )

    def _set_volume_ratio(self, ratio: float) -> None:
        self._volume_ratio = max(0.0, min(2.0, ratio))
        if self.manager is not None and self.manager.ready.is_set():
            self._spawn(
                self._patch_player({"volume": round(self._volume_ratio * 100)}),
                "volume",
            )

    async def play_zeta_audio(
        self,
        target_audio: Any,
        *,
        ctx: Any,
        after: Callable[[Any], Awaitable[None]],
        volume_percent: float,
    ) -> None:
        if not self.is_connected() or self.manager is None:
            raise RuntimeError("Lavalink voice connection is not ready")

        raw_path = Path(target_audio.get_path())
        audio_path = raw_path if raw_path.is_absolute() else APP_ROOT / raw_path
        audio_path = audio_path.resolve()

        if not audio_path.is_file():
            raise FileNotFoundError(f"Audio file does not exist: {audio_path}")
        if not os.access(audio_path, os.R_OK):
            raise PermissionError(f"Audio file is not readable: {audio_path}")

        track = await self.manager.load_local_track(audio_path)
        encoded = str(track["encoded"])

        self._generation += 1
        self._active_encoded_track = encoded
        self._active_audio_path = audio_path
        self._active_ctx = ctx
        self._after_callback = after
        self._playing = True
        self._last_position_ms = 0
        self._volume_ratio = max(0.0, min(2.0, float(volume_percent) / 100.0))

        try:
            await self._patch_player(
                {
                    "track": {"encoded": encoded},
                    "volume": round(self._volume_ratio * 100),
                    "paused": self._paused,
                }
            )
        except Exception:
            self._playing = False
            self._clear_active_callback()
            raise

        log.info(
            "Playback requested guild=%s title=%r path=%s generation=%s volume=%s paused=%s",
            self.guild_id,
            target_audio.get_title(),
            audio_path,
            self._generation,
            round(self._volume_ratio * 100),
            self._paused,
        )

    async def _dispatch_voice_state(self) -> None:
        if self.manager is None:
            return
        required = {"token", "endpoint", "sessionId", "channelId"}
        if set(self._voice_state) != required:
            return
        if not self.manager.ready.is_set():
            return

        self._voice_ready.clear()
        response = await self.manager.update_player(
            self.guild_id,
            {"voice": dict(self._voice_state)},
        )
        if isinstance(response, dict):
            state = response.get("state") or {}
            if bool(state.get("connected")):
                self._voice_ready.set()
        log.info(
            "Voice state dispatched guild=%s channel=%s endpoint=%r",
            self.guild_id,
            self._voice_state.get("channelId"),
            self._voice_state.get("endpoint"),
        )

    async def _wait_until_voice_ready(self, timeout: float) -> None:
        if self.manager is None:
            raise RuntimeError("Lavalink manager is unavailable")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)

        while not self._voice_ready.is_set():
            player = await self.manager.get_player(self.guild_id)
            if isinstance(player, dict):
                state = player.get("state") or {}
                if bool(state.get("connected")):
                    self._voice_ready.set()
                    break

            if loop.time() >= deadline:
                raise asyncio.TimeoutError(
                    f"Lavalink voice connection did not become ready within {timeout:.0f}s"
                )
            await asyncio.sleep(0.5)

    async def _patch_player(self, payload: dict[str, Any]) -> Any:
        if self.manager is None:
            raise RuntimeError("Lavalink manager is unavailable")
        return await self.manager.update_player(self.guild_id, payload)

    async def _handle_player_update(self, state: dict[str, Any]) -> None:
        connected = bool(state.get("connected"))
        if connected:
            self._voice_ready.set()
        else:
            self._voice_ready.clear()

        position = state.get("position")
        if isinstance(position, int) and not self._resume_after_node_ready:
            self._last_position_ms = max(0, position)

        if connected and self._resume_after_node_ready:
            self._resume_after_node_ready = False
            self._spawn(self._resume_current_after_node_reconnect(), "node-resume")

    async def _handle_lavalink_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        event_track = event.get("track")
        event_encoded = event_track.get("encoded") if isinstance(event_track, dict) else None
        event_info = event_track.get("info") if isinstance(event_track, dict) else None
        event_identifier = event_info.get("identifier") if isinstance(event_info, dict) else None

        if event_type == "TrackStartEvent":
            log.info(
                "Track started guild=%s title=%r track_match=%s",
                self.guild_id,
                (event_track.get("info") or {}).get("title") if isinstance(event_track, dict) else None,
                self._event_matches_active(event_identifier),
            )
            return

        if event_type == "TrackEndEvent":
            reason = str(event.get("reason", ""))
            if not self._event_matches_active(event_identifier):
                log.info("Ignored stale TrackEnd guild=%s reason=%s", self.guild_id, reason)
                return

            log.info(
                "Track ended guild=%s reason=%s generation=%s",
                self.guild_id,
                reason,
                self._generation,
            )
            if reason in {"finished", "loadFailed"}:
                await self._complete_current(self._generation, preserve_pause=False)
            return

        if event_type == "TrackStuckEvent":
            if not self._event_matches_active(event_identifier):
                return
            log.error(
                "Track stuck guild=%s threshold=%r generation=%s",
                self.guild_id,
                event.get("thresholdMs"),
                self._generation,
            )
            await self._patch_player({"track": {"encoded": None}})
            await self._complete_current(self._generation, preserve_pause=False)
            return

        if event_type == "TrackExceptionEvent":
            exception = event.get("exception") or {}
            log.error(
                "Track exception guild=%s message=%r severity=%r cause=%r",
                self.guild_id,
                exception.get("message"),
                exception.get("severity"),
                exception.get("cause"),
            )
            if self._event_matches_active(event_identifier):
                await self._patch_player({"track": {"encoded": None}})
                await self._complete_current(self._generation, preserve_pause=False)
            return

        if event_type == "WebSocketClosedEvent":
            log.error(
                "Discord voice websocket closed guild=%s code=%r reason=%r remote=%r",
                self.guild_id,
                event.get("code"),
                event.get("reason"),
                event.get("byRemote"),
            )
            self._voice_ready.clear()
            return

        log.warning("Unhandled Lavalink event guild=%s event=%r", self.guild_id, event)

    async def _stop_and_advance(self, expected_generation: int, preserve_pause: bool) -> None:
        try:
            await self._patch_player({"track": {"encoded": None}})
        except Exception:
            log.exception("Failed stopping track guild=%s", self.guild_id)
        await self._complete_current(expected_generation, preserve_pause=preserve_pause)

    async def _complete_current(
        self,
        expected_generation: int,
        *,
        preserve_pause: bool,
    ) -> None:
        async with self._finish_lock:
            if expected_generation != self._generation:
                return

            callback = self._after_callback
            ctx = self._active_ctx
            self._playing = False
            if not preserve_pause:
                self._paused = False
            self._clear_active_callback()

            if callback is not None and ctx is not None:
                try:
                    await callback(ctx)
                except Exception:
                    log.exception(
                        "Zeta after callback failed guild=%s generation=%s",
                        self.guild_id,
                        expected_generation,
                    )

    async def _on_node_ready(self) -> None:
        if self._destroyed or self.manager is None:
            return
        self._voice_ready.clear()
        self._resume_after_node_ready = self._playing and self._active_audio_path is not None
        self._resume_position_ms = self._last_position_ms
        try:
            await self._dispatch_voice_state()
        except Exception:
            log.exception("Failed restoring voice state after node reconnect guild=%s", self.guild_id)

    def _on_node_unavailable(self) -> None:
        self._resume_position_ms = self._last_position_ms
        self._voice_ready.clear()

    async def _resume_current_after_node_reconnect(self) -> None:
        if (
            self.manager is None
            or self._active_audio_path is None
            or not self._playing
            or not self.is_connected()
        ):
            return

        try:
            track = await self.manager.load_local_track(self._active_audio_path)
            encoded = str(track["encoded"])
            self._active_encoded_track = encoded
            payload: dict[str, Any] = {
                "track": {"encoded": encoded},
                "volume": round(self._volume_ratio * 100),
                "paused": self._paused,
            }
            resume_position = self._resume_position_ms
            if resume_position > 0:
                payload["position"] = resume_position
            await self._patch_player(payload)
            self._last_position_ms = resume_position
            self._resume_position_ms = 0
            log.info(
                "Playback restored after node reconnect guild=%s position=%s path=%s",
                self.guild_id,
                resume_position,
                self._active_audio_path,
            )
        except Exception:
            log.exception("Failed restoring playback after node reconnect guild=%s", self.guild_id)

    async def _on_manager_closing(self) -> None:
        self._voice_ready.clear()
        self._generation += 1
        self._playing = False
        self._paused = False
        self._clear_active_callback()

    def _event_matches_active(self, event_identifier: Optional[str]) -> bool:
        # Lavalink re-encodes the playing track for its events, so the encoded
        # blob is NOT stable across a track's lifetime (the embedded position
        # changes as the track plays). A local track's info.identifier is its
        # file path and never changes, so match on that instead.
        if self._active_audio_path is None or event_identifier is None:
            return True
        if str(event_identifier) == str(self._active_audio_path):
            return True
        log.warning(
            "Event track mismatch guild=%s event_identifier=%r active_path=%r",
            self.guild_id,
            event_identifier,
            self._active_audio_path,
        )
        return False

    def _clear_active_callback(self) -> None:
        self._active_encoded_track = None
        self._active_audio_path = None
        self._active_ctx = None
        self._after_callback = None

    def _spawn(self, coro: Awaitable[Any], label: str) -> None:
        task = self.client.loop.create_task(coro, name=f"zeta-lavalink-{label}")

        def _report(done: asyncio.Task[Any]) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Background operation failed label=%s guild=%s", label, self.guild_id)

        task.add_done_callback(_report)


_runtime_lock = asyncio.Lock()


async def initialize(bot: discord.Client) -> LavalinkManager:
    async with _runtime_lock:
        manager = getattr(bot, "zeta_lavalink", None)
        if not isinstance(manager, LavalinkManager):
            manager = LavalinkManager(bot)
            bot.zeta_lavalink = manager

        await manager.start()
        return manager


async def shutdown(bot: discord.Client) -> None:
    manager = getattr(bot, "zeta_lavalink", None)
    if not isinstance(manager, LavalinkManager):
        return

    await manager.close()
    try:
        delattr(bot, "zeta_lavalink")
    except AttributeError:
        pass


def _field(data: Any, name: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(name, default)
    return getattr(data, name, default)

