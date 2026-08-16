import logging

from loguru import logger as _log

from app.constants import LOG_PATH


def setup_logger(with_debug: bool = False) -> None:
    """
    Register the file sinks.

    Call this EXACTLY once, from app.main. A second call adds a second set of
    sinks and every line is then written twice — which is what happened while
    a duplicate setup_logger lived in app/core/config.py alongside this one.
    """
    log_path = LOG_PATH

    # App Info Log
    _log.add(
        f"{log_path}/app.log",
        rotation="1 day",
        compression="zip",
        filter=lambda record: record["extra"].get("context") not in {"system", "comm", "trade"},
        level="INFO",
    )

    # App Error logs
    _log.add(
        f"{log_path}/error.log",
        rotation="1 day",
        compression="zip",
        filter=lambda record: record["extra"].get("context") not in {"system", "comm", "trade"}
        and record["level"].name in {"ERROR", "CRITICAL"},
        level="DEBUG" if with_debug else "ERROR",
    )

    # System logs (system)
    _log.add(
        f"{log_path}/system.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "system",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Communication logs (communication)
    _log.add(
        f"{log_path}/comm.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "comm",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Background Job logs (communication)
    _log.add(
        f"{log_path}/job.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "job",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )

    # Background Job logs (communication)
    _log.add(
        f"{log_path}/external.log",
        rotation="1 day",
        compression="zip",
        level="DEBUG" if with_debug else "INFO",
        filter=lambda record: record["extra"].get("context") == "external",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
    )


# slogger is for System Logger
slogger = _log.bind(context="system")

# clogger is for Communication Logger
clogger = _log.bind(context="comm")

# clogger is for Communication Logger
jlogger = _log.bind(context="job")

# clogger is for Communication Logger
elogger = _log.bind(context="external")

# Generic Logger
logger = _log


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        logger_opt = logger.opt(depth=6, exception=record.exc_info if record.exc_info else None)
        logger_opt.log(record.levelname, record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO)
