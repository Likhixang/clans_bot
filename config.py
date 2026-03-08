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
    "gold_mine_2": 0,
    "gold_mine_3": 0,
    "elixir_collector": 1,
    "elixir_collector_2": 0,
    "elixir_collector_3": 0,
    "gold_storage": 1,
    "gold_storage_2": 0,
    "gold_storage_3": 0,
    "elixir_storage": 1,
    "elixir_storage_2": 0,
    "elixir_storage_3": 0,
    "barracks": 1,
    "cannon_2": 0,
    "cannon_3": 0,
    "cannon_4": 0,
    "cannon_5": 0,
    "archer_tower_2": 0,
    "archer_tower_3": 0,
    "archer_tower_4": 0,
    "archer_tower_5": 0,
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
    "gold_mine_2": {
        "name": "金矿Ⅱ", "emoji": "⛏️",
        "resource": "gold",
        "max_level": 10, "th_required": 2,
        "costs": [300, 600, 1500, 3500, 8000, 18000, 42000, 85000, 170000, 320000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出金币（第2座）",
    },
    "gold_mine_3": {
        "name": "金矿Ⅲ", "emoji": "⛏️",
        "resource": "gold",
        "max_level": 10, "th_required": 5,
        "costs": [800, 1500, 3200, 7000, 15000, 32000, 70000, 140000, 280000, 500000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出金币（第3座）",
    },
    "elixir_collector": {
        "name": "圣水收集器", "emoji": "💧",
        "resource": "elixir",
        "max_level": 10, "th_required": 1,
        "costs": [100, 300, 800, 2000, 5000, 12000, 30000, 60000, 120000, 250000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出圣水",
    },
    "elixir_collector_2": {
        "name": "圣水收集器Ⅱ", "emoji": "💧",
        "resource": "elixir",
        "max_level": 10, "th_required": 2,
        "costs": [300, 600, 1500, 3500, 8000, 18000, 42000, 85000, 170000, 320000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出圣水（第2座）",
    },
    "elixir_collector_3": {
        "name": "圣水收集器Ⅲ", "emoji": "💧",
        "resource": "elixir",
        "max_level": 10, "th_required": 5,
        "costs": [800, 1500, 3200, 7000, 15000, 32000, 70000, 140000, 280000, 500000],
        "production": [100, 200, 400, 700, 1200, 2000, 3500, 5500, 8000, 12000],
        "desc": "每小时产出圣水（第3座）",
    },
    "gold_storage": {
        "name": "金币仓库", "emoji": "🏦",
        "resource": "gold",
        "max_level": 10, "th_required": 1,
        "costs": [200, 500, 1500, 4000, 10000, 25000, 60000, 120000, 250000, 500000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "金币存储上限",
    },
    "gold_storage_2": {
        "name": "金币仓库Ⅱ", "emoji": "🏦",
        "resource": "gold",
        "max_level": 10, "th_required": 3,
        "costs": [800, 1800, 4500, 11000, 26000, 65000, 150000, 300000, 600000, 1200000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "金币存储上限（第2座）",
    },
    "gold_storage_3": {
        "name": "金币仓库Ⅲ", "emoji": "🏦",
        "resource": "gold",
        "max_level": 10, "th_required": 6,
        "costs": [2000, 4200, 10000, 24000, 55000, 130000, 300000, 600000, 1200000, 2200000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "金币存储上限（第3座）",
    },
    "elixir_storage": {
        "name": "圣水仓库", "emoji": "🧪",
        "resource": "elixir",
        "max_level": 10, "th_required": 1,
        "costs": [200, 500, 1500, 4000, 10000, 25000, 60000, 120000, 250000, 500000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "圣水存储上限",
    },
    "elixir_storage_2": {
        "name": "圣水仓库Ⅱ", "emoji": "🧪",
        "resource": "elixir",
        "max_level": 10, "th_required": 3,
        "costs": [800, 1800, 4500, 11000, 26000, 65000, 150000, 300000, 600000, 1200000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "圣水存储上限（第2座）",
    },
    "elixir_storage_3": {
        "name": "圣水仓库Ⅲ", "emoji": "🧪",
        "resource": "elixir",
        "max_level": 10, "th_required": 6,
        "costs": [2000, 4200, 10000, 24000, 55000, 130000, 300000, 600000, 1200000, 2200000],
        "capacity": [5000, 12000, 30000, 60000, 120000, 250000, 500000, 1000000, 2500000, 5000000],
        "desc": "圣水存储上限（第3座）",
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
    "cannon_2": {
        "name": "加农炮Ⅱ", "emoji": "💣",
        "resource": "gold",
        "max_level": 10, "th_required": 4,
        "costs": [900, 2000, 4500, 10000, 22000, 50000, 100000, 200000, 380000, 760000],
        "defense": [100, 220, 380, 580, 850, 1200, 1600, 2100, 2800, 3600],
        "desc": "地面防御建筑（第2座）",
    },
    "cannon_3": {
        "name": "加农炮Ⅲ", "emoji": "💣",
        "resource": "gold",
        "max_level": 10, "th_required": 5,
        "costs": [1400, 2800, 6000, 13000, 28000, 62000, 120000, 230000, 430000, 850000],
        "defense": [100, 220, 380, 580, 850, 1200, 1600, 2100, 2800, 3600],
        "desc": "地面防御建筑（第3座）",
    },
    "cannon_4": {
        "name": "加农炮Ⅳ", "emoji": "💣",
        "resource": "gold",
        "max_level": 10, "th_required": 7,
        "costs": [2200, 4200, 8500, 18000, 36000, 76000, 145000, 270000, 500000, 980000],
        "defense": [100, 220, 380, 580, 850, 1200, 1600, 2100, 2800, 3600],
        "desc": "地面防御建筑（第4座）",
    },
    "cannon_5": {
        "name": "加农炮Ⅴ", "emoji": "💣",
        "resource": "gold",
        "max_level": 10, "th_required": 9,
        "costs": [3200, 6000, 12000, 25000, 48000, 98000, 185000, 340000, 620000, 1200000],
        "defense": [100, 220, 380, 580, 850, 1200, 1600, 2100, 2800, 3600],
        "desc": "地面防御建筑（第5座）",
    },
    "archer_tower": {
        "name": "箭塔", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 3,
        "costs": [500, 1200, 3000, 7000, 16000, 38000, 80000, 160000, 320000, 700000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑",
    },
    "archer_tower_2": {
        "name": "箭塔Ⅱ", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 4,
        "costs": [1200, 2600, 5600, 12000, 25000, 52000, 105000, 210000, 410000, 820000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑（第2座）",
    },
    "archer_tower_3": {
        "name": "箭塔Ⅲ", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 6,
        "costs": [1800, 3800, 7800, 16000, 32000, 65000, 130000, 250000, 480000, 920000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑（第3座）",
    },
    "archer_tower_4": {
        "name": "箭塔Ⅳ", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 8,
        "costs": [2600, 5200, 10500, 21000, 41000, 82000, 160000, 300000, 560000, 1050000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑（第4座）",
    },
    "archer_tower_5": {
        "name": "箭塔Ⅴ", "emoji": "🏹",
        "resource": "gold",
        "max_level": 10, "th_required": 10,
        "costs": [3600, 7200, 14000, 28000, 54000, 105000, 200000, 360000, 650000, 1200000],
        "defense": [150, 320, 550, 850, 1200, 1650, 2200, 2900, 3800, 5000],
        "desc": "对空对地防御建筑（第5座）",
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
LOOT_PERCENT = {3: 0.60, 2: 0.40, 1: 0.20, 0: 0.10}
# 掠夺权重：仓库更难抢，收集器/矿更容易被抢
LOOT_STORAGE_FACTOR = 0.55
LOOT_COLLECTOR_FACTOR = 1.35
TROPHY_ATTACK = {3: 30, 2: 20, 1: 10, 0: -15}
TROPHY_DEFENSE = {3: -15, 2: -10, 1: -5, 0: 10}

# ===== 部落 =====
CLAN_CREATE_COST = 5000  # 金币
CLAN_MAX_MEMBERS = 50

# ===== 新手保护 =====
NEWBIE_SHIELD = 8 * 3600  # 8小时

# ===== 护盾溢出衰减（方案5）=====
# 阈值 = 基础值 + 大本营等级 * 每级增量
SHIELD_DECAY_THRESHOLD_BASE = 20_000
SHIELD_DECAY_THRESHOLD_PER_TH = 40_000
# 新手宽限（秒）：宽限内不触发“资源溢出加速掉盾”
SHIELD_DECAY_NEWBIE_GRACE = 10 * 3600
# 分段衰减速率（每小时额外衰减秒数，已调高）
SHIELD_DECAY_RATE_LOW = 30 * 60     # 1.0x~1.5x 阈值
SHIELD_DECAY_RATE_MID = 60 * 60     # 1.5x~2.0x 阈值
SHIELD_DECAY_RATE_HIGH = 120 * 60   # >=2.0x 阈值

# ===== 停机维护 =====
# 每次停机修复后更新此处，停机补偿公告会自动带上本次修复说明
LAST_FIX_DESC = (
    "停机补偿公告的“更新内容”文案已更新，并优化为列表展示；野外袭击通知已改为普通通知（不再 @ 基地主人）。"
)
