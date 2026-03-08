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
from config import (
    SUPER_ADMIN_ID, TZ_BJ, LOOT_PERCENT, ALLOWED_CHAT_ID,
    SHIELD_DECAY_THRESHOLD_BASE, SHIELD_DECAY_THRESHOLD_PER_TH, SHIELD_DECAY_NEWBIE_GRACE,
    SHIELD_DECAY_RATE_LOW, SHIELD_DECAY_RATE_MID, SHIELD_DECAY_RATE_HIGH,
)
from models import (
    get_all_player_uids, get_player, collect_resources,
    add_gold, add_elixir, set_field, add_battle_log, set_building_damage,
    get_effective_building_defense, iter_damageable_defense_buildings,
    apply_building_damage_increments,
)
from combat import (
    _pending_collectable, _calc_resource_loot, _estimate_last_collect_after_loot,
)
from utils import safe_html, fmt_num, send

logger = logging.getLogger(__name__)
BOT_ATTACKER_NAMES = [
    "🐺 狼群",
    "🐻 熊群",
    "🐗 野猪群",
    "🦅 鹰群",
    "🐍 蛇群",
    "🐒 猴群",
]
BOT_ATTACKER_PROFILES = {
    "🐺 狼群": {"atk_min": 800, "atk_max": 4200, "dmg_scale": 1.0, "mult": {"cannon": 0.95, "archer_tower": 1.10, "wall": 1.05}},
    "🐻 熊群": {"atk_min": 1000, "atk_max": 5000, "dmg_scale": 1.2, "mult": {"cannon": 1.20, "archer_tower": 0.90, "wall": 1.10}},
    "🐗 野猪群": {"atk_min": 900, "atk_max": 4600, "dmg_scale": 1.15, "mult": {"cannon": 1.10, "archer_tower": 0.95, "wall": 1.25}},
    "🦅 鹰群": {"atk_min": 850, "atk_max": 4300, "dmg_scale": 0.95, "mult": {"cannon": 0.85, "archer_tower": 1.25, "wall": 0.80}},
    "🐍 蛇群": {"atk_min": 750, "atk_max": 3900, "dmg_scale": 0.9, "mult": {"cannon": 1.05, "archer_tower": 1.00, "wall": 0.90}},
    "🐒 猴群": {"atk_min": 820, "atk_max": 4100, "dmg_scale": 1.0, "mult": {"cannon": 0.90, "archer_tower": 1.15, "wall": 0.95}},
}
BOT_ATTACK_WINDOW_SECONDS = 5 * 60 * 60   # 5h 一个窗口
BOT_ATTACK_WINDOW_MAX_TARGETS = 5         # 每个窗口最多袭击 5 人
BOT_ATTACK_MIN_GAP_SECONDS = 25 * 60      # 同窗口内袭击最小间隔，避免连发
BOT_ATTACK_PLAN_KEY = "coc:bot_attack_window_plan"

DB_FILE = "backup.db"
BACKUP_GLOB = "backup_*.db"
BACKUP_KEEP = 3
AUTO_COLLECT_TICK_SECONDS = 30
SHIELD_DECAY_TICK_SECONDS = 60


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
        shield_observe_hits INTEGER DEFAULT 0,
        bot_last_attack REAL DEFAULT 0,
        bot_next_attack_at REAL DEFAULT 0,
        building_damage TEXT DEFAULT '{}',
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
    if "shield_observe_hits" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN shield_observe_hits INTEGER DEFAULT 0")
    if "bot_last_attack" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN bot_last_attack REAL DEFAULT 0")
    if "bot_next_attack_at" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN bot_next_attack_at REAL DEFAULT 0")
    if "building_damage" not in cols:
        c.execute("ALTER TABLE players ADD COLUMN building_damage TEXT DEFAULT '{}'")
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
                int(raw.get("shield_observe_hits", 0)),
                float(raw.get("bot_last_attack", 0)),
                float(raw.get("bot_next_attack_at", 0)),
                raw.get("building_damage", "{}"),
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
            "INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            + ("shield_observe_hits" if "shield_observe_hits" in has else "0 as shield_observe_hits")
            + ","
            + ("bot_last_attack" if "bot_last_attack" in has else "0 as bot_last_attack")
            + ","
            + ("bot_next_attack_at" if "bot_next_attack_at" in has else "0 as bot_next_attack_at")
            + ","
            + ("building_damage" if "building_damage" in has else "'{}' as building_damage")
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
            "shield_observe_hits": str(row[17]),
            "bot_last_attack": str(row[18]),
            "bot_next_attack_at": str(row[19]),
            "building_damage": row[20],
            "created_at": row[21],
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


