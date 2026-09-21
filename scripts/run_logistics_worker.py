"""Periodic local rule patrol under an explicitly selected user's store scope."""
from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger("logistics.worker")


async def patrol_once(session, username: str) -> dict:
    from sqlalchemy import select
    from backend.auth import require_department
    from backend.common import UserDepartment, UserStatus
    from backend.models import User
    from backend.logistics import patrol, run_json, scope_ids, shipment_rows

    actor = await session.scalar(select(User).where(User.username == username).execution_options(populate_existing=True))
    if actor is None or actor.status != UserStatus.ACTIVE:
        raise ValueError("Logistics worker requires an active user")
    require_department(actor, UserDepartment.LOGISTICS)
    ids = await scope_ids(session, actor, None)
    rows = await shipment_rows(session, actor)
    return run_json(await patrol(session, actor, rows, ids))


async def run_forever(username: str, interval: float = 300) -> None:
    from fastapi import HTTPException
    from backend.database import async_session_factory

    while True:
        try:
            async with async_session_factory() as session:
                result = await patrol_once(session, username)
            logger.info("Patrol completed: %s scanned, %s new tasks", result["scanned_shipments"], result["created_exceptions"])
        except ValueError:
            logger.error("Patrol paused: configured user is missing or disabled")
        except HTTPException:
            logger.error("Patrol paused: configured user no longer has logistics access")
        except Exception:
            # Avoid writing connection strings or raw business payloads to logs.
            logger.error("Patrol failed; the next interval will retry with a fresh session")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在指定用户的店铺权限内定时运行物流巡检")
    parser.add_argument("--username", required=True)
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("interval must be at least 30 seconds")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever(args.username, args.interval))
