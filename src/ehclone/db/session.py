from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ehclone.config import config


engine = create_engine(
    url=config.db.url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={'options': '-c timezone=UTC'},
)


Session = sessionmaker(bind=engine, expire_on_commit=False)

@contextmanager
def session_generator():
    with Session() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
