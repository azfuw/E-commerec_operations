import argparse
import asyncio
from functools import partial
import os
import socket


async def main(once: bool) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.config import get_settings
    from backend.database import async_session_factory
    from backend.optimization_trusted_input import load_optimization_trusted_input
    from backend.optimization_worker import run_once

    settings = get_settings()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    trusted_input_loader = partial(
        load_optimization_trusted_input,
        session_factory=async_session_factory,
        settings=settings,
    )
    async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as checkpointer:
        await checkpointer.setup()
        while True:
            processed = await run_once(
                async_session_factory,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
                trusted_input_loader=trusted_input_loader,
            )
            if once:
                return
            if processed is None:
                await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(arguments.once))