def _shield_decay_rate_per_hour(p: dict) -> float:
    """返回每小时额外衰减秒数。"""
    th_lv = int(p.get("buildings", {}).get("town_hall", 1))
    threshold = SHIELD_DECAY_THRESHOLD_BASE + th_lv * SHIELD_DECAY_THRESHOLD_PER_TH
    threshold = max(1.0, float(threshold))
    loot_total = float(int(p.get("gold", 0)) + int(p.get("elixir", 0)))
    loot_total += float(_pending_collectable(p, "gold") + _pending_collectable(p, "elixir"))
    ratio = loot_total / threshold
    if ratio >= 2.0:
        return float(SHIELD_DECAY_RATE_HIGH)
    if ratio >= 1.5:
        return float(SHIELD_DECAY_RATE_MID)
    if ratio >= 1.0:
        return float(SHIELD_DECAY_RATE_LOW)
    return 0.0


async def shield_decay_task():
    """护盾溢出衰减：资源越多，护盾掉得越快。"""
    last_run = time.time()
    while True:
        try:
            now = time.time()
            delta = max(1.0, now - last_run)
            last_run = now
            uids = await get_all_player_uids()
            for uid in uids:
                p = await get_player(uid)
                if not p:
                    continue
                shield_until = float(p.get("shield_until", 0))
                if shield_until <= now:
                    continue
                # 新手宽限
                created_at = float(p.get("created_at", 0))
                if created_at > 0 and (now - created_at) < SHIELD_DECAY_NEWBIE_GRACE:
                    continue
                decay_per_hour = _shield_decay_rate_per_hour(p)
                if decay_per_hour <= 0:
                    continue
                decay_seconds = decay_per_hour * (delta / 3600.0)
                new_until = shield_until - decay_seconds
                if new_until <= now:
                    await set_field(uid, "shield_until", 0)
                    await set_field(uid, "shield_source", "")
                    await set_field(uid, "shield_purchase_points", 0)
                    await set_field(uid, "shield_refund_eligible", 0)
                else:
                    await set_field(uid, "shield_until", new_until)
        except Exception as e:
            logger.error(f"护盾衰减任务异常: {e}")
        await asyncio.sleep(SHIELD_DECAY_TICK_SECONDS)


def _defense_group_by_bid(bid: str) -> str:
    if bid == "wall":
        return "wall"
    if bid == "cannon" or bid.startswith("cannon_"):
        return "cannon"
    return "archer_tower"


def _wildlife_defense_power(p: dict, attacker: str) -> float:
    profile = BOT_ATTACKER_PROFILES.get(attacker, {})
    mult = profile.get("mult", {})
    total = 0.0
    for bid in iter_damageable_defense_buildings(p):
        base_group = _defense_group_by_bid(bid)
        ratio = float(mult.get(base_group, 1.0))
        total += get_effective_building_defense(p, bid) * ratio
    return total


def _bot_attack_stars(defender: dict, attacker: str) -> int:
    profile = BOT_ATTACKER_PROFILES.get(attacker, {})
    attack_power = random.uniform(float(profile.get("atk_min", 900)), float(profile.get("atk_max", 5200)))
    final_atk = attack_power * random.uniform(0.85, 1.15)
    final_def = max(1.0, _wildlife_defense_power(defender, attacker) * random.uniform(0.85, 1.15))
    ratio = final_atk / final_def
    if ratio >= 2.0:
        return 3
    if ratio >= 1.2:
        return 2
    if ratio >= 0.6:
        return 1
    return 0


