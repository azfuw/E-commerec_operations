import os
import subprocess
from pathlib import Path

import pytest

from scripts.run_phase10_e2e import (
    LocalResources, Runner, build_child_environment, build_run_names, derive_database_urls,
    validate_cleanup_target,
)


def test_names_and_url_derivation():
    assert build_run_names("a1b2c3d4") == ("ecommerce_phase10_a1b2c3d4", "phase10_a1b2c3d4")
    for value in ("", "A1b2c3d4", "a1b2c3d4_extra", "../a1b2c3d4"):
        with pytest.raises(ValueError):
            build_run_names(value)
    with pytest.raises(ValueError, match="PHASE10_CLEANUP_SCOPE_INVALID"):
        validate_cleanup_target("ecommerce", "knowledge_chunks")
    with pytest.raises(ValueError):
        validate_cleanup_target("ecommerce_phase10_a1b2c3d4", "phase10_12345678")
    urls = derive_database_urls("postgresql+asyncpg://user:p%40ss@127.0.0.1:5434/ecommerce", "a1b2c3d4")
    assert all("p%40ss@" in url and url.endswith("/ecommerce_phase10_a1b2c3d4") for url in urls)
    for url in ("postgresql://u:p@example.com/db", "postgresql://u:p@localhost/db?host=external"):
        with pytest.raises(ValueError):
            derive_database_urls(url, "a1b2c3d4")


def test_environment_is_explicit_and_offline(monkeypatch):
    for key in ("RUN_PHASE10_DEEPSEEK", "RUN_PHASE10_PLATFORM_DELIVERY"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "real-key-must-not-propagate")
    monkeypatch.setenv("HTTP_PROXY", "http://external:9000")
    monkeypatch.setenv("JWT_SECRET_KEY", "ambient-secret")
    env = build_child_environment("a1b2c3d4")
    assert "RUN_PHASE10_DEEPSEEK" not in env and "RUN_PHASE10_PLATFORM_DELIVERY" not in env
    assert env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:8765"
    assert env["MILVUS_COLLECTION"] == "phase10_a1b2c3d4"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert "HTTP_PROXY" not in env
    assert "real-key-must-not-propagate" not in env.values()
    assert "ambient-secret" not in env.values()
    assert env["KNOWLEDGE_UPLOAD_DIR"].endswith("a1b2c3d4\\knowledge-uploads")
    assert env["PYTHONPATH"].endswith("phase10-platform-delivery")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == r"D:\E-commerce_operations_env\playwright-browsers"
    assert env["TEMP"] == env["TMP"] and env["TEMP"].endswith("a1b2c3d4\\temp")


