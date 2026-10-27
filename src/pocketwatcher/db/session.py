from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from pocketwatcher.settings import get_settings


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
