import random
import time

from config import (
    TROOPS, BUILDINGS, SHIELD_DURATION, LOOT_PERCENT,
    TROPHY_ATTACK, TROPHY_DEFENSE,
)
from models import (
    get_player, get_all_player_uids, get_defense_power,
    add_gold, add_elixir, set_troops, set_field, incr_field,
    get_max_gold, get_max_elixir, add_battle_log,
)


async def find_target(attacker_uid: str, attacker: dict) -> tuple[str, dict] | None:
    """随机找一个可攻击的对手"""
    all_uids = await get_all_player_uids()
    candidates = list(all_uids - {attacker_uid})
    random.shuffle(candidates)

    now = time.time()
    for uid in candidates[:20]:  # 最多扫描 20 人
        p = await get_player(uid)
        if not p:
            continue
        # 有护盾跳过
        if p["shield_until"] > now:
            continue
        # 至少有一些资源可抢
        if p["gold"] + p["elixir"] < 100:
            continue
        return uid, p
    return None


def calculate_attack(attacker: dict, defender: dict) -> dict:
    """计算战斗结果"""
    troops = attacker["troops"]
    if not any(v > 0 for v in troops.values()):
        return {"stars": 0, "attack_power": 0, "defense_power": 0,
                "gold_loot": 0, "elixir_loot": 0, "details": "没有部队！"}

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

    for bid in ("cannon", "archer_tower"):
        lv = bld.get(bid, 0)
        if lv > 0:
            val = BUILDINGS[bid]["defense"][lv - 1]
            if bid == "cannon":
                cannon_def += val
            else:
                tower_def += val

    wall_lv = bld.get("wall", 0)
    if wall_lv > 0:
        wall_def = BUILDINGS["wall"]["defense"][wall_lv - 1]

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

    # ── 战利品 ──
    pct = LOOT_PERCENT[stars] * loot_multiplier
    gold_loot = round(defender["gold"] * pct)
    elixir_loot = round(defender["elixir"] * pct)

    details_parts = []
    details_parts.append(f"⚔️ 攻击力 {int(final_atk)} vs 🛡️ 防御力 {int(final_def)}")
    details_parts.append(f"比值 {ratio:.2f} → {'⭐' * stars if stars else '💀 0星'}")

    return {
        "stars": stars,
        "attack_power": int(final_atk),
        "defense_power": int(final_def),
        "gold_loot": gold_loot,
        "elixir_loot": elixir_loot,
        "loot_multiplier": loot_multiplier,
        "details": "\n".join(details_parts),
    }


async def execute_attack(attacker_uid: str, defender_uid: str,
                         attacker: dict, defender: dict, result: dict):
    """执行战斗结算：转移资源、清空部队、加护盾、更新战绩"""
    stars = result["stars"]
    gold_loot = result["gold_loot"]
    elixir_loot = result["elixir_loot"]

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
    if gold_loot > 0:
        await add_gold(defender_uid, -gold_loot)
    if elixir_loot > 0:
        await add_elixir(defender_uid, -elixir_loot)

    # 攻击方部队清空
    await set_troops(attacker_uid, {})

    # 护盾
    if stars > 0:
        shield = time.time() + SHIELD_DURATION[stars]
        await set_field(defender_uid, "shield_until", shield)

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
