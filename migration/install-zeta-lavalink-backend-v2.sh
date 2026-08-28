#!/usr/bin/env bash
# v2 (2026-08-28): two fixes over the 2026-08-10 original, found by sandbox-testing
# Lavalink 4.2.2 on Ubuntu 24.04 / OpenJDK 21 before touching production:
#   1. The zeta-lavalink systemd unit now sets LANG=C.UTF-8. Without a UTF-8
#      locale the JVM's sun.jnu.encoding falls back to ASCII and Lavalink's
#      local source cannot see files with non-ASCII names -> every Chinese
#      song title fails to load (loadType "empty"). Verified in sandbox:
#      same file loads as "track" with the locale set, "empty" without.
#   2. The local-source smoke test now uses a Chinese filename with a space,
#      so an installation that mishandles UTF-8 paths fails the smoke test
#      and auto-rolls back instead of passing with an ASCII-only check.
set -Eeuo pipefail

APP="/opt/zeta/Zeta-DiscordBot"
PY="$APP/.venv/bin/python"
CORE="$APP/zeta_bot/core.py"
YTDLP="$APP/zeta_bot/ytdlp.py"
BACKEND="$APP/zeta_bot/lavalink_backend.py"
LAVALINK_VERSION="4.2.2"
LAVALINK_DIR="/opt/zeta/lavalink"
LAVALINK_JAR="$LAVALINK_DIR/Lavalink.jar"
LAVALINK_CONFIG="$LAVALINK_DIR/application.yml"
LAVALINK_ENV="/etc/zeta-lavalink.env"
LAVALINK_SERVICE="/etc/systemd/system/zeta-lavalink.service"
BOT_DROPIN_DIR="/etc/systemd/system/zeta-bot.service.d"
BOT_DROPIN="$BOT_DROPIN_DIR/20-lavalink.conf"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/opt/zeta/lavalink-migration-backup-$TIMESTAMP"
ROLLBACK_SCRIPT="/root/rollback-zeta-lavalink.sh"
JAR_URL="https://github.com/lavalink-devs/Lavalink/releases/download/$LAVALINK_VERSION/Lavalink.jar"
TMP_JAR="/tmp/Lavalink-$LAVALINK_VERSION-$TIMESTAMP.jar"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run this installer as root"
  exit 1
fi

for required in "$APP" "$PY" "$CORE" "$YTDLP"; do
  if [ ! -e "$required" ]; then
    echo "ERROR: required path is missing: $required"
    exit 1
  fi
done

SITE="$("$PY" -c 'import site; print(site.getsitepackages()[0])' | tail -n 1 | tr -d '\r')"
SITECUSTOMIZE="$SITE/sitecustomize.py"
if [ -z "$SITE" ] || [ ! -d "$SITE" ]; then
  echo "ERROR: unable to resolve virtualenv site-packages: $SITE"
  exit 1
fi

OLD_PATCH_FILES=(
  "$SITE/zeta_voice_stability.py"
  "$SITE/zeta_voice_stability.pth"
  "$SITE/zeta_pycord_dave_transition_fix.py"
  "$SITE/zeta_pycord_dave_transition_fix.pth"
)

mkdir -p "$BACKUP/files"
MANIFEST="$BACKUP/manifest.tsv"
: > "$MANIFEST"

backup_path() {
  local source="$1"
  local key="$2"
  if [ -e "$source" ]; then
    cp -a "$source" "$BACKUP/files/$key"
    printf '1\t%s\t%s\n' "$source" "$key" >> "$MANIFEST"
  else
    printf '0\t%s\t%s\n' "$source" "$key" >> "$MANIFEST"
  fi
}

restore_from_manifest() {
  while IFS=$'\t' read -r existed target key; do
    [ -n "$target" ] || continue
    if [ "$existed" = "1" ]; then
      rm -rf "$target"
      mkdir -p "$(dirname "$target")"
      cp -a "$BACKUP/files/$key" "$target"
    else
      rm -rf "$target"
    fi
  done < "$MANIFEST"
}

