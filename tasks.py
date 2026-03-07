import asyncio
import datetime
import glob
import json
import logging
import os
import sqlite3

from core import bot, redis, points_redis
from config import SUPER_ADMIN_ID, TZ_BJ
from models import get_all_player_uids, get_player, collect_resources

logger = logging.getLogger(__name__)

DB_FILE = "backup.db"
BACKUP_GLOB = "backup_*.db"
BACKUP_KEEP = 3
AUTO_COLLECT_TICK_SECONDS = 30


def _points_key(uid: str) -> str:
    return f"user_balance:{uid}"


def list_backup_files() -> list[str]:
    files = sorted(glob.glob(BACKUP_GLOB), reverse=True)
    if not files and os.path.exists(DB_FILE):
        return [DB_FILE]
    return files


def get_latest_backup_path() -> str | None:
    files = list_backup_files()
    return files[0] if files else None


def _new_backup_path() -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    return f"backup_{ts}.db"


def _prune_old_backups() -> None:
    files = sorted(glob.glob(BACKUP_GLOB), reverse=True)
    for stale in files[BACKUP_KEEP:]:
        try:
            os.remove(stale)
        except OSError as e:
            logger.warning("清理旧备份失败: %s err=%s", stale, e)
    if files and os.path.exists(DB_FILE):
        try:
            os.remove(DB_FILE)
        except OSError as e:
            logger.warning("清理旧格式备份失败: %s err=%s", DB_FILE, e)


