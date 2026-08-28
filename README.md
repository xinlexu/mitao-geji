# 🍑 蜜桃歌姬

「蜜桃成熟了」服务器的专属 Discord 音乐机器人。

## 功能

- 网易云音乐：单曲 / 歌单，会员完整音质（不是 30 秒试听）
- 哔哩哔哩、YouTube 播放与站内搜索
- 可交互播放列表面板 + 中文 Slash 指令

## 常用指令

`/加入语音频道` · `/播放 链接或关键词` · `/音量` · `/播放列表` · `/清空播放列表`

## 架构

- 歌曲解析与下载：yt-dlp（网易云会员 Cookie，本地缓存）
- 语音发送与 DAVE(E2EE) 加密：Lavalink 4.2.2（koe / libdave），独立 systemd 服务
- 机器人本体：Python / Pycord，对接层 `zeta_bot/lavalink_backend.py`

## 运维速查（服务器上执行）

- 服务状态：`systemctl status zeta-bot zeta-lavalink`
- 实时日志：`journalctl -u zeta-bot -f -o cat`
- 重启机器人：`systemctl restart zeta-bot`
- 备份代码到本仓库：`bash /root/backup-to-github.sh`
- 详见《蜜桃歌姬-维护手册.md》与 migration/ 目录

## 更新日志

- 2026-08-28 语音后端迁移至 Lavalink：根治长播无声；修复切歌即停与中文文件名加载
- 2026-08-04 网易云会员完整歌曲打通（Cookie + HTTPS 修复 + 异步下载）

---
基于 31Zeta/Zeta-DiscordBot v0.14.0 二次开发。