backup_path "$CORE" "core.py"
backup_path "$BACKEND" "lavalink_backend.py"
backup_path "$LAVALINK_JAR" "Lavalink.jar"
backup_path "$LAVALINK_CONFIG" "application.yml"
backup_path "$LAVALINK_ENV" "zeta-lavalink.env"
backup_path "$LAVALINK_SERVICE" "zeta-lavalink.service"
backup_path "$BOT_DROPIN" "20-lavalink.conf"
backup_path "$SITECUSTOMIZE" "sitecustomize.py"

index=0
for path in "${OLD_PATCH_FILES[@]}"; do
  backup_path "$path" "old-patch-$index-$(basename "$path")"
  index=$((index + 1))
done

MONITOR_ENABLED=0
MONITOR_ACTIVE=0
systemctl is-enabled --quiet zeta-voice-monitor.service 2>/dev/null && MONITOR_ENABLED=1 || true
systemctl is-active --quiet zeta-voice-monitor.service 2>/dev/null && MONITOR_ACTIVE=1 || true
printf '%s\n' "$MONITOR_ENABLED" > "$BACKUP/monitor-enabled"
printf '%s\n' "$MONITOR_ACTIVE" > "$BACKUP/monitor-active"

LAVALINK_WAS_ENABLED=0
LAVALINK_WAS_ACTIVE=0
systemctl is-enabled --quiet zeta-lavalink.service 2>/dev/null && LAVALINK_WAS_ENABLED=1 || true
systemctl is-active --quiet zeta-lavalink.service 2>/dev/null && LAVALINK_WAS_ACTIVE=1 || true
printf '%s\n' "$LAVALINK_WAS_ENABLED" > "$BACKUP/lavalink-enabled"
printf '%s\n' "$LAVALINK_WAS_ACTIVE" > "$BACKUP/lavalink-active"

YTDLP_SHA_BEFORE="$(sha256sum "$YTDLP" | awk '{print $1}')"
printf '%s\n' "$YTDLP_SHA_BEFORE" > "$BACKUP/ytdlp.sha256"

rollback() {
  local rc="${1:-1}"
  trap - ERR
  set +e

  echo
  echo "MIGRATION_FAILED; rolling back. exit_code=$rc"

  systemctl stop zeta-bot.service >/dev/null 2>&1 || true
  systemctl stop zeta-lavalink.service >/dev/null 2>&1 || true

  restore_from_manifest
  systemctl daemon-reload >/dev/null 2>&1 || true

  if [ "$(cat "$BACKUP/lavalink-enabled" 2>/dev/null)" = "1" ]; then
    systemctl enable zeta-lavalink.service >/dev/null 2>&1 || true
  else
    systemctl disable zeta-lavalink.service >/dev/null 2>&1 || true
  fi

  if [ "$(cat "$BACKUP/lavalink-active" 2>/dev/null)" = "1" ]; then
    systemctl start zeta-lavalink.service >/dev/null 2>&1 || true
  fi

  if [ "$(cat "$BACKUP/monitor-enabled" 2>/dev/null)" = "1" ]; then
    systemctl enable zeta-voice-monitor.service >/dev/null 2>&1 || true
  else
    systemctl disable zeta-voice-monitor.service >/dev/null 2>&1 || true
  fi

  if [ "$(cat "$BACKUP/monitor-active" 2>/dev/null)" = "1" ]; then
    systemctl start zeta-voice-monitor.service >/dev/null 2>&1 || true
  else
    systemctl stop zeta-voice-monitor.service >/dev/null 2>&1 || true
  fi

  chown zeta:zeta "$CORE" "$YTDLP" 2>/dev/null || true
  [ ! -e "$BACKEND" ] || chown zeta:zeta "$BACKEND" 2>/dev/null || true

  systemctl start zeta-bot.service >/dev/null 2>&1 || true

  echo "Rollback complete. Backup: $BACKUP"
  exit "$rc"
}

trap 'rollback $?' ERR

