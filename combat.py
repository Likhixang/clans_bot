import random
import time

from config import (
    TROOPS, BUILDINGS, SHIELD_DURATION, LOOT_PERCENT,
    TROPHY_ATTACK, TROPHY_DEFENSE, LOOT_STORAGE_FACTOR, LOOT_COLLECTOR_FACTOR,
)
from models import (
    get_player, get_all_player_uids, get_defense_power, get_effective_building_defense,
    add_gold, add_elixir, set_troops, set_field, incr_field, set_building_damage,
    iter_damageable_defense_buildings, apply_building_damage_increments,
    get_max_gold, get_max_elixir, add_battle_log,
)

def _building_series_ids(base_bid: str) -> list[str]:
    ids = [bid for bid in BUILDINGS if bid == base_bid or bid.startswith(f"{base_bid}_")]
    ids.sort(key=lambda x: (0 if x == base_bid else int(x.rsplit("_", 1)[1])))
    return ids


def _pending_collectable(defender: dict, resource: str, now_ts: float | None = None) -> int:
    """计算当前矿/收集器里可被掠夺的未收取资源。"""
    now = time.time() if now_ts is None else now_ts
    elapsed_h = max(0.0, (now - float(defender.get("last_collect", 0))) / 3600.0)
    if elapsed_h <= 0:
        return 0

    bld = defender["buildings"]
    if resource == "gold":
        series = "gold_mine"
        current = float(defender.get("gold", 0))
        cap = float(get_max_gold(defender))
    else:
        series = "elixir_collector"
        current = float(defender.get("elixir", 0))
        cap = float(get_max_elixir(defender))

    prod_per_hour = 0.0
    for bid in _building_series_ids(series):
        lv = bld.get(bid, 0)
        if lv > 0:
            prod_per_hour += BUILDINGS[bid]["production"][lv - 1]

    produced = prod_per_hour * elapsed_h
    room = max(0.0, cap - current)
    return max(0, round(min(produced, room)))


def _calc_pvp_damage_increments(defender: dict, stars: int) -> dict[str, float]:
    """玩家进攻造成的防御建筑损伤。"""
    star_factor = {0: 0.45, 1: 0.8, 2: 1.15, 3: 1.45}.get(stars, 0.8)
    increments: dict[str, float] = {}
    for bid in iter_damageable_defense_buildings(defender):
        if bid == "wall":
            base = random.uniform(0.004, 0.016)
        elif bid == "cannon" or bid.startswith("cannon_"):
            base = random.uniform(0.007, 0.022)
        else:
            base = random.uniform(0.008, 0.024)
        increments[bid] = min(0.20, base * star_factor)
    return increments


def calc_estimated_loot_total(p: dict) -> int:
    """估算基地当前可被掠夺资源（仓库 + 收集器未收取）。"""
    pending_gold = _pending_collectable(p, "gold")
    pending_elixir = _pending_collectable(p, "elixir")
    return int(max(0, p.get("gold", 0))) + int(max(0, p.get("elixir", 0))) + pending_gold + pending_elixir


def calc_points_shield_cost(p: dict) -> int:
    """积分护盾价格（50~500）：TH/防御/可掠夺资源三者加权。"""
    th_lv = int(p.get("buildings", {}).get("town_hall", 1))
    defense = float(get_defense_power(p))
    loot_total = float(calc_estimated_loot_total(p))

    th_score = min(1.0, max(0.0, th_lv / 10.0))
    defense_score = min(1.0, max(0.0, defense / 22000.0))
    loot_score = min(1.0, max(0.0, loot_total / 3000000.0))

    weighted = 0.35 * th_score + 0.35 * defense_score + 0.30 * loot_score
    cost = round(50 + weighted * 450)
    return max(50, min(500, int(cost)))


def calc_defense_shield_seconds(defender: dict, stars: int) -> int:
    """防守护盾时长：越强基地越短，最低 1 小时。"""
    if stars <= 0:
        return 0
    base = int(SHIELD_DURATION.get(stars, 0))
    if base <= 0:
        return 0

    th_lv = int(defender.get("buildings", {}).get("town_hall", 1))
    defense = float(get_defense_power(defender))
    trophies = float(defender.get("trophies", 0))

    th_score = min(1.0, max(0.0, th_lv / 10.0))
    defense_score = min(1.0, max(0.0, defense / 22000.0))
    trophy_score = min(1.0, max(0.0, trophies / 6000.0))
    strength = 0.45 * th_score + 0.40 * defense_score + 0.15 * trophy_score

    reduced = int(round(base * (1.0 - 0.75 * strength)))
    return max(3600, min(base, reduced))


