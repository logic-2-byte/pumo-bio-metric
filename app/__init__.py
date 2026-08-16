"""
Biometric capture service.

Talks to eSSL/ZKTeco readers on the branch LAN and writes their punches into
the LMS database. See `app.sync` for how, and why it writes to the database
directly rather than calling the LMS API.
"""

from app.core.logger import clogger, elogger, jlogger, logger, slogger

__all__ = ("clogger", "elogger", "jlogger", "logger", "slogger")
