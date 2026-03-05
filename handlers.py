import asyncio
import datetime
import json
import logging
import time

from aiogram import Router, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Callable, Any, Dict, Awaitable

from config import (
    BUILDINGS, TROOPS, ALLOWED_CHAT_ID, ALLOWED_THREAD_ID,
    CLAN_CREATE_COST, SUPER_ADMIN_ID, ADMIN_IDS,
    SHIELD_DURATION, TROPHY_ATTACK, NEWBIE_SHIELD, TZ_BJ,
    LAST_FIX_DESC,
)
from core import redis, bot
from models import (
    ensure_player, get_player, collect_resources,
    add_gold, add_elixir, set_buildings, set_troops,
    get_max_gold, get_max_elixir, get_army_capacity, get_army_size,
    get_defense_power, get_available_troops,
    create_clan, get_clan, join_clan, leave_clan, list_clans,
    get_all_player_uids, incr_field, get_battle_log,
    set_field,
)
from combat import (
    find_target, find_targets, calculate_attack, execute_attack,
    preview_attack, recommend_troops,
)
from tasks import perform_backup, perform_restore
from utils import safe_html, mention, fmt_num, send, pin_in_topic, auto_delete, delete_msg_by_id

router = Router()

logger = logging.getLogger(__name__)

# ───────────────────── 停机维护中间件 ─────────────────────

# 超管命令白名单：维护期间仍允许超管执行这些命令
_ADMIN_COMMANDS = {"clan_maintain", "clan_compensate", "clan_backup_db", "clan_restore_db"}


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, types.Message):
            chat_id = event.chat.id
            if await redis.exists(f"maintenance:{chat_id}"):
                # 超管命令放行
                if event.from_user and event.from_user.id == SUPER_ADMIN_ID:
                    text = (event.text or "").strip()
                    if text.startswith("/"):
                        cmd = text.split()[0].lstrip("/").split("@")[0]
                        if cmd in _ADMIN_COMMANDS:
                            return await handler(event, data)
                tip = await event.reply("🔧 <b>系统维护中</b>，暂停所有功能，请等待维护完成后再操作。")
                asyncio.create_task(auto_delete([tip], 10))
                return
        elif isinstance(event, types.CallbackQuery):
            chat_id = event.message.chat.id if event.message else None
            if chat_id and await redis.exists(f"maintenance:{chat_id}"):
                try:
                    await event.answer("🔧 系统维护中，请稍后再试", show_alert=True)
                except Exception:
                    pass
                return
        return await handler(event, data)


router.message.middleware(MaintenanceMiddleware())
router.callback_query.middleware(MaintenanceMiddleware())

# ───────────────────── 村庄可视化 ─────────────────────

# 5×5 村庄布局：外→内 = 城墙 → 防御/兵营 → 资源 → 大本营
VILLAGE_POS = {
    (1, 1): "cannon",          # 防御层 左
    (1, 2): "barracks",        # 兵营 中
    (1, 3): "archer_tower",    # 防御层 右
    (2, 1): "gold_mine",       # 资源层
    (2, 2): "town_hall",       # ★ 核心
    (2, 3): "elixir_collector",
    (3, 1): "gold_storage",    # 仓库层
    (3, 2): "elixir_storage",
    (3, 3): None,              # 草地
}


def _render_village(p: dict, name: str, clan_name: str = "") -> str:
    bld = p["buildings"]
    th_lv = bld.get("town_hall", 1)
    built_count = sum(1 for v in bld.values() if v > 0)
    total_slots = sum(1 for info in BUILDINGS.values()
                      if info["th_required"] <= th_lv)
    gold_max = get_max_gold(p)
    elixir_max = get_max_elixir(p)

    def _bar(cur: int, cap: int, width: int = 10) -> str:
        if cap <= 0:
            return "░" * width
        ratio = max(0.0, min(1.0, cur / cap))
        fill = int(round(ratio * width))
        return "▓" * fill + "░" * (width - fill)

    lines = [
        f"🏰 <b>{safe_html(name)} 的基地</b>",
        f"🏠 大本营 Lv.{th_lv}  |  建筑进度 {built_count}/{total_slots}",
    ]
    if clan_name:
        lines.append(f"🏯 所属部落: <b>{safe_html(clan_name)}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🗺️ <b>基地俯瞰</b>")

    # ── 城墙外观 ──
    wall_lv = bld.get("wall", 0)
    wall_req = BUILDINGS["wall"]["th_required"]
    if wall_lv > 0:
        wall_ch = "🧱"
    elif th_lv >= wall_req:
        wall_ch = "🟫"
    else:
        wall_ch = "🌲"

    # ── 渲染 5×5 网格 ──
    for r in range(5):
        row_ch = []
        for c in range(5):
            if r == 0 or r == 4 or c == 0 or c == 4:
                row_ch.append(wall_ch)
            else:
                bid = VILLAGE_POS.get((r, c))
                if bid is None:
                    row_ch.append("🌿")
                elif bid == "town_hall":
                    row_ch.append("🏰")
                else:
                    lv = bld.get(bid, 0)
                    req = BUILDINGS[bid]["th_required"]
                    if lv > 0:
                        row_ch.append(BUILDINGS[bid]["emoji"])
                    elif th_lv >= req:
                        row_ch.append("🟫")
                    else:
                        row_ch.append("🔒")
        lines.append(" ".join(row_ch))

    lines.append("")
    lines.append("图例: 🧱已建  🟫可建  🔒未解锁  🌿空地")
    lines.append("")

    # ── 图例：已建造 ──
    built_items = []
    for bid, info in BUILDINGS.items():
        lv = bld.get(bid, 0)
        if lv > 0:
            built_items.append(f"{info['emoji']}{info['name']} Lv.{lv}")
    lines.append("🏗️ <b>已建建筑</b>")
    if built_items:
        for i in range(0, len(built_items), 2):
            lines.append("  • " + "  |  ".join(built_items[i:i + 2]))
    else:
        lines.append("  • 暂无")

    # ── 图例：可建造 / 未解锁 ──
    buildable = []
    locked = []
    for bid, info in BUILDINGS.items():
        if bld.get(bid, 0) == 0:
            req = info["th_required"]
            if th_lv >= req:
                buildable.append(info["name"])
            else:
                locked.append(f"{info['name']}(Lv.{req})")

    lines.append("")
    lines.append("🧭 <b>下一步建议</b>")
    lines.append(f"  • 可建造: {', '.join(buildable) if buildable else '无'}")
    lines.append(f"  • 未解锁: {', '.join(locked) if locked else '无'}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"💰 金币 {fmt_num(p['gold'])}/{fmt_num(gold_max)}  [{_bar(p['gold'], gold_max)}]"
    )
    lines.append(
        f"💧 圣水 {fmt_num(p['elixir'])}/{fmt_num(elixir_max)}  [{_bar(p['elixir'], elixir_max)}]"
    )
    lines.append(
        f"🏆 奖杯 {p['trophies']}  |  ⚔️ 战绩 {p['attack_wins']}胜{p['attack_losses']}负  |  🛡️ 防御 {fmt_num(get_defense_power(p))}"
    )
    army_text = f"🗡️ 部队 {get_army_size(p)}/{get_army_capacity(p)}"
    if p["shield_until"] > time.time():
        remain = int(p["shield_until"] - time.time())
        h, m = divmod(remain // 60, 60)
        army_text += f"  🛡️ 护盾 {h}h{m}m"
    lines.append(army_text)

    return "\n".join(lines)


def _village_kb(uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 收集", callback_data=f"vm:collect:{uid}"),
            InlineKeyboardButton(text="🏪 商店", callback_data=f"vm:shop:{uid}"),
            InlineKeyboardButton(text="🗡️ 部队", callback_data=f"vm:army:{uid}"),
        ],
        [
            InlineKeyboardButton(text="⚔️ 攻击", callback_data=f"vm:attack:{uid}"),
            InlineKeyboardButton(text="🏯 部落", callback_data=f"vm:clan:{uid}"),
            InlineKeyboardButton(text="📜 战绩", callback_data=f"vm:log:{uid}"),
        ],
        [
            InlineKeyboardButton(text="🏆 排行", callback_data=f"vm:rank:{uid}"),
            InlineKeyboardButton(text="❓ 帮助", callback_data=f"vm:help:{uid}"),
            InlineKeyboardButton(text="🔄 刷新", callback_data=f"vm:refresh:{uid}"),
        ],
    ])


# ───────────────────── 权限检查 ─────────────────────

def _check(msg: types.Message) -> bool:
    if ALLOWED_CHAT_ID and msg.chat.id != ALLOWED_CHAT_ID:
        return False
    if ALLOWED_THREAD_ID and msg.message_thread_id != ALLOWED_THREAD_ID:
        return False
    return True


def _uid(msg: types.Message) -> str:
    return str(msg.from_user.id)


def _name(msg: types.Message) -> str:
    u = msg.from_user
    return u.full_name or u.username or "无名"


# ───────────────────── /start /help ─────────────────────

