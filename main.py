import asyncio
import logging
import time

from aiogram.types import BotCommand, BotCommandScopeAllGroupChats
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from core import bot, dp, redis, points_redis
from handlers import router, _compensation_cleanup
from models import sanitize_all_player_resources
from tasks import hourly_backup_task, auto_collect_task, daily_collect_all_task, random_bot_attack_task, shield_decay_task, war_progress_task
from config import (
    RUN_MODE,
    WEBHOOK_BASE_URL,
    WEBHOOK_PATH,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    WEBHOOK_SECRET_TOKEN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

dp.include_router(router)

COMMANDS = [
    BotCommand(command="clan_start", description="注册 / 进入游戏"),
    BotCommand(command="clan_me", description="查看村庄面板"),
    BotCommand(command="clan_help", description="查看帮助"),
    BotCommand(command="clan_collect", description="收集资源"),
    BotCommand(command="clan_attack", description="攻击其他玩家"),
    BotCommand(command="clan_army", description="查看当前部队"),
    BotCommand(command="clan_train", description="训练部队"),
    BotCommand(command="clan_troops", description="兵种列表"),
    BotCommand(command="clan_rank", description="奖杯排行榜"),
    BotCommand(command="clan_log", description="战绩记录"),
    BotCommand(command="clan_auto", description="购买自动收集"),
    BotCommand(command="clan_shield", description="购买积分护盾"),
    BotCommand(command="clan_buy", description="积分购买资源"),
    BotCommand(command="clan_swap", description="金币圣水互换"),
    BotCommand(command="clan_sell", description="资源兑换积分"),
    BotCommand(command="clan_repair", description="修复防御建筑"),
    BotCommand(command="clan_shop", description="建筑商店"),
    BotCommand(command="clan_build", description="建造新建筑"),
    BotCommand(command="clan_remove", description="移除建筑并返还资源"),
    BotCommand(command="clan_upgrade", description="升级建筑"),
    BotCommand(command="clan_wiki", description="图鉴导航"),
    BotCommand(command="clan_wiki_troops", description="兵种图鉴"),
    BotCommand(command="clan_wiki_defense", description="防御图鉴"),
    BotCommand(command="clan_wiki_buildings", description="功能建筑图鉴"),
    BotCommand(command="clan_create", description="创建部落"),
    BotCommand(command="clan_info", description="查看部落信息"),
    BotCommand(command="clan_list", description="部落列表"),
    BotCommand(command="clan_join", description="加入部落"),
    BotCommand(command="clan_leave", description="离开部落"),
    BotCommand(command="clan_war", description="部落战中心"),
    BotCommand(command="clan_war_challenge", description="首领发起部落战"),
    BotCommand(command="clan_war_history", description="最近部落战战报"),
    BotCommand(command="clan_give", description="[超管] 回复加积分"),
    BotCommand(command="clan_take", description="[超管] 回复扣积分"),
    BotCommand(command="clan_maintain", description="[超管] 停机维护"),
    BotCommand(command="clan_compensate", description="[超管] 停机补偿"),
    BotCommand(command="clan_backup_db", description="[超管] 备份数据库"),
    BotCommand(command="clan_restore_db", description="[超管] 恢复数据库"),
    BotCommand(command="clan_group", description="对全服所有玩家发起群攻"),
]


async def _recover_compensation_pins():
    """重启恢复：检查残留的补偿置顶，重启清理协程或立即清除"""
    try:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="compensation_pin:*", count=100)
            for key in keys:
                val = await redis.get(key)
                if not val:
                    continue
                parts = val.split(":")
                msg_id = int(parts[0])
                created_at = int(parts[1]) if len(parts) > 1 else 0
                chat_id_str = key.split(":", 1)[1]
                remaining = 1800 - (time.time() - created_at) if created_at else 0
                if remaining <= 0:
                    try:
                        await bot.unpin_chat_message(chat_id=int(chat_id_str), message_id=msg_id)
                    except Exception:
                        pass
                    try:
                        await bot.delete_message(int(chat_id_str), msg_id)
                    except Exception:
                        pass
                    await redis.delete(key)
                    logger.info(f"[startup] 清理过期补偿置顶 chat={chat_id_str}")
                else:
                    asyncio.create_task(_compensation_cleanup(int(chat_id_str), msg_id, remaining, key))
                    logger.info(f"[startup] 恢复补偿清理 chat={chat_id_str} 剩余{int(remaining)}s")
            if cursor == 0:
                break
    except Exception as e:
        logger.warning(f"[startup] 补偿清理恢复异常: {e}")


async def main():
    logger.info("Bot starting …")
    total_players, fixed_players = await sanitize_all_player_resources()
    logger.info("Resource normalization finished: total=%s fixed=%s", total_players, fixed_players)
    asyncio.create_task(hourly_backup_task())
    asyncio.create_task(auto_collect_task())
    asyncio.create_task(daily_collect_all_task())
    asyncio.create_task(random_bot_attack_task())
    asyncio.create_task(shield_decay_task())
    asyncio.create_task(war_progress_task())
    await _recover_compensation_pins()
    await bot.set_my_commands(COMMANDS)
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    logger.info("Bot commands registered.")

    runner: web.AppRunner | None = None
    configured_mode = (RUN_MODE or "polling").strip().lower()
    if configured_mode not in {"polling", "webhook"}:
        logger.warning("未知 RUN_MODE=%s，已回退到 polling", RUN_MODE)
        configured_mode = "polling"

    effective_mode = configured_mode
    if configured_mode == "webhook" and not WEBHOOK_BASE_URL:
        logger.warning("WEBHOOK_BASE_URL 未配置，已自动回退到 polling 模式")
        effective_mode = "polling"

    webhook_path = WEBHOOK_PATH if WEBHOOK_PATH.startswith("/") else f"/{WEBHOOK_PATH}"

    try:
        if effective_mode == "webhook":
            webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}{webhook_path}"
            await bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET_TOKEN or None,
                drop_pending_updates=True,
            )

            app = web.Application()
            request_handler = SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                secret_token=WEBHOOK_SECRET_TOKEN or None,
            )
            request_handler.register(app, path=webhook_path)
            setup_application(app, dp, bot=bot)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=WEBHOOK_HOST, port=WEBHOOK_PORT)
            await site.start()
            logger.info(
                "Webhook started at %s%s (listen %s:%d)",
                WEBHOOK_BASE_URL.rstrip("/"),
                webhook_path,
                WEBHOOK_HOST,
                WEBHOOK_PORT,
            )
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Bot starting in polling mode ...")
            await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Webhook startup failed, fallback to polling: %s", e)
        effective_mode = "polling"
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        logger.info("Bot running in polling mode")
        await dp.start_polling(bot)
    finally:
        if effective_mode == "webhook":
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    pass
        try:
            await redis.aclose()
        except Exception:
            pass
        if points_redis is not redis:
            try:
                await points_redis.aclose()
            except Exception:
                pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
