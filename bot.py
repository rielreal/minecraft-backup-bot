import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

BASE = Path(__file__).resolve().parent
SERVER = Path(os.environ.get("SERVER_DIR", "/root/server"))
RCON_CONFIG = os.environ.get("RCON_CONFIG", "/root/.rcon-cli.yaml")

BACKUP_MODE = os.environ.get("BACKUP_MODE", "builtin")
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL_MIN", "15"))
BACKUP_ONLY_IF_PLAYERS = os.environ.get("BACKUP_ONLY_IF_PLAYERS", "false").lower() == "true"
LOCAL_KEEP = int(os.environ.get("LOCAL_KEEP", "96"))
WATCH_DIR = SERVER / os.environ.get("WATCH_DIR", "ftbbackups3")
EXTERNAL_BACKUP_CMD = os.environ.get("EXTERNAL_BACKUP_CMD", "ftbbackups3 start")

BACKUP_DIR = (BASE / "backups") if BACKUP_MODE == "builtin" else WATCH_DIR
STATE_FILE = BASE / "state.json"
TMP = BASE / "tmp"

ANSI = re.compile(r"\x1b\[[0-9;]*m|§.")
SPARK_URL = re.compile(r"https://spark\.lucko\.me/(?!docs)\S+")


def csv_list(value):
    return [v.strip() for v in value.split(",") if v.strip()]


CONFIG_TARGETS = csv_list(os.environ.get(
    "CONFIG_TARGETS", "config,moddata,schematics,server.properties,whitelist.json,ops.json"
))
CONFIG_EXCLUDES = tuple(csv_list(os.environ.get("CONFIG_EXCLUDES", "config/fancymenu,config/spark")))
REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:mc-backups")
REMOTE_MIN_AGE = os.environ.get("REMOTE_KEEP", "12h")
REMOTE_TRASH_MIN_AGE = os.environ.get("REMOTE_TRASH_KEEP", "24h")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])

TPS_COMMAND = os.environ.get("TPS_COMMAND", "")
TPS_CANDIDATES = ("neoforge tps", "forge tps", "tps")


def detect_world():
    try:
        for line in (SERVER / "server.properties").read_text().splitlines():
            if line.startswith("level-name="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "world"


WORLD_DIR = os.environ.get("WORLD_DIR") or detect_world()
WORLD_TARGETS = csv_list(os.environ.get("WORLD_TARGETS", ""))


def rcon(cmd):
    try:
        r = subprocess.run(
            ["rcon-cli", "--config", RCON_CONFIG, cmd],
            capture_output=True, text=True, timeout=30,
        )
        return ANSI.sub("", r.stdout + r.stderr).strip()
    except Exception as e:
        return f"rcon error: {e}"


def player_count():
    m = re.search(r"There are (\d+)", rcon("list"))
    return int(m.group(1)) if m else None


def player_list():
    m = re.search(r"There are \d+ of a max of \d+ players online:\s*(.*)", rcon("list"))
    if not m:
        return None
    return {n.strip() for n in m.group(1).split(",") if n.strip()}


detected_tps_cmd = None


def tps_output():
    global detected_tps_cmd
    if TPS_COMMAND:
        return rcon(TPS_COMMAND)
    if detected_tps_cmd:
        return rcon(detected_tps_cmd)
    for c in TPS_CANDIDATES:
        out = rcon(c)
        if out and "Unknown" not in out and "rcon error" not in out:
            detected_tps_cmd = c
            return out
    return "No TPS command available on this server."


def java_stats():
    try:
        r = subprocess.run(["pgrep", "-x", "java"], capture_output=True, text=True, timeout=10)
        pid = r.stdout.split()[0]
        rss = None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
        uptime = time.time() - Path(f"/proc/{pid}").stat().st_mtime
        return rss, uptime
    except Exception:
        return None, None


def sys_mem():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
        return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]
    except Exception:
        return None, None


def log_size():
    p = SERVER / "logs" / "latest.log"
    return p.stat().st_size if p.exists() else 0


def log_tail_from(offset):
    p = SERVER / "logs" / "latest.log"
    try:
        with open(p, "rb") as f:
            f.seek(offset)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def rclone_up(path, sub):
    subprocess.run(
        ["rclone", "copy", str(path), f"{REMOTE}/{sub}/"],
        capture_output=True, text=True, timeout=600, check=True,
    )


