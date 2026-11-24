# Intel Mac 部署记录

2026-11-24：已从 Linux 云服务器切换到 Intel Mac，三平台实际下载和 Lavalink 本地音频读取验证通过。用户已确认 Discord 有声音，并在修正进程调度后确认同一首网易云歌曲连续播放正常。云端机器人已停止并禁用开机启动，原服务与文件保留以便回滚。网易云会员 Cookie 未更新，会员曲目的可用性仍需单独确认。

## 目录与版本

部署根目录为 `~/mitao-geji`，不依赖系统 Python、Java 或 Homebrew 包升级：

- `app/`：仓库源码、独立 `.venv`、私有 `configs/`、`data/`、`downloads/`；`bin/ffmpeg` 指向运行时工具。
- `runtime/`：独立运行时、`launch_service.py`、`private/lavalink.env` 和 `ALLOW_BOT_START` 启动标记。
- `lavalink/`：从已验证生产环境复制的 Lavalink.jar 与私有 application.yml。
- `logs/`：三个 launchd 服务的标准输出和错误日志；机器人自身日志仍在 `app/logs/`。

实测版本：macOS 15.7.7 x86_64、Python 3.12.16、Temurin JRE 21.0.13.1、Node 22.23.5、Deno 2.9.4、FFmpeg/ffprobe 9.1。Pycord 2.8.1、yt-dlp 2026.7.4、EJS 0.8.0、bgutil 1.3.2 和其他 Python 依赖与当时云端保持一致，见本目录的版本快照。

Lavalink 4.2.2 与 bgutil 1.3.2 分别只监听 `127.0.0.1:2333` 和 `127.0.0.1:4416`。bgutil 1.3.2 原生服务默认监听所有接口；部署时仅将其 `server/build/main.js` 两处 `host` 字段改为 `127.0.0.1`，保留原文件用于核对。不要把这些接口开放到公网。

## 启动与维护

`~/Library/LaunchAgents/` 中包含 `com.mitao.bot`、`com.mitao.lavalink`、`com.mitao.bgutil`。这是登录用户的 LaunchAgent：电脑重启后需登录该用户才会启动，未设置自动登录或绕过 FileVault。

**机器人和 Lavalink 必须使用 `ProcessType=Interactive`、`LowPriorityIO=false`。** 初次使用 `Background` 时实际出现声音断续；仅对已有 PID 执行 `taskpolicy -B` 没有解决。重新加载 Interactive 配置后，用户确认连续正常。bgutil 可保持 Background。这里的后台常驻与低优先级后台调度是不同概念。

恢复配置时，将本目录 `launch_service.py` 复制到部署目录 `runtime/`，再用 `write_launch_agents.py --root "$HOME/mitao-geji"` 生成 plist。生成器不会启动服务，也不会覆盖不同的已有配置。先准备运行时与私有配置，验证 `/ping`、Lavalink 认证接口和本地文件加载，再启动机器人。

Mac 终端查看状态：

```sh
launchctl print "gui/$(id -u)/com.mitao.bot"
tail -f "$HOME/mitao-geji/logs/launchd-bot.out.log"
```

只重启机器人：

```sh
launchctl kickstart -k "gui/$(id -u)/com.mitao.bot"
```

改过 plist 后需要 `bootout` 再 `bootstrap`，单纯 kickstart 不会重新读取配置。bootout 后应等旧进程退出、服务标签消失再 bootstrap，避免遇到暂时的 `Input/output error`。重启后在 Discord 使用 `/继续播放` 恢复。

接电时已确认系统空闲休眠为 0，屏幕可以关闭；保持联网。合盖运行未验证，不建议合盖。断电、主动睡眠、退出登录或关机会停止服务。

## 数据与回滚

Cookie、Token、私有环境文件、队列、缓存和下载运行时不得提交 Git。迁移时通过认证 SSH 直接传输这些文件，不在聊天或日志中显示内容。仅当新 YouTube Cookie 比 `.cookies-combined.txt` 更新时，代码才优先使用它；更换 Cookie 不需要重启。

迁移复制了当时保存队列及所需音频，并校验文件哈希；旧绝对媒体路径转换为 Mac 路径。用户随后明确不要求持续同步队列，因此最终切换没有等待新增队列完全追平。云端保留切换时的数据备份和其余下载缓存。

回滚顺序不可颠倒：先在 Mac 删除 `runtime/ALLOW_BOT_START` 标记并 bootout `com.mitao.bot`，确认该进程退出；再在云端启用并启动 `zeta-bot`。同一个 Discord 机器人不能两边同时运行。恢复 Mac 时反过来先停止云端，再恢复启动标记并 bootstrap Mac 的 bot 服务。

## 验证范围

- Windows 与 Mac 均通过完整 13 组、235 项离线测试。
- Mac 实测 YouTube `lNCQbx-y9V8`、B站 `BV1q4FTeBEMj` P2、网易云免费歌 `17241424` 完整下载，ffprobe 音频检查与 Lavalink 本地加载通过。
- YouTube 本次取得 MP4 format 18，含 AAC 音轨；B站为 AAC，网易云为 MP3。
- 用户确认 Mac 实际出声，随后确认调度修复后网易云连续正常；不代表所有链接、会员权益、长时间运行或重启后自动恢复都已逐项实测。
