import asyncio
import logging

from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from core import bot, dp
from handlers import router
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
    BotCommand(command="clan_backup_db", description="[超管] 备份数据库"),
    BotCommand(command="clan_restore_db", description="[超管] 恢复数据库"),
]


async def main():
    logger.info("Bot starting …")
    asyncio.create_task(hourly_backup_task())
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(COMMANDS)
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    logger.info("Bot commands registered.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
