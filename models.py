import json
import math
import time
import uuid

from core import redis, points_redis
from config import (
    STARTING_GOLD, STARTING_ELIXIR, STARTING_POINTS, STARTING_BUILDINGS,
    BUILDINGS, TROOPS, CLAN_MAX_MEMBERS, NEWBIE_SHIELD,
)

SHARED_POINTS_INIT = 20000.0


def _round_half_up(n: float) -> int:
    if n >= 0:
        return int(math.floor(n + 0.5))
    return int(math.ceil(n - 0.5))


def _to_int_str(raw) -> str:
    if raw is None:
        return "0"
    s = str(raw).strip()
    try:
        return str(int(s))
    except Exception:
        return str(_round_half_up(float(s or 0)))


def _building_series_ids(base_bid: str) -> list[str]:
    """同类建筑序列（如 gold_mine / gold_mine_2 / gold_mine_3）。"""
    ids = [bid for bid in BUILDINGS if bid == base_bid or bid.startswith(f"{base_bid}_")]

    def _sort_key(bid: str) -> tuple[int, str]:
        if bid == base_bid:
            return 1, bid
        suffix = bid[len(base_bid) + 1:]
        if suffix.isdigit():
            return int(suffix), bid
        return 9999, bid

    return sorted(ids, key=_sort_key)


def _sum_capacity_by_series(bld: dict, base_bid: str, default_main_lv: int = 1) -> float:
    total = 0.0
    for bid in _building_series_ids(base_bid):
        if bid == base_bid:
            lv = bld.get(bid, default_main_lv)
        else:
            lv = bld.get(bid, 0)
        if lv > 0:
            total += BUILDINGS[bid]["capacity"][lv - 1]
    return total


# ───────────────────── 玩家 ─────────────────────

async def get_player(uid: str) -> dict | None:
    data = await redis.hgetall(f"coc:{uid}")
    if not data:
        return None
    p = _parse(data)
    if str(p["gold"]) != str(data.get("gold", "")) or str(p["elixir"]) != str(data.get("elixir", "")):
        await redis.hset(f"coc:{uid}", mapping={
            "gold": str(p["gold"]),
            "elixir": str(p["elixir"]),
        })
    p["points"] = await get_points(uid)
    return p


async def ensure_player(uid: str, name: str) -> dict:
    data = await redis.hgetall(f"coc:{uid}")
    if data:
        # 更新名字
        if data.get("name") != name:
            await redis.hset(f"coc:{uid}", "name", name)
            data["name"] = name
        p = _parse(data)
        if str(p["gold"]) != str(data.get("gold", "")) or str(p["elixir"]) != str(data.get("elixir", "")):
            await redis.hset(f"coc:{uid}", mapping={
                "gold": str(p["gold"]),
                "elixir": str(p["elixir"]),
            })
        if "auto_collect_until" not in data:
            await redis.hset(f"coc:{uid}", "auto_collect_until", "0")
        p["points"] = await get_points(uid)
        if abs(float(data.get("points", 0)) - p["points"]) > 1e-9:
            await redis.hset(f"coc:{uid}", "points", str(p["points"]))
        return p
    return await init_player(uid, name)


async def init_player(uid: str, name: str) -> dict:
    now = time.time()
    shared_points = await ensure_shared_points_account(uid)
    data = {
        "name": name,
        "gold": str(STARTING_GOLD),
        "elixir": str(STARTING_ELIXIR),
        "points": str(shared_points),
        "buildings": json.dumps(STARTING_BUILDINGS),
        "troops": json.dumps({}),
        "shield_until": str(now + NEWBIE_SHIELD),
        "clan_id": "",
        "last_collect": str(now),
        "attack_wins": "0",
        "attack_losses": "0",
        "trophies": "0",
        "auto_collect_until": "0",
        "created_at": str(now),
    }
    await redis.hset(f"coc:{uid}", mapping=data)
    await redis.sadd("coc:all_players", uid)
    p = _parse(data)
    p["points"] = await get_points(uid)
    await redis.hset(f"coc:{uid}", "points", str(p["points"]))
    return p


def _parse(data: dict) -> dict:
    return {
        "name": data.get("name", "未知"),
        "gold": _round_half_up(float(data.get("gold", 0))),
        "elixir": _round_half_up(float(data.get("elixir", 0))),
        "points": round(float(data.get("points", 0)), 2),
        "buildings": json.loads(data.get("buildings", "{}")),
        "troops": json.loads(data.get("troops", "{}")),
        "shield_until": float(data.get("shield_until", 0)),
        "clan_id": data.get("clan_id", ""),
        "last_collect": float(data.get("last_collect", 0)),
        "attack_wins": int(data.get("attack_wins", 0)),
        "attack_losses": int(data.get("attack_losses", 0)),
        "trophies": int(data.get("trophies", 0)),
        "auto_collect_until": float(data.get("auto_collect_until", 0)),
    }