def _calc_resource_loot(stored: int, pending: int, pct: float) -> tuple[int, int, int]:
    """返回 (total_loot, storage_loot, collector_loot)。"""
    storage_target = round(stored * pct * LOOT_STORAGE_FACTOR)
    collector_target = round(pending * pct * LOOT_COLLECTOR_FACTOR)

    available_total = max(0, stored) + max(0, pending)
    total = min(available_total, max(0, storage_target) + max(0, collector_target))

    collector_loot = min(max(0, pending), total, max(0, collector_target))
    storage_loot = min(max(0, stored), total - collector_loot, max(0, storage_target))

    remain = total - collector_loot - storage_loot
    if remain > 0:
        add_collector = min(max(0, pending) - collector_loot, remain)
        collector_loot += add_collector
        remain -= add_collector
    if remain > 0:
        add_storage = min(max(0, stored) - storage_loot, remain)
        storage_loot += add_storage
        remain -= add_storage

    return collector_loot + storage_loot, storage_loot, collector_loot


def _estimate_last_collect_after_loot(defender: dict, gold_collector_loot: int, elixir_collector_loot: int) -> float | None:
    """按被抢走的收集器资源比例，估算新的 last_collect。"""
    now = time.time()
    last = float(defender.get("last_collect", 0))
    elapsed_h = max(0.0, (now - last) / 3600.0)
    if elapsed_h <= 0:
        return None

    pending_gold = _pending_collectable(defender, "gold", now_ts=now)
    pending_elix = _pending_collectable(defender, "elixir", now_ts=now)

    ratios = []
    if pending_gold > 0:
        remain = max(0.0, pending_gold - max(0, gold_collector_loot))
        ratios.append(remain / pending_gold)
    if pending_elix > 0:
        remain = max(0.0, pending_elix - max(0, elixir_collector_loot))
        ratios.append(remain / pending_elix)
    if not ratios:
        return None

    kept_ratio = min(ratios)
    new_elapsed_h = elapsed_h * kept_ratio
    return now - new_elapsed_h * 3600.0


async def find_target(attacker_uid: str, attacker: dict) -> tuple[str, dict] | None:
    """随机找一个可攻击的对手（跳过同部落成员）"""
    all_uids = await get_all_player_uids()
    candidates = list(all_uids - {attacker_uid})
    random.shuffle(candidates)

    attacker_clan = attacker.get("clan_id", "")
    now = time.time()
    for uid in candidates[:20]:
        p = await get_player(uid)
        if not p:
            continue
        if p["shield_until"] > now:
            continue
        if p["gold"] + p["elixir"] < 100:
            continue
        # 同部落保护
        if attacker_clan and p.get("clan_id") == attacker_clan:
            continue
        return uid, p
    return None


async def find_targets(attacker_uid: str, attacker: dict, count: int = 5) -> list[tuple[str, dict]]:
    """返回最多 count 个候选目标（跳过同部落成员）"""
    all_uids = await get_all_player_uids()
    candidates = list(all_uids - {attacker_uid})
    random.shuffle(candidates)

    attacker_clan = attacker.get("clan_id", "")
    now = time.time()
    results = []
    for uid in candidates[:50]:
        p = await get_player(uid)
        if not p:
            continue
        if p["shield_until"] > now:
            continue
        if p["gold"] + p["elixir"] < 100:
            continue
        if attacker_clan and p.get("clan_id") == attacker_clan:
            continue
        results.append((uid, p))
        if len(results) >= count:
            break
    return results


