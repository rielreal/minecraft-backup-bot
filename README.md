# minecraft-backup-bot

Discord bot that backs up RCON-enabled Minecraft servers (vanilla, Paper, Fabric, Forge, NeoForge) to Google Drive and doubles as a remote console.

One Python file, one `.env`. Point it at a server directory, give it a Discord token and an rclone remote, and systemd keeps it running.

## Features

- **Consistent world snapshots** on a schedule: the bot runs `save-off` and `save-all flush`, zips the frozen world folder plus configured world targets, then turns autosave back on, even when the zip fails halfway
- **Offsite to Google Drive** via rclone; the bot posts a share link, file size, and player count to your channel for each backup
- **Config archives** (`tar.zst`): the bot hashes file contents and uploads on real changes, so mods that rewrite identical files on a timer trigger nothing
- **Retention that fails safe**: keeps `LOCAL_KEEP` zips on disk, moves remote history older than `REMOTE_KEEP` to Drive trash, and permanently purges trashed backup files older than `REMOTE_TRASH_KEEP`. If backups stop, deletion stops with them.
- **Join/leave feed** in the same channel
- **Slash commands**: `/rcon` (full console, owner-locked), `/backup`, `/backupcheck`, `/status`, `/backups`, `/tps`, `/health`, `/profile`
- **Diagnostics from Discord**: per-dimension tick rates, server memory, uptime, disk, and [spark](https://spark.lucko.me/) profiler runs. `/profile 60` posts a flame-graph report link in the channel (needs the spark mod or plugin).
- **Two modes**: `builtin`, where the bot snapshots over RCON, or `external`, where a backup mod such as [FTB Backups 3](https://github.com/FTBTeam/FTB-Backups-3) snapshots and the bot watches its output folder, uploads, and notifies

## How it works

The bot runs three loops:

| Loop | Interval | Job |
|------|----------|-----|
| scheduler | 60s | snapshots the world when the newest zip is older than `BACKUP_INTERVAL_MIN`, rotates local zips (builtin mode) |
| watcher | 60s | uploads new zips to Drive, posts the link, prunes the remote and Drive trash, archives config on change |
| presence | 20s | polls the player list over RCON, posts join/leave diffs |

The bot reads the world folder name from `level-name` in `server.properties`, auto-includes matching Paper/Spigot `*_nether` and `*_the_end` sibling folders when they exist, and tracks finished uploads in `state.json`, so a restart re-uploads nothing and skips nothing.

## Requirements

- Python 3.11+, `tar`, `zstd`
- [rcon-cli](https://github.com/itzg/rcon-cli)
- [rclone](https://rclone.org/) 1.7x with a configured Google Drive remote
- RCON enabled in `server.properties`, with the port firewalled to localhost:

```properties
enable-rcon=true
rcon.password=something-long-random
rcon.port=25575
```

and a matching `~/.rcon-cli.yaml`:

```yaml
host: 127.0.0.1
port: 25575
password: something-long-random
```

## Setup

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications), copy the bot token, and invite the bot to your server: OAuth2 URL generator, scopes `bot` + `applications.commands`, permissions Send Messages, Embed Links, Attach Files
2. Configure a Drive remote with `rclone config`. On a headless box, run `rclone authorize drive` on a machine with a browser and paste the token.
3. Install beside the server directory, not inside it:

```sh
git clone https://github.com/rielreal/minecraft-backup-bot mcbot && cd mcbot
./setup.sh
```

4. Fill in `.env`, then test in the foreground:

```sh
set -a; . ./.env; set +a; venv/bin/python bot.py
```

5. Install as a service:

```sh
cp mcbot.service /etc/systemd/system/
systemctl enable --now mcbot
```

## Configuration

Everything lives in `.env`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `DISCORD_TOKEN` | required | bot token |
| `GUILD_ID` | required | Discord server ID |
| `CHANNEL_ID` | required | channel for backup messages |
| `OWNER_ID` | required | user ID that may use `/rcon`, `/backup`, `/profile` |
| `SERVER_DIR` | `/root/server` | Minecraft server directory |
| `RCON_CONFIG` | `/root/.rcon-cli.yaml` | rcon-cli config path |
| `WORLD_DIR` | auto | world folder; read from `level-name` when unset |
| `WORLD_TARGETS` | auto | comma-separated world folders to zip, relative to `SERVER_DIR`; overrides `WORLD_DIR` auto-detection when set |
| `BACKUP_MODE` | `builtin` | `builtin` or `external` |
| `BACKUP_INTERVAL_MIN` | `15` | minutes between snapshots (builtin) |
| `BACKUP_ONLY_IF_PLAYERS` | `false` | skip snapshots when nobody has played since the last one |
| `LOCAL_KEEP` | `96` | local zips to keep |
| `WATCH_DIR` | `ftbbackups3` | folder to watch, relative to `SERVER_DIR` (external) |
| `EXTERNAL_BACKUP_CMD` | `ftbbackups3 start` | console command behind `/backup` (external) |
| `RCLONE_REMOTE` | `gdrive:mc-backups` | rclone destination |
| `REMOTE_KEEP` | `12h` | Drive retention before visible backup files are moved to trash, as an rclone duration |
| `REMOTE_TRASH_KEEP` | `24h` | Google Drive trash retention before old trashed backup files are permanently deleted; set empty, `0`, or `false` to disable |
| `CONFIG_TARGETS` | `config,moddata,...` | files and dirs in the config archive, relative to `SERVER_DIR`; the bot skips entries that don't exist |
| `CONFIG_EXCLUDES` | `config/fancymenu,config/spark` | paths left out of the config archive (bloat, churn) |
| `TPS_COMMAND` | auto | console command behind `/tps`; tries `neoforge tps`, `forge tps`, `tps` when unset |

## Restore

Download the newest world zip and `config-*.tar.zst` from Drive or from the links in your channel, extract both into the server directory, start the server.

## Notes

- Drive links come from `rclone link` and open for anyone who has them; treat them as public
- The world archive fails instead of succeeding partially if it cannot read a selected world target or file. It skips only `session.lock` and `*.tmp` files.
- Paper and Spigot keep the nether and end in separate `world_nether` and `world_the_end` folders; the bot auto-includes those when `WORLD_TARGETS` is unset. If your server stores world data in other sibling folders, set `WORLD_TARGETS=world,world_nether,world_the_end,other_world_folder`.
- After setup, inspect a test zip with `unzip -l backups/newest.zip | rg 'world/(level.dat|playerdata|region|stats)'` before relying on retention.
- The bot reads server files straight from disk, so it has to run on the same machine as the server
