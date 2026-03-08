import asyncio
import datetime
import glob
import json
import logging
import os
import random
import sqlite3
import time

from core import bot, redis, points_redis
from config import SUPER_ADMIN_ID, TZ_BJ, LOOT_PERCENT, ALLOWED_CHAT_ID
from models import (
    get_all_player_uids, get_player, collect_resources, get_defense_power,
    add_gold, add_elixir, set_field, add_battle_log,
)
from combat import (
    _pending_collectable, _calc_resource_loot, _estimate_last_collect_after_loot,
    calc_defense_shield_seconds,
)
from utils import mention, fmt_num, send

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
        shield_source TEXT DEFAULT '',
        shield_purchase_points REAL DEFAULT 0,
        shield_refund_eligible INTEGER DEFAULT 0,
        bot_last_attack REAL DEFAULT 0,
        created_at TEXT
    )''')
    cols = [row[1] for row in c.execute("PRAGMA table_info(players)").fetchall()]
    if "points" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN points REAL DEFAULT 0")
    if "auto_collect_until" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN auto_collect_until REAL DEFAULT 0")
    if "shield_source" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN shield_source TEXT DEFAULT ''")
    if "shield_purchase_points" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN shield_purchase_points REAL DEFAULT 0")
    if "shield_refund_eligible" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN shield_refund_eligible INTEGER DEFAULT 0")
    if "bot_last_attack" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN bot_last_attack REAL DEFAULT 0")
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
                raw.get("shield_source", ""),
                float(raw.get("shield_purchase_points", 0)),
                int(raw.get("shield_refund_eligible", 0)),
                float(raw.get("bot_last_attack", 0)),
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
            "INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        has = set(cols)
        c.execute(
            "SELECT "
            "uid,name,gold,elixir,"
            + ("points" if "points" in has else "0 as points")
            + ",buildings,troops,shield_until,clan_id,last_collect,attack_wins,attack_losses,trophies,"
            + ("auto_collect_until" if "auto_collect_until" in has else "0 as auto_collect_until")
            + ","
            + ("shield_source" if "shield_source" in has else "'' as shield_source")
            + ","
            + ("shield_purchase_points" if "shield_purchase_points" in has else "0 as shield_purchase_points")
            + ","
            + ("shield_refund_eligible" if "shield_refund_eligible" in has else "0 as shield_refund_eligible")
            + ","
            + ("bot_last_attack" if "bot_last_attack" in has else "0 as bot_last_attack")
            + ",created_at FROM players"
        )
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
            "shield_source": row[14],
            "shield_purchase_points": str(row[15]),
            "shield_refund_eligible": str(row[16]),
            "bot_last_attack": str(row[17]),
            "created_at": row[18],
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


def _bot_attack_stars(defense: float) -> int:
    attack_power = random.uniform(900, 5200)
    final_atk = attack_power * random.uniform(0.85, 1.15)
    final_def = max(1.0, defense * random.uniform(0.85, 1.15))
    ratio = final_atk / final_def
    if ratio >= 2.0:
        return 3
    if ratio >= 1.2:
        return 2
    if ratio >= 0.6:
        return 1
    return 0


def _bot_target_strength(p: dict) -> float:
    th_lv = int(p.get("buildings", {}).get("town_hall", 1))
    defense = float(get_defense_power(p))
    trophies = float(p.get("trophies", 0))
    th_score = min(1.0, max(0.0, th_lv / 10.0))
    def_score = min(1.0, max(0.0, defense / 22000.0))
    trophy_score = min(1.0, max(0.0, trophies / 6000.0))
    return 0.45 * th_score + 0.40 * def_score + 0.15 * trophy_score


def _bot_target_cooldown_seconds(p: dict) -> int:
    """越强冷却越短；最短 20 分钟，最长 1.5 小时。"""
    strength = _bot_target_strength(p)
    return int(round(5400 - 4200 * strength))


async def _notify_bot_attack(uid: str, p: dict, result: dict):
    stars = int(result.get("stars", 0))
    failed_by_shield = bool(result.get("failed_by_shield", False))
    gold = int(result.get("gold", 0))
    elixir = int(result.get("elixir", 0))
    shield_seconds = int(result.get("shield_seconds", 0))
    h, m = divmod(max(0, shield_seconds) // 60, 60)
    target = mention(uid, p.get("name", "未知玩家"))
    if failed_by_shield:
        text = (
            f"🤖 机器人突袭通知\n"
            f"{target} 的基地遭到攻击，但护盾生效，进攻失败。\n"
            f"结算：⭐0  |  💰0  |  💧0"
        )
    else:
        shield_text = f"\n🛡️ 防守护盾：{h}小时{m}分钟" if shield_seconds > 0 else ""
        text = (
            f"🤖 机器人突袭通知\n"
            f"{target} 的基地遭到攻击！\n"
            f"结算：{'⭐' * stars if stars > 0 else '⭐0'}  |  💰-{fmt_num(gold)}  |  💧-{fmt_num(elixir)}"
            f"{shield_text}"
        )
    try:
        if ALLOWED_CHAT_ID:
            await send(ALLOWED_CHAT_ID, text)
        else:
            await bot.send_message(chat_id=int(uid), text=text)
    except Exception as e:
        logger.warning("机器人进攻通知发送失败 uid=%s err=%s", uid, e)


async def _execute_bot_attack(uid: str, p: dict):
    now = time.time()
    if float(p.get("shield_until", 0)) > now:
        await set_field(uid, "bot_last_attack", now)
        await add_battle_log(uid, {
            "type": "defense",
            "opponent": "🤖 掠夺者",
            "stars": 0,
            "gold": 0,
            "elixir": 0,
            "trophies": 0,
            "time": now,
        })
        result = {"stars": 0, "gold": 0, "elixir": 0, "shield_seconds": 0, "failed_by_shield": True}
        await _notify_bot_attack(uid, p, result)
        return

    stars = _bot_attack_stars(get_defense_power(p))
    pct = LOOT_PERCENT[stars]
    pending_gold = _pending_collectable(p, "gold")
    pending_elixir = _pending_collectable(p, "elixir")
    gold_loot, gold_storage_loot, gold_collector_loot = _calc_resource_loot(
        int(p["gold"]), pending_gold, pct
    )
    elixir_loot, elixir_storage_loot, elixir_collector_loot = _calc_resource_loot(
        int(p["elixir"]), pending_elixir, pct
    )
    shield_seconds = 0
    if gold_storage_loot > 0:
        await add_gold(uid, -gold_storage_loot)
    if elixir_storage_loot > 0:
        await add_elixir(uid, -elixir_storage_loot)
    if gold_collector_loot > 0 or elixir_collector_loot > 0:
        new_last = _estimate_last_collect_after_loot(p, gold_collector_loot, elixir_collector_loot)
        if new_last is not None:
            await set_field(uid, "last_collect", new_last)
    if stars > 0:
        shield_seconds = calc_defense_shield_seconds(p, stars)
        shield = time.time() + shield_seconds
        await set_field(uid, "shield_until", shield)
        await set_field(uid, "shield_source", "defense")
        await set_field(uid, "shield_purchase_points", 0)
        await set_field(uid, "shield_refund_eligible", 0)
    await set_field(uid, "bot_last_attack", time.time())
    await add_battle_log(uid, {
        "type": "defense",
        "opponent": "🤖 掠夺者",
        "stars": stars,
        "gold": -int(gold_loot),
        "elixir": -int(elixir_loot),
        "trophies": 0,
        "time": time.time(),
    })
    result = {
        "stars": stars,
        "gold": int(gold_loot),
        "elixir": int(elixir_loot),
        "shield_seconds": shield_seconds,
        "failed_by_shield": False,
    }
    await _notify_bot_attack(uid, p, result)


async def random_bot_attack_task():
    """机器人随机时间、随机强度攻击任意玩家（护盾目标会失败并零结算）。"""
    next_attack_at = time.time() + random.randint(60, 180)
    while True:
        try:
            now = time.time()
            if now < next_attack_at:
                await asyncio.sleep(min(15, int(next_attack_at - now)))
                continue
            weighted_targets = []
            for uid in await get_all_player_uids():
                p = await get_player(uid)
                if not p:
                    continue
                elapsed = now - float(p.get("bot_last_attack", 0))
                cooldown = _bot_target_cooldown_seconds(p)
                if elapsed < cooldown:
                    continue
                strength = _bot_target_strength(p)
                urgency = min(2.0, elapsed / max(cooldown, 1))
                weight = (0.5 + 1.5 * strength) * (0.8 + 0.6 * urgency)
                weighted_targets.append((uid, p, max(0.01, weight)))

            if weighted_targets:
                choices = [(uid, p) for uid, p, _w in weighted_targets]
                weights = [w for _uid, _p, w in weighted_targets]
                target_uid, target_p = random.choices(choices, weights=weights, k=1)[0]
                await _execute_bot_attack(target_uid, target_p)
        except Exception as e:
            logger.error(f"机器人随机进攻任务异常: {e}")
        next_attack_at = time.time() + random.randint(120, 420)