def calculate_attack(attacker: dict, defender: dict,
                     selected_troops: dict | None = None) -> dict:
    """计算战斗结果。selected_troops 指定出战部队，None 则使用全部"""
    troops = selected_troops if selected_troops is not None else attacker["troops"]
    if not any(v > 0 for v in troops.values()):
        return {"stars": 0, "attack_power": 0, "defense_power": 0,
                "gold_loot": 0, "elixir_loot": 0, "details": "没有部队！",
                "troops_used": {}}

    # ── 攻击力 ──
    base_attack = 0
    wall_attack = 0
    has_air = False
    loot_multiplier = 1.0

    for tid, cnt in troops.items():
        if cnt <= 0:
            continue
        t = TROOPS[tid]
        power = t["power"] * cnt
        base_attack += power

        if t.get("wall_damage"):
            wall_attack += power * t["wall_damage"]
        else:
            wall_attack += power

        if t.get("bypass_wall"):
            has_air = True
        if t.get("loot_bonus"):
            goblin_ratio = (cnt * t["housing"]) / max(sum(
                TROOPS[k]["housing"] * v for k, v in troops.items() if v > 0
            ), 1)
            loot_multiplier += (t["loot_bonus"] - 1) * goblin_ratio

    # ── 防御力 ──
    bld = defender["buildings"]
    cannon_def = 0
    tower_def = 0
    wall_def = 0

    for bid in _building_series_ids("cannon") + _building_series_ids("archer_tower"):
        lv = bld.get(bid, 0)
        if lv > 0:
            val = get_effective_building_defense(defender, bid)
            if bid == "cannon" or bid.startswith("cannon_"):
                cannon_def += val
            else:
                tower_def += val

    wall_lv = bld.get("wall", 0)
    if wall_lv > 0:
        wall_def = get_effective_building_defense(defender, "wall")

    # 空军无视城墙
    effective_wall = 0 if has_air else wall_def
    # 炸弹人对城墙额外伤害：用 wall_attack 对抗 wall_def
    total_def = cannon_def + tower_def + effective_wall

    # 随机因子
    atk_roll = random.uniform(0.85, 1.15)
    def_roll = random.uniform(0.85, 1.15)
    final_atk = base_attack * atk_roll
    final_def = total_def * def_roll

    # 炸弹人效果：如果有炸弹人，城墙防御减半
    if any(TROOPS[t].get("wall_damage") for t in troops if troops[t] > 0):
        effective_wall *= 0.3
        final_def = (cannon_def + tower_def) * def_roll + effective_wall * def_roll

    # ── 星级判定 ──
    if final_def == 0:
        ratio = 10.0
    else:
        ratio = final_atk / final_def

    if ratio >= 2.0:
        stars = 3
    elif ratio >= 1.2:
        stars = 2
    elif ratio >= 0.6:
        stars = 1
    else:
        stars = 0

    # ── 战利品：仓库 + 矿/收集器（仓库比例低，矿/收集器比例高） ──
    pct = LOOT_PERCENT[stars] * loot_multiplier
    pending_gold = _pending_collectable(defender, "gold")
    pending_elixir = _pending_collectable(defender, "elixir")
    gold_loot, gold_storage_loot, gold_collector_loot = _calc_resource_loot(
        int(defender["gold"]), pending_gold, pct
    )
    elixir_loot, elixir_storage_loot, elixir_collector_loot = _calc_resource_loot(
        int(defender["elixir"]), pending_elixir, pct
    )

    details_parts = []
    details_parts.append(f"⚔️ 攻击力 {int(final_atk)} vs 🛡️ 防御力 {int(final_def)}")
    details_parts.append(f"比值 {ratio:.2f} → {'⭐' * stars if stars else '💀 0星'}")

    # 记录使用的部队
    troops_used = {tid: cnt for tid, cnt in troops.items() if cnt > 0}

    return {
        "stars": stars,
        "attack_power": int(final_atk),
        "defense_power": int(final_def),
        "gold_loot": gold_loot,
        "elixir_loot": elixir_loot,
        "gold_storage_loot": gold_storage_loot,
        "gold_collector_loot": gold_collector_loot,
        "elixir_storage_loot": elixir_storage_loot,
        "elixir_collector_loot": elixir_collector_loot,
        "loot_multiplier": loot_multiplier,
        "details": "\n".join(details_parts),
        "troops_used": troops_used,
    }


