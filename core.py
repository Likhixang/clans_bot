import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from redis.asyncio import Redis

from config import TOKEN

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()
redis = Redis(host='redis', port=6379, db=0, decode_responses=True, password=os.getenv('REDIS_PASSWORD'))
