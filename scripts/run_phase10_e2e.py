"""One attempt against owned local resources; never starts or removes Docker services."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from uuid import UUID

from sqlalchemy.engine import make_url


WORKTREE = Path(__file__).resolve().parents[1]
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))
EVIDENCE_ROOT = Path(r"D:\E-commerce_operations_env\phase10-evidence")
MODEL_ROOT = Path(r"D:\E-commerce_operations_env\models")
PYTHON = r"D:\E-commerce_operations_env\python.exe"


def build_run_names(run_id: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9a-f]{8}", run_id):
        raise ValueError("PHASE10_RUN_ID_INVALID")
    return f"ecommerce_phase10_{run_id}", f"phase10_{run_id}"


def validate_cleanup_target(database: str, collection: str) -> None:
    suffix = database.removeprefix("ecommerce_phase10_")
    if not re.fullmatch(r"[0-9a-f]{8}", suffix) or (database, collection) != build_run_names(suffix):
        raise ValueError("PHASE10_CLEANUP_SCOPE_INVALID")


def derive_database_urls(source: str, run_id: str) -> tuple[str, str]:
    url = make_url(source)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"} or url.query:
        raise ValueError("PHASE10_DATABASE_URL_INVALID")
    database, _ = build_run_names(run_id)
    return tuple(url.set(drivername=driver, database=database).render_as_string(hide_password=False)
                 for driver in ("postgresql+asyncpg", "postgresql"))


def build_child_environment(run_id: str, *, evidence_root: Path = EVIDENCE_ROOT) -> dict[str, str]:
    database, collection = build_run_names(run_id)
    database_url, graph_url = derive_database_urls(
        os.environ.get("DATABASE_URL", "postgresql+asyncpg://ecommerce:ecommerce@127.0.0.1:5434/ecommerce"), run_id
    )
    # An allowlist prevents inherited API keys, proxies and opt-in flags reaching children.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    temp = evidence_root / run_id / "temp"
    env.update({
        "PYTHONPATH": str(WORKTREE), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        "APP_ENV": "phase10_e2e", "DATABASE_URL": database_url, "LANGGRAPH_DATABASE_URL": graph_url,
        "JWT_SECRET_KEY": f"phase10-synthetic-jwt-{run_id}-local-only", "JWT_ALGORITHM": "HS256", "ACCESS_TOKEN_MINUTES": "60",
        "DEEPSEEK_API_KEY": "phase10-deterministic-key", "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_BASE_URL": "http://127.0.0.1:8765", "DEEPSEEK_TIMEOUT_SECONDS": "45",
        "DEEPSEEK_PRICE_PER_MILLION_TOKENS": "0",
        "PLATFORM_BASE_URL": "http://127.0.0.1:8766", "PLATFORM_CLIENT_ID": "phase10-synthetic-client",
        "PLATFORM_CLIENT_SECRET": "phase10-synthetic-client-secret", "PLATFORM_WEBHOOK_SECRET": "phase10-synthetic-webhook-secret",
        "PLATFORM_TIMEOUT_SECONDS": "10", "PLATFORM_DELIVERY_LEASE_SECONDS": "60",
        "PLATFORM_SIMULATOR_CLIENT_ID": "phase10-synthetic-client", "PLATFORM_SIMULATOR_CLIENT_SECRET": "phase10-synthetic-client-secret",
        "PLATFORM_SIMULATOR_FAULT": "accepted_then_disconnect",
        "ANALYSIS_LEASE_SECONDS": "60", "OPTIMIZATION_LEASE_SECONDS": "60", "KNOWLEDGE_LEASE_SECONDS": "60",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT_SECONDS": "30",
        "KNOWLEDGE_UPLOAD_DIR": str(evidence_root / run_id / "knowledge-uploads"),
        "KNOWLEDGE_EMBEDDING_MODEL_PATH": str(MODEL_ROOT / "bge-m3"),
        "KNOWLEDGE_RERANKER_MODEL_PATH": str(MODEL_ROOT / "bge-reranker-v2-m3"),
        "MILVUS_URI": "http://127.0.0.1:19530", "MILVUS_COLLECTION": collection,
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "NO_PROXY": "127.0.0.1,localhost,::1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HOME": str(evidence_root / run_id / "temp" / "huggingface"),
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_LOGS_MAX": "0", "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_CACHE": str(evidence_root / run_id / "temp" / "npm-cache"),
        "PLAYWRIGHT_BROWSERS_PATH": r"D:\E-commerce_operations_env\playwright-browsers",
        "TEMP": str(evidence_root / run_id / "temp"), "TMP": str(evidence_root / run_id / "temp"),
        # Injected Windows IMEs expand %SystemDrive%\ProgramData themselves.
        "SYSTEMDRIVE": str(temp / "system-drive"), "PROGRAMDATA": str(temp / "system-drive" / "ProgramData"),
        "USERPROFILE": str(temp / "profile"), "APPDATA": str(temp / "profile" / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(temp / "profile" / "AppData" / "Local"),
        "PHASE10_E2E_BASE_URL": "http://127.0.0.1:4174/app/",
        "PHASE10_EVIDENCE_DIR": str(evidence_root / run_id), "PHASE10_RUN_ID": run_id,
    })
    return env


def canonical_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class LocalResources:
    def __init__(self, environment: dict[str, str]):
        self.environment = environment
        self.database = make_url(environment["DATABASE_URL"]).database
        self.collection = environment["MILVUS_COLLECTION"]
        validate_cleanup_target(self.database, self.collection)
        self.index = None
        self.database_created = self.collection_created = False

    async def connect(self, *, maintenance=False):
        import asyncpg
        url = make_url(self.environment["DATABASE_URL"]).set(drivername="postgresql")
        if maintenance:
            url = url.set(database="postgres")
        return await asyncpg.connect(url.render_as_string(hide_password=False), timeout=10, command_timeout=15)

    async def create_database(self):
        connection = await self.connect(maintenance=True)
        try:
            # CREATE fails on collision. The caller marks ownership only after success.
            await connection.execute(f'CREATE DATABASE "{self.database}"')
            self.database_created = True
        finally:
            await connection.close(timeout=5)

    async def drop_database(self):
        validate_cleanup_target(self.database, self.collection)
        connection = await self.connect(maintenance=True)
        try:
            await connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()", self.database)
            await connection.execute(f'DROP DATABASE "{self.database}"')
        finally:
            await connection.close(timeout=5)

    async def setup_checkpoints(self):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        # Finish migrations once before workers can race on migration-version inserts.
        async with asyncio.timeout(60):
            async with AsyncPostgresSaver.from_conn_string(self.environment["LANGGRAPH_DATABASE_URL"]) as saver:
                await saver.setup()

    async def create_collection(self):
        from backend.knowledge_index import MilvusKnowledgeIndex
        from pymilvus import DataType
        self.index = MilvusKnowledgeIndex(uri=self.environment["MILVUS_URI"], collection=self.collection, timeout_seconds=10)
        client = self.index._client
        if await asyncio.to_thread(client.has_collection, collection_name=self.collection, timeout=10):
            raise RuntimeError("PHASE10_COLLECTION_ALREADY_EXISTS")
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        for name in ("chunk_id", "document_id", "version_id", "category"):
            schema.add_field(field_name=name, datatype=DataType.VARCHAR, is_primary=name == "chunk_id", max_length=256 if name == "category" else 36)
        schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
        indexes.add_index(field_name="sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
        await asyncio.to_thread(client.create_collection, collection_name=self.collection, schema=schema, timeout=10)
        self.collection_created = True
        await asyncio.to_thread(client.create_index, collection_name=self.collection, index_params=indexes, timeout=10)
        await asyncio.to_thread(client.load_collection, collection_name=self.collection, timeout=10)

    async def drop_collection(self):
        validate_cleanup_target(self.database, self.collection)
        await asyncio.to_thread(self.index._client.drop_collection, collection_name=self.collection, timeout=10)

    async def snapshot(self):
        connection = await self.connect()
        try:
            async with connection.transaction(readonly=True, isolation="repeatable_read"):
                skus = [dict(row) for row in await connection.fetch("SELECT * FROM product_skus ORDER BY id")]
                inventory = [dict(row) for row in await connection.fetch("SELECT * FROM inventory_snapshots ORDER BY id")]
                products = {row["id"]: row["current_version"] for row in await connection.fetch("SELECT id,current_version FROM products")}
                return {"skus": skus, "inventory": inventory, "products": products}
        finally:
            await connection.close(timeout=5)

    async def verify(self, evidence: Path, baseline: dict):
        proof = json.loads((evidence / "browser-proof.json").read_text(encoding="utf-8"))
        require(set(proof) == {"success", "rejection", "manual_failures"})
        for kind, fields in {"success": {"proposal_id", "product_id", "publish_record_id"}, "rejection": {"proposal_id", "product_id"}, "manual_failures": {"proposal_id", "product_id", "revision_ids", "first_revision_sha256"}}.items():
            require(set(proof[kind]) == fields)
            for key in fields & {"proposal_id", "product_id", "publish_record_id"}:
                require(str(UUID(proof[kind][key])) == proof[kind][key])
        after = await self.snapshot()
        require(after["skus"] == baseline["skus"] and after["inventory"] == baseline["inventory"])
        expected = dict(baseline["products"])
        expected[proof["success"]["product_id"]] += 1
        require(after["products"] == expected)
        connection = await self.connect()
        try:
            async with connection.transaction(readonly=True):
                for case in proof.values():
                    require(await connection.fetchval("SELECT product_id FROM product_proposals WHERE id=$1", case["proposal_id"]) == case["product_id"])
                records = await connection.fetch("SELECT id,proposal_id,product_id FROM publish_records")
                require(len(records) == 1 and dict(records[0]) == {"id": proof["success"]["publish_record_id"], "proposal_id": proof["success"]["proposal_id"], "product_id": proof["success"]["product_id"]})
                deliveries = await connection.fetch("SELECT id,status,attempt_count,external_operation_id FROM platform_deliveries WHERE publish_record_id=$1", records[0]["id"])
                require(len(deliveries) == 1 and deliveries[0]["status"] == "succeeded" and deliveries[0]["attempt_count"] == 2)
                failed = proof["manual_failures"]
                ids = failed["revision_ids"]
                require(isinstance(ids, list) and len(ids) == len(set(ids)) == 2)
                rows = await connection.fetch("SELECT r.id,r.parent_revision_id,r.revision_number,r.proposal_output,c.passed FROM proposal_revisions r JOIN compliance_reviews c ON c.proposal_revision_id=r.id WHERE r.proposal_id=$1 AND r.origin='manual' ORDER BY r.revision_number", failed["proposal_id"])
                require([row["id"] for row in rows] == ids and all(row["passed"] is False for row in rows))
                require(rows[1]["parent_revision_id"] == rows[0]["id"] and rows[1]["revision_number"] == rows[0]["revision_number"] + 1)
                require(canonical_digest(json.loads(rows[0]["proposal_output"])) == failed["first_revision_sha256"])
                require(await connection.fetchval("SELECT count(*) FROM approval_actions WHERE action='approve' AND proposal_id=ANY($1::varchar[])", [failed["proposal_id"], proof["rejection"]["proposal_id"]]) == 0)
                require(await connection.fetchval("SELECT count(*) FROM approval_actions WHERE action='reject' AND proposal_id=$1", proof["rejection"]["proposal_id"]) == 1)
            self.delivery = dict(deliveries[0])
        finally:
            await connection.close(timeout=5)
        (evidence / "database-proof.json").write_text(json.dumps({"sku_price_stock_unchanged": True, "inventory_unchanged": True, "product_versions_verified": True, "manual_revisions_immutable": True, "one_publish_one_delivery": True}), encoding="utf-8")


def require(condition: bool):
    if not condition:
        raise RuntimeError("PHASE10_EVIDENCE_INVALID")


class Runner:
    def __init__(self, run_id: str, *, evidence_root: Path = EVIDENCE_ROOT, resources=None):
        self.run_id = run_id
        self.database, self.collection = build_run_names(run_id)
        self.worktree = WORKTREE
        self.evidence = evidence_root / run_id
        self.environment = build_child_environment(run_id, evidence_root=evidence_root)
        self.resources = resources or LocalResources(self.environment)
        self.popen = subprocess.Popen
        self.processes = []
        self.child_environments = []
        self.database_created = self.collection_created = False
        self.stage = "preflight"

    def record(self, status: str):
        with (self.evidence / "lifecycle.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stage": self.stage, "status": status}) + "\n")

    def preflight(self):
        require(Path(PYTHON).is_file() and (MODEL_ROOT / "bge-m3").is_dir() and (MODEL_ROOT / "bge-reranker-v2-m3").is_dir())
        require(shutil.which("npm.cmd" if os.name == "nt" else "npm") is not None)
        for port in (8765, 8766, 4174):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))

    def start(self, name: str, command: list[str], *, extra_env=None):
        environment = {**self.environment, **(extra_env or {})}
        process = self.popen(command, cwd=self.worktree, env=environment,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.processes.append((name, process))
        self.child_environments.append(environment)
        return process

    async def command(self, name: str, command: list[str], *, timeout=180, extra_env=None):
        self.stage = name
        process = self.start(name, command, extra_env=extra_env)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError("PHASE10_PROCESS_TIMEOUT")
            await asyncio.sleep(0.2)
        require(process.poll() == 0)
        self.record("passed")

    async def health(self):
        import httpx
        pending = {8765, 8766, 4174}
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            while pending and time.monotonic() < deadline:
                require(all(process.poll() is None for name, process in self.processes if name in {"model", "platform", "api"} or name.endswith("worker")))
                for port in list(pending):
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/health/live")
                        if response.status_code == 200 and response.json() == {"status": "ok"}:
                            pending.remove(port)
                    except (httpx.HTTPError, ValueError):
                        pass
                if pending:
                    await asyncio.sleep(0.25)
        require(not pending)

    async def stop(self, process):
        if process.poll() is None:
            if os.name == "nt" and any(child is process and name in {"build", "playwright"} for name, child in self.processes):
                # npm launches Node/Chromium descendants; scope native tree cleanup to this live parent.
                result = await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                 creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
                require(result.returncode == 0 or process.poll() is not None)
                await asyncio.to_thread(process.wait, timeout=5)
                return
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)

    async def fault_checks(self):
        await self.webhook_checks()
        self.stage = "worker_recovery"
        await self.worker_recovery_check()
        self.stage = "faults"
        await self.milvus_unavailable_check()

    def fault_evidence(self, name: str, value: dict):
        path = self.evidence / "fault-proof.json"
        evidence = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        evidence[name] = value
        path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")

    async def webhook_checks(self):
        import httpx
        delivery = self.resources.delivery
        body = json.dumps({"event_type": "publish.confirmed", "delivery_id": delivery["id"], "external_operation_id": delivery["external_operation_id"]}, separators=(",", ":")).encode()
        event_id = "phase10_" + self.run_id
        timestamp = str(int(time.time()))

        def headers(stamp, event=event_id):
            signature = hmac.new(self.environment["PLATFORM_WEBHOOK_SECRET"].encode(), stamp.encode() + b"." + event.encode() + b"." + body, hashlib.sha256).hexdigest()
            return {"Content-Type": "application/json", "X-Webhook-Timestamp": stamp, "X-Event-ID": event, "X-Webhook-Signature": signature}

        connection = await self.resources.connect()
        try:
            async def counts():
                return tuple(await connection.fetchrow("SELECT (SELECT count(*) FROM platform_webhook_receipts), (SELECT count(*) FROM audit_events WHERE event_type='platform_webhook_received')"))

            before = await counts()
            async with httpx.AsyncClient(base_url="http://127.0.0.1:4174", timeout=10, trust_env=False) as client:
                invalid_headers = [
                    {**headers(timestamp), "X-Webhook-Signature": "0" * 64},
                    headers(str(int(timestamp) - 301)),
                    {**headers(timestamp), "X-Event-ID": event_id + "_altered"},
                ]
                for envelope in invalid_headers:
                    result = await client.post("/integrations/platform/webhooks", content=body, headers=envelope)
                    require(result.status_code == 401 and await counts() == before)
                first = await client.post("/integrations/platform/webhooks", content=body, headers=headers(timestamp))
                second = await client.post("/integrations/platform/webhooks", content=body, headers=headers(timestamp))
                require(first.status_code == 201 and first.json() == {"created": True})
                require(second.status_code == 200 and second.json() == {"created": False})
                require(await counts() == (before[0] + 1, before[1] + 1))
                simulator = await client.get("http://127.0.0.1:8766/__phase10/evidence")
                require(simulator.status_code == 200 and simulator.json() == {"mutation_count": 1, "operation_count": 1, "disconnect_used": True})
            self.fault_evidence("webhooks", {"invalid_signature_zero_writes": True, "stale_timestamp_zero_writes": True, "altered_event_zero_writes": True, "duplicate_one_receipt_one_audit": True})
            self.fault_evidence("platform", {"mutation_count": 1, "operation_count": 1, "disconnect_used": True})
        finally:
            await connection.close(timeout=5)

    async def worker_recovery_check(self):
        import httpx
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from backend.analysis_runs import finalize_analysis_run
        from backend.common import WorkflowQuality

        for name, process in self.processes:
            if name == "analysis_worker":
                await self.stop(process)
        arrived, release = threading.Event(), threading.Event()

        class BlockModel(BaseHTTPRequestHandler):
            def do_POST(self):
                # Do not read, retain or log the synthetic prompt or its headers.
                arrived.set()
                release.wait(65)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), BlockModel)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = None
        try:
            connection = await self.resources.connect()
            store_id = await connection.fetchval("SELECT id FROM stores WHERE code=$1", "flagship")
            require(store_id is not None)
            async with httpx.AsyncClient(base_url="http://127.0.0.1:4174", timeout=10, trust_env=False) as client:
                login = await client.post("/auth/login", json={"username": "operator", "password": "DemoPass!2026"})
                require(login.status_code == 200)
                accepted = await client.post("/analysis-runs", headers={"Authorization": "Bearer " + login.json()["access_token"]}, json={"store_id": store_id, "start_date": "2026-07-26", "end_date": "2026-08-24"})
                require(accepted.status_code == 202)
                run_id = accepted.json()["workflow_run_id"]
            crashed = self.start("fault_analysis_worker", [PYTHON, "-m", "scripts.run_analysis_worker", "--once"], extra_env={"DEEPSEEK_BASE_URL": f"http://127.0.0.1:{server.server_port}", "ANALYSIS_LEASE_SECONDS": "5"})
            deadline = time.monotonic() + 45
            while not arrived.is_set() and time.monotonic() < deadline:
                require(crashed.poll() is None)
                await asyncio.sleep(0.1)
            require(arrived.is_set())
            claimed = await connection.fetchrow("SELECT status,attempt_count,lease_owner FROM workflow_runs WHERE id=$1", run_id)
            require(claimed["status"] == "processing" and claimed["attempt_count"] == 1)
            old_owner = claimed["lease_owner"]
            await self.stop(crashed)
            recovered = self.start("recovery_analysis_worker", [PYTHON, "-m", "scripts.run_analysis_worker"])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                require(recovered.poll() is None)
                final = await connection.fetchrow("SELECT status,attempt_count,lease_owner,error_code FROM workflow_runs WHERE id=$1", run_id)
                if final["status"] not in {"accepted", "processing"}:
                    break
                await asyncio.sleep(0.25)
            require(final["status"] == "awaiting_selection" and final["attempt_count"] == 2 and final["lease_owner"] is None and final["error_code"] is None)
            engine = create_async_engine(self.environment["DATABASE_URL"])
            try:
                async with async_sessionmaker(engine)() as session:
                    stale_commit = await finalize_analysis_run(session, workflow_run_id=run_id, lease_owner=old_owner, candidate_count=0, quality_status=WorkflowQuality.NORMAL, quality={})
                    require(stale_commit is False)
            finally:
                await engine.dispose()
            self.fault_evidence("worker_recovery", {"workflow_run_id": run_id, "status": "awaiting_selection", "attempt_count": 2, "interrupted_during_model_request": True, "old_owner_commit_rejected": True})
        finally:
            release.set()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
            if connection is not None:
                await connection.close(timeout=5)

    async def milvus_unavailable_check(self):
        # A reserved, non-listening loopback socket reliably refuses connections.
        with socket.socket() as refusal:
            refusal.bind(("127.0.0.1", 0))
            port = refusal.getsockname()[1]
            await self.command("milvus_probe", [PYTHON, "-m", "scripts.run_phase10_e2e", "--milvus-probe"], timeout=30,
                               extra_env={"MILVUS_URI": f"http://127.0.0.1:{port}"})
        proof = json.loads((self.evidence / "milvus-proof.json").read_text(encoding="utf-8"))
        require(set(proof) == {"status", "error_code", "retryable", "boundary"})
        require(proof["status"] == "failed" and proof["retryable"] is True and proof["boundary"] == "dependency_startup")
        require(proof["error_code"] in {"KNOWLEDGE_MILVUS_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_TIMEOUT"})
        self.fault_evidence("milvus", proof)

    async def run(self) -> int:
        # Never adopt an old evidence directory, even with a syntactically valid run ID.
        self.evidence.mkdir(parents=True, exist_ok=False)
        (self.evidence / "temp").mkdir()
        for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
            Path(self.environment[key]).mkdir(parents=True, exist_ok=True)
        failed = False
        try:
            self.preflight()
            self.stage = "create_database"
            await self.resources.create_database()
            self.database_created = True
            self.stage = "create_collection"
            await self.resources.create_collection()
            self.collection_created = True
            await self.command("migrate", [PYTHON, "-m", "alembic", "upgrade", "head"])
            await self.command("seed", [PYTHON, "-m", "scripts.seed_demo"])
            self.stage = "checkpoint_setup"
            await self.resources.setup_checkpoints()
            self.record("passed")
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
            await self.command("build", [npm, "--prefix", "frontend", "run", "build"])
            baseline = await self.resources.snapshot()
            for name, module, port, factory in (
                ("model", "tests.support.deterministic_deepseek:create_deterministic_deepseek", 8765, True),
                ("platform", "tests.support.platform_simulator:app", 8766, False),
                ("api", "backend.main:create_app", 4174, True),
            ):
                self.start(name, [PYTHON, "-m", "uvicorn", module, *(["--factory"] if factory else []), "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "critical"])
            for worker in ("analysis", "knowledge", "optimization", "manual_review", "platform_delivery"):
                extra_env = {"RUN_PHASE10_PLATFORM_DELIVERY": "1"} if worker == "platform_delivery" else {}
                if worker == "knowledge":
                    extra_env["CUDA_VISIBLE_DEVICES"] = "-1"
                self.start(worker + "_worker", [PYTHON, "-m", "scripts.run_" + worker + "_worker"], extra_env=extra_env)
            self.stage = "health"
            await self.health()
            await self.command("playwright", [npm, "--prefix", "frontend", "run", "test:e2e", "--", "--config", "playwright.real.config.ts"], timeout=900)
            self.stage = "database_evidence"
            await self.resources.verify(self.evidence, baseline)
            self.stage = "faults"
            await self.fault_checks()
            self.record("passed")
        except (Exception, KeyboardInterrupt):
            failed = True
            self.record("failed")
        finally:
            self.stage = "cleanup"
            for _, process in reversed(self.processes):
                try:
                    await self.stop(process)
                except Exception:
                    failed = True
                    self.record("process_cleanup_failed")
            for created, drop in ((self.collection_created or getattr(self.resources, "collection_created", False), self.resources.drop_collection), (self.database_created or getattr(self.resources, "database_created", False), self.resources.drop_database)):
                if created:
                    try:
                        validate_cleanup_target(self.database, self.collection)
                        await drop()
                    except Exception:
                        failed = True
                        self.record("resource_cleanup_failed")
            self.record("failed" if failed else "passed")
        return int(failed)


async def milvus_probe():
    from backend.knowledge_index import KnowledgeDependencyError, MilvusKnowledgeIndex
    run_id = os.environ["PHASE10_RUN_ID"]
    _, collection = build_run_names(run_id)
    evidence = EVIDENCE_ROOT / run_id
    require(Path(os.environ["PHASE10_EVIDENCE_DIR"]) == evidence and evidence.is_dir())
    from urllib.parse import urlsplit
    target = urlsplit(os.environ["MILVUS_URI"])
    require(target.scheme == "http" and target.hostname == "127.0.0.1" and target.username is None and not target.query and not target.fragment)
    try:
        index = MilvusKnowledgeIndex(uri=os.environ["MILVUS_URI"], collection=collection, timeout_seconds=2)
        await index.ensure_collection()
    except KnowledgeDependencyError as error:
        require(error.code in {"KNOWLEDGE_MILVUS_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_TIMEOUT"} and error.retryable)
        (evidence / "milvus-proof.json").write_text(json.dumps({"status": "failed", "error_code": error.code, "retryable": True, "boundary": "dependency_startup"}), encoding="utf-8")
        return 0
    return 1


def main():
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    run_id = None
    try:
        require(sys.argv[1:] in ([], ["--milvus-probe"]))
        run_id = os.environ["PHASE10_RUN_ID"] if sys.argv[1:] else secrets.token_hex(4)
        build_run_names(run_id)
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = asyncio.run(milvus_probe() if sys.argv[1:] else Runner(run_id).run())
    except (Exception, KeyboardInterrupt):
        result = 1
    status = "PHASE10_E2E_PASSED" if result == 0 else "PHASE10_E2E_FAILED"
    print(status + (f" run_id={run_id}" if run_id and re.fullmatch(r"[0-9a-f]{8}", run_id) else ""))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
