import sys
import logging
from loguru import logger

from ehclone.config import config


class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


logger.remove()

logger.add(
    sys.stdout,
    format='<green>{time:YYYY-MM-DD HH:mm:ss}</green> | '
           '<level>{level: <8}</level> | '
           '<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - '
           '<level>{message}</level>',
    level=config.log.console_level,
    colorize=True,
    enqueue=True,
)


if config.log.dir:
    config.log.dir.mkdir(exist_ok=True)
    logger.add(
        config.log.dir / '{time:YYYY-MM-DD}.log',
        format='{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}',
        level=config.log.file_level,
        rotation='1 day',
        retention='14 days',
        encoding='utf-8',
        enqueue=True,
    )
