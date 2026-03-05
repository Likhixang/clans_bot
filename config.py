import os
import datetime

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN missing")

RUN_MODE = os.getenv("RUN_MODE", "webhook").strip().lower()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "https://cl.khixang.dpdns.org").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook"
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0").strip() or "0.0.0.0"
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8989"))
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()

POINTS_REDIS_HOST = os.getenv("POINTS_REDIS_HOST", "").strip()
POINTS_REDIS_PORT = int(os.getenv("POINTS_REDIS_PORT", "6379"))
POINTS_REDIS_DB = int(os.getenv("POINTS_REDIS_DB", "0"))
POINTS_REDIS_PASSWORD = os.getenv("POINTS_REDIS_PASSWORD", "").strip()

SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
ALLOWED_THREAD_ID = int(os.getenv("ALLOWED_THREAD_ID", "0"))

TZ_BJ = datetime.timezone(datetime.timedelta(hours=8))

# ===== 初始状态 =====
STARTING_GOLD = 1000
STARTING_ELIXIR = 1000
STARTING_POINTS = 0
STARTING_BUILDINGS = {
    "town_hall": 1,
    "gold_mine": 1,
    "elixir_collector": 1,
    "gold_storage": 1,
    "elixir_storage": 1,
    "barracks": 1,
}

# ===== 建筑定义 =====
# costs[i] = 从 Lv.i 升到 Lv.(i+1) 的费用；costs[0] = 建造费用
BUILDINGS = {
    "town_hall": {
        "name": "大本营", "emoji": "🏰",
        "resource": "gold",
        "max_level": 10, "th_required": 1,
        "costs": [0, 1000, 4000, 10000, 25000, 60000, 120000, 250000, 500000, 1000000],
        "desc": "基地核心，等级决定其他建筑上限",
    },
    "gold_mine": {
        "name": "金矿", "emoji": "⛏️",
        "resource": "gold",
        "max_level": 10, "th_required": 1,
        "costs": [100, 300, 800, 2000, 5000, 12000, 30000, 60000, 120000, 250000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出金币",
    },
    "elixir_collector": {
        "name": "圣水收集器", "emoji": "💧",
        "resource": "elixir",
        "max_level": 10, "th_required": 1,
        "costs": [100, 300, 800, 2000, 5000, 12000, 30000, 60000, 120000, 250000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出圣水",
    },
    "gold_storage": {
        "name": "金币仓库", "emoji": "🏦",
        "resource": "gold",
        "max_level": 10, "th_required": 1,
        "costs": [200, 500, 1500, 4000, 10000, 25000, 60000, 120000, 250000, 500000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "金币存储上限",
    },
    "elixir_storage": {
        "name": "圣水仓库", "emoji": "🧪",
        "resource": "elixir",
        "max_level": 10, "th_required": 1,
        "costs": [200, 500, 1500, 4000, 10000, 25000, 60000, 120000, 250000, 500000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "圣水存储上限",
    },
    "barracks": {
        "name": "兵营", "emoji": "🏕️",
        "resource": "elixir",
        "max_level": 8, "th_required": 1,
        "costs": [200, 600, 2000, 5000, 12000, 30000, 70000, 150000],
        "capacity": [30, 50, 80, 120, 160, 200, 250, 300],
        "desc": "训练部队，等级决定兵种解锁和部队上限",
    },
    "cannon": {
        "name": "加农炮", "emoji": "💣",
        "resource": "gold",
        "max_level": 10, "th_required": 2,
        "costs": [300, 800, 2000, 5000, 12000, 30000, 70000, 140000, 280000, 600000],
        "defense": [100, 220, 380, 580, 850, 1200, 1600, 2100, 2800, 3600],
        "desc": "地面防御建筑",
    },
    "archer_tower": {
        "name": "箭塔", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 3,
        "costs": [500, 1200, 3000, 7000, 16000, 38000, 80000, 160000, 320000, 700000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑",
    },
    "wall": {
        "name": "城墙", "emoji": "🧱",
        "resource": "gold",
        "max_level": 10, "th_required": 2,
        "costs": [50, 200, 600, 1500, 4000, 10000, 25000, 60000, 150000, 400000],
        "defense": [50, 120, 250, 450, 750, 1200, 2000, 3200, 5000, 8000],
        "desc": "基础防线，增加总防御值",
    },
}

# 建筑等级上限 = min(大本营等级 + 1, 建筑自身max_level)，大本营自身无此限制

# ===== 兵种定义 =====
TROOPS = {
    "barbarian": {
        "name": "野蛮人", "emoji": "⚔️",
        "cost": 50, "power": 25,
        "housing": 1, "barracks_level": 1,
        "desc": "便宜可靠的近战单位",
    },
    "archer": {
        "name": "弓箭手", "emoji": "🏹",
        "cost": 100, "power": 35,
        "housing": 1, "barracks_level": 2,
        "desc": "远程攻击单位",
    },
    "giant": {
        "name": "巨人", "emoji": "🦍",
        "cost": 500, "power": 60,
        "housing": 5, "barracks_level": 3,
        "desc": "高生命值肉盾，吸收伤害",
    },
    "goblin": {
        "name": "哥布林", "emoji": "👺",
        "cost": 80, "power": 15,
        "housing": 1, "barracks_level": 4,
        "loot_bonus": 2.0,
        "desc": "攻击力低但抢双倍资源",
    },
    "wall_breaker": {
        "name": "炸弹人", "emoji": "💥",
        "cost": 300, "power": 20,
        "housing": 2, "barracks_level": 5,
        "wall_damage": 8.0,
        "desc": "对城墙造成 8 倍伤害",
    },
    "balloon": {
        "name": "气球兵", "emoji": "🎈",
        "cost": 600, "power": 80,
        "housing": 5, "barracks_level": 6,
        "bypass_wall": True,
        "desc": "空中单位，无视城墙",
    },
    "wizard": {
        "name": "法师", "emoji": "🧙",
        "cost": 800, "power": 110,
        "housing": 4, "barracks_level": 7,
        "desc": "高伤害范围攻击法师",
    },
    "dragon": {
        "name": "飞龙", "emoji": "🐉",
        "cost": 2000, "power": 220,
        "housing": 20, "barracks_level": 8,
        "bypass_wall": True,
        "desc": "终极空中单位",
    },
}

# ===== PvP 常量 =====
SHIELD_DURATION = {3: 8 * 3600, 2: 4 * 3600, 1: 2 * 3600}
LOOT_PERCENT = {3: 0.30, 2: 0.20, 1: 0.10, 0: 0.0}
TROPHY_ATTACK = {3: 30, 2: 20, 1: 10, 0: -15}
TROPHY_DEFENSE = {3: -15, 2: -10, 1: -5, 0: 10}

# ===== 部落 =====
CLAN_CREATE_COST = 5000  # 金币
CLAN_MAX_MEMBERS = 50

# ===== 新手保护 =====
NEWBIE_SHIELD = 8 * 3600  # 8小时

# ===== 停机维护 =====
# 每次停机修复后更新此处，停机补偿公告会自动带上本次修复说明
LAST_FIX_DESC = "1) 积分与 dice_bot 完全互通（共享 user_balance）。2) 两边历史积分已合并，不丢分。3) 新号首次仅初始化一次 20000 积分。4) 资源兑换与显示逻辑已修复。"
