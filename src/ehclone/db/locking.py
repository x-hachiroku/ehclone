from contextlib import contextmanager

from sqlalchemy import text

from ehclone.db.session import engine


@contextmanager
def application_lock():
    """Serialize CLI invocations sharing a database."""
    # Session-level advisory locks survive commits and release on disconnect.
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        acquired = connection.scalar(text('SELECT pg_try_advisory_lock(23333333)'))
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(text('SELECT pg_advisory_unlock(23333333)'))