def rclone_link(sub, name):
    try:
        r = subprocess.run(
            ["rclone", "link", f"{REMOTE}/{sub}/{name}"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def rclone_prune(sub):
    subprocess.run(
        ["rclone", "delete", f"{REMOTE}/{sub}/", "--min-age", REMOTE_MIN_AGE],
        capture_output=True, text=True, timeout=120,
    )


def rclone_prune_trash(sub):
    if REMOTE_TRASH_MIN_AGE.lower() in {"", "0", "false", "off", "none"}:
        return
    subprocess.run(
        [
            "rclone", "delete", f"{REMOTE}/{sub}/",
            "--drive-trashed-only",
            "--drive-use-trash=false",
            "--min-age", REMOTE_TRASH_MIN_AGE,
        ],
        capture_output=True, text=True, timeout=120,
    )


def rclone_prune_all(sub):
    rclone_prune(sub)
    rclone_prune_trash(sub)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    zips = sorted(BACKUP_DIR.glob("*.zip"))
    return {"uploaded": [z.name for z in zips[:-1]], "config_sig": ""}


def save_state(state):
    state["uploaded"] = state["uploaded"][-500:]
    STATE_FILE.write_text(json.dumps(state))


def config_sig():
    h = hashlib.sha256()
    for t in CONFIG_TARGETS:
        p = SERVER / t
        if p.is_file():
            h.update(t.encode())
            h.update(p.read_bytes())
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = str(f.relative_to(SERVER))
                if f.is_file() and not rel.startswith(CONFIG_EXCLUDES):
                    h.update(rel.encode())
                    try:
                        h.update(f.read_bytes())
                    except OSError:
                        pass
    return h.hexdigest()


def make_config_tar():
    TMP.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
    path = TMP / f"config-{stamp}.tar.zst"
    targets = [t for t in CONFIG_TARGETS if (SERVER / t).exists()]
    subprocess.run(
        ["tar", "--zstd"] + [f"--exclude={e}" for e in CONFIG_EXCLUDES] + ["-cf", str(path), "-C", str(SERVER)] + targets,
        check=True, timeout=300,
    )
    return path


def default_world_targets():
    targets = [WORLD_DIR]
    for extra in (f"{WORLD_DIR}_nether", f"{WORLD_DIR}_the_end"):
        if (SERVER / extra).exists():
            targets.append(extra)
    return targets


def checked_world_targets():
    targets = WORLD_TARGETS or default_world_targets()
    checked = []
    seen = set()
    for target in targets:
        p = Path(target)
        if p.is_absolute() or ".." in p.parts:
            raise RuntimeError(f"world target `{target}` must be relative to SERVER_DIR")
        if target in seen:
            continue
        seen.add(target)
        checked.append(target)
    return checked


def zip_dir_entry(zf, path, arcname, written):
    name = arcname.as_posix().rstrip("/") + "/"
    if name in written:
        return
    mtime = max(path.stat().st_mtime, 315532800)
    info = zipfile.ZipInfo(name, time.localtime(mtime)[:6])
    info.external_attr = (0o40755 & 0xFFFF) << 16
    zf.writestr(info, b"")
    written.add(name)


def stat_key(path):
    st = path.stat()
    return st.st_dev, st.st_ino


def rel_error_path(path):
    try:
        return str(Path(path).relative_to(SERVER))
    except (TypeError, ValueError):
        return str(path)


def write_backup_tree(zf, target, written):
    root = SERVER / target
    if not root.exists():
        raise RuntimeError(f"world target `{target}` does not exist")
    if not root.is_dir():
        raise RuntimeError(f"world target `{target}` is not a directory")

    errors = []
    copied = 0
    seen_dirs = set()
    try:
        seen_dirs.add(stat_key(root))
        zip_dir_entry(zf, root, root.relative_to(SERVER), written)
    except OSError as e:
        raise RuntimeError(f"cannot read world target `{target}`: {e}") from e

    def walk_error(e):
        errors.append(f"{rel_error_path(e.filename)}: {e}")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=True, onerror=walk_error):
        current = Path(dirpath)
        try:
            zip_dir_entry(zf, current, current.relative_to(SERVER), written)
        except OSError as e:
            errors.append(f"{current.relative_to(SERVER)}: {e}")
            dirnames[:] = []
            continue

        keep_dirs = []
        for name in sorted(dirnames):
            child = current / name
            try:
                key = stat_key(child)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                keep_dirs.append(name)
            except OSError as e:
                errors.append(f"{child.relative_to(SERVER)}: {e}")
        dirnames[:] = keep_dirs

        for name in sorted(filenames):
            f = current / name
            if f.name == "session.lock" or f.suffix == ".tmp":
                continue
            try:
                arcname = f.relative_to(SERVER).as_posix()
                if arcname in written:
                    continue
                zf.write(f, arcname)
                written.add(arcname)
                copied += 1
            except OSError as e:
                errors.append(f"{f.relative_to(SERVER)}: {e}")

    if errors:
        sample = "; ".join(errors[:10])
        more = f"; and {len(errors) - 10} more" if len(errors) > 10 else ""
        raise RuntimeError(f"world backup skipped unreadable paths: {sample}{more}")
    return copied


def make_world_backup():
    TMP.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)
    out = rcon("save-off")
    if "rcon error" in out:
        raise RuntimeError(out)
    try:
        rcon("save-all flush")
        time.sleep(2)
        stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        tmp_path = TMP / f"world-{stamp}.zip"
        targets = checked_world_targets()
        copied = 0
        written = set()
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for target in targets:
                    copied += write_backup_tree(zf, target, written)
            if copied == 0:
                raise RuntimeError(f"world backup found no files in: {', '.join(targets)}")
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        final = BACKUP_DIR / tmp_path.name
        shutil.move(tmp_path, final)
        return final
    finally:
        rcon("save-on")


