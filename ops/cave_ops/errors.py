"""Exceptions shared by every module."""

from __future__ import annotations


class CaveError(Exception):
    """An expected failure. ``cli.main`` prints the message and exits with status 1.

    Raise it with a message that tells the user what to do, not with a traceback in mind.
    """