def _init_db(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (
        uid TEXT PRIMARY KEY,
        name TEXT,
        gold REAL,
        elixir REAL,
        points REAL DEFAULT 0,
        buildings TEXT,
        troops TEXT,
        shield_until REAL,
        clan_id TEXT,
        last_collect REAL,
        attack_wins INTEGER,
        attack_losses INTEGER,
        trophies INTEGER,
        auto_collect_until REAL DEFAULT 0,
        created_at TEXT
    )''')
    cols = [row[1] for row in c.execute("PRAGMA table_info(players)").fetchall()]
    if "points" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN points REAL DEFAULT 0")
    if "auto_collect_until" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN auto_collect_until REAL DEFAULT 0")
    c.execute('''CREATE TABLE IF NOT EXISTS clans (
        clan_id TEXT PRIMARY KEY,
        name TEXT,
        leader TEXT,
        level INTEGER,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS clan_members (
        clan_id TEXT,
        uid TEXT,
        PRIMARY KEY (clan_id, uid)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS battle_logs (
        uid TEXT,
        idx INTEGER,
        log_data TEXT,
        PRIMARY KEY (uid, idx)
    )''')
    conn.commit()


async def perform_backup() -> dict:
    """备份全部 Redis 数据到 SQLite，返回统计信息。"""
    # ── 从 Redis 读取所有数据 ──
    player_uids = list(await redis.smembers("coc:all_players"))
    players_data = []
    battles_data = []
    for uid in player_uids:
        raw = await redis.hgetall(f"coc:{uid}")
        if raw:
            shared_points_raw = await points_redis.get(_points_key(uid))
            points_val = float(shared_points_raw) if shared_points_raw is not None else float(raw.get("points", 0))
            players_data.append((
                uid,
                raw.get("name", ""),
                float(raw.get("gold", 0)),
                float(raw.get("elixir", 0)),
                points_val,
                raw.get("buildings", "{}"),
                raw.get("troops", "{}"),
                float(raw.get("shield_until", 0)),
                raw.get("clan_id", ""),
                float(raw.get("last_collect", 0)),
                int(raw.get("attack_wins", 0)),
                int(raw.get("attack_losses", 0)),
                int(raw.get("trophies", 0)),
                float(raw.get("auto_collect_until", 0)),
                raw.get("created_at", ""),
            ))
        # 战斗日志
        logs = await redis.lrange(f"coc:{uid}:battles", 0, 99)
        for idx, log in enumerate(logs):
            battles_data.append((uid, idx, log))

    clan_ids = list(await redis.smembers("coc:all_clans"))
    clans_data = []
    members_data = []
    for cid in clan_ids:
        raw = await redis.hgetall(f"clan:{cid}")
        if raw:
            clans_data.append((
                cid,
                raw.get("name", ""),
                raw.get("leader", ""),
                int(raw.get("level", 1)),
                raw.get("created_at", ""),
            ))
        mems = await redis.smembers(f"clan_members:{cid}")
        for m_uid in mems:
            members_data.append((cid, m_uid))

    # ── 写入 SQLite（在线程中执行阻塞 IO）──
    # 与 dice_bot 一致：使用 INSERT OR REPLACE 增量覆盖，不做全表清空。
    backup_file = _new_backup_path()

    def db_write():
        conn = sqlite3.connect(backup_file)
        _init_db(conn)
        c = conn.cursor()
        c.execute("BEGIN TRANSACTION")
        c.executemany(
            "INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            players_data,
        )
        c.executemany(
            "INSERT OR REPLACE INTO clans VALUES (?,?,?,?,?)",
            clans_data,
        )
        c.executemany(
            "INSERT OR REPLACE INTO clan_members VALUES (?,?)",
            members_data,
        )
        c.executemany(
            "INSERT OR REPLACE INTO battle_logs VALUES (?,?,?)",
            battles_data,
        )
        conn.commit()
        conn.close()

    await asyncio.to_thread(db_write)
    _prune_old_backups()
    stats = {
        "players": len(players_data),
        "clans": len(clans_data),
        "battles": len(battles_data),
        "backup_file": backup_file,
    }
    logger.info(f"备份完成: {stats}")
    return stats


async def perform_restore() -> dict:
    """从 SQLite 恢复全部数据到 Redis，返回统计信息。"""
    def _round_half_up(n: float) -> int:
        if n >= 0:
            return int(n + 0.5)
        return int(n - 0.5)

    backup_file = get_latest_backup_path()
    if not backup_file:
        return {}

    def db_read():
        conn = sqlite3.connect(backup_file)
        _init_db(conn)
        c = conn.cursor()
        cols = [row[1] for row in c.execute("PRAGMA table_info(players)").fetchall()]
        has_points = "points" in cols
        has_auto_collect = "auto_collect_until" in cols
        if has_points and has_auto_collect:
            c.execute("SELECT * FROM players")
        elif has_points and not has_auto_collect:
            c.execute("SELECT uid,name,gold,elixir,points,buildings,troops,shield_until,clan_id,last_collect,attack_wins,attack_losses,trophies,0 as auto_collect_until,created_at FROM players")
        elif (not has_points) and has_auto_collect:
            c.execute("SELECT uid,name,gold,elixir,0 as points,buildings,troops,shield_until,clan_id,last_collect,attack_wins,attack_losses,trophies,auto_collect_until,created_at FROM players")
        else:
            c.execute("SELECT uid,name,gold,elixir,0 as points,buildings,troops,shield_until,clan_id,last_collect,attack_wins,attack_losses,trophies,0 as auto_collect_until,created_at FROM players")
        players = c.fetchall()
        c.execute("SELECT * FROM clans")
        clans = c.fetchall()
        c.execute("SELECT * FROM clan_members")
        members = c.fetchall()
        c.execute("SELECT * FROM battle_logs")
        battles = c.fetchall()
        conn.close()
        return players, clans, members, battles

    players, clans, members, battles = await asyncio.to_thread(db_read)
    if not players and not clans:
        return {}

    # ── 恢复玩家 ──
    pipe = redis.pipeline()
    points_pipe = points_redis.pipeline()
    pipe.delete("coc:all_players")
    for row in players:
        uid = row[0]
        mapping = {
            "name": row[1],
            "gold": str(_round_half_up(float(row[2] or 0))),
            "elixir": str(_round_half_up(float(row[3] or 0))),
            "points": str(row[4]),
            "buildings": row[5],
            "troops": row[6],
            "shield_until": str(row[7]),
            "clan_id": row[8],
                "last_collect": str(row[9]),
                "attack_wins": str(row[10]),
                "attack_losses": str(row[11]),
                "trophies": str(row[12]),
                "auto_collect_until": str(row[13]),
                "created_at": row[14],
            }
        pipe.hset(f"coc:{uid}", mapping=mapping)
        pipe.sadd("coc:all_players", uid)
        points_pipe.set(_points_key(uid), str(row[4]))
    await pipe.execute()
    await points_pipe.execute()

    # ── 恢复部落 ──
    pipe = redis.pipeline()
    pipe.delete("coc:all_clans")
    for row in clans:
        cid = row[0]
        pipe.hset(f"clan:{cid}", mapping={
            "name": row[1],
            "leader": row[2],
            "level": str(row[3]),
            "created_at": row[4],
        })
        pipe.sadd("coc:all_clans", cid)
    await pipe.execute()

    # ── 恢复部落成员 ──
    # 先删除旧的 clan_members 集合
    for row in clans:
        cid = row[0]
        await redis.delete(f"clan_members:{cid}")
    pipe = redis.pipeline()
    for row in members:
        pipe.sadd(f"clan_members:{row[0]}", row[1])
    await pipe.execute()

    # ── 恢复战斗日志 ──
    restored_uids = set()
    for row in players:
        uid = row[0]
        await redis.delete(f"coc:{uid}:battles")
        restored_uids.add(uid)
    # 按 idx 排序写入（idx=0 是最新的，lpush 需要倒序）
    battles_by_uid = {}
    for uid, idx, log_data in battles:
        battles_by_uid.setdefault(uid, []).append((idx, log_data))
    for uid, logs in battles_by_uid.items():
        logs.sort(key=lambda x: x[0], reverse=True)
        pipe = redis.pipeline()
        for _, log_data in logs:
            pipe.lpush(f"coc:{uid}:battles", log_data)
        pipe.ltrim(f"coc:{uid}:battles", 0, 99)
        await pipe.execute()

    stats = {
        "players": len(players),
        "clans": len(clans),
        "battles": len(battles),
        "backup_file": backup_file,
    }
    logger.info(f"恢复完成: {stats}")
    return stats


async def hourly_backup_task():
    """每小时整点自动备份并通知超管。"""
    while True:
        now = datetime.datetime.now(TZ_BJ)
        next_run = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        await asyncio.sleep((next_run - now).total_seconds())

        try:
            stats = await perform_backup()
            latest = stats.get("backup_file") or get_latest_backup_path() or "无"
            await bot.send_message(
                chat_id=SUPER_ADMIN_ID,
                text=(
                    f"🛡 <b>系统自动通报：每小时灾备完成</b>\n\n"
                    f"⏰ 时间：{datetime.datetime.now(TZ_BJ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"👤 玩家：<b>{stats['players']}</b> 条\n"
                    f"🏯 部落：<b>{stats['clans']}</b> 个\n"
                    f"⚔️ 战斗日志：<b>{stats['battles']}</b> 条\n"
                    f"🗂 最新备份：<code>{latest}</code>\n"
                    f"♻️ 仅保留最近 <b>{BACKUP_KEEP}</b> 份。"
                ),
            )
        except Exception as e:
            logger.error(f"每小时备份失败: {e}")


async def auto_collect_task():
    """后台自动收集：按周期结算处于自动收集有效期内的玩家。"""
    while True:
        try:
            uids = await get_all_player_uids()
            for uid in uids:
                raw_until = await redis.hget(f"coc:{uid}", "auto_collect_until")
                if not raw_until:
                    continue
                until = float(raw_until)
                if until <= 0:
                    continue
                p = await get_player(uid)
                if not p:
                    continue
                # 即使已过期，也补算到截止时刻，确保 6 小时收益完整到账。
                await collect_resources(uid, p, until_ts=until)
        except Exception as e:
            logger.error(f"自动收集任务异常: {e}")
        await asyncio.sleep(AUTO_COLLECT_TICK_SECONDS)
