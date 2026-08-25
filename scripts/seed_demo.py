import argparse
import asyncio

from backend.database import async_session_factory
from backend.seed import clear_demo_data, seed_demo_data


async def run(seed: int, reset: bool) -> None:
    async with async_session_factory() as session:
        if reset:
            await clear_demo_data(session)
        summary = await seed_demo_data(session, seed=seed)
        await session.commit()
    print(
        f"Seeded stores={summary.stores} products={summary.products} "
        f"days={summary.days} anomalies={','.join(summary.anomaly_types)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed deterministic local demo data.")
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.seed, args.reset))


if __name__ == "__main__":
    main()
