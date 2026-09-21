import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def load_migration():
    spec = importlib.util.spec_from_file_location("department_migration", Path(__file__).parents[1] / "alembic/versions/0009_user_department.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_department_migration_preserves_legacy_users_and_enforces_database_values():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, username VARCHAR(64) NOT NULL)"))
        connection.execute(text("CREATE TABLE user_store_scopes (user_id VARCHAR(36) REFERENCES users(id), store_id VARCHAR(36))"))
        connection.execute(text("INSERT INTO users VALUES ('legacy', 'legacy-user')"))
        connection.execute(text("INSERT INTO user_store_scopes VALUES ('legacy', 'store')"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            load_migration().upgrade()
        assert connection.execute(text("SELECT id, username, department FROM users")).one() == ("legacy", "legacy-user", "operations")
        assert connection.execute(text("SELECT * FROM user_store_scopes")).one() == ("legacy", "store")
        connection.execute(text("UPDATE users SET department='logistics' WHERE id='legacy'"))
        for value in ("invalid", None):
            with pytest.raises(IntegrityError):
                connection.execute(text("UPDATE users SET department=:value"), {"value": value})
        with Operations.context(context):
            load_migration().downgrade()
        assert "department" not in {item["name"] for item in inspect(connection).get_columns("users")}
        assert connection.execute(text("SELECT id, username FROM users")).one() == ("legacy", "legacy-user")
        assert connection.execute(text("SELECT * FROM user_store_scopes")).one() == ("legacy", "store")
    engine.dispose()


def test_department_postgres_offline_migration_compiles():
    stream = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": stream})
    with Operations.context(context):
        load_migration().upgrade()
        load_migration().downgrade()
    sql = stream.getvalue()
    assert "DEFAULT 'operations' NOT NULL" in sql
    assert "CONSTRAINT user_department CHECK (department IN ('operations', 'logistics'))" in sql
    assert "DROP COLUMN department" in sql
