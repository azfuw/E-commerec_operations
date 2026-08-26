import argparse
import asyncio
import os
import socket


async def main(once: bool) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.analysis_worker import run_once
    from backend.config import get_settings
    from backend.database import async_session_factory

    settings = get_settings()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as checkpointer:
        await checkpointer.setup()
        while True:
            processed = await run_once(
                async_session_factory,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
            )
            if once:
                return
            if processed is None:
                await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.once))