def prune_local():
    if LOCAL_KEEP < 1:
        return
    zips = sorted(BACKUP_DIR.glob("*.zip"), key=lambda z: z.stat().st_mtime)
    for z in zips[:-LOCAL_KEEP]:
        z.unlink()


def last_backup_age():
    zips = sorted(BACKUP_DIR.glob("*.zip"), key=lambda z: z.stat().st_mtime)
    if not zips:
        return None, None
    return zips[-1], time.time() - zips[-1].stat().st_mtime


def backup_report(path):
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

    files = [n for n in names if not n.endswith("/")]
    level = [n for n in files if n.endswith("/level.dat")]
    playerdata = [n for n in files if "/playerdata/" in n and n.endswith(".dat")]
    stats = [n for n in files if "/stats/" in n and n.endswith(".json")]
    advancements = [n for n in files if "/advancements/" in n and n.endswith(".json")]
    regions = [n for n in files if "/region/" in n and n.endswith(".mca")]
    dimension_dirs = sorted({
        n.split("/dimensions/", 1)[1].split("/", 2)[0] + "/" + n.split("/dimensions/", 1)[1].split("/", 2)[1]
        for n in names
        if "/dimensions/" in n and len(n.split("/dimensions/", 1)[1].split("/", 2)) >= 2
    })

    lines = [
        f"`{path.name}` ({mb(path.stat().st_size)})",
        f"Files: {len(files)}",
        f"level.dat: {len(level)}",
        f"playerdata: {len(playerdata)}",
        f"stats: {len(stats)}",
        f"advancements: {len(advancements)}",
        f"region chunks: {len(regions)}",
    ]
    if dimension_dirs:
        dims = ", ".join(dimension_dirs[:12])
        more = f", +{len(dimension_dirs) - 12} more" if len(dimension_dirs) > 12 else ""
        lines.append(f"dimensions: {dims}{more}")
    return "\n".join(lines)


def mb(n):
    return f"{n / 1048576:.1f} MB"


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

last_players = None
played_since_backup = False


