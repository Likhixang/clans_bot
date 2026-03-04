# Clans Bot 🏰

Telegram 群组策略游戏机器人，灵感来自《部落冲突》。玩家可以建造村庄、训练军队、掠夺资源、加入部落。

## 功能

- **村庄系统** — 5×5 可视化村庄网格，建造 / 升级建筑
- **资源系统** — 金矿 & 圣水收集器自动产出，仓库存储上限
- **兵种系统** — 8 种兵种，各具特色（空中单位、城墙克星、资源掠夺加成等）
- **PvP 战斗** — 多目标选择、自定义配兵、战前预览、智能推荐配兵
- **部落系统** — 创建 / 加入部落，同部落保护，查看成员与排行
- **战绩系统** — 按日分组查看，翻页浏览，出战部队记录
- **交互面板** — 全按钮操作，无需记忆命令

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

| 变量 | 说明 |
|------|------|
| `BOT_TOKEN` | Telegram Bot Token（从 @BotFather 获取） |
| `SUPER_ADMIN_ID` | 超级管理员 Telegram UID |
| `ADMIN_IDS` | 管理员 UID 列表，逗号分隔 |
| `ALLOWED_CHAT_ID` | 限定群组 ID（0 = 不限制） |
| `ALLOWED_THREAD_ID` | 限定话题 ID（0 = 不限制） |
| `REDIS_PASSWORD` | Redis 密码 |

### 2. Docker 部署

```bash
docker compose up -d --build
```

容器会自动启动：
- `clans_bot` — 机器人主进程
- `clans_redis` — Redis 数据库

### 3. 使用

在 Telegram 群组中发送 `/clan_start` 注册并开始游戏。

## 命令列表

| 命令 | 说明 |
|------|------|
| `/clan_start` | 注册 / 进入游戏 |
| `/clan_me` | 查看村庄面板（推荐） |
| `/clan_help` | 帮助信息 |
| `/clan_collect` | 收集资源 |
| `/clan_shop` | 建筑商店 |
| `/clan_build <建筑ID>` | 建造新建筑 |
| `/clan_upgrade <建筑ID>` | 升级建筑 |
| `/clan_troops` | 兵种列表 |
| `/clan_train <兵种ID> [数量]` | 训练部队 |
| `/clan_army` | 查看当前部队 |
| `/clan_attack` | 攻击其他玩家 |
| `/clan_log` | 战绩记录（按日查看） |
| `/clan_rank` | 奖杯排行榜 |
| `/clan_create <名称>` | 创建部落 |
| `/clan_join <部落ID>` | 加入部落 |
| `/clan_leave` | 离开部落 |
| `/clan_info` | 查看部落信息 |
| `/clan_list` | 部落列表 |
| `/clan_give <UID> <gold/elixir> <数量>` | [超管] 发放资源 |
| `/clan_backup_db` | [超管] 备份数据库 |
| `/clan_restore_db` | [超管] 恢复数据库 |

> 大多数功能可通过 `/clan_me` 面板上的按钮直接操作。

## 战斗系统

1. **选择目标** — 系统推荐 5 个候选对手，展示大本营等级、奖杯、防御力、资源
2. **选择部队** — 通过 ➕/➖/全部/清零 按钮自由配兵，或点击 🧠 智能推荐
3. **战前预览** — 预估星级范围和掠夺量
4. **确认出击** — 只消耗选中的部队，未选中的保留
5. **同部落保护** — 不会匹配到同一部落的成员

## 项目结构

```
├── main.py          # 入口 & 命令注册
├── core.py          # Bot / Dispatcher / Redis 初始化
├── config.py        # 常量：建筑、兵种、PvP 规则
├── models.py        # Redis 数据层
├── handlers.py      # 命令 & 回调处理
├── combat.py        # PvP 战斗逻辑（配兵推荐、战前预览）
├── tasks.py         # 定时备份 & 恢复
├── utils.py         # 工具函数
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## 技术栈

- **Python 3.12** + **aiogram 3**
- **Redis 7** — 数据持久化
- **SQLite** — 定时备份
- **Docker Compose** — 一键部署