# ───────────────────── 资源收集 ─────────────────────

async def collect_resources(uid: str, p: dict, until_ts: float | None = None) -> tuple[int, int]:
    """收集资源，返回 (gold_gained, elixir_gained)。可指定截止时间用于限时自动收集。"""
    now = time.time()
    collect_to = now if until_ts is None else min(now, float(until_ts))
    elapsed_h = (collect_to - p["last_collect"]) / 3600
    if elapsed_h < 0.005:  # < 18 秒
        return 0, 0

    bld = p["buildings"]
    gold_prod_per_hour = 0.0
    for bid in _building_series_ids("gold_mine"):
        lv = bld.get(bid, 0)
        if lv > 0:
            gold_prod_per_hour += BUILDINGS[bid]["production"][lv - 1]

    elix_prod_per_hour = 0.0
    for bid in _building_series_ids("elixir_collector"):
        lv = bld.get(bid, 0)
        if lv > 0:
            elix_prod_per_hour += BUILDINGS[bid]["production"][lv - 1]

    gold_prod = gold_prod_per_hour * elapsed_h
    elix_prod = elix_prod_per_hour * elapsed_h

    max_gold = get_max_gold(p)
    max_elix = get_max_elixir(p)

    new_gold = min(p["gold"] + gold_prod, max_gold)
    new_elix = min(p["elixir"] + elix_prod, max_elix)

    final_gold = _round_half_up(new_gold)
    final_elix = _round_half_up(new_elix)
    gained_g = final_gold - p["gold"]
    gained_e = final_elix - p["elixir"]

    await redis.hset(f"coc:{uid}", mapping={
        "gold": str(final_gold),
        "elixir": str(final_elix),
        "last_collect": str(collect_to),
    })
    p["gold"] = final_gold
    p["elixir"] = final_elix
    p["last_collect"] = collect_to
    return gained_g, gained_e


# ───────────────────── 资源操作 ─────────────────────

async def add_gold(uid: str, amount: float):
    await _ensure_integer_resource_fields(uid)
    await redis.hincrby(f"coc:{uid}", "gold", _round_half_up(amount))


async def add_elixir(uid: str, amount: float):
    await _ensure_integer_resource_fields(uid)
    await redis.hincrby(f"coc:{uid}", "elixir", _round_half_up(amount))


async def add_points(uid: str, amount: float):
    val = await points_redis.incrbyfloat(_points_key(uid), round(amount, 2))
    await redis.hset(f"coc:{uid}", "points", str(round(float(val), 2)))


def _points_key(uid: str) -> str:
    return f"user_balance:{uid}"


async def get_points(uid: str) -> float:
    raw = await points_redis.get(_points_key(uid))
    if raw is not None:
        return round(float(raw), 2)

    local_raw = await redis.hget(f"coc:{uid}", "points")
    local_points = round(float(local_raw or 0), 2)
    if local_points > 0:
        await points_redis.set(_points_key(uid), local_points)
    return local_points


async def ensure_shared_points_account(uid: str) -> float:
    key = _points_key(uid)
    await points_redis.setnx(key, SHARED_POINTS_INIT)
    raw = await points_redis.get(key)
    return round(float(raw or SHARED_POINTS_INIT), 2)


async def _ensure_integer_resource_fields(uid: str):
    key = f"coc:{uid}"
    raw_gold, raw_elixir = await redis.hmget(key, "gold", "elixir")
    int_gold = _to_int_str(raw_gold)
    int_elixir = _to_int_str(raw_elixir)
    if str(raw_gold) != int_gold or str(raw_elixir) != int_elixir:
        await redis.hset(key, mapping={
            "gold": int_gold,
            "elixir": int_elixir,
        })


async def sanitize_all_player_resources() -> tuple[int, int]:
    """全量清洗玩家资源字段，确保 gold/elixir 均为整数字符串。"""
    uids = await redis.smembers("coc:all_players")
    fixed = 0
    for uid in uids:
        key = f"coc:{uid}"
        raw_gold, raw_elixir = await redis.hmget(key, "gold", "elixir")
        int_gold = _to_int_str(raw_gold)
        int_elixir = _to_int_str(raw_elixir)
        if str(raw_gold) != int_gold or str(raw_elixir) != int_elixir:
            await redis.hset(key, mapping={
                "gold": int_gold,
                "elixir": int_elixir,
            })
            fixed += 1
    return len(uids), fixed


