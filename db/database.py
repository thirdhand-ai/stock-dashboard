"""SQLite connection helper."""
import sqlite3
from contextlib import contextmanager

from config.settings import DB_PATH
from db.schema import init_db


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_session():
    conn = get_connection()
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
