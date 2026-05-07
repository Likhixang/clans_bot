[English](README.md) | [中文](README.zh-CN.md)

# Clans Bot 🏰

A Telegram group strategy game bot inspired by Clash of Clans. Players build villages, train armies, raid resources, and join clans.

## Features

- **Village System** — 5×5 to 8×8 visual village grid; build, upgrade, and remove buildings (with confirmation panel)
- **Resource System** — Gold mines & elixir collectors auto-produce; storage caps; daily resource settlement at 23:59 for all players (no need to enable auto-collect)
- **Exchange System** — Points buy gold/elixir at 1:1; gold-elixir swap with 2% loss; purchase auto-collect (6h) and point shields (6h, dynamic pricing; first 4 purchases per day eligible for shield-break refund)
- **Shared Points** — Integrates with dice_bot's `user_balance:{uid}` for a shared point ledger
  - First account creation on either side grants 20,000 points; creating on the other side does not double-issue
- **Troop System** — 12 troop types with distinct traits (air units, wall breakers, loot bonuses)
- **High TH Expansion** — Town Hall up to Lv.15, with Air Defense/Mortar defense lines and mid-to-late-game troop tiers
- **PvP Combat** — Attack by replying to a target, custom troop composition, pre-battle preview, smart troop recommendation, shield-break refund rules
- **Wild Raids** — Random animal raids weighted by player strength, auto-settled with normal notifications
- **Clan System** — Create/join clans, same-clan protection, member and ranking views
- **Clan War (MVP)** — 5v5, preparation/battle phases, sign-up, battle log, auto-switching pinned phase reminders
- **Battle Records** — Daily grouped history with pagination and deployed troop records
- **Interactive Panel** — Fully button-operated, no command memorization needed
- **Maintenance Mode** — One-click maintenance that blocks all actions and pins a maintenance notice
- **Maintenance Compensation** — Server-wide gold +500, elixir +500 (no points), pinned compensation notice (auto-cleared after 30 minutes)

## Quick Start

### 1. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram Bot Token (from @BotFather) |
| `SUPER_ADMIN_ID` | Super-admin Telegram UID |
| `ADMIN_IDS` | Admin UID list, comma-separated |
| `ALLOWED_CHAT_ID` | Restricted group ID (0 = unrestricted) |
| `ALLOWED_THREAD_ID` | Restricted topic ID (0 = unrestricted) |
| `REDIS_PASSWORD` | Redis password |
| `POINTS_REDIS_HOST` | Shared points Redis host (set to `dice_redis` for dice_bot integration) |
| `POINTS_REDIS_PORT` | Shared points Redis port (default 6379) |
| `POINTS_REDIS_DB` | Shared points Redis DB (default 0) |
| `POINTS_REDIS_PASSWORD` | Shared points Redis password |

### 2. Docker Deployment

```bash
docker compose up -d --build
```

Containers start automatically:
- `clans_bot` — main bot process
- `clans_redis` — Redis database

### 3. Usage

Send `/clan_start` in a Telegram group to register and start playing.

## Command List