def _calc_wildlife_damage_increments(p: dict, stars: int, attacker: str) -> dict[str, float]:
    """按袭击星级和动物克制关系，生成建筑损伤增量。"""
    profile = BOT_ATTACKER_PROFILES.get(attacker, {})
    mult = profile.get("mult", {})
    dmg_scale = float(profile.get("dmg_scale", 1.0))
    star_factor = {0: 0.5, 1: 0.9, 2: 1.25, 3: 1.6}.get(stars, 0.9)
    increments: dict[str, float] = {}
    for bid in iter_damageable_defense_buildings(p):
        group = _defense_group_by_bid(bid)
        group_mult = float(mult.get(group, 1.0))
        inc = random.uniform(0.008, 0.028) * star_factor * dmg_scale * group_mult
        increments[bid] = min(0.22, max(0.0, inc))
    return increments


def _current_attack_window_start(now: float) -> int:
    now_int = int(now)
    return now_int - (now_int % BOT_ATTACK_WINDOW_SECONDS)


def _generate_spread_offsets(count: int, span_seconds: int, min_gap_seconds: int) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [random.uniform(0.0, float(span_seconds))]
    max_usable_gap = span_seconds // max(1, count - 1)
    gap = int(max(0, min(min_gap_seconds, max_usable_gap)))
    slack = max(0.0, float(span_seconds - gap * (count - 1)))
    anchors = sorted(random.uniform(0.0, slack) for _ in range(count))
    return [anchors[i] + i * gap for i in range(count)]


