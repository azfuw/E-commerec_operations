import importlib.util
import io
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from backend.models import Base


def load_migration():
    spec = importlib.util.spec_from_file_location("logistics_migration", Path(__file__).parents[1] / "alembic/versions/0008_logistics_department.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_models_and_installs_append_only_guards():
    migration = load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(connection, tables=[table for table in Base.metadata.sorted_tables if not table.name.startswith("logistics_")])
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        assert compare_metadata(context, Base.metadata) == []
        triggers = connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='logistics_events'")).scalars().all()
        assert set(triggers) == {"logistics_events_no_update", "logistics_events_no_delete"}
        with Operations.context(context):
            migration.downgrade()
        assert not any(name.startswith("logistics_") for name in inspect(connection).get_table_names())
    engine.dispose()


def test_postgresql_offline_migration_compiles_immutable_trigger():
    stream = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": stream})
    with Operations.context(context):
        load_migration().upgrade()
    sql = stream.getvalue()
    assert "CREATE TABLE logistics_shipments" in sql
    assert "CREATE TRIGGER logistics_events_immutable BEFORE UPDATE OR DELETE" in sql
    assert "UNIQUE (dedupe_key)" in sql