@tasks.loop(seconds=60)
async def scheduler():
    global played_since_backup
    if BACKUP_MODE != "builtin":
        return
    try:
        _, age = last_backup_age()
        if age is not None and age < BACKUP_INTERVAL * 60:
            return
        n = await asyncio.to_thread(player_count)
        if n is None:
            return
        if BACKUP_ONLY_IF_PLAYERS and not played_since_backup and n == 0:
            return
        await asyncio.to_thread(make_world_backup)
        played_since_backup = False
        await asyncio.to_thread(prune_local)
    except Exception as e:
        print(f"scheduler error: {e!r}", flush=True)
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(f"World backup failed: ```{str(e)[:500]}```")


@tasks.loop(seconds=60)
async def watcher():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return
    try:
        state = load_state()
        now = time.time()
        uploaded_any = False
        for z in sorted(BACKUP_DIR.glob("*.zip")):
            if z.name in state["uploaded"] or now - z.stat().st_mtime < 60:
                continue
            players = await asyncio.to_thread(player_count)
            ptxt = f", {players} online" if players is not None else ""
            try:
                await asyncio.to_thread(rclone_up, z, "world")
                link = await asyncio.to_thread(rclone_link, "world", z.name)
                label = f"[`{z.name}`](<{link}>)" if link else f"`{z.name}`"
                await channel.send(f"World backup {label} ({mb(z.stat().st_size)}{ptxt}) uploaded to Drive")
                uploaded_any = True
            except subprocess.CalledProcessError as e:
                await channel.send(f"Drive upload failed for `{z.name}`: ```{(e.stderr or '')[-500:]}```")
                continue
            state["uploaded"].append(z.name)
            save_state(state)
        if uploaded_any:
            await asyncio.to_thread(rclone_prune_all, "world")
        sig = await asyncio.to_thread(config_sig)
        if sig != state.get("config_sig"):
            tar = None
            try:
                tar = await asyncio.to_thread(make_config_tar)
                size = tar.stat().st_size
                await asyncio.to_thread(rclone_up, tar, "config")
                link = await asyncio.to_thread(rclone_link, "config", tar.name)
                label = f"[`{tar.name}`](<{link}>)" if link else f"`{tar.name}`"
                note = f"Config changed: {label} ({mb(size)}) uploaded to Drive"
                if size <= channel.guild.filesize_limit - 524288:
                    await channel.send(content=note, file=discord.File(tar))
                else:
                    await channel.send(note)
                state["config_sig"] = sig
                save_state(state)
                await asyncio.to_thread(rclone_prune_all, "config")
            except subprocess.CalledProcessError as e:
                await channel.send(f"Config upload failed: ```{(e.stderr or '')[-500:]}```")
            finally:
                if tar:
                    tar.unlink(missing_ok=True)
    except Exception as e:
        print(f"watcher error: {e!r}", flush=True)


@tasks.loop(seconds=20)
async def presence():
    global last_players, played_since_backup
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return
    try:
        cur = await asyncio.to_thread(player_list)
        if cur is None:
            return
        if cur:
            played_since_backup = True
        if last_players is None:
            last_players = cur
            return
        for n in sorted(cur - last_players):
            await channel.send(f"**{n}** joined ({len(cur)} online)")
        for n in sorted(last_players - cur):
            await channel.send(f"**{n}** left ({len(cur)} online)")
        last_players = cur
    except Exception as e:
        print(f"presence error: {e!r}", flush=True)


