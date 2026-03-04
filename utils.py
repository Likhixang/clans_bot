import asyncio
import html

from aiogram import types
from aiogram.methods import PinChatMessage

from core import bot
from config import ALLOWED_THREAD_ID


def safe_html(text: str) -> str:
    return html.escape(str(text))


def mention(user_id, name):
    return f"<a href='tg://user?id={user_id}'>{safe_html(name)}</a>"


def thread_id():
    return ALLOWED_THREAD_ID or None


async def auto_delete(msgs: list, delay: int = 15):
    if delay > 0:
        await asyncio.sleep(delay)
    for m in msgs:
        try:
            await m.delete()
        except Exception:
            pass


async def send(chat_id: int, text: str, reply_markup=None, delay_delete: int = 0):
    msg = await bot.send_message(
        chat_id, text,
        reply_markup=reply_markup,
        message_thread_id=thread_id(),
    )
    if delay_delete > 0:
        asyncio.create_task(auto_delete([msg], delay_delete))
    return msg


def fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


async def pin_in_topic(chat_id: int, message_id: int, disable_notification: bool = False):
    """pin_chat_message 的话题感知包装"""
    kwargs = {"chat_id": chat_id, "message_id": message_id, "disable_notification": disable_notification}
    if ALLOWED_THREAD_ID:
        kwargs["message_thread_id"] = ALLOWED_THREAD_ID
    await bot(PinChatMessage(**kwargs))


async def delete_msg_by_id(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
