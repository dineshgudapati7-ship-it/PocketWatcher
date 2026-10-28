from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from pocketwatcher.settings import get_settings

CONNECT_TIMEOUT_SECONDS = 10


def connect_args(url: str) -> dict:
    # libpq waits forever for an unreachable server by default; fail with an error instead.
    return {"connect_timeout": CONNECT_TIMEOUT_SECONDS} if url.startswith("postgresql") else {}


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args(url))


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