def test_windows_profile_and_expanded_ime_cache_stay_in_run_temp(tmp_path, monkeypatch):
    import ntpath

    for key in ("SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        monkeypatch.setenv(key, "C:\\ambient-personal")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    env = build_child_environment("a1b2c3d4", evidence_root=tmp_path)
    temp = tmp_path / "a1b2c3d4" / "temp"
    for key in ("SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        assert Path(env[key]).is_relative_to(temp)
    assert env["SYSTEMROOT"] == env["WINDIR"] == r"C:\Windows"
    with monkeypatch.context() as child:
        for key, value in env.items():
            child.setenv(key, value)
        for cache in (r"%SystemDrive%\ProgramData\SogouInput\cache", r"%USERPROFILE%\.cache", r"%APPDATA%\cache", r"%LOCALAPPDATA%\cache", r"%PROGRAMDATA%\cache"):
            expanded = Path(ntpath.expandvars(cache))
            assert "%" not in str(expanded)
            assert expanded.is_absolute() and expanded.is_relative_to(temp)
            if os.name == "nt":
                import ctypes

                buffer = ctypes.create_unicode_buffer(32768)
                count = ctypes.windll.kernel32.ExpandEnvironmentStringsW(cache, buffer, len(buffer))
                assert 0 < count <= len(buffer) and Path(buffer.value) == expanded
        assert Path.home().is_relative_to(temp)


class FakeResources:
    def __init__(self, events, fail=None):
        self.events, self.fail = events, fail

    async def create_database(self):
        self.events.append("create_database")
        if self.fail == "create_database":
            raise RuntimeError("sensitive failure")

    async def create_collection(self):
        self.events.append("create_collection")
        if self.fail == "create_collection":
            raise RuntimeError("sensitive failure")

    async def drop_collection(self):
        self.events.append("drop_collection")

    async def drop_database(self):
        self.events.append("drop_database")

    async def setup_checkpoints(self):
        self.events.append("checkpoint_setup")
        if self.fail == "checkpoint_setup":
            raise RuntimeError("sensitive checkpoint failure")
        if self.fail == "checkpoint_timeout":
            raise TimeoutError("sensitive checkpoint timeout")

    async def snapshot(self):
        return {}

    async def verify(self, evidence, baseline):
        self.events.append("verify")


class FakeProcess:
    def __init__(self, name, events, code=0, running=False):
        self.name, self.events, self.code, self.running = name, events, code, running

    def poll(self):
        return None if self.running else self.code

    def terminate(self):
        self.events.append("terminate:" + self.name)
        self.running = False

    def wait(self, timeout):
        return self.code

    def kill(self):
        self.events.append("kill:" + self.name)
        self.running = False


@pytest.mark.parametrize("failure", [None, "create_database", "create_collection", "migrate", "checkpoint_setup", "checkpoint_timeout", "health", "playwright"])
async def test_owned_cleanup_all_failure_paths(tmp_path, monkeypatch, failure):
    events, children = [], []
    resources = FakeResources(events, failure)
    runner = Runner("a1b2c3d4", evidence_root=tmp_path, resources=resources)

    def popen(command, **kwargs):
        assert kwargs["cwd"] == runner.worktree
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["PYTHONPATH"] == str(runner.worktree)
        for key in ("SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
            directory = Path(kwargs["env"][key])
            assert directory.is_relative_to(runner.evidence / "temp") and directory.is_dir()
        events.append("seed" if "scripts.seed_demo" in command else "start")
        name = "migrate" if "alembic" in command else "playwright" if "test:e2e" in command else str(len(children))
        running = "uvicorn" in command or any(str(c).startswith("scripts.run_") for c in command)
        child = FakeProcess(name, events, code=int(failure == name), running=running)
        children.append(child)
        return child

    async def health():
        if failure == "health":
            raise RuntimeError("sensitive health URL")

    async def faults():
        events.append("faults")

    monkeypatch.setattr(runner, "popen", popen)
    monkeypatch.setattr(runner, "health", health)
    monkeypatch.setattr(runner, "fault_checks", faults)
    monkeypatch.setattr(runner, "preflight", lambda: None)
    assert await runner.run() == int(failure is not None)
    assert ("drop_database" in events) == (failure != "create_database")
    assert ("drop_collection" in events) == (failure not in {"create_database", "create_collection"})
    assert [item for item in events if item.startswith("terminate:")] == [
        "terminate:" + child.name for child in reversed(children)
        if "terminate:" + child.name in events
    ]
    assert "sensitive" not in (runner.evidence / "lifecycle.jsonl").read_text()
    if failure in {"checkpoint_setup", "checkpoint_timeout"}:
        assert len(children) == 2  # Migration and seed only; no build, services or workers.
        assert events[-3:] == ["checkpoint_setup", "drop_collection", "drop_database"]
    if failure is None:
        assert events.count("checkpoint_setup") == 1
        assert events.index("checkpoint_setup") == events.index("seed") + 1
        assert events[-2:] == ["drop_collection", "drop_database"]
        worker_envs = runner.child_environments
        assert sum(env.get("RUN_PHASE10_PLATFORM_DELIVERY") == "1" for env in worker_envs) == 1
        for (name, _), environment in zip(runner.processes, worker_envs, strict=True):
            if name == "knowledge_worker":
                assert environment["CUDA_VISIBLE_DEVICES"] == "-1"
            else:
                assert "CUDA_VISIBLE_DEVICES" not in environment
        assert "CUDA_VISIBLE_DEVICES" not in runner.environment


@pytest.mark.parametrize("blocked_at", [None, "connect", "setup"])
async def test_checkpoint_setup_uses_run_url_and_bounds_connection_and_migrations(monkeypatch, blocked_at):
    import asyncio
    from contextlib import asynccontextmanager
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    monkeypatch.setenv("LANGGRAPH_DATABASE_URL", "postgresql://ambient:secret@localhost/ecommerce")
    resources = LocalResources(build_child_environment("a1b2c3d4"))
    events = []
    timeout = asyncio.timeout

    def bounded_timeout(seconds):
        assert 0 < seconds <= 60
        return timeout(0.01)

    class Saver:
        async def setup(self):
            events.append("setup")
            if blocked_at == "setup":
                await asyncio.Future()

    @asynccontextmanager
    async def from_conn_string(url):
        assert url == resources.environment["LANGGRAPH_DATABASE_URL"]
        assert url.endswith("/ecommerce_phase10_a1b2c3d4")
        events.append("connect")
        try:
            if blocked_at == "connect":
                await asyncio.Future()
            yield Saver()
        finally:
            events.append("close")

    monkeypatch.setattr(AsyncPostgresSaver, "from_conn_string", from_conn_string)
    monkeypatch.setattr(asyncio, "timeout", bounded_timeout)
    if blocked_at:
        with pytest.raises(TimeoutError):
            await resources.setup_checkpoints()
    else:
        await resources.setup_checkpoints()
    assert events == (["connect", "close"] if blocked_at == "connect" else ["connect", "setup", "close"])


async def test_database_ownership_survives_close_failure_and_refuses_existing(monkeypatch):
    resources = LocalResources(build_child_environment("a1b2c3d4"))
    calls = []

    class Connection:
        async def execute(self, query, *args):
            calls.append((query, args))

        async def close(self, timeout):
            raise RuntimeError("close failure")

    async def connect(**kwargs):
        assert kwargs == {"maintenance": True}
        return Connection()

    monkeypatch.setattr(resources, "connect", connect)
    with pytest.raises(RuntimeError):
        await resources.create_database()
    assert resources.database_created
    with pytest.raises(RuntimeError):
        await resources.drop_database()
    assert calls == [
        ('CREATE DATABASE "ecommerce_phase10_a1b2c3d4"', ()),
        ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()", ("ecommerce_phase10_a1b2c3d4",)),
        ('DROP DATABASE "ecommerce_phase10_a1b2c3d4"', ()),
    ]
    resources.database_created = False

    async def existing(*args):
        raise RuntimeError("already exists")

    monkeypatch.setattr(Connection, "execute", existing)
    with pytest.raises(RuntimeError):
        await resources.create_database()
    assert not resources.database_created


@pytest.mark.parametrize("existing", [False, True])
async def test_collection_never_adopts_and_cleans_partial_index_failure(monkeypatch, existing):
    import sys
    from types import SimpleNamespace
    from backend import knowledge_index
    resources = LocalResources(build_child_environment("a1b2c3d4"))
    calls = []
    fields, indexes = [], []

    class Client:
        def has_collection(self, **kwargs):
            return existing

        def create_schema(self, **kwargs):
            assert kwargs == {"auto_id": False, "enable_dynamic_field": False}
            return SimpleNamespace(add_field=lambda **kwargs: fields.append(kwargs))

        def prepare_index_params(self):
            return SimpleNamespace(add_index=lambda **kwargs: indexes.append(kwargs))

        def create_collection(self, **kwargs):
            calls.append(("create", kwargs["collection_name"]))

        def create_index(self, **kwargs):
            raise RuntimeError("index failed")

        def drop_collection(self, **kwargs):
            calls.append(("drop", kwargs["collection_name"]))

    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(DataType=SimpleNamespace(VARCHAR=1, FLOAT_VECTOR=2, SPARSE_FLOAT_VECTOR=3)))
    monkeypatch.setattr(knowledge_index, "MilvusKnowledgeIndex", lambda **kwargs: SimpleNamespace(_client=Client()))
    with pytest.raises(RuntimeError):
        await resources.create_collection()
    assert resources.collection_created is (not existing)
    if resources.collection_created:
        await resources.drop_collection()
    assert calls == ([] if existing else [("create", "phase10_a1b2c3d4"), ("drop", "phase10_a1b2c3d4")])
    if not existing:
        assert fields == [
            {"field_name": name, "datatype": 1, "is_primary": name == "chunk_id", "max_length": 256 if name == "category" else 36}
            for name in ("chunk_id", "document_id", "version_id", "category")
        ] + [
            {"field_name": "dense_vector", "datatype": 2, "dim": 1024},
            {"field_name": "sparse_vector", "datatype": 3},
        ]
        assert indexes == [
            {"field_name": "dense_vector", "index_type": "AUTOINDEX", "metric_type": "COSINE"},
            {"field_name": "sparse_vector", "index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"},
        ]


async def test_process_timeout_escalates_only_recorded_child(tmp_path):
    events = []
    runner = Runner("a1b2c3d4", evidence_root=tmp_path, resources=FakeResources(events))
    child = FakeProcess("owned", events, running=True)

    def wait(timeout):
        if "kill:owned" not in events:
            raise subprocess.TimeoutExpired("owned", timeout)
        return 0

    child.wait = wait
    await runner.stop(child)
    assert events == ["terminate:owned", "kill:owned"]


async def test_existing_evidence_directory_refused_without_cleanup(tmp_path):
    events = []
    runner = Runner("a1b2c3d4", evidence_root=tmp_path, resources=FakeResources(events))
    runner.evidence.mkdir()
    with pytest.raises(FileExistsError):
        await runner.run()
    assert not events


async def test_windows_npm_cleanup_targets_only_recorded_live_parent(tmp_path, monkeypatch):
    import os
    if os.name != "nt":
        pytest.skip("Windows native cleanup")
    runner = Runner("a1b2c3d4", evidence_root=tmp_path, resources=FakeResources([]))
    child = FakeProcess("playwright", [], running=True)
    child.pid = 12345
    runner.processes.append(("playwright", child))
    commands = []

    def native(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 10 and kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        child.running = False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", native)
    await runner.stop(child)
    await runner.stop(child)
    assert commands == [["taskkill", "/PID", "12345", "/T", "/F"]]


@pytest.mark.parametrize("invalid_writes", [False, True])
async def test_webhook_fault_gate_requires_zero_invalid_writes_and_one_replay(tmp_path, monkeypatch, invalid_writes):
    import httpx
    runner = Runner("a1b2c3d4", evidence_root=tmp_path, resources=FakeResources([]))
    runner.evidence.mkdir()
    runner.resources.delivery = {"id": "delivery-1", "external_operation_id": "operation-1"}
    counts = [0, 0]
    request_count = 0

    class Connection:
        async def fetchrow(self, query):
            return tuple(counts)

        async def close(self, timeout):
            pass

    async def connect():
        return Connection()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, **kwargs):
            nonlocal request_count
            request_count += 1
            assert path == "/integrations/platform/webhooks"
            if request_count <= 3:
                if invalid_writes:
                    counts[0] += 1
                return httpx.Response(401)
            if request_count == 4:
                counts[:] = [1, 1]
                return httpx.Response(201, json={"created": True})
            return httpx.Response(200, json={"created": False})

        async def get(self, path):
            return httpx.Response(200, json={"mutation_count": 1, "operation_count": 1, "disconnect_used": True})

    runner.resources.connect = connect
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    if invalid_writes:
        with pytest.raises(RuntimeError, match="PHASE10_EVIDENCE_INVALID"):
            await runner.webhook_checks()
        assert not (runner.evidence / "fault-proof.json").exists()
    else:
        await runner.webhook_checks()
        assert request_count == 5 and counts == [1, 1]


@pytest.mark.parametrize("code", ["KNOWLEDGE_MILVUS_UNAVAILABLE", "KNOWLEDGE_MODEL_UNAVAILABLE"])
async def test_milvus_probe_requires_typed_actual_dependency_failure(tmp_path, monkeypatch, code):
    import json
    from backend import knowledge_index
    from scripts import run_phase10_e2e as launcher
    evidence = tmp_path / "a1b2c3d4"
    evidence.mkdir()
    monkeypatch.setattr(launcher, "EVIDENCE_ROOT", tmp_path)
    monkeypatch.setenv("PHASE10_RUN_ID", "a1b2c3d4")
    monkeypatch.setenv("PHASE10_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("MILVUS_URI", "http://127.0.0.1:12345")

    def index(**kwargs):
        assert kwargs["uri"] == "http://127.0.0.1:12345" and kwargs["collection"] == "phase10_a1b2c3d4"
        raise knowledge_index.KnowledgeDependencyError(code, retryable=True)

    monkeypatch.setattr(knowledge_index, "MilvusKnowledgeIndex", index)
    if code == "KNOWLEDGE_MILVUS_UNAVAILABLE":
        assert await launcher.milvus_probe() == 0
        assert json.loads((evidence / "milvus-proof.json").read_text()) == {"status": "failed", "error_code": code, "retryable": True, "boundary": "dependency_startup"}
    else:
        with pytest.raises(RuntimeError):
            await launcher.milvus_probe()
        assert not (evidence / "milvus-proof.json").exists()
