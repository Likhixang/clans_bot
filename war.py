import json
import time
import uuid

from core import redis


def _war_key(war_id: str) -> str:
    return f"coc:war:{war_id}"


def _war_roster_key(war_id: str, clan_id: str) -> str:
    return f"coc:war:roster:{war_id}:{clan_id}"


def _war_used_key(war_id: str) -> str:
    return f"coc:war:used:{war_id}"


def _war_best_key(war_id: str) -> str:
    return f"coc:war:best:{war_id}"


def _war_log_key(war_id: str) -> str:
    return f"coc:war:log:{war_id}"


def _war_active_key(clan_id: str) -> str:
    return f"coc:war:active:{clan_id}"


WAR_ALL_ACTIVE_KEY = "coc:war:all_active"


def _parse_war(data: dict) -> dict:
    return {
        "id": data.get("id", ""),
        "clan_a": data.get("clan_a", ""),
        "clan_b": data.get("clan_b", ""),
        "state": data.get("state", "prep"),
        "created_at": float(data.get("created_at", 0) or 0),
        "prep_until": float(data.get("prep_until", 0) or 0),
        "battle_until": float(data.get("battle_until", 0) or 0),
        "ended_at": float(data.get("ended_at", 0) or 0),
        "max_members": int(data.get("max_members", 0) or 0),
        "attacks_per_member": int(data.get("attacks_per_member", 0) or 0),
        "min_members": int(data.get("min_members", 0) or 0),
        "chat_id": int(data.get("chat_id", 0) or 0),
        "pin_message_id": int(data.get("pin_message_id", 0) or 0),
        "pin_phase": data.get("pin_phase", ""),
        "winner_clan": data.get("winner_clan", ""),
        "result_summary": data.get("result_summary", ""),
    }


async def get_war(war_id: str) -> dict | None:
    data = await redis.hgetall(_war_key(war_id))
    if not data:
        return None
    return _parse_war(data)


async def get_active_war_id(clan_id: str) -> str:
    return await redis.get(_war_active_key(clan_id)) or ""


async def list_active_war_ids() -> list[str]:
    return list(await redis.smembers(WAR_ALL_ACTIVE_KEY))


async def create_war(
    clan_a: str,
    clan_b: str,
    *,
    prep_seconds: int,
    max_members: int,
    attacks_per_member: int,
    min_members: int,
    chat_id: int,
) -> str:
    war_id = str(uuid.uuid4())[:10]
    now = time.time()
    prep_until = now + prep_seconds
    await redis.hset(
        _war_key(war_id),
        mapping={
            "id": war_id,
            "clan_a": clan_a,
            "clan_b": clan_b,
            "state": "prep",
            "created_at": str(now),
            "prep_until": str(prep_until),
            "battle_until": "0",
            "ended_at": "0",
            "max_members": str(max_members),
            "attacks_per_member": str(attacks_per_member),
            "min_members": str(min_members),
            "chat_id": str(chat_id),
            "pin_message_id": "0",
            "pin_phase": "",
            "winner_clan": "",
            "result_summary": "",
        },
    )
    await redis.set(_war_active_key(clan_a), war_id)
    await redis.set(_war_active_key(clan_b), war_id)
    await redis.sadd(WAR_ALL_ACTIVE_KEY, war_id)
    return war_id


async def set_war_phase(war_id: str, state: str, *, prep_until: float | None = None, battle_until: float | None = None) -> None:
    mapping = {"state": state}
    if prep_until is not None:
        mapping["prep_until"] = str(prep_until)
    if battle_until is not None:
        mapping["battle_until"] = str(battle_until)
    await redis.hset(_war_key(war_id), mapping=mapping)


async def set_war_pin(war_id: str, message_id: int, phase: str) -> None:
    await redis.hset(_war_key(war_id), mapping={"pin_message_id": str(message_id), "pin_phase": phase})


async def clear_war_pin(war_id: str) -> None:
    await redis.hset(_war_key(war_id), mapping={"pin_message_id": "0", "pin_phase": ""})


async def add_war_roster_member(war_id: str, clan_id: str, uid: str) -> None:
    await redis.sadd(_war_roster_key(war_id, clan_id), uid)


async def remove_war_roster_member(war_id: str, clan_id: str, uid: str) -> None:
    await redis.srem(_war_roster_key(war_id, clan_id), uid)


async def get_war_roster(war_id: str, clan_id: str) -> list[str]:
    return list(await redis.smembers(_war_roster_key(war_id, clan_id)))


async def get_war_used_attacks(war_id: str, uid: str) -> int:
    return int(await redis.hget(_war_used_key(war_id), uid) or 0)


async def incr_war_used_attacks(war_id: str, uid: str) -> int:
    return int(await redis.hincrby(_war_used_key(war_id), uid, 1))


async def append_war_attack_log(war_id: str, record: dict) -> None:
    await redis.lpush(_war_log_key(war_id), json.dumps(record, ensure_ascii=False))
    await redis.ltrim(_war_log_key(war_id), 0, 199)


async def get_war_attack_logs(war_id: str, limit: int = 40) -> list[dict]:
    rows = await redis.lrange(_war_log_key(war_id), 0, max(0, limit - 1))
    out = []
    for row in rows:
        try:
            out.append(json.loads(row))
        except Exception:
            continue
    return out


async def get_war_best_for_target(war_id: str, target_uid: str) -> dict | None:
    raw = await redis.hget(_war_best_key(war_id), target_uid)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def upsert_war_best_for_target(
    war_id: str,
    target_uid: str,
    *,
    stars: int,
    destruction: float,
    attacker_uid: str,
    ts: float,
) -> bool:
    cur = await get_war_best_for_target(war_id, target_uid)
    if cur:
        cur_stars = int(cur.get("stars", 0))
        cur_dest = float(cur.get("destruction", 0))
        if stars < cur_stars:
            return False
        if stars == cur_stars and destruction <= cur_dest:
            return False
    record = {
        "stars": int(stars),
        "destruction": round(float(destruction), 2),
        "attacker_uid": str(attacker_uid),
        "time": float(ts),
    }
    await redis.hset(_war_best_key(war_id), target_uid, json.dumps(record, ensure_ascii=False))
    return True


async def calc_war_score(war_id: str, defender_uids: list[str]) -> tuple[int, float]:
    stars = 0
    destruction = 0.0
    for uid in defender_uids:
        rec = await get_war_best_for_target(war_id, uid)
        if not rec:
            continue
        stars += int(rec.get("stars", 0))
        destruction += float(rec.get("destruction", 0))
    return stars, round(destruction, 2)


async def finish_war(war: dict, winner_clan: str, summary: str) -> None:
    war_id = war["id"]
    await redis.hset(
        _war_key(war_id),
        mapping={
            "state": "ended",
            "ended_at": str(time.time()),
            "winner_clan": winner_clan,
            "result_summary": summary,
        },
    )
    await redis.delete(_war_active_key(war["clan_a"]))
    await redis.delete(_war_active_key(war["clan_b"]))
    await redis.srem(WAR_ALL_ACTIVE_KEY, war_id)