@tree.command(name="rcon", description="Run a server console command")
@app_commands.describe(command="Console command without leading slash")
async def rcon_cmd(interaction: discord.Interaction, command: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await interaction.response.defer()
    out = await asyncio.to_thread(rcon, command)
    await interaction.followup.send(f"```\n{out[:1900] or '(no output)'}\n```")


@tree.command(name="backup", description="Trigger a world backup now")
async def backup_cmd(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await interaction.response.defer()
    if BACKUP_MODE == "builtin":
        try:
            path = await asyncio.to_thread(make_world_backup)
            await interaction.followup.send(f"Backup `{path.name}` created ({mb(path.stat().st_size)}) — uploading to Drive shortly.")
        except Exception as e:
            await interaction.followup.send(f"Backup failed: ```{str(e)[:500]}```")
    else:
        out = await asyncio.to_thread(rcon, EXTERNAL_BACKUP_CMD)
        await interaction.followup.send(f"Backup started — will upload to Drive when done.\n```\n{out[:500] or 'ok'}\n```")


@tree.command(name="status", description="Server and backup status")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    players = await asyncio.to_thread(rcon, "list")
    z, age = last_backup_age()
    last = f"`{z.name}` ({mb(z.stat().st_size)}, {int(age / 60)} min ago)" if z and age is not None else "none"
    if BACKUP_MODE == "builtin":
        nxt = max(0, BACKUP_INTERVAL * 60 - (age or 0))
        nxt_txt = f"Next backup in: {int(nxt / 60):02d}:{int(nxt % 60):02d}"
    else:
        nxt_txt = await asyncio.to_thread(rcon, "ftbbackups3 time")
    du = shutil.disk_usage("/")
    await interaction.followup.send(
        f"**Players:** {players}\n**Last backup:** {last}\n**{nxt_txt}**\n"
        f"**Disk:** {du.free // 1073741824} GB free of {du.total // 1073741824} GB"
    )


@tree.command(name="tps", description="Tick rate per dimension")
async def tps_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    out = await asyncio.to_thread(tps_output)
    await interaction.followup.send(f"```\n{out[:1900] or '(no output)'}\n```")


@tree.command(name="health", description="Server performance and resource usage")
async def health_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    tps = await asyncio.to_thread(tps_output)
    overall = [l for l in tps.splitlines() if l.lower().startswith("overall")]
    tps_line = overall[0] if overall else (tps.splitlines()[0] if tps else "n/a")
    rss, up = await asyncio.to_thread(java_stats)
    used, total = sys_mem()
    du = shutil.disk_usage("/")
    lines = [f"**TPS:** {tps_line}"]
    if rss is not None:
        lines.append(f"**Server memory:** {rss / 1073741824:.1f} GB")
    if up is not None:
        lines.append(f"**Server uptime:** {int(up / 3600)}h {int(up % 3600 / 60)}m")
    if used is not None and total is not None:
        lines.append(f"**System memory:** {used / 1073741824:.1f} / {total / 1073741824:.0f} GB")
    lines.append(f"**Disk:** {du.free // 1073741824} GB free of {du.total // 1073741824} GB")
    await interaction.followup.send("\n".join(lines))


@tree.command(name="profile", description="Run the spark profiler and get a report link")
@app_commands.describe(seconds="Profiling duration in seconds (10-300, default 30)")
async def profile_cmd(interaction: discord.Interaction, seconds: int = 30):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    seconds = max(10, min(300, seconds))
    await interaction.response.defer()
    offset = log_size()
    out = await asyncio.to_thread(rcon, f"spark profiler start --timeout {seconds}")
    if "Unknown" in out or "rcon error" in out:
        await interaction.followup.send("spark is not available on this server.")
        return
    await interaction.followup.send(f"Profiling for {seconds}s — report link will follow.")
    deadline = time.time() + seconds + 60
    while time.time() < deadline:
        await asyncio.sleep(5)
        m = SPARK_URL.search(log_tail_from(offset))
        if m:
            await interaction.followup.send(f"Profiler report: {m.group(0)}")
            return
    await interaction.followup.send("Profiler finished but no report link appeared in the log.")


@tree.command(name="backups", description="List recent backups")
async def backups_cmd(interaction: discord.Interaction):
    zips = sorted(BACKUP_DIR.glob("*.zip"), key=lambda z: z.stat().st_mtime, reverse=True)[:10]
    if not zips:
        await interaction.response.send_message("No backups yet.")
        return
    lines = [f"`{z.name}` — {mb(z.stat().st_size)}" for z in zips]
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="backupcheck", description="Inspect the newest local backup zip")
async def backupcheck_cmd(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    z, _ = last_backup_age()
    if not z:
        await interaction.followup.send("No local backup zip found.", ephemeral=True)
        return
    try:
        report = await asyncio.to_thread(backup_report, z)
        await interaction.followup.send(f"```\n{report[:1900]}\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Backup check failed: ```{str(e)[:500]}```", ephemeral=True)


@bot.event
async def setup_hook():
    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} (mode: {BACKUP_MODE}, world: {WORLD_DIR})", flush=True)
    for loop in (watcher, presence, scheduler):
        if not loop.is_running():
            loop.start()


if __name__ == "__main__":
    bot.run(TOKEN)
