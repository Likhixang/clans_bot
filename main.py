import asyncio
import logging
import time

from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from core import bot, dp, redis
from handlers import router, _compensation_cleanup
from tasks import hourly_backup_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

dp.include_router(router)

COMMANDS = [
    BotCommand(command="clan_start", description="注册 / 进入游戏"),
    BotCommand(command="clan_me", description="查看村庄面板"),
    BotCommand(command="clan_collect", description="收集资源"),
    BotCommand(command="clan_shop", description="建筑商店"),
    BotCommand(command="clan_build", description="建造新建筑"),
    BotCommand(command="clan_upgrade", description="升级建筑"),
    BotCommand(command="clan_troops", description="兵种列表"),
    BotCommand(command="clan_train", description="训练部队"),
    BotCommand(command="clan_army", description="查看当前部队"),
    BotCommand(command="clan_attack", description="攻击其他玩家"),
    BotCommand(command="clan_log", description="战绩记录"),
    BotCommand(command="clan_rank", description="奖杯排行榜"),
    BotCommand(command="clan_create", description="创建部落"),
    BotCommand(command="clan_info", description="查看部落信息"),
    BotCommand(command="clan_list", description="部落列表"),
    BotCommand(command="clan_join", description="加入部落"),
    BotCommand(command="clan_leave", description="离开部落"),
    BotCommand(command="clan_help", description="帮助"),
    BotCommand(command="clan_give", description="[超管] 发放资源"),
    BotCommand(command="clan_take", description="[超管] 扣除资源"),
    BotCommand(command="clan_maintain", description="[超管] 停机维护"),
    BotCommand(command="clan_compensate", description="[超管] 停机补偿"),
    BotCommand(command="clan_backup_db", description="[超管] 备份数据库"),
    BotCommand(command="clan_restore_db", description="[超管] 恢复数据库"),
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
    asyncio.create_task(hourly_backup_task())
    await _recover_compensation_pins()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(COMMANDS)
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    logger.info("Bot commands registered.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
