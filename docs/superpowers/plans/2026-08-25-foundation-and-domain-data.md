# Foundation and Domain Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first independently testable vertical slice: repository foundation, PostgreSQL domain model, authentication and store authorization, deterministic demo data, and deterministic commerce analytics.

**Architecture:** Use one FastAPI modular monolith with async SQLAlchemy and Alembic. Keep this phase free of LLM, LangGraph, Milvus, Worker, and frontend code; it establishes trusted business facts and deterministic tools that later Agents will call.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy asyncio, asyncpg, Alembic, PyJWT, pwdlib/Argon2, PostgreSQL 16, pytest, HTTPX, aiosqlite, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-25-ecommerce-operations-design.md`

## Global Constraints

- Work only in `D:\E-commerce_operations`.
- Read the approved spec before editing and do not expand its scope.
- The project is local-only; do not add deployment, cloud, platform API, image, video, MCP, Redis, Celery, Kafka, LangGraph, Milvus, DeepSeek, Worker, or frontend code in this phase.
- Use Python 3.11 and Pydantic v2.
- Never print or copy the DeepSeek API key; this phase does not need it.
- Use `apply_patch` for file edits. Preserve user files and the approved spec.
- Use fixed random seeds and UTC-aware timestamps.
- Authorization checks must be server-side and include role plus store scope.
- Tests run without external API calls.
- Today, 2026-08-25, stop no later than 22:20 Asia/Shanghai. At the deadline finish only the current atomic edit, run the smallest relevant check, save progress, and report. Do not begin another step.
- Each completed task receives its own Git commit. If Git is not initialized, initialize it only in `D:\E-commerce_operations` during Task 1.

---

## Planned File Map

```text
.
├── .env.example                         # Non-secret configuration contract
├── .gitignore                           # Secrets, caches, environments, generated data
├── docker-compose.yml                   # PostgreSQL development service
├── pyproject.toml                       # Runtime and test dependencies
├── alembic.ini
├── alembic/
│   ├── env.py                           # Async migration configuration
│   └── versions/0001_initial_schema.py # First schema migration
├── backend/
│   ├── __init__.py
│   ├── main.py                          # FastAPI factory and router assembly
│   ├── config.py                        # Settings from environment
│   ├── database.py                      # Engine, session factory, Base
│   ├── common.py                        # UTC timestamp and shared enums
│   ├── auth.py                          # Password, JWT, current user, role checks
│   ├── models.py                        # Business fact ORM models
│   ├── schemas.py                       # API and analytics Pydantic contracts
│   ├── routes.py                        # Health, auth, store and product endpoints
│   ├── seed.py                          # Deterministic dataset generation
│   └── analytics.py                     # Deterministic metrics and anomaly tools
├── scripts/
│   └── seed_demo.py                     # Idempotent seed command
└── tests/
    ├── conftest.py                      # Async test DB and client
    ├── test_health.py
    ├── test_models.py
    ├── test_auth_and_scope.py
    ├── test_seed.py
    └── test_analytics.py
```

The phase intentionally keeps related logic in a few focused modules. Split a module only if it becomes difficult to review during implementation; do not create single-implementation repositories, factories, or service interfaces.

---

### Task 1: Repository, Configuration, and Health Slice

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `docker-compose.yml`
- Create: `backend/__init__.py`
- Create: `backend/config.py`
- Create: `backend/database.py`
- Create: `backend/main.py`
- Create: `tests/conftest.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Produces: `backend.main:create_app() -> FastAPI`
- Produces: `backend.config:get_settings() -> Settings`
- Produces: `backend.database:get_session() -> AsyncIterator[AsyncSession]`
- Produces: `GET /health/live -> {"status": "ok"}`

- [x] **Step 1: Initialize Git and verify the exact root**

Run:

```powershell
Get-Location
git init
git status --short
```

Expected: root is `D:\E-commerce_operations`; Git reports the approved docs as untracked. Do not initialize any parent directory.

- [x] **Step 1A: Create a project-local Python 3.11 environment**