| Command | Description |
|---------|-------------|
| `/clan_start` | Register / enter game |
| `/clan_me` | View village panel (recommended) |
| `/clan_help` | Help info |
| `/clan_collect` | Collect resources |
| `/clan_shield` | Buy point shield (6h, dynamic price; first 4 shield-breaks per day refunded) |
| `/clan_repair <name/all>` | Repair damaged defenses with gold |
| `/clan_buy <gold/elixir> <points>` | Buy resources with points at 1:1 |
| `/clan_swap <gold/elixir> <amount>` | Swap gold/elixir (2% loss, rounded) |
| `/clan_sell <gold/elixir> <amount>` | Sell resources for points (100 resources = 1 point, 2% resource tax) |
| `/clan_shop` | Building shop |
| `/clan_build` | Build new structure (use shop buttons) |
| `/clan_remove <ID/name>` | Remove building (refund decays linearly over 3 days to 0; Town Hall is irremovable; Chinese names supported) |
| `/clan_upgrade` | Upgrade building (use shop buttons) |
| `/clan_wiki` | Wiki navigation |
| `/clan_wiki_troops` | Troop wiki (combat-oriented) |
| `/clan_wiki_defense` | Defense wiki (combat-oriented) |
| `/clan_wiki_buildings` | Utility building wiki |
| `/clan_troops` | Troop list |
| `/clan_train <name> <count>` | Train troops (Chinese names supported; use troop buttons) |
| `/clan_army` | View current army |
| `/clan_attack` | Attack another player (must reply to target's message) |
| `/clan_log` | Battle records (daily view) |
| `/clan_rank` | Trophy leaderboard |
| `/clan_create <name>` | Create a clan |
| `/clan_join <name>` | Join a clan |
| `/clan_leave` | Leave current clan |
| `/clan_war` | Clan war center (sign-up/attack/log) |
| `/clan_war_challenge <clan>` | [Leader] Declare war |
| `/clan_war_history [count]` | Recent war reports (default 10, max 30) |
| `/clan_info` | View clan info |
| `/clan_list` | Clan list |
| `/clan_give <amount>` (reply) | [Super-admin] Grant points |
| `/clan_take <amount>` (reply) | [Super-admin] Deduct points |
| `/clan_maintain` | [Super-admin] Enable maintenance mode |
| `/clan_compensate [notes]` | [Super-admin] Issue compensation (gold +500, elixir +500 server-wide) |
| `/clan_backup_db` | [Super-admin] Backup database |
| `/clan_restore_db` | [Super-admin] Restore database |

> Most features are accessible via buttons on the `/clan_me` panel.
>
> Maintenance mode prompts only take effect in the business topic specified by `ALLOWED_CHAT_ID + ALLOWED_THREAD_ID`, without interfering with other topics in the same group.

## TH15 Expansion Highlights

- Town Hall cap: `Lv.15`
- Grid expansion cadence:
  - TH `1–3`: `5×5`
  - TH `4–5`: `6×6`
  - TH `6–7`: `7×7`
  - TH `8+`: `8×8`
- New defense buildings:
  - `🚀 Air Defense` (TH5)
  - `🚀 Air Defense II` (TH8)
  - `🚀 Air Defense III` (TH11)
  - `🧨 Mortar` (TH5)
  - `🧨 Mortar II` (TH9)
- New troops and barracks unlocks:
  - `🤖 P.E.K.K.A` (Barracks Lv.7)
  - `😇 Angel` (Barracks Lv.8)
  - `⚡ Electro Dragon` (Barracks Lv.9)
  - `🦣 Yeti` (Barracks Lv.10)

## Combat System

1. **Initiation** — Must reply to a real player's message before using `/clan_attack`; replying to a bot is intercepted (a fine is imposed for attempting to attack a bot)
2. **Troop Selection** — Freely compose via ➕/➖/All/Clear buttons, or tap 🧠 Smart Recommend
3. **Pre-Battle Preview** — Estimated star range and loot amount
4. **Confirm Attack** — Only selected troops are consumed; unselected ones are kept
5. **Same-Clan Protection** — Cannot attack clan members
6. **Air/Ground Counter System** — Dynamically resolved by air/ground population ratio: air troops are more vulnerable to anti-air but stronger against ground defenses; ground troops are more vulnerable to ground defenses but stronger against anti-air (preview matches actual combat)
7. **Loot Multiplier Correction** — Goblin loot bonus is calculated by barracks capacity proportion with a hard cap, preventing abnormal loot amplification from small goblin counts

## Shields and Balance

1. **Point Shield** — Purchased at the exchange center, 6h duration, dynamically priced by TH/defense/lootable resources (50–500 points)
2. **No Shield Stacking** — Cannot purchase another point shield while any shield is active (including shields gained from being attacked)
3. **Manual Break Refund** — For the first 4 point shields purchased per player per day, voluntarily breaking the shield to attack refunds points proportionally by remaining time (rounded to integer); from the 5th purchase onward, breaks are not refunded
4. **Defense Shield Scaling** — Player PvP only: shield duration gained from being attacked is shortened by base strength weighting; 0 stars still grants a short shield (~30–45 min); 1–3 stars scale by star count with a minimum of 1 hour
5. **Wild Raid Resolution** — Wild raids can hit any player; if the target has a shield, the attack fails with all-zero settlement, but shield duration is randomly reduced based on target TH level (random even at same level, higher TH more likely to lose more); the event is still logged with a normal notification; wild raids do not grant shields
6. **Raid Scheduling** — Uses distributed time slots; at most 5 players are raided within a 5-hour window, with staggered raid times to avoid bursts
7. **Resource Overflow Shield Decay** — During shield, if "estimated lootable resources (storage + collectors)" exceeds a dynamic threshold, random accelerated decay triggers; more resources and higher target TH lead to faster random decay (applies to both purchased and PvP shields)
8. **Recon Cost and Shield Decay** — Each observation lock costs 100 gold; each player can observe the same target at most 3 times per shield cycle (resets next cycle); every 3 cumulative observations on a target randomly reduces shield duration by target TH level (random, higher TH more vulnerable, never clears shield entirely)
9. **Break-Shield Direct Reply** — If you have a shield and initiate via "drop shield and attack," you proceed directly against the replied target: target has a shield → immediate failure; no shield → recon/troop selection
10. **Bot Attack Penalty** — Replying to a bot's message to attack incurs a fine of 1,000 gold (or reduces to 0 if insufficient)
11. **Defense Damage and Repair** — After being attacked by a player or wild raid, defense buildings accumulate damage and lose defense proportionally (can drop to 0); repair with gold via shop building detail page or `/clan_repair`
12. **Building Removal** — Remove non-Town-Hall buildings via shop detail page or `/clan_remove`; refund decays linearly from original build cost over 3 days, reaching 0; button operation includes confirmation

## Clan War (MVP)

1. **Scale and Phases** — Fixed `5v5`, preparation phase (sign-up) then battle phase (attacks)
2. **Sign-Up Rules** — Sign up or cancel via panel buttons during preparation; both sides must reach minimum headcount (default 3) for war to start
3. **Attack Rules** — Max 2 attacks per person during battle phase; only clan war stars/destruction % count, no impact on regular loot, trophies, or shields
4. **Scoring** — Only the best result per defense target is kept (star count first, then destruction %)
5. **Reminders** — Prominent announcements pinned during preparation and battle phases; old pins auto-removed on phase transitions or war end
6. **Membership Lock** — During an active clan war, joining or leaving the corresponding clan is forbidden to prevent roster bypass
7. **Rewards** — Points awarded by participation roster at war end: winners +120/person, losers +60/person, draw +90/person each side

### New Building Effects

- `Builder's Hut`: boosts auto-collect efficiency (+3%/level, cap +30%)
- `Laboratory`: boosts total troop attack (+2%/level, cap +20%)
- `Spell Factory`: reduces point shield price (−2%/level, cap −18%)
- `Siege Workshop`: increases troop capacity (+12/level)
- `Hero Altar` + `Clan Castle`: provides global defense aura (combined cap ~+35%)
- `Clan Castle`: bonus points on clan war settlement (+2/level, cap +20)

## Super-Admin Privileges

The account tied to `SUPER_ADMIN_ID` has permanent privileges:
- Automatically calibrated to "all buildings unlocked + fully maxed" on login/load
- Auto-collect permanently enabled (long-lived timestamp)
- Base building damage permanently zero (no repairs needed, always optimal)
- Default permanent no-shield; shields only take effect when actively purchased (no auto-shield from being attacked or system flows)

## Project Structure

```
├── main.py          # Entry point & command registration
├── core.py          # Bot / Dispatcher / Redis initialization
├── config.py        # Constants: buildings, troops, PvP rules
├── models.py        # Redis data layer
├── handlers.py      # Command & callback handlers
├── combat.py        # PvP combat logic (troop recommendation, pre-battle preview)
├── tasks.py         # Scheduled backup & restore + auto-collect + daily settlement + wild raids
├── utils.py         # Utility functions
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Tech Stack

- **Python 3.12** + **aiogram 3**
- **Redis 7** — data persistence
- **SQLite** — scheduled backups
- **Docker Compose** — one-click deployment