cat > "$ROLLBACK_SCRIPT" <<EOF_ROLLBACK
#!/usr/bin/env bash
set -u
BACKUP="$BACKUP"
MANIFEST="\$BACKUP/manifest.tsv"
restore_from_manifest() {
  while IFS=\$'\t' read -r existed target key; do
    [ -n "\$target" ] || continue
    if [ "\$existed" = "1" ]; then
      rm -rf "\$target"
      mkdir -p "\$(dirname "\$target")"
      cp -a "\$BACKUP/files/\$key" "\$target"
    else
      rm -rf "\$target"
    fi
  done < "\$MANIFEST"
}
systemctl stop zeta-bot.service >/dev/null 2>&1 || true
systemctl stop zeta-lavalink.service >/dev/null 2>&1 || true
restore_from_manifest
systemctl daemon-reload
if [ "\$(cat "\$BACKUP/lavalink-enabled" 2>/dev/null)" = "1" ]; then
  systemctl enable zeta-lavalink.service >/dev/null 2>&1 || true
else
  systemctl disable zeta-lavalink.service >/dev/null 2>&1 || true
fi
if [ "\$(cat "\$BACKUP/lavalink-active" 2>/dev/null)" = "1" ]; then
  systemctl start zeta-lavalink.service >/dev/null 2>&1 || true
fi
if [ "\$(cat "\$BACKUP/monitor-enabled" 2>/dev/null)" = "1" ]; then
  systemctl enable zeta-voice-monitor.service >/dev/null 2>&1 || true
else
  systemctl disable zeta-voice-monitor.service >/dev/null 2>&1 || true
fi
if [ "\$(cat "\$BACKUP/monitor-active" 2>/dev/null)" = "1" ]; then
  systemctl start zeta-voice-monitor.service >/dev/null 2>&1 || true
else
  systemctl stop zeta-voice-monitor.service >/dev/null 2>&1 || true
fi
chown zeta:zeta "$CORE" "$YTDLP" 2>/dev/null || true
[ ! -e "$BACKEND" ] || chown zeta:zeta "$BACKEND" 2>/dev/null || true
systemctl start zeta-bot.service
echo "Zeta Lavalink migration rolled back from: \$BACKUP"
EOF_ROLLBACK
chmod 700 "$ROLLBACK_SCRIPT"

wait_for_apt() {
  while     fuser /var/lib/apt/lists/lock >/dev/null 2>&1 ||     fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 ||     fuser /var/lib/dpkg/lock >/dev/null 2>&1
  do
    echo "APT is busy; retrying in 5 seconds..."
    sleep 5
  done
}

echo "=== 1. Install Java and system dependencies ==="
wait_for_apt
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y   openjdk-21-jre-headless   curl   ca-certificates   unzip   procps

echo "=== Ensure swap is available for the 1GB droplet ==="
if ! swapon --show --noheadings | grep -q .; then
  if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
  fi
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

java -version

echo "=== 2. Download Lavalink $LAVALINK_VERSION ==="
rm -f "$TMP_JAR"
curl   --fail   --location   --retry 5   --retry-all-errors   --connect-timeout 20   --max-time 600   --output "$TMP_JAR"   "$JAR_URL"

JAR_SIZE="$(stat -c '%s' "$TMP_JAR")"
if [ "$JAR_SIZE" -lt 10000000 ]; then
  echo "ERROR: downloaded Lavalink.jar is unexpectedly small: $JAR_SIZE bytes"
  false
fi

unzip -tq "$TMP_JAR" > "/tmp/lavalink-jar-test-$TIMESTAMP.txt"
unzip -p "$TMP_JAR" META-INF/MANIFEST.MF > "/tmp/lavalink-manifest-$TIMESTAMP.txt"
test -s "/tmp/lavalink-manifest-$TIMESTAMP.txt"

echo "Downloaded Lavalink.jar: $JAR_SIZE bytes"

echo "=== 3. Create Lavalink credentials and configuration ==="
PASSWORD=""
if [ -f "$LAVALINK_ENV" ]; then
  PASSWORD="$(sed -n 's/^LAVALINK_PASSWORD=//p' "$LAVALINK_ENV" | tail -n 1 | tr -d '\r')"
fi
if [ -z "$PASSWORD" ]; then
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
fi

install -d -o zeta -g zeta -m 700 "$LAVALINK_DIR"
install -o zeta -g zeta -m 600 "$TMP_JAR" "$LAVALINK_JAR"