The machine's default `python` is 3.10. Use the existing Python 3.11.15 interpreter only as a bootstrap and keep all new packages inside the project:

```powershell
& 'C:\Users\15482\anaconda3\envs\deepS\python.exe' -m venv .venv
& '.\.venv\Scripts\python.exe' --version
& '.\.venv\Scripts\python.exe' -m pip install pytest pytest-asyncio httpx
```

Expected: the project-local interpreter reports Python 3.11.x. Never install project packages into `deepS`, `edu_agent`, the base Conda environment, or the system Python. Every subsequent Python command in this plan must use `.\.venv\Scripts\python.exe` or activate `.venv` first.

- [x] **Step 2: Add the failing health test**

Create `tests/test_health.py`:

```python
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


async def test_liveness() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Add `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` in `pyproject.toml`.

- [x] **Step 3: Run the test and confirm the expected failure**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_health.py -v
```

Expected: FAIL because `backend.main` or `create_app` does not exist.

- [x] **Step 4: Add the minimal project configuration**

`pyproject.toml` must declare Python `>=3.11,<3.12` and these runtime dependencies:

```toml
[project]
name = "ecommerce-operations"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = [
  "fastapi>=0.116,<1",
  "uvicorn[standard]>=0.35,<1",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "sqlalchemy[asyncio]>=2.0.41,<3",
  "asyncpg>=0.30,<1",
  "alembic>=1.16,<2",
  "PyJWT>=2.10,<3",
  "pwdlib[argon2]>=0.2,<1",
]

[project.optional-dependencies]
test = [
  "pytest>=8.4,<9",
  "pytest-asyncio>=1.0,<2",
  "httpx>=0.28,<1",
  "aiosqlite>=0.21,<1",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`.env.example` must contain only names and safe example values:

```ini
APP_ENV=development
DATABASE_URL=postgresql+asyncpg://ecommerce:ecommerce@localhost:5434/ecommerce
JWT_SECRET_KEY=replace-with-a-long-random-value
JWT_ALGORITHM=HS256
ACCESS_TOKEN_MINUTES=60
```

`.gitignore` must include `.env.local`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`, `.coverage`, `htmlcov/`, and `data/uploads/`.

After completing `pyproject.toml`, synchronize the project environment:

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -e '.[test]'
```

`tests/conftest.py` must set a test-only JWT secret before importing the application:

```python
import os

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-at-least-32-characters")
```

Its SQLite fixture must enable `PRAGMA foreign_keys=ON` for every connection so foreign-key behavior is not silently skipped.

- [x] **Step 5: Add settings, database boundary, and app factory**

Use this settings contract in `backend/config.py`:

```python
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://ecommerce:ecommerce@localhost:5434/ecommerce"
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`backend/database.py` exposes `Base`, `async_session_factory`, and `get_session`; it must not create tables at import time. `backend/main.py` exposes:

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="智营台 API", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

Do not expose environment secrets in health responses.

- [x] **Step 6: Add the PostgreSQL Compose service**

`docker-compose.yml` contains one PostgreSQL 16 service named `postgres`, host port `5434`, a named volume, and a health check using `pg_isready`. It reads non-secret local defaults and must not include a real DeepSeek key.

- [x] **Step 7: Run the health test and configuration checks**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_health.py -v
docker compose config
& '.\.venv\Scripts\python.exe' -m compileall backend tests
```

Expected: health test PASS, Compose config parses, compilation succeeds.

- [x] **Step 8: Commit Task 1**

```powershell
git add .gitignore .env.example pyproject.toml docker-compose.yml backend tests docs
git commit -m "chore: establish backend project foundation"
```

---

### Task 2: Initial Business Schema and Migration

**Files:**
- Create: `backend/common.py`
- Create: `backend/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial_schema.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces ORM models: `User`, `Store`, `UserStoreScope`, `Product`, `ProductSku`, `Order`, `OrderItem`, `TrafficDaily`, `InventorySnapshot`
- Produces enums: `UserRole`, `UserStatus`, `OrderStatus`, `RefundStatus`
- Normal runtime primary keys are UUID strings generated by `uuid.uuid4`; the demo seed is the sole exception and uses UUID5 from stable business keys so reset/reseed is reproducible.