async def execute_attack(attacker_uid: str, defender_uid: str,
                         attacker: dict, defender: dict, result: dict,
                         selected_troops: dict | None = None):
    """执行战斗结算。selected_troops 指定消耗的部队，None 则清空全部"""
    stars = result["stars"]
    gold_loot = result["gold_loot"]
    elixir_loot = result["elixir_loot"]
    gold_storage_loot = int(result.get("gold_storage_loot", gold_loot))
    gold_collector_loot = int(result.get("gold_collector_loot", 0))
    elixir_storage_loot = int(result.get("elixir_storage_loot", elixir_loot))
    elixir_collector_loot = int(result.get("elixir_collector_loot", 0))

    # 攻击方获得资源（不超过仓库上限）
    max_g = get_max_gold(attacker)
    max_e = get_max_elixir(attacker)
    actual_gold = min(gold_loot, max_g - attacker["gold"])
    actual_elix = min(elixir_loot, max_e - attacker["elixir"])
    actual_gold = max(actual_gold, 0)
    actual_elix = max(actual_elix, 0)

    if actual_gold > 0:
        await add_gold(attacker_uid, actual_gold)
    if actual_elix > 0:
        await add_elixir(attacker_uid, actual_elix)

    # 防守方扣资源
    if gold_storage_loot > 0:
        await add_gold(defender_uid, -gold_storage_loot)
    if elixir_storage_loot > 0:
        await add_elixir(defender_uid, -elixir_storage_loot)
    if gold_collector_loot > 0 or elixir_collector_loot > 0:
        new_last = _estimate_last_collect_after_loot(defender, gold_collector_loot, elixir_collector_loot)
        if new_last is not None:
            await set_field(defender_uid, "last_collect", new_last)

    # 攻击方部队处理
    if selected_troops is not None:
        # 只扣除选中的部队，保留未选中的
        remaining = dict(attacker["troops"])
        for tid, cnt in selected_troops.items():
            remaining[tid] = remaining.get(tid, 0) - cnt
            if remaining[tid] <= 0:
                remaining.pop(tid, None)
        await set_troops(attacker_uid, remaining)
    else:
        await set_troops(attacker_uid, {})

    # 护盾
    if stars > 0:
        shield_seconds = calc_defense_shield_seconds(defender, stars)
        shield = time.time() + shield_seconds
        await set_field(defender_uid, "shield_until", shield)
        await set_field(defender_uid, "shield_source", "defense")
        await set_field(defender_uid, "shield_purchase_points", 0)
        await set_field(defender_uid, "shield_refund_eligible", 0)
        result["defender_shield_seconds"] = shield_seconds

    # 防御设施损伤（被玩家攻击同样会损伤）
    damage_increments = _calc_pvp_damage_increments(defender, stars)
    new_damage_map = apply_building_damage_increments(defender, damage_increments)
    await set_building_damage(defender_uid, new_damage_map)

    # 奖杯
    atk_trophy = TROPHY_ATTACK[stars]
    def_trophy = TROPHY_DEFENSE[stars]
    await incr_field(attacker_uid, "trophies", atk_trophy)
    await incr_field(defender_uid, "trophies", def_trophy)

    # 胜负计数
    if stars > 0:
        await incr_field(attacker_uid, "attack_wins")
    else:
        await incr_field(attacker_uid, "attack_losses")

    result["actual_gold"] = actual_gold
    result["actual_elixir"] = actual_elix
    result["atk_trophy"] = atk_trophy
    result["def_trophy"] = def_trophy

    troops_used = result.get("troops_used", {})

    # 战斗日志
    now = time.time()
    await add_battle_log(attacker_uid, {
        "type": "attack",
        "opponent": defender["name"],
        "stars": stars,
        "gold": actual_gold,
        "elixir": actual_elix,
        "trophies": atk_trophy,
        "time": now,
        "troops_used": troops_used,
    })
    await add_battle_log(defender_uid, {
        "type": "defense",
        "opponent": attacker["name"],
        "stars": stars,
        "gold": -gold_loot,
        "elixir": -elixir_loot,
        "trophies": def_trophy,
        "time": now,
    })


def preview_attack(attacker: dict, defender: dict,
                   selected_troops: dict) -> dict:
    """预览战斗结果（不含随机因子），返回预估星级和掠夺"""
    troops = selected_troops
    if not any(v > 0 for v in troops.values()):
        return {"stars_min": 0, "stars_max": 0, "power": 0, "defense": 0,
                "gold_est": 0, "elixir_est": 0}

    base_attack = 0
    has_air = False
    has_wall_breaker = False
    loot_multiplier = 1.0

    for tid, cnt in troops.items():
        if cnt <= 0:
            continue
        t = TROOPS[tid]
        base_attack += t["power"] * cnt
        if t.get("bypass_wall"):
            has_air = True
        if t.get("wall_damage"):
            has_wall_breaker = True
        if t.get("loot_bonus"):
            goblin_ratio = (cnt * t["housing"]) / max(sum(
                TROOPS[k]["housing"] * v for k, v in troops.items() if v > 0
            ), 1)
            loot_multiplier += (t["loot_bonus"] - 1) * goblin_ratio

    bld = defender["buildings"]
    cannon_def = 0
    tower_def = 0
    wall_def = 0
    for bid in _building_series_ids("cannon") + _building_series_ids("archer_tower"):
        lv = bld.get(bid, 0)
        if lv > 0:
            val = get_effective_building_defense(defender, bid)
            if bid == "cannon" or bid.startswith("cannon_"):
                cannon_def += val
            else:
                tower_def += val
    wall_lv = bld.get("wall", 0)
    if wall_lv > 0:
        wall_def = get_effective_building_defense(defender, "wall")

    effective_wall = 0 if has_air else wall_def
    if has_wall_breaker:
        effective_wall *= 0.3
    total_def = cannon_def + tower_def + effective_wall

    if total_def == 0:
        ratio_min, ratio_max = 10.0, 10.0
    else:
        # 最差: atk*0.85 / def*1.15, 最好: atk*1.15 / def*0.85
        ratio_min = (base_attack * 0.85) / (total_def * 1.15)
        ratio_max = (base_attack * 1.15) / (total_def * 0.85)

    def _stars(r):
        if r >= 2.0:
            return 3
        if r >= 1.2:
            return 2
        if r >= 0.6:
            return 1
        return 0

    stars_min = _stars(ratio_min)
    stars_max = _stars(ratio_max)

    avg_stars = (stars_min + stars_max) / 2
    est_pct = LOOT_PERCENT.get(round(avg_stars), LOOT_PERCENT.get(stars_min, 0)) * loot_multiplier
    pending_gold = _pending_collectable(defender, "gold")
    pending_elixir = _pending_collectable(defender, "elixir")
    gold_est, _, _ = _calc_resource_loot(int(defender["gold"]), pending_gold, est_pct)
    elix_est, _, _ = _calc_resource_loot(int(defender["elixir"]), pending_elixir, est_pct)

    return {
        "stars_min": stars_min,
        "stars_max": stars_max,
        "power": base_attack,
        "defense": int(total_def),
        "gold_est": gold_est,
        "elixir_est": elix_est,
    }