@router.message(Command("clan_start"))
async def cmd_start(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    shield_h = NEWBIE_SHIELD // 3600
    shield_text = ""
    if p["shield_until"] > time.time():
        remain = int(p["shield_until"] - time.time())
        h, m = divmod(remain // 60, 60)
        shield_text = f"\n🛡️ 新手护盾: {h}小时{m}分钟（免受攻击）"
    text = (
        f"🏰 欢迎来到 <b>部落冲突</b>，{mention(uid, name)}！\n\n"
        f"💰 金币: {fmt_num(p['gold'])}  💧 圣水: {fmt_num(p['elixir'])}"
        f"{shield_text}\n\n"
        "输入 /clan_help 查看所有命令"
    )
    await msg.reply(text)


@router.message(Command("clan_help"))
async def cmd_help(msg: types.Message):
    if not _check(msg):
        return
    text = (
        "📖 <b>部落冲突 - 命令列表</b>\n\n"
        "🏠 <b>基础</b>\n"
        "/clan_start - 注册/进入游戏\n"
        "/clan_me - 查看个人信息\n"
        "/clan_collect - 收集资源\n\n"
        "🏗️ <b>建造</b>\n"
        "/clan_shop - 建筑商店\n"
        "/clan_build - 建造新建筑（推荐用商店按钮）\n"
        "/clan_upgrade - 升级建筑（推荐用商店按钮）\n\n"
        "⚔️ <b>军事</b>\n"
        "/clan_troops - 可训练兵种列表\n"
        "/clan_train - 训练部队（推荐用部队按钮）\n"
        "/clan_army - 查看当前部队\n"
        "/clan_attack - 攻击其他玩家\n"
        "/clan_log - 战绩记录（按日查看）\n\n"
        "🏆 <b>排行</b>\n"
        "/clan_rank - 奖杯排行榜\n\n"
        "🏯 <b>部落</b>\n"
        "/clan_create [名称] - 创建部落\n"
        "/clan_info - 查看部落信息\n"
        "/clan_list - 所有部落列表\n"
        "/clan_join [部落名称] - 加入部落\n"
        "/clan_leave - 离开部落\n"
    )
    await msg.reply(text)


# ───────────────────── /me ─────────────────────

@router.message(Command("clan_me"))
async def cmd_me(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    await collect_resources(uid, p)

    clan_name = ""
    if p["clan_id"]:
        clan = await get_clan(p["clan_id"])
        if clan:
            clan_name = clan["name"]

    text = _render_village(p, name, clan_name)
    await msg.reply(text, reply_markup=_village_kb(uid))


# ───────────────────── /collect ─────────────────────

@router.message(Command("clan_collect"))
async def cmd_collect(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    g, e = await collect_resources(uid, p)

    if g <= 0 and e <= 0:
        await msg.reply("⏳ 还没产出足够的资源，稍后再来！")
        return

    text = (
        f"📦 收集完毕！\n"
        f"💰 金币 +{fmt_num(g)}  → {fmt_num(p['gold'])}\n"
        f"💧 圣水 +{fmt_num(e)}  → {fmt_num(p['elixir'])}"
    )
    await msg.reply(text)


# ───────────────────── /shop ─────────────────────

@router.message(Command("clan_shop"))
async def cmd_shop(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    bld = p["buildings"]
    th_lv = bld.get("town_hall", 1)

    lines = ["🏪 <b>建筑商店</b>\n"]
    for bid, info in BUILDINGS.items():
        cur_lv = bld.get(bid, 0)
        req = info["th_required"]
        if bid == "town_hall":
            max_lv = info["max_level"]
        else:
            max_lv = min(th_lv + 1, info["max_level"])

        if th_lv < req:
            # 未解锁
            lines.append(
                f"🔒 <b>{info['name']}</b> — 大本营 Lv.{req} 解锁"
            )
        elif cur_lv == 0:
            # 已解锁 未建造
            cost = info["costs"][0]
            res = "💰" if info["resource"] == "gold" else "💧"
            lines.append(
                f"{info['emoji']} <b>{info['name']}</b> - 未建造\n"
                f"  建造费: {res} {fmt_num(cost)}"
            )
        elif cur_lv < max_lv:
            cost = info["costs"][cur_lv]
            res = "💰" if info["resource"] == "gold" else "💧"
            lines.append(
                f"{info['emoji']} <b>{info['name']}</b> Lv.{cur_lv}\n"
                f"  升级费: {res} {fmt_num(cost)} → Lv.{cur_lv + 1}"
            )
        else:
            lines.append(
                f"{info['emoji']} <b>{info['name']}</b> Lv.{cur_lv} ✅ 已满级"
            )

    await msg.reply("\n".join(lines))


# ───────────────────── /build ─────────────────────

@router.message(Command("clan_build"))
async def cmd_build(msg: types.Message):
    if not _check(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.reply("请在 /clan_me → 🏪 商店 中选择建筑进行建造")
        return

    bid = args[1].lower()
    if bid not in BUILDINGS:
        await msg.reply(f"❌ 未知建筑: {bid}\n输入 /clan_shop 查看列表")
        return

    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    bld = p["buildings"]

    if bld.get(bid, 0) > 0:
        await msg.reply(f"❌ {BUILDINGS[bid]['name']}已建造，请在商店面板中执行升级")
        return

    info = BUILDINGS[bid]
    th_lv = bld.get("town_hall", 1)
    req = info["th_required"]
    if th_lv < req:
        await msg.reply(f"🔒 {info['name']} 需要大本营 Lv.{req}，当前 Lv.{th_lv}")
        return
    cost = info["costs"][0]
    res = info["resource"]

    if p[res] < cost:
        res_name = "金币" if res == "gold" else "圣水"
        await msg.reply(f"❌ {res_name}不足！需要 {fmt_num(cost)}，当前 {fmt_num(p[res])}")
        return

    if res == "gold":
        await add_gold(uid, -cost)
    else:
        await add_elixir(uid, -cost)

    bld[bid] = 1
    await set_buildings(uid, bld)

    await msg.reply(
        f"✅ 建造 {info['emoji']} <b>{info['name']}</b> Lv.1 完成！\n"
        f"花费: {fmt_num(cost)} {'💰' if res == 'gold' else '💧'}"
    )


# ───────────────────── /upgrade ─────────────────────

@router.message(Command("clan_upgrade"))
async def cmd_upgrade(msg: types.Message):
    if not _check(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.reply("请在 /clan_me → 🏪 商店 中选择建筑进行升级")
        return

    bid = args[1].lower()
    if bid not in BUILDINGS:
        await msg.reply(f"❌ 未知建筑: {bid}")
        return

    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    bld = p["buildings"]
    cur_lv = bld.get(bid, 0)

    if cur_lv == 0:
        await msg.reply(f"❌ 尚未建造 {BUILDINGS[bid]['name']}，请先在商店面板中建造")
        return

    info = BUILDINGS[bid]
    th_lv = bld.get("town_hall", 1)
    if bid == "town_hall":
        max_lv = info["max_level"]
    else:
        max_lv = min(th_lv + 1, info["max_level"])

    if cur_lv >= max_lv:
        if cur_lv >= info["max_level"]:
            await msg.reply(f"❌ {info['name']} 已达到最高等级 Lv.{cur_lv}！")
        else:
            await msg.reply(
                f"❌ {info['name']} 等级受大本营限制！\n"
                f"当前上限 Lv.{max_lv}，请先升级大本营"
            )
        return

    cost = info["costs"][cur_lv]
    res = info["resource"]

    if p[res] < cost:
        res_name = "金币" if res == "gold" else "圣水"
        await msg.reply(f"❌ {res_name}不足！需要 {fmt_num(cost)}，当前 {fmt_num(p[res])}")
        return

    if res == "gold":
        await add_gold(uid, -cost)
    else:
        await add_elixir(uid, -cost)

    bld[bid] = cur_lv + 1
    await set_buildings(uid, bld)

    extra = ""
    if "production" in info:
        prod = info["production"][cur_lv]
        extra = f"\n产量: {fmt_num(prod)}/小时"
    elif "capacity" in info:
        cap = info["capacity"][cur_lv]
        extra = f"\n容量: {fmt_num(cap)}"
    elif "defense" in info:
        defense = info["defense"][cur_lv]
        extra = f"\n防御力: {fmt_num(defense)}"

    await msg.reply(
        f"⬆️ {info['emoji']} <b>{info['name']}</b> 升级到 Lv.{cur_lv + 1}！\n"
        f"花费: {fmt_num(cost)} {'💰' if res == 'gold' else '💧'}{extra}"
    )


# ───────────────────── /troops ─────────────────────

@router.message(Command("clan_troops"))
async def cmd_troops(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    available = get_available_troops(p)
    barracks_lv = p["buildings"].get("barracks", 1)

    lines = [f"🗡️ <b>兵种列表</b>（兵营 Lv.{barracks_lv}）\n"]
    for tid, t in TROOPS.items():
        unlocked = tid in available
        lock = "" if unlocked else f"🔒 需要兵营 Lv.{t['barracks_level']}"
        lines.append(
            f"{t['emoji']} <b>{t['name']}</b>\n"
            f"  💧 费用: {t['cost']} | ⚔️ 战力: {t['power']} | 🏠 占用: {t['housing']}\n"
            f"  {t['desc']}\n"
            f"  {lock if lock else '✅ 已解锁（可在部队面板训练）'}"
        )

    await msg.reply("\n".join(lines))


# ───────────────────── /train ─────────────────────

@router.message(Command("clan_train"))
async def cmd_train(msg: types.Message):
    if not _check(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.reply("请在 /clan_me → 🗡️ 部队 中选择兵种进行训练")
        return

    tid = args[1].lower()
    count = 1
    if len(args) >= 3:
        try:
            count = int(args[2])
        except ValueError:
            await msg.reply("❌ 数量必须是数字")
            return
    if count < 1:
        await msg.reply("❌ 数量至少为1")
        return

    if tid not in TROOPS:
        await msg.reply(f"❌ 未知兵种: {tid}\n输入 /clan_troops 查看列表")
        return

    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    available = get_available_troops(p)
    if tid not in available:
        t = TROOPS[tid]
        await msg.reply(f"🔒 {t['name']} 需要兵营 Lv.{t['barracks_level']}，当前 Lv.{p['buildings'].get('barracks', 1)}")
        return

    t = TROOPS[tid]
    cap = get_army_capacity(p)
    used = get_army_size(p)
    space = cap - used
    housing_needed = t["housing"] * count

    if housing_needed > space:
        max_can = space // t["housing"]
        await msg.reply(
            f"❌ 兵营空间不足！\n"
            f"剩余空间: {space} | 需要: {housing_needed}\n"
            f"最多可训练 {max_can} 个 {t['name']}"
        )
        return

    total_cost = t["cost"] * count
    if p["elixir"] < total_cost:
        max_afford = int(p["elixir"] // t["cost"])
        await msg.reply(
            f"❌ 圣水不足！需要 {fmt_num(total_cost)}，当前 {fmt_num(p['elixir'])}\n"
            f"最多可训练 {max_afford} 个 {t['name']}"
        )
        return

    await add_elixir(uid, -total_cost)
    troops = p["troops"]
    troops[tid] = troops.get(tid, 0) + count
    await set_troops(uid, troops)

    new_used = used + housing_needed
    await msg.reply(
        f"✅ 训练了 {count} 个 {t['emoji']} <b>{t['name']}</b>！\n"
        f"花费: 💧 {fmt_num(total_cost)}\n"
        f"兵力: {new_used}/{cap}"
    )


# ───────────────────── /army ─────────────────────

@router.message(Command("clan_army"))
async def cmd_army(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)
    troops = p["troops"]
    cap = get_army_capacity(p)
    used = get_army_size(p)

    lines = [f"🗡️ <b>当前部队</b> ({used}/{cap})\n"]
    total_power = 0
    if any(v > 0 for v in troops.values()):
        for tid, cnt in troops.items():
            if cnt > 0:
                t = TROOPS[tid]
                power = t["power"] * cnt
                total_power += power
                lines.append(f"  {t['emoji']} {t['name']} ×{cnt}  (⚔️ {fmt_num(power)})")
        lines.append(f"\n总攻击力: ⚔️ {fmt_num(total_power)}")
    else:
        lines.append("  （无部队）\n  使用 /clan_train 训练部队")

    await msg.reply("\n".join(lines))


# ───────────────────── /attack ─────────────────────

_attack_locks: dict[str, float] = {}
_attack_staging: dict[str, dict] = {}  # uid -> {"target_uid", "target_name", "troops": {tid: count}}

@router.message(Command("clan_attack"))
async def cmd_attack(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)

    # 冷却检查
    last = _attack_locks.get(uid, 0)
    if time.time() - last < 30:
        remain = int(30 - (time.time() - last))
        await msg.reply(f"⏳ 攻击冷却中，{remain}秒后可再次攻击")
        return

    # 护盾检查
    if p["shield_until"] > time.time():
        remain = int(p["shield_until"] - time.time())
        h, m = divmod(remain // 60, 60)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ 放弃护盾并攻击", callback_data=f"break_shield_{uid}")]
        ])
        await msg.reply(
            f"🛡️ 你有护盾保护（剩余 {h}小时{m}分钟）\n"
            "攻击将会移除护盾！",
            reply_markup=kb,
        )
        return

    await _do_attack(msg, uid, name, p)


@router.callback_query(F.data.startswith("break_shield_"))
async def cb_break_shield(cb: types.CallbackQuery):
    target_uid = cb.data.split("_")[-1]
    if str(cb.from_user.id) != target_uid:
        await cb.answer("这不是你的操作！", show_alert=True)
        return
    uid = target_uid
    name = cb.from_user.full_name or cb.from_user.username or "无名"
    p = await ensure_player(uid, name)

    await set_field(uid, "shield_until", "0")
    p["shield_until"] = 0

    await cb.message.edit_text("🛡️ → ⚔️ 护盾已移除！正在搜索对手...")
    await cb.answer()
    await _do_attack(cb.message, uid, name, p)


async def _do_attack(msg: types.Message, uid: str, name: str, p: dict):
    troops = p["troops"]
    if not any(v > 0 for v in troops.values()):
        await msg.reply("❌ 你没有部队！先使用 /clan_train 训练部队")
        return

    targets = await find_targets(uid, p, count=5)
    if not targets:
        await msg.reply("🔍 没有找到可攻击的对手（所有人都有护盾或资源不足）")
        return

    lines = ["⚔️ <b>选择攻击目标</b>\n"]
    btns = []
    for t_uid, t_p in targets:
        th_lv = t_p["buildings"].get("town_hall", 1)
        defense = get_defense_power(t_p)
        total_res = t_p["gold"] + t_p["elixir"]
        lines.append(
            f"• {safe_html(t_p['name'])} | 🏰Lv.{th_lv} | "
            f"🏆{t_p['trophies']} | 🛡️{fmt_num(defense)} | "
            f"💰{fmt_num(t_p['gold'])} 💧{fmt_num(t_p['elixir'])}"
        )
        btns.append([InlineKeyboardButton(
            text=f"⚔️ {t_p['name']} (🏰{th_lv} 💰💧{fmt_num(total_res)})",
            callback_data=f"vm:atgt:{t_uid}:{uid}")])
    btns.append([InlineKeyboardButton(
        text="🔄 换一批", callback_data=f"vm:attack:{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await msg.reply("\n".join(lines), reply_markup=kb)


# ───────────────────── /clan_log ─────────────────────

def _fmt_time_ago(ts: float) -> str:
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff}秒前"
    if diff < 3600:
        return f"{diff // 60}分钟前"
    if diff < 86400:
        return f"{diff // 3600}小时前"
    return f"{diff // 86400}天前"


def _format_battle_log_page(logs: list[dict], page: int = 0) -> tuple[str, list[str]]:
    """按日期分组战绩，page=0 为最新一天，返回 (text, date_keys)"""
    if not logs:
        return "📜 <b>战绩记录</b>\n\n暂无战斗记录", []

    # 按北京时间日期分组
    grouped: dict[str, list[dict]] = {}
    for r in logs:
        ts = r.get("time", 0)
        dt = datetime.datetime.fromtimestamp(ts, tz=TZ_BJ)
        key = dt.strftime("%Y-%m-%d")
        grouped.setdefault(key, []).append(r)

    date_keys = sorted(grouped.keys(), reverse=True)
    if not date_keys:
        return "📜 <b>战绩记录</b>\n\n暂无战斗记录", []

    page = max(0, min(page, len(date_keys) - 1))
    key = date_keys[page]

    today = datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now(TZ_BJ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if key == today:
        label = "今天"
    elif key == yesterday:
        label = "昨天"
    else:
        label = key

    lines = [f"📜 <b>战绩记录 · {label}</b>  ({page + 1}/{len(date_keys)})\n"]
    for r in grouped[key]:
        ts = r.get("time", 0)
        dt = datetime.datetime.fromtimestamp(ts, tz=TZ_BJ)
        hm = dt.strftime("%H:%M")
        if r["type"] == "attack":
            icon = "⚔️ 进攻 →"
        else:
            icon = "🛡️ 防守 ←"
        stars = "⭐" * r["stars"] if r["stars"] else "0星"
        gold = r.get("gold", 0)
        elixir = r.get("elixir", 0)
        gold_sign = "+" if gold >= 0 else ""
        elix_sign = "+" if elixir >= 0 else ""
        trophy = r.get("trophies", 0)
        trophy_sign = "+" if trophy >= 0 else ""
        troops_text = ""
        used = r.get("troops_used")
        if used:
            parts = []
            for tid, cnt in used.items():
                if cnt > 0 and tid in TROOPS:
                    parts.append(f"{TROOPS[tid]['emoji']}×{cnt}")
            if parts:
                troops_text = " | " + " ".join(parts)
        lines.append(
            f"<code>{hm}</code> {icon} {safe_html(r.get('opponent', '?'))} | {stars} | "
            f"💰{gold_sign}{fmt_num(gold)} 💧{elix_sign}{fmt_num(elixir)} | "
            f"🏆{trophy_sign}{trophy}{troops_text}"
        )
    return "\n".join(lines), date_keys


@router.message(Command("clan_log"))
async def cmd_log(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    await ensure_player(uid, name)
    logs = await get_battle_log(uid)
    text, date_keys = _format_battle_log_page(logs, 0)
    btns = []
    if len(date_keys) > 1:
        btns.append([InlineKeyboardButton(
            text="◀️ 前一天", callback_data=f"vm:log:1:{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
    await msg.reply(text, reply_markup=kb)


# ───────────────────── /rank ─────────────────────

@router.message(Command("clan_rank"))
async def cmd_rank(msg: types.Message):
    if not _check(msg):
        return
    uids = await get_all_player_uids()
    players = []
    for u in uids:
        p = await get_player(u)
        if p:
            p["uid"] = u
            players.append(p)

    players.sort(key=lambda x: x["trophies"], reverse=True)
    top = players[:20]

    if not top:
        await msg.reply("🏆 还没有玩家注册")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>奖杯排行榜</b>\n"]
    for i, p in enumerate(top):
        prefix = medals[i] if i < 3 else f"#{i + 1}"
        th_lv = p["buildings"].get("town_hall", 1)
        lines.append(
            f"{prefix} {safe_html(p['name'])} | "
            f"🏆 {p['trophies']} | 🏰 Lv.{th_lv} | "
            f"⚔️ {p['attack_wins']}胜"
        )

    await msg.reply("\n".join(lines))


# ───────────────────── 部落系统 ─────────────────────

async def _find_clans_by_name(name: str) -> list[dict]:
    target = name.strip().lower()
    if not target:
        return []
    clans = await list_clans()
    return [c for c in clans if c.get("name", "").strip().lower() == target]


@router.message(Command("clan_create"))
async def cmd_clan_create(msg: types.Message):
    if not _check(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await msg.reply("用法: /clan_create [部落名称]\n或使用 /clan_me → 🏯 部落 → 创建部落")
        return

    clan_name = args[1].strip()
    if len(clan_name) > 20:
        await msg.reply("❌ 部落名称最长20个字符")
        return

    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)

    if p["clan_id"]:
        await msg.reply("❌ 你已经在一个部落中，请先 /clan_leave")
        return

    if p["gold"] < CLAN_CREATE_COST:
        await msg.reply(f"❌ 创建部落需要 💰 {fmt_num(CLAN_CREATE_COST)} 金币")
        return

    dup = await _find_clans_by_name(clan_name)
    if dup:
        await msg.reply("❌ 已存在同名部落，请换一个名称")
        return

    await add_gold(uid, -CLAN_CREATE_COST)
    await create_clan(uid, clan_name)

    await msg.reply(
        f"🏯 部落 <b>{safe_html(clan_name)}</b> 创建成功！\n"
        f"花费: 💰 {fmt_num(CLAN_CREATE_COST)}\n\n"
        "其他玩家可通过部落面板按钮或 /clan_join 部落名称 加入"
    )


@router.message(Command("clan_join"))
async def cmd_clan_join(msg: types.Message):
    if not _check(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.reply("用法: /clan_join [部落名称]\n或使用 /clan_me → 🏯 部落 按钮直接加入")
        return

    clan_name = args[1].strip()
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)

    if p["clan_id"]:
        await msg.reply("❌ 你已经在一个部落中，请先 /clan_leave")
        return

    matched = await _find_clans_by_name(clan_name)
    if not matched:
        await msg.reply("❌ 未找到该部落，请检查名称或使用按钮加入")
        return
    if len(matched) > 1:
        await msg.reply("❌ 存在重名部落，请使用 /clan_me → 🏯 部落 按钮加入")
        return
    clan = matched[0]
    clan_id = clan["id"]

    ok = await join_clan(uid, clan_id)
    if not ok:
        await msg.reply("❌ 部落成员已满")
        return

    await msg.reply(f"✅ 成功加入部落 <b>{safe_html(clan['name'])}</b>！")


@router.message(Command("clan_leave"))
async def cmd_clan_leave(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)

    if not p["clan_id"]:
        await msg.reply("❌ 你不在任何部落中")
        return

    clan = await get_clan(p["clan_id"])
    clan_name = clan["name"] if clan else "未知"
    await leave_clan(uid, p["clan_id"])

    await msg.reply(f"👋 你已离开部落 <b>{safe_html(clan_name)}</b>")


@router.message(Command("clan_info"))
async def cmd_clan_info(msg: types.Message):
    if not _check(msg):
        return
    uid, name = _uid(msg), _name(msg)
    p = await ensure_player(uid, name)

    if not p["clan_id"]:
        await msg.reply("❌ 你不在任何部落中\n使用 /clan_list 查看部落或 /clan_create 创建部落")
        return

    clan = await get_clan(p["clan_id"])
    if not clan:
        await msg.reply("❌ 部落数据异常")
        return

    members = clan.get("members", [])
    member_lines = []
    total_trophies = 0
    for m_uid in members:
        mp = await get_player(m_uid)
        if mp:
            total_trophies += mp["trophies"]
            role = "👑 首领" if m_uid == clan.get("leader") else "👤 成员"
            member_lines.append(
                f"  {role} {safe_html(mp['name'])} | 🏆 {mp['trophies']}"
            )

    text = (
        f"🏯 <b>{safe_html(clan['name'])}</b>\n"
        f"🏆 总奖杯: {total_trophies}\n"
        f"👥 成员: {len(members)}人\n\n"
        + "\n".join(member_lines)
    )
    await msg.reply(text)


@router.message(Command("clan_list"))
async def cmd_clan_list(msg: types.Message):
    if not _check(msg):
        return
    clans = await list_clans()
    if not clans:
        await msg.reply("🏯 还没有部落，使用 /clan_create 创建一个！")
        return

    uid, name = _uid(msg), _name(msg)
    await ensure_player(uid, name)

    lines = ["🏯 <b>部落列表</b>\n"]
    btns = []
    for c in clans:
        count = len(c.get("members", []))
        lines.append(
            f"  <b>{safe_html(c['name'])}</b>\n"
            f"  👥 {count}人"
        )
        btns.append([InlineKeyboardButton(
            text=f"➕ 加入 {c['name']}",
            callback_data=f"vm:cjoin:{c['id']}:{uid}",
        )])

    kb = InlineKeyboardMarkup(inline_keyboard=btns[:8]) if btns else None
    await msg.reply("\n".join(lines), reply_markup=kb)


@router.message(F.text)
async def msg_clan_create_name(msg: types.Message):
    if not _check(msg):
        return
    if not msg.from_user:
        return
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return

    uid, name = _uid(msg), _name(msg)
    pending_key = f"coc:pending_clan_create:{uid}"
    if not await redis.exists(pending_key):
        return

    await redis.delete(pending_key)
    clan_name = text
    if len(clan_name) > 20:
        await msg.reply("❌ 部落名称最长20个字符，请重新点击“创建部落”")
        return

    p = await ensure_player(uid, name)
    if p["clan_id"]:
        await msg.reply("❌ 你已经在一个部落中，请先离开当前部落")
        return
    if p["gold"] < CLAN_CREATE_COST:
        await msg.reply(f"❌ 创建部落需要 💰 {fmt_num(CLAN_CREATE_COST)} 金币")
        return
    dup = await _find_clans_by_name(clan_name)
    if dup:
        await msg.reply("❌ 已存在同名部落，请换一个名称")
        return

    await add_gold(uid, -CLAN_CREATE_COST)
    await create_clan(uid, clan_name)
    await msg.reply(
        f"🏯 部落 <b>{safe_html(clan_name)}</b> 创建成功！\n"
        f"花费: 💰 {fmt_num(CLAN_CREATE_COST)}"
    )


# ───────────────────── 管理员命令 ─────────────────────

def _is_admin(uid: str) -> bool:
    uid_int = int(uid)
    return uid_int == SUPER_ADMIN_ID or uid_int in ADMIN_IDS


@router.message(Command("clan_give"))
async def cmd_give(msg: types.Message):
    if not _check(msg):
        return
    uid = _uid(msg)
    if int(uid) != SUPER_ADMIN_ID:
        return

    args = msg.text.split()

    # 回复某人消息: /clan_give gold 数量
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if len(args) < 3:
            await msg.reply("用法: 回复某人消息 /clan_give [gold/elixir] [数量]")
            return
        target_uid = str(msg.reply_to_message.from_user.id)
        res = args[1].lower()
        try:
            amount = int(args[2])
        except ValueError:
            await msg.reply("❌ 数量必须是数字")
            return
    else:
        await msg.reply("❌ 请回复目标玩家的消息来使用此命令\n用法: 回复某人消息 /clan_give [gold/elixir] [数量]")
        return

    p = await get_player(target_uid)
    if not p:
        await msg.reply("❌ 该玩家尚未注册游戏")
        return

    if res == "gold":
        await add_gold(target_uid, amount)
    elif res == "elixir":
        await add_elixir(target_uid, amount)
    else:
        await msg.reply("❌ 资源类型: gold 或 elixir")
        return

    await msg.reply(f"✅ 已给 {safe_html(p['name'])} {'💰' if res == 'gold' else '💧'} {fmt_num(amount)}")


@router.message(Command("clan_take"))
async def cmd_take(msg: types.Message):
    if not _check(msg):
        return
    uid = _uid(msg)
    if int(uid) != SUPER_ADMIN_ID:
        return

    args = msg.text.split()

    # 回复某人消息: /clan_take gold 数量
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if len(args) < 3:
            await msg.reply("用法: 回复某人消息 /clan_take [gold/elixir] [数量]")
            return
        target_uid = str(msg.reply_to_message.from_user.id)
        res = args[1].lower()
        try:
            amount = int(args[2])
        except ValueError:
            await msg.reply("❌ 数量必须是数字")
            return
    else:
        await msg.reply("❌ 请回复目标玩家的消息来使用此命令\n用法: 回复某人消息 /clan_take [gold/elixir] [数量]")
        return

    p = await get_player(target_uid)
    if not p:
        await msg.reply("❌ 该玩家尚未注册游戏")
        return

    if res == "gold":
        await add_gold(target_uid, -amount)
    elif res == "elixir":
        await add_elixir(target_uid, -amount)
    else:
        await msg.reply("❌ 资源类型: gold 或 elixir")
        return

    await msg.reply(f"✅ 已扣 {safe_html(p['name'])} {'💰' if res == 'gold' else '💧'} {fmt_num(amount)}")


@router.message(Command("clan_backup_db"))
async def cmd_backup_db(msg: types.Message):
    if not _check(msg):
        return
    if msg.from_user.id != SUPER_ADMIN_ID:
        return
    stats = await perform_backup()
    await msg.reply(
        f"✅ <b>手动备份完成！</b>\n"
        f"👤 玩家：{stats['players']} 条\n"
        f"🏯 部落：{stats['clans']} 个\n"
        f"⚔️ 战斗日志：{stats['battles']} 条"
    )


@router.message(Command("clan_restore_db"))
async def cmd_restore_db(msg: types.Message):
    if not _check(msg):
        return
    if msg.from_user.id != SUPER_ADMIN_ID:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚠️ 确认覆盖恢复", callback_data="clan_confirm_restore"),
        InlineKeyboardButton(text="❌ 取消", callback_data="clan_cancel_restore"),
    ]])
    await msg.reply(
        "⚠️ <b>高危操作警告</b> ⚠️\n\n"
        "此操作将用 <code>backup.db</code> 中的数据覆写当前 Redis！\n"
        "确定要恢复吗？",
        reply_markup=markup,
    )


@router.callback_query(F.data == "clan_confirm_restore")
async def cb_confirm_restore(cb: types.CallbackQuery):
    if cb.from_user.id != SUPER_ADMIN_ID:
        await cb.answer("❌ 越权拦截", show_alert=True)
        return
    try:
        await cb.message.edit_text("⏳ 正在从 SQLite 恢复数据...")
    except Exception:
        pass
    stats = await perform_restore()
    if not stats:
        try:
            await cb.message.edit_text("⚠️ 备份数据库为空，无法恢复！")
        except Exception:
            pass
        return
    try:
        await cb.message.edit_text(
            f"✅ <b>系统恢复成功！</b>\n"
            f"👤 玩家：{stats['players']} 条\n"
            f"🏯 部落：{stats['clans']} 个\n"
            f"⚔️ 战斗日志：{stats['battles']} 条"
        )
    except Exception:
        pass


@router.callback_query(F.data == "clan_cancel_restore")
async def cb_cancel_restore(cb: types.CallbackQuery):
    try:
        await cb.message.edit_text("❌ 已取消恢复操作。")
    except Exception:
        pass


# ───────────────────── 停机维护 / 停机补偿 ─────────────────────

async def _compensation_cleanup(chat_id: int, msg_id: int, delay: float, redis_key: str):
    """延迟后清理停机补偿置顶：仅当 key 仍指向本消息时才解钉+删除+清 key"""
    await asyncio.sleep(delay)
    current = await redis.get(redis_key)
    if current and int(current.split(":")[0]) == msg_id:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await delete_msg_by_id(chat_id, msg_id)
        await redis.delete(redis_key)


@router.message(Command("clan_maintain"))
async def cmd_maintain(msg: types.Message):
    if not _check(msg):
        return
    if msg.from_user.id != SUPER_ADMIN_ID:
        return

    chat_id = msg.chat.id

    # 1. 清理所有进行中的攻击状态
    destroyed = len(_attack_staging)
    _attack_staging.clear()
    _attack_locks.clear()

    # 2. 解除旧的置顶公告（维护 / 补偿）
    for old_key in [f"compensation_pin:{chat_id}", f"maintenance_pin:{chat_id}"]:
        old_id = await redis.get(old_key)
        if old_id:
            old_msg = int(old_id.split(":")[0])
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=old_msg)
            except Exception:
                pass
            await delete_msg_by_id(chat_id, old_msg)
            await redis.delete(old_key)

    # 3. 设置维护标记
    await redis.set(f"maintenance:{chat_id}", "1")

    # 4. 发送维护公告并置顶
    body = (
        f"🔧 <b>【停机维护公告】</b>\n\n"
        f"系统即将进行维护，暂时停止服务。\n"
        f"• 已清理 <b>{destroyed}</b> 个进行中的攻击\n\n"
        f"维护完成后将置顶「停机补偿」公告并发放补偿资源，感谢耐心等待！"
    )
    announce = await send(chat_id, body)
    try:
        await pin_in_topic(chat_id, announce.message_id, disable_notification=False)
    except Exception as e:
        logger.warning(f"[maintenance] 置顶失败: {e}")

    await redis.set(f"maintenance_pin:{chat_id}", str(announce.message_id))

    # 删除超管的命令消息
    asyncio.create_task(auto_delete([msg], 0))


@router.message(Command("clan_compensate"))
async def cmd_compensate(msg: types.Message):
    if not _check(msg):
        return
    if msg.from_user.id != SUPER_ADMIN_ID:
        return

    chat_id = msg.chat.id

    # 解析可选的更新说明
    extra_desc = (msg.text or "").split(None, 1)[1].strip() if (msg.text or "").strip().count(" ") >= 1 else ""

    # 1. 给所有注册玩家发放 +500 金币 和 +500 圣水
    uids = await get_all_player_uids()
    for uid in uids:
        await add_gold(uid, 500)
        await add_elixir(uid, 500)

    # 2. 删除超管命令消息
    asyncio.create_task(auto_delete([msg], 0))

    # 3. 解除维护公告置顶并删除
    old_maint_id = await redis.get(f"maintenance_pin:{chat_id}")
    if old_maint_id:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=int(old_maint_id))
        except Exception:
            pass
        await delete_msg_by_id(chat_id, int(old_maint_id))
        await redis.delete(f"maintenance_pin:{chat_id}")

    # 4. 清除维护标记
    await redis.delete(f"maintenance:{chat_id}")

    # 5. 解除旧的补偿置顶（如有）
    old_comp = await redis.get(f"compensation_pin:{chat_id}")
    if old_comp:
        old_comp_msg = int(old_comp.split(":")[0])
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=old_comp_msg)
        except Exception:
            pass
        await delete_msg_by_id(chat_id, old_comp_msg)

    # 6. 发送补偿公告并置顶（固定带上“更新内容”区块）
    desc = (extra_desc or LAST_FIX_DESC or "本次为稳定性维护与体验优化。").strip()
    body = (
        f"🔧 <b>【停机补偿公告】</b>\n\n"
        f"✅ 维护已完成，服务恢复正常。\n"
        f"🎁 已向全体 <b>{len(uids)}</b> 名玩家发放补偿：\n"
        f"• 💰 金币 <b>+500</b>\n"
        f"• 💧 圣水 <b>+500</b>\n\n"
        f"📋 <b>本次更新内容</b>\n"
        f"{desc}\n\n"
        f"感谢耐心等待，继续战斗！"
    )

    announce = await send(chat_id, body)
    try:
        await pin_in_topic(chat_id, announce.message_id, disable_notification=False)
    except Exception:
        pass

    # 存储消息 ID + 时间戳，用于自动清理
    await redis.set(f"compensation_pin:{chat_id}", f"{announce.message_id}:{int(time.time())}")

    # 30 分钟后自动解除置顶并删除
    asyncio.create_task(_compensation_cleanup(chat_id, announce.message_id, 1800, f"compensation_pin:{chat_id}"))


# ───────────────────── 村庄面板回调 ─────────────────────

@router.callback_query(F.data.startswith("vm:"))
async def cb_village_panel(cb: types.CallbackQuery):
    parts = cb.data.split(":")
    action = parts[1]
    owner_uid = parts[-1]  # uid is always the last segment

    if str(cb.from_user.id) != owner_uid:
        await cb.answer("❌ 只有发起人可以操作！", show_alert=True)
        return

    uid = owner_uid
    name = cb.from_user.full_name or cb.from_user.username or "无名"
    p = await ensure_player(uid, name)

    if action == "refresh":
        await collect_resources(uid, p)
        clan_name = ""
        if p["clan_id"]:
            clan = await get_clan(p["clan_id"])
            if clan:
                clan_name = clan["name"]
        text = _render_village(p, name, clan_name)
        try:
            await cb.message.edit_text(text, reply_markup=_village_kb(uid))
        except Exception:
            pass
        await cb.answer("✅ 已刷新")

    elif action == "collect":
        g, e = await collect_resources(uid, p)
        if g <= 0 and e <= 0:
            await cb.answer("⏳ 还没产出足够的资源，稍后再来！", show_alert=True)
        else:
            clan_name = ""
            if p["clan_id"]:
                clan = await get_clan(p["clan_id"])
                if clan:
                    clan_name = clan["name"]
            text = _render_village(p, name, clan_name)
            text += f"\n\n📦 收集: 💰+{fmt_num(g)}  💧+{fmt_num(e)}"
            try:
                await cb.message.edit_text(text, reply_markup=_village_kb(uid))
            except Exception:
                pass
            await cb.answer(f"📦 💰+{fmt_num(g)} 💧+{fmt_num(e)}")

    elif action == "shop":
        bld = p["buildings"]
        th_lv = bld.get("town_hall", 1)
        lines = ["🏪 <b>建筑商店</b>\n"]
        buttons = []
        row = []
        for bid, info in BUILDINGS.items():
            cur_lv = bld.get(bid, 0)
            req = info["th_required"]
            max_lv = info["max_level"] if bid == "town_hall" else min(th_lv + 1, info["max_level"])
            res_icon = "💰" if info["resource"] == "gold" else "💧"

            if th_lv < req:
                lines.append(f"🔒 {info['name']} — 大本营 Lv.{req} 解锁")
            elif cur_lv == 0:
                # 未建造：显示 Lv.1 属性和建造费用
                cost = info["costs"][0]
                stat = ""
                if "production" in info:
                    stat = f" | 产量 {fmt_num(info['production'][0])}/h"
                elif "capacity" in info:
                    stat = f" | 容量 {fmt_num(info['capacity'][0])}"
                elif "defense" in info:
                    stat = f" | 防御 {fmt_num(info['defense'][0])}"
                lines.append(
                    f"{info['emoji']} {info['name']} [未建造]{stat} | 建造: {res_icon}{fmt_num(cost)}"
                )
                row.append(InlineKeyboardButton(
                    text=f"{info['emoji']} {info['name']} [建造]",
                    callback_data=f"vm:bld:{bid}:{uid}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            elif cur_lv >= max_lv:
                # 满级
                stat = ""
                if "production" in info:
                    stat = f" | 产量 {fmt_num(info['production'][cur_lv - 1])}/h"
                elif "capacity" in info:
                    stat = f" | 容量 {fmt_num(info['capacity'][cur_lv - 1])}"
                elif "defense" in info:
                    stat = f" | 防御 {fmt_num(info['defense'][cur_lv - 1])}"
                elif bid == "barracks":
                    stat = f" | 人口上限 {fmt_num(info['capacity'][cur_lv - 1])}"
                lines.append(
                    f"{info['emoji']} {info['name']} Lv.{cur_lv} ✅{stat}"
                )
                row.append(InlineKeyboardButton(
                    text=f"{info['emoji']} {info['name']} Lv.{cur_lv} ✅",
                    callback_data=f"vm:bld:{bid}:{uid}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            else:
                # 可升级
                cost = info["costs"][cur_lv]
                stat = ""
                if "production" in info:
                    stat = f" | {fmt_num(info['production'][cur_lv - 1])}/h"
                elif "capacity" in info:
                    stat = f" | 容量 {fmt_num(info['capacity'][cur_lv - 1])}"
                elif "defense" in info:
                    stat = f" | 防御 {fmt_num(info['defense'][cur_lv - 1])}"
                elif bid == "barracks":
                    stat = f" | 人口上限 {fmt_num(info['capacity'][cur_lv - 1])}"
                lines.append(
                    f"{info['emoji']} {info['name']} Lv.{cur_lv}{stat} | 升级: {res_icon}{fmt_num(cost)} → Lv.{cur_lv + 1}"
                )
                row.append(InlineKeyboardButton(
                    text=f"{info['emoji']} {info['name']} Lv.{cur_lv}",
                    callback_data=f"vm:bld:{bid}:{uid}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
        if row:
            buttons.append(row)
        buttons.append([
            InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}"),
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "bld":
        bid = parts[2]
        owner_uid = parts[3]
        if bid not in BUILDINGS:
            await cb.answer("未知建筑", show_alert=True)
            return
        info = BUILDINGS[bid]
        bld = p["buildings"]
        cur_lv = bld.get(bid, 0)
        th_lv = bld.get("town_hall", 1)
        max_lv = info["max_level"] if bid == "town_hall" else min(th_lv + 1, info["max_level"])
        res_icon = "💰" if info["resource"] == "gold" else "💧"

        lines = [f"{info['emoji']} <b>{info['name']}</b>"]
        if cur_lv == 0:
            cost = info["costs"][0]
            lines.append(f"状态: 未建造")
            lines.append(f"建造费: {res_icon} {fmt_num(cost)}")
            if "production" in info:
                lines.append(f"Lv.1 产量: {fmt_num(info['production'][0])}/小时")
            elif "capacity" in info:
                lines.append(f"Lv.1 容量: {fmt_num(info['capacity'][0])}")
            elif "defense" in info:
                lines.append(f"Lv.1 防御: {fmt_num(info['defense'][0])}")
            btns = [[InlineKeyboardButton(
                text=f"🔨 建造 ({res_icon}{fmt_num(cost)})",
                callback_data=f"vm:bu:{bid}:{uid}")]]
        elif cur_lv >= max_lv:
            lines.append(f"等级: Lv.{cur_lv} ✅ 满级")
            if "production" in info:
                lines.append(f"产量: {fmt_num(info['production'][cur_lv - 1])}/小时")
            elif "capacity" in info:
                lines.append(f"容量: {fmt_num(info['capacity'][cur_lv - 1])}")
            elif "defense" in info:
                lines.append(f"防御: {fmt_num(info['defense'][cur_lv - 1])}")
            if cur_lv < info["max_level"]:
                lines.append(f"\n⚠️ 受大本营限制，需先升级大本营")
            btns = []
        else:
            cost = info["costs"][cur_lv]
            lines.append(f"等级: Lv.{cur_lv}")
            if "production" in info:
                lines.append(f"产量: {fmt_num(info['production'][cur_lv - 1])}/小时")
                lines.append(f"下一级: Lv.{cur_lv + 1} → {fmt_num(info['production'][cur_lv])}/小时")
            elif "capacity" in info:
                lines.append(f"容量: {fmt_num(info['capacity'][cur_lv - 1])}")
                lines.append(f"下一级: Lv.{cur_lv + 1} → {fmt_num(info['capacity'][cur_lv])}")
            elif "defense" in info:
                lines.append(f"防御: {fmt_num(info['defense'][cur_lv - 1])}")
                lines.append(f"下一级: Lv.{cur_lv + 1} → {fmt_num(info['defense'][cur_lv])}")
            elif bid == "town_hall":
                lines.append(f"下一级: Lv.{cur_lv + 1}")
            lines.append(f"升级费: {res_icon} {fmt_num(cost)}")
            btns = [[InlineKeyboardButton(
                text=f"⬆️ 升级 ({res_icon}{fmt_num(cost)})",
                callback_data=f"vm:up:{bid}:{uid}")]]
        btns.append([InlineKeyboardButton(
            text="◀️ 返回商店", callback_data=f"vm:shop:{uid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        try:
            await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "up":
        bid = parts[2]
        owner_uid = parts[3]
        if bid not in BUILDINGS:
            await cb.answer("未知建筑", show_alert=True)
            return
        info = BUILDINGS[bid]
        bld = p["buildings"]
        cur_lv = bld.get(bid, 0)
        th_lv = bld.get("town_hall", 1)
        max_lv = info["max_level"] if bid == "town_hall" else min(th_lv + 1, info["max_level"])
        if cur_lv == 0:
            await cb.answer(f"❌ 尚未建造 {info['name']}", show_alert=True)
            return
        if cur_lv >= max_lv:
            await cb.answer("❌ 已满级或受大本营限制", show_alert=True)
            return
        cost = info["costs"][cur_lv]
        res = info["resource"]
        if p[res] < cost:
            res_name = "金币" if res == "gold" else "圣水"
            await cb.answer(f"❌ {res_name}不足！需要 {fmt_num(cost)}", show_alert=True)
            return
        if res == "gold":
            await add_gold(uid, -cost)
        else:
            await add_elixir(uid, -cost)
        bld[bid] = cur_lv + 1
        await set_buildings(uid, bld)
        p["buildings"] = bld
        p[res] -= cost
        extra = ""
        if "production" in info:
            extra = f"\n产量: {fmt_num(info['production'][cur_lv])}/小时"
        elif "capacity" in info:
            extra = f"\n容量: {fmt_num(info['capacity'][cur_lv])}"
        elif "defense" in info:
            extra = f"\n防御: {fmt_num(info['defense'][cur_lv])}"
        text = (
            f"⬆️ {info['emoji']} <b>{info['name']}</b> 升级到 Lv.{cur_lv + 1}！\n"
            f"花费: {'💰' if res == 'gold' else '💧'} {fmt_num(cost)}{extra}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回商店", callback_data=f"vm:shop:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer(f"✅ 升级到 Lv.{cur_lv + 1}")

    elif action == "bu":
        bid = parts[2]
        owner_uid = parts[3]
        if bid not in BUILDINGS:
            await cb.answer("未知建筑", show_alert=True)
            return
        info = BUILDINGS[bid]
        bld = p["buildings"]
        if bld.get(bid, 0) > 0:
            await cb.answer(f"❌ {info['name']}已建造", show_alert=True)
            return
        th_lv = bld.get("town_hall", 1)
        if th_lv < info["th_required"]:
            await cb.answer(f"🔒 需要大本营 Lv.{info['th_required']}", show_alert=True)
            return
        cost = info["costs"][0]
        res = info["resource"]
        if p[res] < cost:
            res_name = "金币" if res == "gold" else "圣水"
            await cb.answer(f"❌ {res_name}不足！需要 {fmt_num(cost)}", show_alert=True)
            return
        if res == "gold":
            await add_gold(uid, -cost)
        else:
            await add_elixir(uid, -cost)
        bld[bid] = 1
        await set_buildings(uid, bld)
        p["buildings"] = bld
        p[res] -= cost
        text = (
            f"✅ 建造 {info['emoji']} <b>{info['name']}</b> Lv.1 完成！\n"
            f"花费: {'💰' if res == 'gold' else '💧'} {fmt_num(cost)}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回商店", callback_data=f"vm:shop:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer(f"✅ 建造完成")

    elif action == "rates":
        bld = p["buildings"]
        gm_lv = bld.get("gold_mine", 0)
        ec_lv = bld.get("elixir_collector", 0)
        gm_info = BUILDINGS["gold_mine"]
        ec_info = BUILDINGS["elixir_collector"]

        lines = ["📊 <b>资源产量速率</b>\n"]
        cur_gm = f"Lv.{gm_lv} → {fmt_num(gm_info['production'][gm_lv - 1])}/小时" if gm_lv else "未建造"
        lines.append(f"⛏️ 金矿（当前 {cur_gm}）")
        parts_g = []
        for i in range(gm_info["max_level"]):
            lv = i + 1
            prod = gm_info["production"][i]
            if lv == gm_lv:
                parts_g.append(f"Lv.{lv}: ✅{fmt_num(prod)}/h")
            else:
                parts_g.append(f"Lv.{lv}: {fmt_num(prod)}/h")
        lines.append("  " + " | ".join(parts_g[:5]))
        if len(parts_g) > 5:
            lines.append("  " + " | ".join(parts_g[5:]))

        lines.append("")
        cur_ec = f"Lv.{ec_lv} → {fmt_num(ec_info['production'][ec_lv - 1])}/小时" if ec_lv else "未建造"
        lines.append(f"💧 圣水收集器（当前 {cur_ec}）")
        parts_e = []
        for i in range(ec_info["max_level"]):
            lv = i + 1
            prod = ec_info["production"][i]
            if lv == ec_lv:
                parts_e.append(f"Lv.{lv}: ✅{fmt_num(prod)}/h")
            else:
                parts_e.append(f"Lv.{lv}: {fmt_num(prod)}/h")
        lines.append("  " + " | ".join(parts_e[:5]))
        if len(parts_e) > 5:
            lines.append("  " + " | ".join(parts_e[5:]))

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回商店", callback_data=f"vm:shop:{uid}")]
        ])
        try:
            await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "army":
        troops = p["troops"]
        cap = get_army_capacity(p)
        used = get_army_size(p)
        lines = [f"🗡️ <b>部队</b> ({used}/{cap})\n"]
        total_power = 0
        if any(v > 0 for v in troops.values()):
            for tid, cnt in troops.items():
                if cnt > 0:
                    t = TROOPS[tid]
                    power = t["power"] * cnt
                    total_power += power
                    lines.append(f"  {t['emoji']} {t['name']} ×{cnt}  ⚔️ {fmt_num(power)}")
            lines.append(f"\n总攻击力: ⚔️ {fmt_num(total_power)}")
        else:
            lines.append("  （无部队）")

        available = get_available_troops(p)
        lines.append(f"\n📋 可训练兵种 (兵营 Lv.{p['buildings'].get('barracks', 1)}):")

        buttons = []
        row = []
        for tid in available:
            t = TROOPS[tid]
            row.append(InlineKeyboardButton(
                text=f"{t['emoji']} {t['name']} 💧{t['cost']}",
                callback_data=f"vm:sel:{tid}:{uid}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(
            text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "sel":
        tid = parts[2]
        owner_uid = parts[3]
        if tid not in TROOPS:
            await cb.answer("未知兵种", show_alert=True)
            return
        t = TROOPS[tid]
        available = get_available_troops(p)
        if tid not in available:
            await cb.answer(f"🔒 需要兵营 Lv.{t['barracks_level']}", show_alert=True)
            return
        cap = get_army_capacity(p)
        used = get_army_size(p)
        space = cap - used
        max_can = space // t["housing"] if t["housing"] > 0 else 0
        max_afford = int(p["elixir"] // t["cost"]) if t["cost"] > 0 else 0
        actual_max = min(max_can, max_afford)

        lines = [
            f"⚔️ <b>训练 {t['name']}</b>",
            f"💧 费用: {t['cost']}/个 | 🏠 占用: {t['housing']}",
            f"剩余空间: {space} | 💧 圣水: {fmt_num(p['elixir'])}",
        ]

        buttons = []
        row = []
        for cnt in [1, 5, 10]:
            if cnt <= actual_max:
                cost = t["cost"] * cnt
                row.append(InlineKeyboardButton(
                    text=f"×{cnt} ({fmt_num(cost)}💧)",
                    callback_data=f"vm:tr:{tid}:{cnt}:{uid}"))
        if actual_max > 0 and actual_max not in [1, 5, 10]:
            cost = t["cost"] * actual_max
            row.append(InlineKeyboardButton(
                text=f"最大 ×{actual_max}",
                callback_data=f"vm:tr:{tid}:{actual_max}:{uid}"))
        elif actual_max in [1, 5, 10]:
            pass  # already covered
        if not row:
            lines.append("\n❌ 无法训练（空间或圣水不足）")
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(
            text="◀️ 返回部队", callback_data=f"vm:army:{uid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "tr":
        tid = parts[2]
        count = int(parts[3])
        owner_uid = parts[4]
        if tid not in TROOPS:
            await cb.answer("未知兵种", show_alert=True)
            return
        t = TROOPS[tid]
        available = get_available_troops(p)
        if tid not in available:
            await cb.answer(f"🔒 需要兵营 Lv.{t['barracks_level']}", show_alert=True)
            return
        cap = get_army_capacity(p)
        used = get_army_size(p)
        space = cap - used
        housing_needed = t["housing"] * count
        if housing_needed > space:
            await cb.answer("❌ 兵营空间不足", show_alert=True)
            return
        total_cost = t["cost"] * count
        if p["elixir"] < total_cost:
            await cb.answer("❌ 圣水不足", show_alert=True)
            return
        await add_elixir(uid, -total_cost)
        troops = p["troops"]
        troops[tid] = troops.get(tid, 0) + count
        await set_troops(uid, troops)
        p["elixir"] -= total_cost
        new_used = used + housing_needed
        text = (
            f"✅ 训练了 {count} 个 {t['emoji']} <b>{t['name']}</b>！\n"
            f"花费: 💧 {fmt_num(total_cost)}\n"
            f"兵力: {new_used}/{cap}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回部队", callback_data=f"vm:army:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer(f"✅ 训练了 {count}个{t['name']}")

    elif action == "log":
        # 支持 vm:log:{uid} 和 vm:log:{page}:{uid} 两种格式
        if len(parts) == 3:
            page = 0
        else:
            page = int(parts[2])
            owner_uid = parts[3]
        logs = await get_battle_log(uid)
        text, date_keys = _format_battle_log_page(logs, page)
        nav = []
        if page < len(date_keys) - 1:
            nav.append(InlineKeyboardButton(
                text="◀️ 前一天", callback_data=f"vm:log:{page + 1}:{uid}"))
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="后一天 ▶️", callback_data=f"vm:log:{page - 1}:{uid}"))
        btns = []
        if nav:
            btns.append(nav)
        btns.append([InlineKeyboardButton(
            text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "attack":
        # 冷却检查
        last = _attack_locks.get(uid, 0)
        if time.time() - last < 30:
            remain = int(30 - (time.time() - last))
            await cb.answer(f"⏳ 攻击冷却中，{remain}秒后可再次攻击", show_alert=True)
            return

        troops = p["troops"]
        if not any(v > 0 for v in troops.values()):
            await cb.answer("❌ 没有部队！先训练部队", show_alert=True)
            return

        if p["shield_until"] > time.time():
            remain = int(p["shield_until"] - time.time())
            h, m = divmod(remain // 60, 60)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚔️ 放弃护盾并攻击", callback_data=f"vm:brk:{uid}")],
                [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")],
            ])
            try:
                await cb.message.edit_text(
                    f"🛡️ 你有护盾保护（剩余 {h}小时{m}分钟）\n攻击将会移除护盾！",
                    reply_markup=kb,
                )
            except Exception:
                pass
            await cb.answer()
            return

        await _do_attack_inline(cb, uid, name, p)

    elif action == "brk":
        from models import set_field
        await set_field(uid, "shield_until", "0")
        p["shield_until"] = 0
        await _do_attack_inline(cb, uid, name, p)

    elif action == "atgt":
        # 选择攻击目标，进入出兵面板
        target_uid = parts[2]
        target_p = await get_player(target_uid)
        if not target_p:
            await cb.answer("❌ 目标玩家不存在", show_alert=True)
            return
        _attack_staging[uid] = {
            "target_uid": target_uid,
            "target_name": target_p["name"],
            "target_data": target_p,
            "troops": {},
        }
        text, kb = _render_troop_panel(uid, p)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "asel":
        # 调整兵种数量 vm:asel:{tid}:{delta}:{uid}
        tid = parts[2]
        delta = int(parts[3])
        staging = _attack_staging.get(uid)
        if not staging:
            await cb.answer("❌ 请重新选择目标", show_alert=True)
            return
        have = p["troops"].get(tid, 0)
        cur = staging["troops"].get(tid, 0)
        new_val = max(0, min(have, cur + delta))
        if new_val == 0:
            staging["troops"].pop(tid, None)
        else:
            staging["troops"][tid] = new_val
        text, kb = _render_troop_panel(uid, p)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "anop":
        await cb.answer()

    elif action == "arec":
        # 智能配兵推荐
        staging = _attack_staging.get(uid)
        if not staging:
            await cb.answer("❌ 请重新选择目标", show_alert=True)
            return
        target_data = staging.get("target_data")
        if not target_data:
            target_data = await get_player(staging["target_uid"])
        if not target_data:
            await cb.answer("❌ 目标不存在", show_alert=True)
            return
        rec = recommend_troops(p, target_data)
        if not rec:
            await cb.answer("❌ 没有可用部队", show_alert=True)
            return
        staging["troops"] = rec
        text, kb = _render_troop_panel(uid, p)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer("🧠 已智能配兵")

    elif action == "aall":
        # 全部出战
        staging = _attack_staging.get(uid)
        if not staging:
            await cb.answer("❌ 请重新选择目标", show_alert=True)
            return
        staging["troops"] = {tid: cnt for tid, cnt in p["troops"].items() if cnt > 0}
        text, kb = _render_troop_panel(uid, p)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer("💪 已选择全部部队")

    elif action == "aclr":
        # 清空选择
        staging = _attack_staging.get(uid)
        if not staging:
            await cb.answer("❌ 请重新选择目标", show_alert=True)
            return
        staging["troops"] = {}
        text, kb = _render_troop_panel(uid, p)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer("🗑️ 已清空")

    elif action == "ago":
        # 确认进攻
        staging = _attack_staging.pop(uid, None)
        if not staging or not any(v > 0 for v in staging["troops"].values()):
            await cb.answer("❌ 请选择部队", show_alert=True)
            return
        # 冷却检查
        last = _attack_locks.get(uid, 0)
        if time.time() - last < 30:
            remain = int(30 - (time.time() - last))
            await cb.answer(f"⏳ 攻击冷却中，{remain}秒后", show_alert=True)
            return
        target_uid = staging["target_uid"]
        defender = await get_player(target_uid)
        if not defender:
            await cb.answer("❌ 目标不存在", show_alert=True)
            return
        if defender["shield_until"] > time.time():
            await cb.answer("❌ 对方已有护盾保护", show_alert=True)
            return
        # 验证选中部队是否仍然足够
        for tid, cnt in staging["troops"].items():
            if p["troops"].get(tid, 0) < cnt:
                await cb.answer("❌ 部队数量已变化，请重新选择", show_alert=True)
                return
        selected = staging["troops"]
        combat = calculate_attack(p, defender, selected_troops=selected)
        await execute_attack(uid, target_uid, p, defender, combat,
                             selected_troops=selected)
        _attack_locks[uid] = time.time()

        stars = combat["stars"]
        star_str = "⭐" * stars if stars else "💀 0星"

        # 部队明细
        troop_lines = []
        for tid, cnt in selected.items():
            if cnt > 0 and tid in TROOPS:
                t = TROOPS[tid]
                troop_lines.append(f"  {t['emoji']} {t['name']} ×{cnt}")
        troop_text = "\n".join(troop_lines)

        text = (
            f"⚔️ <b>战斗报告</b>\n\n"
            f"🗡️ {mention(uid, name)} → 🛡️ {safe_html(defender['name'])}\n\n"
            f"📋 出战部队:\n{troop_text}\n\n"
            f"{combat['details']}\n\n"
            f"结果: {star_str}\n"
            f"💰 掠夺金币: +{fmt_num(combat.get('actual_gold', 0))}\n"
            f"💧 掠夺圣水: +{fmt_num(combat.get('actual_elixir', 0))}\n"
            f"🏆 奖杯: {'+' if combat['atk_trophy'] >= 0 else ''}{combat['atk_trophy']}\n\n"
            f"⚠️ 出战部队已消耗"
        )
        if stars >= 1:
            shield_h = SHIELD_DURATION[stars] // 3600
            text += f"\n🛡️ 对方获得 {shield_h} 小时护盾"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "aback":
        # 返回目标列表
        _attack_staging.pop(uid, None)
        await _do_attack_inline(cb, uid, name, p)

    elif action == "clan":
        if not p["clan_id"]:
            # 未加入部落 → 显示部落选项面板
            clans = await list_clans()
            lines = [
                "🏯 <b>部落中心</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                "你当前未加入部落",
                "",
            ]
            btns = []
            if clans:
                lines.append("📋 <b>可加入部落</b>")
                for c in clans[:6]:
                    count = len(c.get("members", []))
                    lines.append(f"  • <b>{safe_html(c['name'])}</b>  |  👥 {count}人")
                    btns.append([InlineKeyboardButton(
                        text=f"➕ 加入 {c['name']}",
                        callback_data=f"vm:cjoin:{c['id']}:{uid}")])
            else:
                lines.append("暂无部落，快来创建第一个！")

            btns.append([
                InlineKeyboardButton(
                    text=f"🏗️ 创建部落 (💰{fmt_num(CLAN_CREATE_COST)})",
                    callback_data=f"vm:ccreate:{uid}"),
            ])
            btns.append([InlineKeyboardButton(
                text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")])
            kb = InlineKeyboardMarkup(inline_keyboard=btns)
            try:
                await cb.message.edit_text("\n".join(lines), reply_markup=kb)
            except Exception:
                pass
            await cb.answer()
            return

        clan = await get_clan(p["clan_id"])
        if not clan:
            await cb.answer("部落数据异常", show_alert=True)
            return

        members = clan.get("members", [])
        total_trophies = 0
        member_lines = []
        for m_uid in members:
            mp = await get_player(m_uid)
            if mp:
                total_trophies += mp["trophies"]
                role = "👑" if m_uid == clan.get("leader") else "👤"
                member_lines.append(
                    f"  {role} {safe_html(mp['name'])}  |  🏆 {mp['trophies']}"
                )

        avg_trophy = int(total_trophies / len(members)) if members else 0
        text = (
            f"🏯 <b>{safe_html(clan['name'])}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 成员: {len(members)}人\n"
            f"🏆 总奖杯: {total_trophies}\n"
            f"📈 人均奖杯: {avg_trophy}\n\n"
            f"👥 <b>成员列表</b>\n"
            + "\n".join(member_lines)
        )
        clan_btns = [
            [InlineKeyboardButton(text="👋 离开部落", callback_data=f"vm:cleave:{uid}")],
            [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=clan_btns)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "cjoin":
        clan_id = parts[2]
        owner_uid = parts[3]
        if p["clan_id"]:
            await cb.answer("❌ 你已在一个部落中", show_alert=True)
            return
        clan = await get_clan(clan_id)
        if not clan:
            await cb.answer("❌ 部落不存在", show_alert=True)
            return
        ok = await join_clan(uid, clan_id)
        if not ok:
            await cb.answer("❌ 部落成员已满", show_alert=True)
            return
        p["clan_id"] = clan_id
        # 刷新回村庄
        clan_name = clan["name"]
        await collect_resources(uid, p)
        text = _render_village(p, name, clan_name)
        text += f"\n\n✅ 成功加入部落 <b>{safe_html(clan_name)}</b>！"
        try:
            await cb.message.edit_text(text, reply_markup=_village_kb(uid))
        except Exception:
            pass
        await cb.answer(f"✅ 加入 {clan_name}")

    elif action == "ccreate":
        if p["clan_id"]:
            await cb.answer("❌ 你已在一个部落中，请先离开", show_alert=True)
            return
        if p["gold"] < CLAN_CREATE_COST:
            await cb.answer(f"❌ 金币不足！需要 {fmt_num(CLAN_CREATE_COST)}", show_alert=True)
            return
        await redis.setex(f"coc:pending_clan_create:{uid}", 180, "1")
        text = (
            "🏗️ <b>创建部落</b>\n\n"
            f"费用: 💰 {fmt_num(CLAN_CREATE_COST)} 金币\n"
            f"你当前: 💰 {fmt_num(p['gold'])}\n\n"
            "请直接发送一条消息作为部落名称\n"
            "（20个字符以内，3分钟内有效）"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="取消创建", callback_data=f"vm:ccancel:{uid}")],
            [InlineKeyboardButton(text="◀️ 返回", callback_data=f"vm:clan:{uid}")],
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "ccancel":
        await redis.delete(f"coc:pending_clan_create:{uid}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏯 返回部落中心", callback_data=f"vm:clan:{uid}")],
            [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")],
        ])
        try:
            await cb.message.edit_text("已取消创建部落", reply_markup=kb)
        except Exception:
            pass
        await cb.answer("已取消创建")

    elif action == "cleave":
        if not p["clan_id"]:
            await cb.answer("❌ 你不在任何部落中", show_alert=True)
            return
        clan = await get_clan(p["clan_id"])
        clan_name = clan["name"] if clan else "未知"
        await leave_clan(uid, p["clan_id"])
        p["clan_id"] = ""
        await collect_resources(uid, p)
        text = _render_village(p, name, "")
        text += f"\n\n👋 你已离开部落 <b>{safe_html(clan_name)}</b>"
        try:
            await cb.message.edit_text(text, reply_markup=_village_kb(uid))
        except Exception:
            pass
        await cb.answer(f"👋 已离开 {clan_name}")

    elif action == "rank":
        uids = await get_all_player_uids()
        players = []
        for u in uids:
            rp = await get_player(u)
            if rp:
                rp["uid"] = u
                players.append(rp)
        players.sort(key=lambda x: x["trophies"], reverse=True)
        top = players[:15]

        if not top:
            text = "🏆 <b>奖杯排行榜</b>\n\n暂无玩家"
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = ["🏆 <b>奖杯排行榜</b>\n"]
            for i, rp in enumerate(top):
                prefix = medals[i] if i < 3 else f"<b>#{i + 1}</b>"
                th_lv = rp["buildings"].get("town_hall", 1)
                me_tag = " ← 你" if rp["uid"] == uid else ""
                lines.append(
                    f"{prefix} {safe_html(rp['name'])} | "
                    f"🏆 {rp['trophies']} | 🏰 Lv.{th_lv} | "
                    f"⚔️ {rp['attack_wins']}胜{me_tag}"
                )
            text = "\n".join(lines)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()

    elif action == "help":
        text = (
            "📖 <b>部落冲突 · 帮助</b>\n\n"
            "🏠 <b>基础</b>\n"
            "  📦 收集 — 收取金矿/圣水产出\n"
            "  🏪 商店 — 建造/升级建筑\n"
            "  🗡️ 部队 — 查看与训练部队\n\n"
            "⚔️ <b>战斗</b>\n"
            "  ⚔️ 攻击 — 搜索对手并发起进攻\n"
            "  📜 战绩 — 查看最近战斗记录\n"
            "  🏆 排行 — 奖杯排行榜\n\n"
            "🏯 <b>部落</b>\n"
            "  加入或创建部落，与盟友并肩作战\n\n"
            "💡 <b>提示</b>\n"
            "  • 升级大本营解锁更多建筑\n"
            "  • 升级兵营解锁高级兵种\n"
            "  • 攻击后部队会消耗，需重新训练\n"
            "  • 被攻击后会获得护盾保护"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}")]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()


async def _do_attack_inline(cb: types.CallbackQuery, uid: str, name: str, p: dict):
    """通过村庄面板按钮展示目标选择面板"""
    targets = await find_targets(uid, p, count=5)
    if not targets:
        await cb.answer("🔍 没有可攻击的对手", show_alert=True)
        return

    lines = ["⚔️ <b>选择攻击目标</b>\n"]
    btns = []
    for t_uid, t_p in targets:
        th_lv = t_p["buildings"].get("town_hall", 1)
        defense = get_defense_power(t_p)
        total_res = t_p["gold"] + t_p["elixir"]
        lines.append(
            f"• {safe_html(t_p['name'])} | 🏰Lv.{th_lv} | "
            f"🏆{t_p['trophies']} | 🛡️{fmt_num(defense)} | "
            f"💰{fmt_num(t_p['gold'])} 💧{fmt_num(t_p['elixir'])}"
        )
        btns.append([InlineKeyboardButton(
            text=f"⚔️ {t_p['name']} (🏰{th_lv} 💰💧{fmt_num(total_res)})",
            callback_data=f"vm:atgt:{t_uid}:{uid}")])
    btns.append([
        InlineKeyboardButton(text="🔄 换一批", callback_data=f"vm:attack:{uid}"),
        InlineKeyboardButton(text="◀️ 返回村庄", callback_data=f"vm:refresh:{uid}"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


def _render_troop_panel(uid: str, p: dict) -> tuple[str, InlineKeyboardMarkup]:
    """渲染出兵选择面板（含战斗预览）"""
    staging = _attack_staging.get(uid)
    if not staging:
        return "❌ 无攻击状态", InlineKeyboardMarkup(inline_keyboard=[])

    selected = staging["troops"]
    all_troops = p["troops"]
    target_data = staging.get("target_data")

    lines = [
        f"🗡️ <b>出兵面板</b> → {safe_html(staging['target_name'])}",
        "",
    ]

    # 目标防御概要
    if target_data:
        bld = target_data["buildings"]
        def_parts = []
        for bid in ("cannon", "archer_tower", "wall"):
            lv = bld.get(bid, 0)
            if lv > 0:
                def_parts.append(f"{BUILDINGS[bid]['emoji']}{BUILDINGS[bid]['name']}Lv.{lv}")
        if def_parts:
            lines.append(f"🎯 防御: {' | '.join(def_parts)}")
        lines.append(
            f"💰{fmt_num(target_data['gold'])} 💧{fmt_num(target_data['elixir'])}"
        )
        lines.append("")

    total_power = 0
    for tid, cnt in selected.items():
        if cnt > 0 and tid in TROOPS:
            t = TROOPS[tid]
            total_power += t["power"] * cnt

    for tid, have in all_troops.items():
        if have <= 0:
            continue
        t = TROOPS[tid]
        sel = selected.get(tid, 0)
        power = t["power"] * sel if sel > 0 else 0
        lines.append(f"{t['emoji']} {t['name']}  {sel}/{have}  ⚔️{fmt_num(power)}")

    # 战斗预览
    if total_power > 0 and target_data:
        pv = preview_attack(p, target_data, selected)
        if pv["stars_min"] == pv["stars_max"]:
            star_text = f"{'⭐' * pv['stars_min'] if pv['stars_min'] else '0星'}"
        else:
            lo = '⭐' * pv['stars_min'] if pv['stars_min'] else '0星'
            hi = '⭐' * pv['stars_max']
            star_text = f"{lo} ~ {hi}"
        lines.append(
            f"\n📊 <b>战斗预估</b>\n"
            f"⚔️ {fmt_num(pv['power'])} vs 🛡️ {fmt_num(pv['defense'])}\n"
            f"预计: {star_text}\n"
            f"预计掠夺: 💰{fmt_num(pv['gold_est'])} 💧{fmt_num(pv['elixir_est'])}"
        )
    elif total_power > 0:
        lines.append(f"\n总攻击力: ⚔️ {fmt_num(total_power)}")
    else:
        lines.append("\n⚠️ 请选择部队出战")

    btns = []
    for tid, have in all_troops.items():
        if have <= 0:
            continue
        t = TROOPS[tid]
        sel = selected.get(tid, 0)
        row = []
        row.append(InlineKeyboardButton(
            text=f"{t['emoji']}{t['name']} {sel}/{have}",
            callback_data=f"vm:anop:{uid}"))
        if sel > 0:
            row.append(InlineKeyboardButton(
                text="➖", callback_data=f"vm:asel:{tid}:-1:{uid}"))
            row.append(InlineKeyboardButton(
                text="清零", callback_data=f"vm:asel:{tid}:-{sel}:{uid}"))
        if sel < have:
            row.append(InlineKeyboardButton(
                text="➕", callback_data=f"vm:asel:{tid}:1:{uid}"))
            row.append(InlineKeyboardButton(
                text="全部", callback_data=f"vm:asel:{tid}:{have - sel}:{uid}"))
        btns.append(row)

    # 快捷操作行: 智能配兵 / 全部出战 / 清空
    quick_row = []
    quick_row.append(InlineKeyboardButton(
        text="🧠 智能配兵", callback_data=f"vm:arec:{uid}"))
    has_any = any(v > 0 for v in selected.values())
    if not has_any:
        quick_row.append(InlineKeyboardButton(
            text="💪 全部出战", callback_data=f"vm:aall:{uid}"))
    else:
        quick_row.append(InlineKeyboardButton(
            text="🗑️ 清空", callback_data=f"vm:aclr:{uid}"))
    btns.append(quick_row)

    action_row = []
    if total_power > 0:
        action_row.append(InlineKeyboardButton(
            text="⚔️ 确认进攻！", callback_data=f"vm:ago:{uid}"))
    action_row.append(InlineKeyboardButton(
        text="◀️ 选目标", callback_data=f"vm:aback:{uid}"))
    btns.append(action_row)

    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    return "\n".join(lines), kb