- [x] **Step 1: Write failing constraint tests**

`tests/test_models.py` must prove at least:

```python
async def test_product_code_is_unique_within_store(session):
    store = Store(id="store-1", name="旗舰店", code="flagship")
    session.add_all([
        store,
        Product(id="p-1", store_id="store-1", code="SKU-001", title="商品一", category="数码"),
        Product(id="p-2", store_id="store-1", code="SKU-001", title="商品二", category="数码"),
    ])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_sku_rejects_negative_price(session):
    store = Store(id="store-1", name="旗舰店", code="flagship")
    product = Product(
        id="p-1", store_id="store-1", code="SKU-001", title="商品一", category="数码"
    )
    session.add_all([store, product])
    await session.flush()
    session.add(ProductSku(
        id="sku-1", product_id="p-1", code="P1-BLACK", spec={"颜色": "黑色"},
        price=Decimal("-1.00"), current_stock=10,
    ))
    with pytest.raises(IntegrityError):
        await session.commit()
```

The fixture must roll back between tests.

- [x] **Step 2: Run the model tests and verify failure**

Run `& '.\.venv\Scripts\python.exe' -m pytest tests/test_models.py -v`.

Expected: FAIL because the ORM models do not exist.

- [x] **Step 3: Define exact enums and common timestamp behavior**

`backend/common.py` defines string enums with these exact values:

```python
class UserRole(StrEnum):
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OrderStatus(StrEnum):
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RefundStatus(StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    REFUNDED = "refunded"
    RETURNED = "returned"
```

Provide `utc_now() -> datetime` returning timezone-aware UTC.

- [x] **Step 4: Implement the ORM tables and constraints**

Use SQLAlchemy 2 typed mappings. Required fields and constraints:

| Model | Required fields | Required constraints |
|---|---|---|
| User | id, username, password_hash, role; status and created_at have callable defaults | username unique |
| Store | id, name, code; enabled and created_at have callable defaults | code unique |
| UserStoreScope | user_id, store_id | composite primary key |
| Product | id, store_id, code, title, category; brand/description default empty, JSON fields use callable defaults, current_version defaults 1, enabled defaults true | unique(store_id, code), current_version >= 1 |
| ProductSku | id, product_id, code, spec JSON, price Numeric(12,2), current_stock | unique(product_id, code), price >= 0, stock >= 0 |
| Order | id, store_id, ordered_at, status, total_amount | total_amount >= 0 |
| OrderItem | id, order_id, product_id, sku_id, quantity, unit_price, refund_status | quantity > 0, unit_price >= 0 |
| TrafficDaily | id, store_id, product_id, metric_date, impressions, clicks, visitors, add_to_carts | unique(store_id, product_id, metric_date), every count >= 0, clicks <= impressions |
| InventorySnapshot | id, store_id, sku_id, snapshot_date, on_hand, inbound | unique(store_id, sku_id, snapshot_date), counts >= 0 |

Store JSON columns with safe callable defaults such as `default=list` and `default=dict`, never shared mutable instances.

- [x] **Step 5: Configure Alembic and write the explicit initial migration**

`alembic/env.py` imports `Base.metadata` and uses Alembic's async pattern with `async_engine_from_config` and `connection.run_sync(do_run_migrations)`. Do not swap the `asyncpg` URL to an undeclared synchronous driver. The initial migration explicitly creates all nine tables, foreign keys, unique constraints, and checks above. Do not use application startup `create_all` as the migration mechanism.

