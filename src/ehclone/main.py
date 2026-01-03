from sqlalchemy import text
from sqlalchemy_utils import database_exists, create_database

from ehclone.config import config
from ehclone.logger import logger
from ehclone.db.entities import Base
from ehclone.db.session import engine
from ehclone.core.sync_index import sync_index


if __name__ == '__main__':
    if not database_exists(engine.url):
        logger.info('Database does not exist, creating...')
        create_database(engine.url)
    with engine.begin() as connection:
        connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
    Base.metadata.create_all(engine)

    sync_index()