async def set_field(uid: str, field: str, value):
    await redis.hset(f"coc:{uid}", field, str(value))


async def set_buildings(uid: str, buildings: dict):
    await redis.hset(f"coc:{uid}", "buildings", json.dumps(buildings))


async def set_troops(uid: str, troops: dict):
    await redis.hset(f"coc:{uid}", "troops", json.dumps(troops))


async def incr_field(uid: str, field: str, amount: int = 1):
    await redis.hincrby(f"coc:{uid}", field, amount)


# ───────────────────── 容量 / 上限 ─────────────────────

def get_max_gold(p: dict) -> float:
    return _sum_capacity_by_series(p["buildings"], "gold_storage", default_main_lv=1)


def get_max_elixir(p: dict) -> float:
    return _sum_capacity_by_series(p["buildings"], "elixir_storage", default_main_lv=1)


def get_army_capacity(p: dict) -> int:
    lv = p["buildings"].get("barracks", 1)
    return BUILDINGS["barracks"]["capacity"][lv - 1]


def get_army_size(p: dict) -> int:
    return sum(
        cnt * TROOPS[tid]["housing"]
        for tid, cnt in p["troops"].items() if cnt > 0
    )


def get_defense_power(p: dict) -> float:
    total = 0
    bld = p["buildings"]
    for bid in _building_series_ids("cannon") + _building_series_ids("archer_tower") + ["wall"]:
        lv = bld.get(bid, 0)
        if lv > 0:
            total += BUILDINGS[bid]["defense"][lv - 1]
    return total


def get_available_troops(p: dict) -> list[str]:
    barracks_lv = p["buildings"].get("barracks", 1)
    return [tid for tid, t in TROOPS.items() if t["barracks_level"] <= barracks_lv]


# ───────────────────── 部落 ─────────────────────

async def create_clan(uid: str, name: str) -> str:
    clan_id = str(uuid.uuid4())[:8]
    await redis.hset(f"clan:{clan_id}", mapping={
        "name": name,
        "leader": uid,
        "level": "1",
        "created_at": str(time.time()),
    })
    await redis.sadd(f"clan_members:{clan_id}", uid)
    await redis.sadd("coc:all_clans", clan_id)
    await redis.hset(f"coc:{uid}", "clan_id", clan_id)
    return clan_id


async def get_clan(clan_id: str) -> dict | None:
    data = await redis.hgetall(f"clan:{clan_id}")
    if not data:
        return None
    data["members"] = list(await redis.smembers(f"clan_members:{clan_id}"))
    return data


async def join_clan(uid: str, clan_id: str) -> bool:
    count = await redis.scard(f"clan_members:{clan_id}")
    if count >= CLAN_MAX_MEMBERS:
        return False
    await redis.sadd(f"clan_members:{clan_id}", uid)
    await redis.hset(f"coc:{uid}", "clan_id", clan_id)
    return True


async def leave_clan(uid: str, clan_id: str):
    await redis.srem(f"clan_members:{clan_id}", uid)
    await redis.hset(f"coc:{uid}", "clan_id", "")
    # 如果是最后一人，删除部落
    count = await redis.scard(f"clan_members:{clan_id}")
    if count == 0:
        await redis.delete(f"clan:{clan_id}", f"clan_members:{clan_id}")
        await redis.srem("coc:all_clans", clan_id)


async def list_clans() -> list[dict]:
    clan_ids = await redis.smembers("coc:all_clans")
    clans = []
    for cid in clan_ids:
        c = await get_clan(cid)
        if c:
            c["id"] = cid
            clans.append(c)
    return clans


async def get_all_player_uids() -> set:
    return await redis.smembers("coc:all_players")


# ───────────────────── 战斗日志 ─────────────────────

async def add_battle_log(uid: str, record: dict):
    """添加一条战斗记录，最多保留100条"""
    await redis.lpush(f"coc:{uid}:battles", json.dumps(record))
    await redis.ltrim(f"coc:{uid}:battles", 0, 99)


async def get_battle_log(uid: str) -> list[dict]:
    """获取最近100条战斗记录"""
    raw = await redis.lrange(f"coc:{uid}:battles", 0, 99)
    return [json.loads(r) for r in raw]
