from logging.config import fileConfig
from shlex import quote

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata

from db.base import Base
from db.models import trade, trade_lock, trade_lock_phrase, trade_note, trade_image, account, broker, trade_tag, trade_tag_map, signal_edge_sheet, economic_event

target_metadata = Base.metadata

# 앱과 같은 DB를 쓰도록 .env 의 DATABASE_URL 이 있으면 그걸 우선 사용 (비동기 드라이버 → 동기 드라이버로 바꿈)
import os
from dotenv import load_dotenv
load_dotenv()
_env_url = os.getenv("DATABASE_URL", "").strip()
if _env_url:
    _sync_url = _env_url.replace("+asyncpg", "").replace("+aiosqlite", "")
    config.set_main_option("sqlalchemy.url", _sync_url.replace("%", "%%"))
_IS_SQLITE = config.get_main_option("sqlalchemy.url").startswith("sqlite")

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")

    import re
    def encode_dsn(dsn):
        match = re.match(r'postgresql://(.*?):(.*?)@(.*)', dsn)
        if match:
            user, password, rest = match.groups()
            return f'postgresql://{quote(user)}:{quote(password)}@{rest}'
        return dsn

    encoded_url = encode_dsn(url)
    config.set_main_option("sqlalchemy.url", encoded_url)

    context.configure(
        url=encoded_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_as_batch=_IS_SQLITE,  # SQLite는 ALTER TABLE이 제한적이라 batch 모드 필요
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
