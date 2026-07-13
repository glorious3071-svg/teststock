"""Shared database utilities for teststock."""

from db.connection import get_connection, mysql_config

__all__ = ["get_connection", "mysql_config"]
