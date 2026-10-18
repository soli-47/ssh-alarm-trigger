import json
import os
import re
import time
import hashlib
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# For syslog-style files (/var/log/secure, /var/log/auth.log)
AUTH_PATTERNS = [
    ("ssh_failed", re.compile(
        r"sshd(?:-session)?(?:\[\d+\])?: Failed password for "
    )),
    ("ssh_success", re.compile(
        r"sshd(?:-session)?(?:\[\d+\])?: Accepted "
        r"(?:password|publickey|keyboard-interactive) for "
    )),
]

# For auditd (/var/log/audit/audit.log).
# exe is matched generically: /usr/sbin/sshd, /usr/libexec/openssh/sshd-session, ...
_AUDIT_BASE = (
    r'type=USER_AUTH\b.*?op=PAM:authentication\b.*?'
    r'exe="[^"]*/sshd(?:-session)?".*?'
)
AUDIT_PATTERNS = [
    ("ssh_failed", re.compile(_AUDIT_BASE + r"res=failed\b")),
    ("ssh_success", re.compile(_AUDIT_BASE + r"res=success\b")),
]

def is_audit_path(path):
    return "audit" in str(path).lower()


def is_usable(path):
    """File exists and the current user can actually read it."""
    return os.path.isfile(path) and os.access(path, os.R_OK)


def build_sources(log_paths, logger):
    """
    Decide which patterns each path uses, based on what is REALLY available
    on this machine (not only on what the config lists):

      - audit file(s) exist and are readable -> audit patterns, audit only
        (auth paths are ignored to avoid duplicate alerts for one login)
      - no usable audit file                 -> auth patterns on auth paths
    Returns a list of (path, patterns, label).
    """
    audit_cfg = [p for p in log_paths if is_audit_path(p)]
    auth_cfg = [p for p in log_paths if not is_audit_path(p)]

    audit_ok = [p for p in audit_cfg if is_usable(p)]

    for p in audit_cfg:
        if p not in audit_ok:
            reason = "not found" if not os.path.exists(p) else "not readable (run as root?)"
            logger.warning("Audit log configured but %s: %s", reason, p)

    if audit_ok:
        if auth_cfg:
            logger.warning(
                "Audit log is available -> using audit only. Ignored: %s",
                ", ".join(auth_cfg),
            )
        return [(p, AUDIT_PATTERNS, "audit") for p in audit_ok]

    if audit_cfg:
        logger.warning("No usable audit log -> falling back to auth log(s)")

    return [(p, AUTH_PATTERNS, "auth") for p in auth_cfg]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def setup_logger(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(BASE_DIR / log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("collector")


def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as fh:
        config = json.load(fh)

    if not config.get("log_paths"):
        raise ValueError("log_paths cannot be empty")

    config["poll_interval"] = float(config.get("poll_interval", 3))
    if config["poll_interval"] <= 0:
        raise ValueError("poll_interval must be greater than zero")

    config["start_at_end"] = bool(config.get("start_at_end", True))
    # True: ignore everything written while the service was down / before start
    config["skip_backlog_on_start"] = bool(config.get("skip_backlog_on_start", True))
    return config


def connect(db_path):
    db = sqlite3.connect(db_path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")  # collector and sender share the DB
    return db


def init_db(db_path):
    with closing(connect(db_path)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_hash TEXT UNIQUE NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS file_offsets (
                path TEXT PRIMARY KEY,
                offset INTEGER NOT NULL
            )
        """)
        db.commit()


def skip_backlog(db_path, sources, logger):
    """Move every offset to the current end of file: only new lines count."""
    with closing(connect(db_path)) as db:
        for path, _, _ in sources:
            try:
                size = Path(path).stat().st_size
            except OSError:
                db.execute("DELETE FROM file_offsets WHERE path = ?", (str(path),))
                continue
            db.execute("""
                INSERT INTO file_offsets (path, offset) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET offset = excluded.offset
            """, (str(path), size))
        db.commit()
    logger.info("Backlog skipped: starting from the end of all log files")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def save_event(db, event_type, source, message):
    event_hash = hashlib.sha256(
        f"{source}\0{message}".encode("utf-8", errors="replace")
    ).hexdigest()
    occurred_at = datetime.now().astimezone().isoformat()

    cursor = db.execute("""
        INSERT OR IGNORE INTO events
        (event_hash, occurred_at, event_type, status, source, message)
        VALUES (?, ?, ?, 'pending', ?, ?)
    """, (event_hash, occurred_at, event_type, source, message))
    return cursor.rowcount == 1


def match_line(line, patterns):
    for event_type, pattern in patterns:
        if pattern.search(line):
            return event_type
    return None


def read_file(path, patterns, db_path, start_at_end, logger):
    try:
        with open(path, "rb") as file, closing(connect(db_path)) as db:
            row = db.execute(
                "SELECT offset FROM file_offsets WHERE path = ?", (str(path),)
            ).fetchone()

            current_size = Path(path).stat().st_size
            offset = row[0] if row else (current_size if start_at_end else 0)

            if current_size < offset:
                offset = 0
                logger.warning("Log file truncated/rotated: %s", path)

            file.seek(offset)

            while True:
                raw_line = file.readline()
                # Stop at EOF or at a half-written line; it is re-read next cycle
                if not raw_line or not raw_line.endswith(b"\n"):
                    break

                offset = file.tell()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                event_type = match_line(line, patterns)
                if event_type and save_event(db, event_type, str(path), line):
                    logger.info("New event %s | %s", event_type, line)

            # Events and offset are committed together (atomic)
            db.execute("""
                INSERT INTO file_offsets (path, offset) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET offset = excluded.offset
            """, (str(path), offset))
            db.commit()

    except FileNotFoundError:
        logger.warning("Log file not found: %s", path)
    except PermissionError:
        logger.error("Permission denied: %s", path)
    except OSError:
        logger.exception("Could not read log file: %s", path)
    except sqlite3.Error:
        logger.exception("Database error while reading: %s", path)


def main():
    config = load_config()
    logger = setup_logger(config.get("log_file", "collector.log"))
    db_path = BASE_DIR / config.get("database", "collector.db")

    init_db(db_path)
    sources = build_sources(config["log_paths"], logger)
    if not sources:
        raise ValueError("No usable log source found (check log_paths in config.json)")

    for path, _, label in sources:
        logger.info("Watching %s [%s patterns]", path, label)

    if config["skip_backlog_on_start"]:
        skip_backlog(db_path, sources, logger)

    logger.info("Linux Log Collector started")

    while True:
        for path, patterns, _ in sources:
            read_file(path, patterns, db_path, config["start_at_end"], logger)
        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCollector stopped")
    except Exception:
        logging.exception("Collector stopped due to an unexpected error")
        raise