cat > "$LAVALINK_CONFIG" <<EOF_CONFIG
server:
  port: 2333
  address: 127.0.0.1
  http2:
    enabled: false

lavalink:
  server:
    password: "$PASSWORD"
    sources:
      youtube: false
      bandcamp: false
      soundcloud: false
      twitch: false
      vimeo: false
      nico: false
      http: false
      local: true
    filters:
      volume: true
      equalizer: false
      karaoke: false
      timescale: false
      tremolo: false
      vibrato: false
      distortion: false
      rotation: false
      channelMix: false
      lowPass: false
    nonAllocatingFrameBuffer: true
    trackStuckThresholdMs: 10000
    playerUpdateInterval: 1
    gc-warnings: true

metrics:
  prometheus:
    enabled: false

logging:
  level:
    root: INFO
    lavalink: INFO
  request:
    enabled: false
EOF_CONFIG
chown zeta:zeta "$LAVALINK_CONFIG"
chmod 600 "$LAVALINK_CONFIG"

cat > "$LAVALINK_ENV" <<EOF_ENV
LAVALINK_HOST=127.0.0.1
LAVALINK_PORT=2333
LAVALINK_PASSWORD=$PASSWORD
LAVALINK_NODE_READY_TIMEOUT=45
LAVALINK_VOICE_READY_TIMEOUT=45
EOF_ENV
chown root:zeta "$LAVALINK_ENV"
chmod 640 "$LAVALINK_ENV"

cat > "$LAVALINK_SERVICE" <<'EOF_SERVICE'
[Unit]
Description=Local Lavalink audio node for Zeta
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=zeta
Group=zeta
WorkingDirectory=/opt/zeta/lavalink
Environment="HOME=/opt/zeta"
# UTF-8 locale is REQUIRED: without it the JVM cannot open files whose names
# contain non-ASCII characters (every Chinese song title in the Zeta cache).
Environment="LANG=C.UTF-8"
EnvironmentFile=/etc/zeta-lavalink.env
UMask=0077
ExecStart=/usr/bin/java -Xms64m -Xmx320m -XX:+UseG1GC -jar /opt/zeta/lavalink/Lavalink.jar
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/opt/zeta/lavalink
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF_SERVICE
chmod 644 "$LAVALINK_SERVICE"

systemctl daemon-reload
systemctl enable zeta-lavalink.service >/dev/null
systemctl restart zeta-lavalink.service

echo "=== 4. Wait for Lavalink health check ==="
LAVALINK_VERSION_ACTUAL=""
for attempt in $(seq 1 90); do
  if systemctl is-active --quiet zeta-lavalink.service; then
    LAVALINK_VERSION_ACTUAL="$(curl -fsS -H "Authorization: $PASSWORD" http://127.0.0.1:2333/version 2>/dev/null || true)"
    if [ -n "$LAVALINK_VERSION_ACTUAL" ]; then
      break
    fi
  fi
  sleep 1
done

if [ -z "$LAVALINK_VERSION_ACTUAL" ]; then
  echo "ERROR: Lavalink did not become healthy"
  systemctl status zeta-lavalink.service --no-pager -l || true
  journalctl -u zeta-lavalink.service --since "10 minutes ago" --no-pager -l | tail -n 300 || true
  false
fi

if [[ "$LAVALINK_VERSION_ACTUAL" != "$LAVALINK_VERSION"* ]]; then
  echo "ERROR: expected Lavalink $LAVALINK_VERSION, got $LAVALINK_VERSION_ACTUAL"
  false
fi

echo "Lavalink healthy: $LAVALINK_VERSION_ACTUAL"

echo "=== 5. Verify Lavalink can read a local audio file (UTF-8 filename) ==="
# Deliberately uses a Chinese filename with a space: Zeta names every cached
# download after the song title, so the smoke test must prove that Lavalink
# can load non-ASCII paths. If this fails, the installer rolls back.
SMOKE_WAV="$LAVALINK_DIR/冒烟测试 本地音源.wav"
runuser -u zeta -- python3 - "$SMOKE_WAV" <<'PY_SMOKE_WAV'
import math
import struct
import sys
import wave

