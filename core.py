import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from redis.asyncio import Redis

from config import (
    TOKEN,
    POINTS_REDIS_HOST,
    POINTS_REDIS_PORT,
    POINTS_REDIS_DB,
    POINTS_REDIS_PASSWORD,
)

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode='HTML'),
    session=AiohttpSession(timeout=12),
)
dp = Dispatcher()
redis = Redis(host='redis', port=6379, db=0, decode_responses=True, password=os.getenv('REDIS_PASSWORD'))

if POINTS_REDIS_HOST:
    points_redis = Redis(
        host=POINTS_REDIS_HOST,
        port=POINTS_REDIS_PORT,
        db=POINTS_REDIS_DB,
        decode_responses=True,
        password=POINTS_REDIS_PASSWORD or None,
    )
else:
    points_redis = redis
