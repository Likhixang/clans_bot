import json
import math
import time
import uuid

from core import redis, points_redis
from config import (
    STARTING_GOLD, STARTING_ELIXIR, STARTING_POINTS, STARTING_BUILDINGS,
    BUILDINGS, TROOPS, CLAN_MAX_MEMBERS, NEWBIE_SHIELD, SUPER_ADMIN_ID,
    BUILDING_REMOVE_FULL_REFUND_WINDOW, BUILDING_REMOVE_REFUND_DECAY_PER_SEC,
)

SHARED_POINTS_INIT = 20000.0
DAMAGE_DEFENSE_BASES = ("cannon", "archer_tower", "air_defense", "mortar", "wall")
SUPER_ADMIN_AUTO_COLLECT_UNTIL = 4102444800.0  # 2100-01-01 UTC


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


def _maxed_buildings() -> dict:
    out = {}
    for bid, info in BUILDINGS.items():
        max_lv = int(info.get("max_level", 0) or 0)
        if max_lv > 0:
            out[bid] = max_lv
    return out


async def _ensure_super_admin_privileges(uid: str, data: dict | None = None) -> dict | None:
    if not SUPER_ADMIN_ID or uid != str(SUPER_ADMIN_ID):
        return data
    payload = {
        "buildings": json.dumps(_maxed_buildings(), ensure_ascii=False),
        "auto_collect_until": str(SUPER_ADMIN_AUTO_COLLECT_UNTIL),
    }
    await redis.hset(f"coc:{uid}", mapping=payload)
    if data is not None:
        data.update(payload)
    return data


# ───────────────────── 玩家 ─────────────────────

async def get_player(uid: str) -> dict | None:
    data = await redis.hgetall(f"coc:{uid}")
    if not data:
        return None
    await _ensure_super_admin_privileges(uid, data)
    p = _parse(data)
    p["is_super_admin"] = int(bool(SUPER_ADMIN_ID and uid == str(SUPER_ADMIN_ID)))
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
        await _ensure_super_admin_privileges(uid, data)
        # 更新名字
        if data.get("name") != name:
            await redis.hset(f"coc:{uid}", "name", name)
            data["name"] = name
        p = _parse(data)
        p["is_super_admin"] = int(bool(SUPER_ADMIN_ID and uid == str(SUPER_ADMIN_ID)))
        if str(p["gold"]) != str(data.get("gold", "")) or str(p["elixir"]) != str(data.get("elixir", "")):
            await redis.hset(f"coc:{uid}", mapping={
                "gold": str(p["gold"]),
                "elixir": str(p["elixir"]),
            })
        if "auto_collect_until" not in data:
            await redis.hset(f"coc:{uid}", "auto_collect_until", "0")
        if "shield_source" not in data:
            await redis.hset(f"coc:{uid}", "shield_source", "")
        if "shield_purchase_points" not in data:
            await redis.hset(f"coc:{uid}", "shield_purchase_points", "0")
        if "shield_refund_eligible" not in data:
            await redis.hset(f"coc:{uid}", "shield_refund_eligible", "0")
        if "shield_observe_hits" not in data:
            await redis.hset(f"coc:{uid}", "shield_observe_hits", "0")
        if "bot_last_attack" not in data:
            await redis.hset(f"coc:{uid}", "bot_last_attack", "0")
        if "bot_next_attack_at" not in data:
            await redis.hset(f"coc:{uid}", "bot_next_attack_at", "0")
        if "building_damage" not in data:
            await redis.hset(f"coc:{uid}", "building_damage", "{}")
        if "building_placed_at" not in data:
            await redis.hset(f"coc:{uid}", "building_placed_at", "{}")
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
        "shield_source": "newbie",
        "shield_purchase_points": "0",
        "shield_refund_eligible": "0",
        "shield_observe_hits": "0",
        "bot_last_attack": "0",
        "bot_next_attack_at": "0",
        "building_damage": "{}",
        "building_placed_at": "{}",
        "created_at": str(now),
    }
    if SUPER_ADMIN_ID and uid == str(SUPER_ADMIN_ID):
        data["buildings"] = json.dumps(_maxed_buildings(), ensure_ascii=False)
        data["auto_collect_until"] = str(SUPER_ADMIN_AUTO_COLLECT_UNTIL)
    await redis.hset(f"coc:{uid}", mapping=data)
    await redis.sadd("coc:all_players", uid)
    p = _parse(data)
    p["is_super_admin"] = int(bool(SUPER_ADMIN_ID and uid == str(SUPER_ADMIN_ID)))
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
        "shield_source": data.get("shield_source", ""),
        "shield_purchase_points": round(float(data.get("shield_purchase_points", 0)), 2),
        "shield_refund_eligible": int(data.get("shield_refund_eligible", 0)),
        "shield_observe_hits": int(data.get("shield_observe_hits", 0)),
        "bot_last_attack": float(data.get("bot_last_attack", 0)),
        "bot_next_attack_at": float(data.get("bot_next_attack_at", 0)),
        "building_damage": json.loads(data.get("building_damage", "{}")),
        "building_placed_at": json.loads(data.get("building_placed_at", "{}")),
        "created_at": float(data.get("created_at", 0)),
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
    builder_lv = int(bld.get("builder_hut", 0))
    if builder_lv > 0:
        # 工人小屋：提高自动采集效率，满级最高 +30%
        boost = min(0.30, builder_lv * 0.03)
        gold_prod *= (1.0 + boost)
        elix_prod *= (1.0 + boost)

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


