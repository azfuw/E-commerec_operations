"""Opt-in PostgreSQL migration check; all data lives in a rolled-back schema."""
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from backend.config import get_settings
from backend.logistics_models import Shipment, ShipmentEvent
from backend.models import Base, Order, Store


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="explicit PostgreSQL integration opt-in required"
)


def test_department_migration_preserves_history_and_roundtrips_on_postgresql():
    engine = sa.create_engine(get_settings().database_url.replace("+asyncpg", "+psycopg"))
    migrations = []
    for path in sorted((Path(__file__).parents[1] / "alembic/versions").glob("000[1-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    schema = "department_check_" + uuid4().hex
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
                connection.dialect.default_schema_name = schema
                context = MigrationContext.configure(connection)
                with Operations.context(context):
                    for migration in migrations[:-1]:
                        migration.upgrade()
                connection.execute(sa.text("""INSERT INTO users (id, username, password_hash, role, status, created_at)
                    VALUES ('legacy', 'legacy', 'test-only', 'supervisor', 'active', CURRENT_TIMESTAMP)"""))
                now = datetime.now(UTC)
                connection.execute(Store.__table__.insert().values(id="store", name="Retained", code="retained"))
                connection.execute(Order.__table__.insert().values(id="order", store_id="store", ordered_at=now,
                    status="paid", total_amount=100))
                connection.execute(Shipment.__table__.insert().values(id="shipment", order_id="order", store_id="store",
                    carrier="Test", tracking_no="retained", destination="Retained", dispatch_due_at=now,
                    expected_delivery_at=now + timedelta(days=1)))
                connection.execute(ShipmentEvent.__table__.insert().values(id="event", shipment_id="shipment",
                    event_type="created", occurred_at=now, description="Retained history", actor_id="legacy",
                    actor_name="legacy", source="manual"))
                with Operations.context(context):
                    migrations[-1].upgrade()
                assert connection.scalar(sa.text("SELECT department FROM users WHERE id='legacy'")) == "operations"
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(sa.text("UPDATE users SET department='unknown' WHERE id='legacy'"))
                connection.execute(sa.text("UPDATE users SET department='logistics' WHERE id='legacy'"))
                assert compare_metadata(context, Base.metadata) == []
                with Operations.context(context):
                    migrations[-1].downgrade()
                assert "department" not in {column["name"] for column in sa.inspect(connection).get_columns("users")}
                with Operations.context(context):
                    migrations[-1].upgrade()
                assert compare_metadata(context, Base.metadata) == []
                assert connection.scalar(sa.text("SELECT department FROM users WHERE id='legacy'")) == "operations"
                assert connection.scalar(sa.text("SELECT description FROM logistics_events WHERE id='event'")) == "Retained history"
                assert connection.scalar(sa.text("SELECT tracking_no FROM logistics_shipments WHERE id='shipment'")) == "retained"
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