def recommend_troops(attacker: dict, defender: dict) -> dict:
    """智能配兵：优先使用尽量少的兵达到稳定胜利（预览最低1星）。"""
    available = {tid: cnt for tid, cnt in attacker["troops"].items() if cnt > 0}
    if not available:
        return {}

    bld = defender["buildings"]
    wall_lv = bld.get("wall", 0)
    tower_lv = bld.get("archer_tower", 0)
    has_wall = wall_lv > 0
    has_tower = tower_lv > 0

    # 优先级策略:
    # 1) 有高城墙 → 空军优先(dragon > balloon) 或 炸弹人
    # 2) 有箭塔(对空) → 地面重型(giant > wizard) + 炸弹人破墙
    # 3) 资源多 → 掺入少量哥布林
    # 4) 通用补充: wizard > archer > barbarian
    #
    # 执行方式：按优先级逐个加兵，每加一步就做一次预览。
    # 达到“最低1星”即停止，避免无脑全派。

    result: dict[str, int] = {}

    def _add_one(tid: str) -> bool:
        if tid not in available or available[tid] <= 0:
            return False
        result[tid] = result.get(tid, 0) + 1
        available[tid] -= 1
        return True

    gold = defender.get("gold", 0)
    elixir = defender.get("elixir", 0)
    total_res = gold + elixir

    plan: list[str] = []

    if has_wall and not has_tower:
        for tid in ("dragon", "balloon", "wizard"):
            cnt = available.get(tid, 0)
            if cnt > 0:
                plan.extend([tid] * cnt)
        if total_res > 5000:
            plan.extend(["goblin"] * min(available.get("goblin", 0), 5))
    elif has_wall and has_tower:
        # 有墙有塔 -> 炸弹人破墙 + 地面重型
        plan.extend(["wall_breaker"] * min(available.get("wall_breaker", 0), 5))
        for tid in ("giant", "wizard", "dragon"):
            cnt = available.get(tid, 0)
            if cnt > 0:
                plan.extend([tid] * cnt)
        if total_res > 5000:
            plan.extend(["goblin"] * min(available.get("goblin", 0), 5))
    else:
        # 无墙 -> 高攻优先
        for tid in ("dragon", "wizard", "balloon", "giant"):
            cnt = available.get(tid, 0)
            if cnt > 0:
                plan.extend([tid] * cnt)
        if total_res > 5000:
            plan.extend(["goblin"] * min(available.get("goblin", 0), 5))

    # 最后通用补充（轻单位）
    for tid in ("archer", "barbarian", "goblin"):
        cnt = available.get(tid, 0)
        if cnt > 0:
            plan.extend([tid] * cnt)

    for tid in plan:
        if not _add_one(tid):
            continue
        pv = preview_attack(attacker, defender, result)
        if pv["stars_min"] >= 1:
            return {t: c for t, c in result.items() if c > 0}

    # 兜底：如果稳定1星做不到，返回当前最强组合（可能接近全派）
    return {t: c for t, c in result.items() if c > 0}