- [x] **Step 6: Run model and migration checks**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_models.py -v
docker compose up -d postgres
$env:JWT_SECRET_KEY='local-development-only-secret-at-least-32-characters'
alembic upgrade head
alembic current
```

Expected: tests PASS; PostgreSQL becomes healthy; current revision is `0001`.

- [x] **Step 7: Commit Task 2**

```powershell
git add backend/common.py backend/models.py alembic.ini alembic tests/test_models.py
git commit -m "feat: add commerce business schema"
```

---

### Task 3: Authentication and Store-Scoped Read APIs

**Files:**
- Create: `backend/schemas.py`
- Create: `backend/auth.py`
- Create: `backend/routes.py`
- Modify: `backend/main.py`
- Create: `tests/test_auth_and_scope.py`

**Interfaces:**
- Produces: `POST /auth/login -> AccessToken`
- Produces: `GET /stores -> list[StoreSummary]`
- Produces: `GET /stores/{store_id}/products -> list[ProductSummary]`
- Produces: `get_current_user() -> User`
- Produces: `require_roles(*roles) -> FastAPI dependency`
- Produces: `require_store_access(store_id, user, session) -> Store`

- [x] **Step 1: Write failing API authorization tests**

Cover these exact behaviors:

```python
async def test_operator_only_sees_assigned_stores(client, operator_token):
    response = await client.get("/stores", headers={"Authorization": f"Bearer {operator_token}"})
    assert response.status_code == 200
    assert [store["code"] for store in response.json()] == ["flagship"]


