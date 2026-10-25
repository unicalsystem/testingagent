from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from testpilot import models  # noqa: F401  (register tables)
from testpilot.config import get_settings
from testpilot.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url.get_secret_value(), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url.get_secret_value(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        # Serialise concurrent migration attempts (e.g. two deploys at once).
        conn.exec_driver_sql("SELECT pg_advisory_lock(74220002)")
        try:
            context.configure(connection=conn, target_metadata=target_metadata, compare_type=True,
                              transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            conn.exec_driver_sql("SELECT pg_advisory_unlock(74220002)")
            conn.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
