"""Database access for the pipeline.

Flyway owns the schema (CLAUDE.md rule 4). Nothing in this module, or in anything that
imports it, may issue DDL: the pipeline reads and writes rows only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from jg.config import db_url

#: Rows come back as plain dicts keyed by column name.
Conn = psycopg.Connection[dict[str, Any]]


def dsn() -> str:
    """libpq connection string. The single place the pipeline learns where the DB is."""
    return db_url()


@contextmanager
def connect() -> Iterator[Conn]:
    """Yield a psycopg 3 connection with ``dict_row``.

    Commits on clean exit, rolls back on any exception, always closes. Callers that want
    several independent units of work should open several connections rather than
    committing by hand inside one.
    """
    conn: Conn = psycopg.connect(dsn(), row_factory=dict_row)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.commit()
        conn.close()
