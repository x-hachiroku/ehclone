def main():
    from time import sleep
    from sqlalchemy import text
    from sqlalchemy_utils import create_database, database_exists

    from ehclone.db.entities import Base
    from ehclone.db.locking import application_lock
    from ehclone.db.session import engine
    from ehclone.logger import logger

    if not database_exists(engine.url):
        logger.info('Database does not exist, creating...')
        create_database(engine.url)
    with application_lock() as acquired:
        if not acquired:
            logger.info('Another ehclone instance is running; skipping this invocation')
            return
        with engine.begin() as connection:
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            Base.metadata.create_all(connection)

        from ehclone.core import sync_index, sync_thumbs, dedupe, download_manager

        while True:
            logger.info("Checking download status...")
            download_manager.run()
            logger.info("Syncing index...")
            sync_index.run()
            logger.info("Syncing thumbs...")
            sync_thumbs.run()
            logger.info("Deduping...")
            dedupe.run()

            logger.info('Pipeline pass finished; waiting 300 seconds before the next pass')
            sleep(300)


if __name__ == '__main__':
    main()
