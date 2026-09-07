import argparse
import asyncio
import os
import socket


def _secret_present(value) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


def _validate_startup(settings) -> None:
    if (
        os.getenv("RUN_PHASE10_PLATFORM_DELIVERY") != "1"
        or not settings.platform_base_url
        or not settings.platform_base_url.strip()
        or not _secret_present(settings.platform_client_id)
        or not _secret_present(settings.platform_client_secret)
        or not _secret_present(settings.platform_webhook_secret)
    ):
        raise SystemExit("platform delivery is disabled or incomplete")


async def main(once: bool) -> None:
    from backend.config import Settings
    from backend.database import async_session_factory
    from backend.platform_client import CommercePlatformClient
    from backend.platform_delivery_worker import run_once

    settings = Settings()
    _validate_startup(settings)
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    client = CommercePlatformClient(settings)
    try:
        while True:
            processed = await run_once(
                async_session_factory,
                settings=settings,
                lease_owner=lease_owner,
                client=client,
            )
            if once:
                return
            if processed is None:
                await asyncio.sleep(1)
    finally:
        await client.aclose()


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args(argv)
    failed = False
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main(arguments.once))
    except SystemExit:
        raise
    except Exception:
        failed = True
    if failed:
        raise SystemExit("PLATFORM_DELIVERY_FAILED")


if __name__ == "__main__":
    cli()
