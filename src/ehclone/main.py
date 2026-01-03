from ehclone.config import config
from ehclone.logger import logger

from ehclone.core.sync_index import sync_index

from ehclone.db.session import engine
from ehclone.db.entities import Base
from sqlalchemy_utils import database_exists, create_database


if __name__ == '__main__':
    if not database_exists(engine.url):
        logger.info('Database does not exist, creating...')
        create_database(engine.url)
    else:
        Base.metadata.create_all(engine)

    sync_index()