def _build_window_plan(window_start: int, uids: list[str]) -> dict:
    max_targets = min(BOT_ATTACK_WINDOW_MAX_TARGETS, len(uids))
    if max_targets <= 0:
        return {"window_start": window_start, "events": []}
    chosen_uids = random.sample(uids, k=max_targets)
    # 给窗口前后留少量边距，减少窗口边界处“扎堆”触发
    edge_padding = min(8 * 60, BOT_ATTACK_WINDOW_SECONDS // 10)
    span = max(1, BOT_ATTACK_WINDOW_SECONDS - edge_padding * 2)
    offsets = _generate_spread_offsets(max_targets, span, BOT_ATTACK_MIN_GAP_SECONDS)
    events: list[dict] = []
    for uid, offset in zip(chosen_uids, offsets):
        at = float(window_start + edge_padding + int(offset))
        events.append({"uid": uid, "at": at, "done": 0})
    events.sort(key=lambda e: float(e.get("at", 0)))
    return {"window_start": window_start, "events": events}


async def _load_or_create_attack_plan(now: float) -> dict:
    current_start = _current_attack_window_start(now)
    raw = await redis.get(BOT_ATTACK_PLAN_KEY)
    if raw:
        try:
            plan = json.loads(raw)
            if int(plan.get("window_start", -1)) == current_start:
                return plan
        except Exception:
            pass
    uids = list(await get_all_player_uids())
    plan = _build_window_plan(current_start, uids)
    await redis.set(BOT_ATTACK_PLAN_KEY, json.dumps(plan, ensure_ascii=False))
    return plan


async def _save_attack_plan(plan: dict) -> None:
    await redis.set(BOT_ATTACK_PLAN_KEY, json.dumps(plan, ensure_ascii=False))


async def _notify_bot_attack(uid: str, p: dict, result: dict):
    stars = int(result.get("stars", 0))
    failed_by_shield = bool(result.get("failed_by_shield", False))
    gold = int(result.get("gold", 0))
    elixir = int(result.get("elixir", 0))
    attacker = str(result.get("attacker", "🤖 袭击者"))
    shield_cut_seconds = int(result.get("shield_cut_seconds", 0))
    target = safe_html(p.get("name", "未知玩家"))
    if failed_by_shield:
        cut_text = ""
        if shield_cut_seconds > 0:
            h, m = divmod(shield_cut_seconds // 60, 60)
            cut_text = f"\n护盾惩罚：🛡️ -{h}小时{m}分钟（剩余时间减半）"
        text = (
            f"⚠️ 野外袭击通知\n"
            f"{attacker} 试图袭击 {target} 的基地，但护盾生效，进攻失败。\n"
            f"结算：⭐0  |  💰0  |  💧0"
            f"{cut_text}"
        )
    else:
        text = (
            f"⚠️ 野外袭击通知\n"
            f"{attacker} 袭击了 {target} 的基地！\n"
            f"结算：{'⭐' * stars if stars > 0 else '⭐0'}  |  💰-{fmt_num(gold)}  |  💧-{fmt_num(elixir)}"
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
    attacker = random.choice(BOT_ATTACKER_NAMES)
    if float(p.get("shield_until", 0)) > now:
        shield_until = float(p.get("shield_until", 0))
        remaining = max(0.0, shield_until - now)
        cut_seconds = int(remaining * 0.5)
        new_until = now + (remaining * 0.5)
        await set_field(uid, "shield_until", new_until)
        await set_field(uid, "bot_last_attack", now)
        await add_battle_log(uid, {
            "type": "defense",
            "opponent": attacker,
            "stars": 0,
            "gold": 0,
            "elixir": 0,
            "trophies": 0,
            "time": now,
        })
        result = {
            "stars": 0,
            "gold": 0,
            "elixir": 0,
            "failed_by_shield": True,
            "attacker": attacker,
            "shield_cut_seconds": cut_seconds,
        }
        await _notify_bot_attack(uid, p, result)
        return

    stars = _bot_attack_stars(p, attacker)
    pct = LOOT_PERCENT[stars]
    pending_gold = _pending_collectable(p, "gold")
    pending_elixir = _pending_collectable(p, "elixir")
    gold_loot, gold_storage_loot, gold_collector_loot = _calc_resource_loot(
        int(p["gold"]), pending_gold, pct
    )
    elixir_loot, elixir_storage_loot, elixir_collector_loot = _calc_resource_loot(
        int(p["elixir"]), pending_elixir, pct
    )
    if gold_storage_loot > 0:
        await add_gold(uid, -gold_storage_loot)
    if elixir_storage_loot > 0:
        await add_elixir(uid, -elixir_storage_loot)
    if gold_collector_loot > 0 or elixir_collector_loot > 0:
        new_last = _estimate_last_collect_after_loot(p, gold_collector_loot, elixir_collector_loot)
        if new_last is not None:
            await set_field(uid, "last_collect", new_last)
    # 动物袭击不提供护盾；护盾只来自玩家攻击或积分购买。
    damage_increments = _calc_wildlife_damage_increments(p, stars, attacker)
    new_damage_map = apply_building_damage_increments(p, damage_increments)
    await set_building_damage(uid, new_damage_map)
    await set_field(uid, "bot_last_attack", time.time())
    await add_battle_log(uid, {
        "type": "defense",
        "opponent": attacker,
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
        "failed_by_shield": False,
        "attacker": attacker,
    }
    await _notify_bot_attack(uid, p, result)


async def random_bot_attack_task():
    """按 5 小时窗口分散袭击：每窗口最多随机 5 人，随机时间触发。"""
    while True:
        try:
            now = time.time()
            plan = await _load_or_create_attack_plan(now)
            events = plan.get("events", [])
            changed = False
            for event in events:
                if int(event.get("done", 0)) == 1:
                    continue
                attack_at = float(event.get("at", 0))
                if attack_at <= 0 or now < attack_at:
                    continue
                uid = str(event.get("uid", ""))
                if not uid:
                    event["done"] = 1
                    changed = True
                    continue
                p = await get_player(uid)
                if p:
                    await _execute_bot_attack(uid, p)
                event["done"] = 1
                changed = True
                # 单次循环只处理一个，避免同一时刻连发
                break
            if changed:
                await _save_attack_plan(plan)
        except Exception as e:
            logger.error(f"机器人随机进攻任务异常: {e}")
        await asyncio.sleep(random.randint(40, 80))
