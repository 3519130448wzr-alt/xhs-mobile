import os

from alembic import context
from sqlalchemy import create_engine, pool

from xhs_mobile.models import Base


def database_url():
    url = context.config.attributes.get("database_url") or os.environ.get("XHS_DATABASE_URL")
    if not url:
        raise RuntimeError("XHS_DATABASE_URL is required")
    return url


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif (connection := context.config.attributes.get("connection")) is not None:
    # Tests and embedding callers own their connection and transaction. This also
    # permits a disposable PostgreSQL search_path without touching public tables.
    migrate(connection)
else:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
