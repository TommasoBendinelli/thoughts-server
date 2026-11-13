# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .models import Base


def create_database_engine(
    url: str,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pooled: bool = True,
) -> Engine:
    """An engine for one Library's database.

    ``pool_size`` connections stay open while idle and ``max_overflow`` more are
    opened under load and closed when returned (SQLAlchemy's defaults, 5 and 10,
    when not given). With many Libraries in one process, and several processes
    per Library, what stays open while idle is what counts against PostgreSQL's
    ``max_connections``. ``pooled=False`` keeps none: every use opens its own
    connection, for engines used once in a while.
    """
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    parsed = make_url(url)
    in_memory = parsed.drivername.startswith("sqlite") and parsed.database in {None, "", ":memory:"}
    if parsed.drivername.startswith("sqlite") and not in_memory:
        from pathlib import Path

        Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    options: dict = {}
    if not in_memory:
        if not pooled:
            options["poolclass"] = NullPool
        else:
            if pool_size is not None:
                options["pool_size"] = pool_size
            if max_overflow is not None:
                options["max_overflow"] = max_overflow
    # The ping at checkout costs one round trip and is what makes a connection that
    # PostgreSQL closed (a restart, an idle timeout) invisible to requests.
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


class Database:
    def __init__(self, url: str, *, pool_size: int | None = None, max_overflow: int | None = None):
        self.engine = create_database_engine(url, pool_size=pool_size, max_overflow=max_overflow)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        # The Library's default-folder segment is read once per process.
        from .default_folder_segment import load_from_engine
        load_from_engine(self.engine)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()