path = sys.argv[1]
sample_rate = 48000
frames = bytearray()
for index in range(sample_rate // 4):
    sample = int(1000 * math.sin(2 * math.pi * 440 * index / sample_rate))
    frames.extend(struct.pack("<h", sample))

with wave.open(path, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(sample_rate)
    output.writeframes(bytes(frames))
PY_SMOKE_WAV

SMOKE_JSON_FILE="/tmp/lavalink-local-source-smoke-$TIMESTAMP.json"
curl -fsS \
  -H "Authorization: $PASSWORD" \
  --get \
  --data-urlencode "identifier=$SMOKE_WAV" \
  --output "$SMOKE_JSON_FILE" \
  http://127.0.0.1:2333/v4/loadtracks

python3 - "$SMOKE_JSON_FILE" <<'PY_SMOKE_VALIDATE'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as source:
    payload = json.load(source)
if payload.get("loadType") != "track":
    raise SystemExit(f"Local source smoke test failed: {payload!r}")
data = payload.get("data")
if not isinstance(data, dict) or not data.get("encoded"):
    raise SystemExit(f"Local source returned no encoded track: {payload!r}")
print("LOCAL_AUDIO_SOURCE_OK")
PY_SMOKE_VALIDATE
rm -f "$SMOKE_WAV" "$SMOKE_JSON_FILE"

echo "=== 6. Stop Zeta and remove experimental Pycord runtime patches ==="
systemctl stop zeta-bot.service
rm -f "${OLD_PATCH_FILES[@]}"
rm -f \
  "$SITE/__pycache__/zeta_voice_stability"*.pyc \
  "$SITE/__pycache__/zeta_pycord_dave_transition_fix"*.pyc \
  2>/dev/null || true

python3 - "$SITECUSTOMIZE" <<'PY_REMOVE_OLD_DIAGNOSTICS'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

text = path.read_text(encoding="utf-8")
start_marker = "# ZETA_VOICE_DIAGNOSTICS_BEGIN"
end_marker = "# ZETA_VOICE_DIAGNOSTICS_END"

if start_marker in text and end_marker in text:
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    while end < len(text) and text[end] in "\r\n":
        end += 1
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    print("OLD_VOICE_DIAGNOSTIC_BLOCK_REMOVED")
PY_REMOVE_OLD_DIAGNOSTICS

systemctl disable --now zeta-voice-monitor.service >/dev/null 2>&1 || true

echo "=== 7. Install the dedicated Lavalink backend ==="
cat > "$BACKEND" <<'PY_BACKEND_ZETA_20260810'
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
BACKEND_VERSION = "2026-08-10.2"

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

        if event_type == "TrackStartEvent":
            log.info(
                "Track started guild=%s title=%r track_match=%s",
                self.guild_id,
                (event_track.get("info") or {}).get("title") if isinstance(event_track, dict) else None,
                event_encoded == self._active_encoded_track,
            )
            return

        if event_type == "TrackEndEvent":
            reason = str(event.get("reason", ""))
            if not self._event_matches_active(event_encoded):
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
            if not self._event_matches_active(event_encoded):
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
            if self._event_matches_active(event_encoded):
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

    def _event_matches_active(self, event_encoded: Optional[str]) -> bool:
        if self._active_encoded_track is None or event_encoded is None:
            return True
        return self._active_encoded_track == event_encoded

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

PY_BACKEND_ZETA_20260810
chown zeta:zeta "$BACKEND"
chmod 644 "$BACKEND"

echo "=== 8. Patch Zeta core while preserving its queue/UI/download logic ==="
cat > "/tmp/patch-zeta-lavalink-$TIMESTAMP.py" <<'PY_PATCH_ZETA_20260810'
from pathlib import Path
import sys

core_path = Path(sys.argv[1])
text = core_path.read_text(encoding="utf-8")

# 1. Import the backend next to the other Zeta functional modules.
if "from zeta_bot import lavalink_backend\n" not in text:
    marker = "from zeta_bot.help import HelpMenu\n"
    if marker not in text:
        raise RuntimeError("Unable to locate HelpMenu import")
    text = text.replace(marker, marker + "from zeta_bot import lavalink_backend\n", 1)

# 2. Initialize Lavalink after the audio library is ready. Failure is non-fatal;
# join_callback retries initialization and reports the actual exception.
init_tag = "# ZETA_LAVALINK_BACKEND_INIT_V1"
if init_tag not in text:
    marker = "    # 设置机器人状态\n"
    if marker not in text:
        raise RuntimeError("Unable to locate on_ready initialization point")

    init_block = '''    # ZETA_LAVALINK_BACKEND_INIT_V1
    try:
        await lavalink_backend.initialize(bot)
        await console.rp(
            f"Lavalink音频后端已就绪（{lavalink_backend.BACKEND_VERSION}）",
            "[系统]",
        )
    except Exception as error:
        await console.rp(
            f"Lavalink音频后端初始化失败：{error!r}",
            "[系统]",
            message_type=utils.PrintType.ERROR,
            print_head=True,
        )

'''
    text = text.replace(marker, init_block + marker, 1)

# 3. Replace native Pycord voice connection with the Lavalink VoiceProtocol.
join_start = text.find("async def join_callback(")
join_end = text.find("\n\nasync def leave_callback(", join_start)
if join_start < 0 or join_end < 0:
    raise RuntimeError("Unable to locate join_callback")
join_block = text[join_start:join_end]

# Remove the abandoned Stage-mode block if it still exists.
stage_tag = "    # ZETA_STAGE_MODE_V1\n"
console_marker = '    await console.rp(f"加入语音频道：{channel.name}", ctx.guild)\n'
if stage_tag in join_block:
    start = join_block.index(stage_tag)
    end = join_block.find(console_marker, start)
    if end < 0:
        raise RuntimeError("Unable to remove stale Stage-mode block")
    join_block = join_block[:start] + join_block[end:]

join_block = join_block.replace(
    "        await channel.connect(self_deaf=True)\n",
    "        await channel.connect()\n",
    1,
)

normal_connect = "        await channel.connect()\n"
lavalink_connect = (
    "        await channel.connect(\n"
    "            cls=lavalink_backend.LavalinkVoiceClient,\n"
    "        )\n"
)
if lavalink_connect not in join_block:
    if normal_connect not in join_block:
        raise RuntimeError("Unable to locate channel.connect() in join_callback")
    join_block = join_block.replace(normal_connect, lavalink_connect, 1)

text = text[:join_start] + join_block + text[join_end:]

# 4. Lavalink disconnect does not invoke Zeta's play_next callback. Remove the
# native Pycord workaround that duplicated the first queue item before leave.
leave_start = text.find("async def leave_callback(")
leave_end = text.find("\n\nasync def search_audio_callback(", leave_start)
if leave_start < 0 or leave_end < 0:
    raise RuntimeError("Unable to locate leave_callback")
leave_block = text[leave_start:leave_end]
leave_tag = "        # ZETA_LAVALINK_LEAVE_PRESERVE_QUEUE_V1\n"
legacy_leave_workaround = (
    "        # 防止因退出频道自动删除正在播放的音频\n"
    "        if len(current_playlist) > 0:\n"
    "            current_audio = current_playlist.get_audio(0)\n"
    "            current_playlist.insert_audio(current_audio, 0)\n"
    "\n"
)
if leave_tag not in leave_block:
    if legacy_leave_workaround not in leave_block:
        raise RuntimeError("Unable to locate native leave queue workaround")
    leave_block = leave_block.replace(
        legacy_leave_workaround,
        leave_tag + "        # The existing queue is preserved without inserting a duplicate.\n\n",
        1,
    )
text = text[:leave_start] + leave_block + text[leave_end:]

# 5. Replace the Pycord/FFmpeg sender in play_audio while preserving Zeta's
# queue, UI, local cache, and current ytdlp/NetEase flow.
play_start = text.find("async def play_audio(")
play_end = text.find("\n\nasync def play_next(", play_start)
if play_start < 0 or play_end < 0:
    raise RuntimeError("Unable to locate play_audio")
play_block = text[play_start:play_end]

backend_tag = "    # ZETA_LAVALINK_PLAYBACK_V1\n"
if backend_tag not in play_block:
    lock_marker = '    audio_lib_main.lock_audio(f"{ctx.guild.id}_NOW_PLAYING", target_audio)\n'
    log_marker = '    await console.rp(f"开始播放：{target_audio.get_path()} 时长：{target_audio.get_duration_str()}", ctx.guild)\n'
    lock_pos = play_block.find(lock_marker)
    log_pos = play_block.find(log_marker)
    if lock_pos < 0 or log_pos < 0 or log_pos <= lock_pos:
        raise RuntimeError("Unable to locate Pycord playback block")

    replacement = lock_marker + '''
    # ZETA_LAVALINK_PLAYBACK_V1
    if not isinstance(voice_client, lavalink_backend.LavalinkVoiceClient):
        audio_lib_main.unlock_audio(
            f"{ctx.guild.id}_NOW_PLAYING",
            target_audio,
        )
        raise RuntimeError(
            "当前语音连接不是Lavalink后端，请先让机器人离开语音频道后重新加入"
        )

    try:
        await voice_client.play_zeta_audio(
            target_audio,
            ctx=ctx,
            after=play_next,
            volume_percent=current_guild.get_voice_volume(),
        )
    except Exception:
        audio_lib_main.unlock_audio(
            f"{ctx.guild.id}_NOW_PLAYING",
            target_audio,
        )
        raise

'''
    play_block = play_block[:lock_pos] + replacement + play_block[log_pos:]

text = text[:play_start] + play_block + text[play_end:]

# 6. Cleanly close the Lavalink control connection before exec/exit.
shutdown_line = "    await lavalink_backend.shutdown(bot)\n"

# Automatic scheduled reboot.
auto_start = text.find("async def auto_reboot():")
auto_end = text.find("\n\nasync def auto_reboot_reminder():", auto_start)
if auto_start < 0 or auto_end < 0:
    raise RuntimeError("Unable to locate auto_reboot")
auto_block = text[auto_start:auto_end]
if shutdown_line not in auto_block:
    marker = "    os.execl(python_path, python_path, * sys.argv)\n"
    if marker not in auto_block:
        raise RuntimeError("Unable to locate auto_reboot exec")
    auto_block = auto_block.replace(marker, shutdown_line + marker, 1)
text = text[:auto_start] + auto_block + text[auto_end:]

# Manual reboot.
reboot_start = text.find("async def reboot_callback(")
reboot_end = text.find("\n\nasync def shutdown_callback(", reboot_start)
if reboot_start < 0 or reboot_end < 0:
    raise RuntimeError("Unable to locate reboot_callback")
reboot_block = text[reboot_start:reboot_end]
if shutdown_line not in reboot_block:
    marker = "    os.execl(python_path, python_path, * sys.argv)\n"
    if marker not in reboot_block:
        raise RuntimeError("Unable to locate reboot_callback exec")
    reboot_block = reboot_block.replace(marker, shutdown_line + marker, 1)
text = text[:reboot_start] + reboot_block + text[reboot_end:]

# Manual shutdown.
shutdown_start = text.find("async def shutdown_callback(")
shutdown_end = text.find("\n\nclass PlaylistMenu", shutdown_start)
if shutdown_start < 0 or shutdown_end < 0:
    raise RuntimeError("Unable to locate shutdown_callback")
shutdown_block = text[shutdown_start:shutdown_end]
if shutdown_line not in shutdown_block:
    marker = "    await bot.close()\n"
    if marker not in shutdown_block:
        raise RuntimeError("Unable to locate bot.close()")
    shutdown_block = shutdown_block.replace(marker, shutdown_line + marker, 1)
text = text[:shutdown_start] + shutdown_block + text[shutdown_end:]

core_path.write_text(text, encoding="utf-8")
print("ZETA_LAVALINK_CORE_PATCH_OK")

PY_PATCH_ZETA_20260810
python3 "/tmp/patch-zeta-lavalink-$TIMESTAMP.py" "$CORE"
chown zeta:zeta "$CORE"
chmod 644 "$CORE"

echo "=== 9. Configure Zeta systemd dependency/environment ==="
install -d -m 755 "$BOT_DROPIN_DIR"
cat > "$BOT_DROPIN" <<'EOF_DROPIN'
[Unit]
Wants=zeta-lavalink.service
After=zeta-lavalink.service

[Service]
EnvironmentFile=/etc/zeta-lavalink.env
EOF_DROPIN
chmod 644 "$BOT_DROPIN"

systemctl daemon-reload

echo "=== 10. Verify syntax, code markers, and preserved NetEase modifications ==="
runuser -u zeta -- env   HOME=/opt/zeta   LAVALINK_HOST=127.0.0.1   LAVALINK_PORT=2333   LAVALINK_PASSWORD="$PASSWORD"   "$PY" -m py_compile   "$CORE"   "$BACKEND"   "$YTDLP"

python3 - "$CORE" "$BACKEND" <<'PY_VERIFY_ZETA_20260810'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
backend = Path(sys.argv[2]).read_text(encoding="utf-8")

assert "ZETA_LAVALINK_BACKEND_INIT_V1" in core
assert "ZETA_LAVALINK_PLAYBACK_V1" in core
assert "ZETA_LAVALINK_LEAVE_PRESERVE_QUEUE_V1" in core
assert "cls=lavalink_backend.LavalinkVoiceClient" in core
assert "await voice_client.play_zeta_audio(" in core
assert "ZETA_STAGE_MODE_V1" not in core

play_start = core.index("async def play_audio(")
play_end = core.index("\n\nasync def play_next(", play_start)
play_block = core[play_start:play_end]
assert "FFmpegPCMAudio" not in play_block
assert "voice_client.play(" not in play_block

assert 'BACKEND_VERSION = "2026-08-10.2"' in backend
assert '"channelId"' in backend
assert "/v4/websocket" in backend
assert "/v4/loadtracks" in backend
assert "cached, unusable voice client" in backend

print("CODE_MARKERS_OK")
PY_VERIFY_ZETA_20260810

YTDLP_SHA_AFTER="$(sha256sum "$YTDLP" | awk '{print $1}')"
if [ "$YTDLP_SHA_AFTER" != "$YTDLP_SHA_BEFORE" ]; then
  echo "ERROR: ytdlp.py changed unexpectedly"
  echo "before=$YTDLP_SHA_BEFORE"
  echo "after =$YTDLP_SHA_AFTER"
  false
fi

echo "NETEASE_YTDLP_PRESERVED"

echo "=== 11. Start Zeta ==="
systemctl start zeta-bot.service

for attempt in $(seq 1 60); do
  if systemctl is-active --quiet zeta-bot.service; then
    if journalctl -u zeta-bot.service --since "2 minutes ago" --no-pager -o cat | grep -q "启动完成"; then
      break
    fi
  fi
  sleep 1
done

systemctl is-active --quiet zeta-bot.service

if ! journalctl -u zeta-bot.service --since "3 minutes ago" --no-pager -o cat | grep -q "Lavalink音频后端已就绪"; then
  echo "ERROR: Zeta started but did not report a ready Lavalink backend"
  systemctl status zeta-bot.service --no-pager -l || true
  journalctl -u zeta-bot.service --since "10 minutes ago" --no-pager -l | tail -n 400 || true
  false
fi

trap - ERR
rm -f \
  "$TMP_JAR" \
  "/tmp/lavalink-jar-test-$TIMESTAMP.txt" \
  "/tmp/lavalink-manifest-$TIMESTAMP.txt" \
  "/tmp/patch-zeta-lavalink-$TIMESTAMP.py"

echo "=== 12. Final status ==="
systemctl status zeta-lavalink.service --no-pager -l
systemctl status zeta-bot.service --no-pager -l

echo
echo "============================================================"
echo "ZETA_LAVALINK_MIGRATION_OK"
echo "Lavalink: $LAVALINK_VERSION_ACTUAL"
echo "Backend: 2026-08-10.2"
echo "Backup: $BACKUP"
echo "Rollback: bash $ROLLBACK_SCRIPT"
echo "NetEase cookie/ytdlp.py: preserved"
echo "Zeta queue/cache/UI: preserved"
echo "Old Pycord DAVE runtime patches: removed"
echo "============================================================"
