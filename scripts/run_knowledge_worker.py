import argparse
import asyncio
import os
import socket


async def main(once: bool) -> None:
    from backend.config import get_settings
    from backend.database import async_session_factory
    from backend.knowledge_index import LocalKnowledgeModels, MilvusKnowledgeIndex
    from backend.knowledge_worker import run_once

    settings = get_settings()
    models = LocalKnowledgeModels(
        embedding_model_path=settings.knowledge_embedding_model_path,
        reranker_model_path=settings.knowledge_reranker_model_path,
        timeout_seconds=settings.knowledge_dependency_timeout_seconds,
    )
    index = MilvusKnowledgeIndex(
        uri=settings.milvus_uri,
        collection=settings.milvus_collection,
        timeout_seconds=settings.knowledge_dependency_timeout_seconds,
    )
    await index.ensure_collection()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    while True:
        processed = await run_once(
            async_session_factory,
            settings=settings,
            lease_owner=lease_owner,
            models=models,
            index=index,
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