async def test_operator_cannot_read_unassigned_store(client, operator_token):
    response = await client.get(
        "/stores/other-store/products",
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert response.status_code == 403


async def test_disabled_user_cannot_login(client):
    response = await client.post("/auth/login", json={"username": "disabled", "password": "DemoPass!2026"})
    assert response.status_code == 401
```

Also test bad password and missing token.

- [x] **Step 2: Run the authorization tests and confirm failure**

Run `& '.\.venv\Scripts\python.exe' -m pytest tests/test_auth_and_scope.py -v`.

Expected: FAIL because auth routes and dependencies do not exist.

- [x] **Step 3: Implement password and JWT functions**

`backend/auth.py` uses one `PasswordHash.recommended()` instance and exposes these exact concrete functions:

```python
password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user: User, settings: Settings) -> str:
    now = utc_now()
    payload = {
        "sub": user.id,
        "role": user.role.value,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
```

`get_current_user` is an async FastAPI dependency receiving the bearer token, database session, and settings; it decodes only `settings.jwt_algorithm`, loads `payload["sub"]`, and rejects missing, invalid, expired, unknown, or disabled users with HTTP 401. `require_roles(*roles)` returns a dependency that rejects unlisted roles with HTTP 403. `require_store_access(store_id, user, session)` returns the enabled `Store`; admins may access every enabled store, while other roles require a `UserStoreScope` row.

JWT claims are `sub`, `role`, `iat`, and `exp`. Never accept role or store scope from request bodies. Return the same 401 message for unknown username and wrong password.

- [x] **Step 4: Implement schemas and routes**

Pydantic contracts:

```python
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class StoreSummary(BaseModel):
    id: str
    code: str
    name: str


class ProductSummary(BaseModel):
    id: str
    code: str
    title: str
    category: str
    current_version: int
```

Admins see all enabled stores; operators and supervisors see only joined scopes. Product queries require store access and filter `Product.enabled is True`.

- [x] **Step 5: Register routes and run tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_auth_and_scope.py tests/test_health.py -v
```

Expected: PASS.

- [x] **Step 6: Commit Task 3**

```powershell
git add backend/auth.py backend/schemas.py backend/routes.py backend/main.py tests/test_auth_and_scope.py
git commit -m "feat: enforce role and store authorization"
```

---

### Task 4: Deterministic Demo Dataset

**Files:**
- Create: `backend/seed.py`
- Create: `scripts/seed_demo.py`
- Create: `tests/test_seed.py`

**Interfaces:**
- Produces: `async seed_demo_data(session: AsyncSession, seed: int = 20260825) -> SeedSummary`
- Produces: `async clear_demo_data(session: AsyncSession) -> None`
- Produces a CLI that supports `--seed` and `--reset`.

- [x] **Step 1: Write failing deterministic seed tests**

Tests must assert:

```python
async def test_seed_is_idempotent(session):
    first = await seed_demo_data(session)
    second = await seed_demo_data(session)
    assert second == first


async def test_seed_contains_known_anomaly_cases(session):
    summary = await seed_demo_data(session)
    assert summary.stores == 3
    assert summary.products == 300
    assert summary.days == 90
    assert {"low_conversion", "sales_drop", "high_refund", "stock_risk"} <= set(summary.anomaly_types)
```

Also verify the same seed produces the same store codes and anomaly product codes after reset.

- [x] **Step 2: Run tests and confirm failure**

Run `& '.\.venv\Scripts\python.exe' -m pytest tests/test_seed.py -v`.

Expected: FAIL because seeding functions do not exist.

- [x] **Step 3: Implement deterministic identities and dataset shape**

Use `random.Random(seed)` and UUID5 derived from stable business keys. Generate exactly:

- 3 stores: `flagship`, `digital`, `home`.
- 300 products, distributed 100 per store.
- 1-3 SKUs per product.
- 90 days of traffic and inventory snapshots.
- Orders sufficient to calculate paid/completed sales, refunds and returns.
- Four explicitly controlled anomaly cohorts: low conversion, sales drop, high refund, and stock risk.

Do not depend on Faker. Dates are derived from one explicit `end_date` argument defaulting to `date(2026, 8, 24)` so tomorrow's run produces identical rows.

`SeedSummary` contains integer counts, `days`, and sorted `anomaly_types` only; it contains no credentials.

- [x] **Step 4: Make seeding idempotent**

Use stable unique keys and PostgreSQL-compatible upserts or existence checks. `--reset` deletes only rows owned by the known demo store IDs, in foreign-key-safe order; it must never issue a broad database drop.

Create users with documented demo usernames `operator`, `supervisor`, and `admin`; hash the default local password rather than storing plaintext. The CLI may print usernames but must not print hashes, JWTs, environment variables, or real secrets.

- [x] **Step 5: Run seed tests and a PostgreSQL smoke run**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_seed.py -v
& '.\.venv\Scripts\python.exe' scripts/seed_demo.py --reset --seed 20260825
& '.\.venv\Scripts\python.exe' scripts/seed_demo.py --seed 20260825
```

Expected: tests PASS; second CLI invocation reports unchanged counts and creates no duplicates.

- [x] **Step 6: Commit Task 4**

```powershell
git add backend/seed.py scripts/seed_demo.py tests/test_seed.py
git commit -m "feat: add deterministic commerce demo data"
```

---

### Task 5: Deterministic Analytics and Anomaly Tools

**Files:**
- Create: `backend/analytics.py`
- Modify: `backend/schemas.py`
- Create: `tests/test_analytics.py`

**Interfaces:**
- Produces: `async get_store_summary(session, store_id, start_date, end_date) -> StoreMetrics`
- Produces: `async get_product_metrics(session, store_id, product_id, start_date, end_date) -> ProductMetrics`
- Produces: `async find_anomalous_products(session, store_id, start_date, end_date, limit=5) -> list[AnomalyCandidate]`
- Produces: `async compare_store_products(session, store_id, product_ids, start_date, end_date) -> list[ProductMetrics]`
- Produces: `async get_inventory_risk(session, store_id, product_id) -> InventoryRisk`
- Produces: `ProductMetrics.from_totals(*, impressions: int, clicks: int, orders: int, units: int, revenue: Decimal, refunds: int) -> ProductMetrics`
- Produces: `ProductNotInStore`, raised when the requested product is not owned by the requested store.

- [x] **Step 1: Write failing metric and anomaly tests**

Cover formulas and trust boundaries:

```python
def test_product_metric_formulas():
    metrics = ProductMetrics.from_totals(
        impressions=1000, clicks=100, orders=5, units=6,
        revenue=Decimal("299.00"), refunds=1,
    )
    assert metrics.ctr == Decimal("0.1000")
    assert metrics.conversion_rate == Decimal("0.0500")
    assert metrics.refund_rate == Decimal("0.2000")


async def test_anomaly_ranking_uses_seeded_labels(session):
    await seed_demo_data(session)
    store_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
    candidates = await find_anomalous_products(
        session, store_id, date(2026, 7, 26), date(2026, 8, 24), limit=5
    )
    assert len(candidates) == 5
    assert candidates[0].score >= candidates[-1].score
    assert all(candidate.evidence for candidate in candidates)


async def test_store_boundary_is_enforced(session):
    await seed_demo_data(session)
    flagship_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
    home_product_id = await session.scalar(
        select(Product.id).join(Store).where(Store.code == "home")
    )
    with pytest.raises(ProductNotInStore):
        await get_product_metrics(
            session, flagship_id, home_product_id,
            date(2026, 7, 26), date(2026, 8, 24),
        )
```

- [x] **Step 2: Run tests and confirm failure**

Run `& '.\.venv\Scripts\python.exe' -m pytest tests/test_analytics.py -v`.

Expected: FAIL because analytics contracts do not exist.

- [x] **Step 3: Implement exact metrics with safe zero handling**

Use Decimal quantized to four places for rates:

```text
ctr = clicks / impressions
conversion_rate = paid_or_completed_order_count / clicks
refund_rate = refunded_or_returned_order_item_count / paid_or_completed_order_item_count
average_order_value = revenue / paid_or_completed_order_count
```

Return Decimal zero when a denominator is zero; never raise division errors or invent rates. Keep raw totals in every metrics result so Agent explanations remain auditable.

- [x] **Step 4: Implement deterministic anomaly rules**

Compare the requested period with the immediately preceding equal-length period. Emit these labels:

```text
low_conversion: clicks >= 50 and conversion_rate < 60% of store median
sales_drop: current revenue <= 70% of previous-period revenue
high_refund: completed items >= 10 and refund_rate >= 10%
stock_risk: on_hand <= max(3, seven_day_units_sold)
```

Each `AnomalyCandidate` includes `product_id`, `product_code`, `anomaly_types`, `score`, `business_impact`, `metrics`, and an `evidence` list containing exact observed values and comparison values. Rank by score descending, then product code ascending for deterministic ties.

- [x] **Step 5: Run analytics and full phase tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_analytics.py -v
& '.\.venv\Scripts\python.exe' -m pytest -v
& '.\.venv\Scripts\python.exe' -m compileall backend scripts tests
```

Expected: all tests PASS and compilation succeeds.

- [x] **Step 6: Commit Task 5**

```powershell
git add backend/analytics.py backend/schemas.py tests/test_analytics.py
git commit -m "feat: add deterministic commerce analytics tools"
```

---

### Task 6: Phase Verification and Progress Handoff

**Files:**
- Modify only when verification exposes a real defect.
- Update: this plan's completed checkboxes after verified tasks.

**Interfaces:**
- Produces a runnable, authenticated backend foundation and deterministic tool layer for the next Agent phase.

- [x] **Step 1: Run the complete verification set**

```powershell
docker compose config
docker compose ps
alembic current
& '.\.venv\Scripts\python.exe' -m pytest -v
& '.\.venv\Scripts\python.exe' -m compileall backend scripts tests
git status --short
git log --oneline --decorate -8
```

Expected: Compose parses; PostgreSQL is healthy if started; migration is current; tests and compilation pass; only intentionally unfinished plan checkbox edits may remain.

- [x] **Step 2: Audit the implementation against phase scope**

Confirm all of the following with exact file and test evidence:

- No LLM, Agent, RAG, Worker, frontend, platform integration or unrelated infrastructure was introduced.
- No secret value was committed or printed.
- Store authorization is enforced in the backend.
- Seed output is deterministic and idempotent.
- Analytics expose raw evidence and deterministic rankings.
- README claims were not added for unimplemented capabilities.

- [x] **Step 3: Save the deadline handoff**

At or before 22:20 Asia/Shanghai, stop new work and report:

```text
Completed tasks and commit hashes:
Current task and exact next unchecked step:
Files changed but not committed:
Tests actually run and their results:
Services left running:
Blockers or failed checks:
Tomorrow's first command:
```

Do not mark an unfinished task complete. Do not continue to the Agent/RAG phase today.