async def set_building_damage(uid: str, damage: dict):
    await redis.hset(f"coc:{uid}", "building_damage", json.dumps(damage))


async def set_building_placed_at(uid: str, building_placed_at: dict):
    await redis.hset(f"coc:{uid}", "building_placed_at", json.dumps(building_placed_at))


async def incr_field(uid: str, field: str, amount: int = 1):
    await redis.hincrby(f"coc:{uid}", field, amount)


# ───────────────────── 容量 / 上限 ─────────────────────

def get_max_gold(p: dict) -> float:
    return _sum_capacity_by_series(p["buildings"], "gold_storage", default_main_lv=1)


def get_max_elixir(p: dict) -> float:
    return _sum_capacity_by_series(p["buildings"], "elixir_storage", default_main_lv=1)


def get_army_capacity(p: dict) -> int:
    lv = p["buildings"].get("barracks", 1)
    base = BUILDINGS["barracks"]["capacity"][lv - 1]
    workshop_lv = int(p.get("buildings", {}).get("workshop", 0))
    # 攻城工坊：额外提升部队容量，满级 +120
    return base + workshop_lv * 12


def get_army_size(p: dict) -> int:
    return sum(
        cnt * TROOPS[tid]["housing"]
        for tid, cnt in p["troops"].items() if cnt > 0
    )


def get_defense_power(p: dict) -> float:
    total = 0
    bld = p["buildings"]
    for bid in (
        _building_series_ids("cannon")
        + _building_series_ids("archer_tower")
        + _building_series_ids("air_defense")
        + _building_series_ids("mortar")
        + ["wall"]
    ):
        lv = bld.get(bid, 0)
        if lv > 0:
            total += get_effective_building_defense(p, bid)
    hero_lv = int(bld.get("hero_altar", 0))
    castle_lv = int(bld.get("clan_castle", 0))
    if total > 0 and (hero_lv > 0 or castle_lv > 0):
        # 英雄祭坛 + 部落城堡：全局防御光环，满级最高 +35%
        aura = min(0.35, hero_lv * 0.02 + castle_lv * 0.015)
        total *= (1.0 + aura)
    return total


def get_building_damage_ratio(p: dict, bid: str) -> float:
    raw = p.get("building_damage", {})
    if not isinstance(raw, dict):
        return 0.0
    v = float(raw.get(bid, 0) or 0)
    return min(1.0, max(0.0, v))


def get_effective_building_defense(p: dict, bid: str) -> float:
    lv = int(p.get("buildings", {}).get(bid, 0))
    if lv <= 0:
        return 0.0
    base = float(BUILDINGS[bid]["defense"][lv - 1])
    dmg_ratio = get_building_damage_ratio(p, bid)
    return max(0.0, base * (1.0 - dmg_ratio))


def get_repair_cost_for_building(p: dict, bid: str) -> int:
    lv = int(p.get("buildings", {}).get(bid, 0))
    if lv <= 0 or "defense" not in BUILDINGS.get(bid, {}):
        return 0
    dmg_ratio = get_building_damage_ratio(p, bid)
    if dmg_ratio <= 0:
        return 0
    costs = BUILDINGS[bid].get("costs", [])
    idx = min(max(lv - 1, 0), max(len(costs) - 1, 0))
    ref_cost = float(costs[idx]) if costs else 1000.0
    return max(20, int(round(ref_cost * 0.18 * dmg_ratio)))


def iter_damageable_defense_buildings(p: dict) -> list[str]:
    bld = p.get("buildings", {})
    ids: list[str] = []
    for base in DAMAGE_DEFENSE_BASES:
        for bid in _building_series_ids(base):
            if int(bld.get(bid, 0)) > 0 and "defense" in BUILDINGS.get(bid, {}):
                ids.append(bid)
    return ids


def apply_building_damage_increments(p: dict, increments: dict[str, float]) -> dict:
    cur = p.get("building_damage", {})
    if not isinstance(cur, dict):
        cur = {}
    new_map = dict(cur)
    for bid, inc in increments.items():
        if inc <= 0:
            continue
        old = min(1.0, max(0.0, float(new_map.get(bid, 0) or 0)))
        new_map[bid] = min(1.0, old + float(inc))
    return new_map


def get_building_remove_refund(p: dict, bid: str, now_ts: float | None = None) -> int:
    lv = int(p.get("buildings", {}).get(bid, 0))
    if lv <= 0:
        return 0
    costs = BUILDINGS.get(bid, {}).get("costs", [])
    if not costs:
        return 0
    base_cost = float(costs[0])
    if base_cost <= 0:
        return 0
    placed_map = p.get("building_placed_at", {})
    if not isinstance(placed_map, dict):
        return 0
    placed_at = float(placed_map.get(bid, 0) or 0)
    if placed_at <= 0:
        return 0
    now = float(now_ts if now_ts is not None else time.time())
    age_seconds = max(0.0, now - placed_at)
    if age_seconds >= BUILDING_REMOVE_FULL_REFUND_WINDOW:
        return 0
    refund_ratio = max(0.0, 1.0 - age_seconds * BUILDING_REMOVE_REFUND_DECAY_PER_SEC)
    return max(0, int(round(base_cost * refund_ratio)))


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
