import json
import os
import smtplib
import socket
import sqlite3
import ssl
import time
import logging
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

from templates import build_message

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "sender_config.json"

# Whitelist: column names are never taken from outside
CHANNEL_COLUMNS = {"bale": "bale_sent", "email": "email_sent"}


# ---------------------------------------------------------------------------
# Config / logger
# ---------------------------------------------------------------------------
def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as fh:
        config = json.load(fh)

    for key in ("database", "bot_api_base", "chat_id"):
        if not config.get(key):
            raise ValueError(f"Missing configuration: {key}")

    if not config["bot_api_base"].startswith("https://"):
        raise ValueError("HTTPS endpoint is required")

    config["poll_interval"] = float(config.get("poll_interval", 5))
    config["batch_size"] = int(config.get("batch_size", 10))
    config["timeout"] = float(config.get("timeout", 10))

    if config["poll_interval"] <= 0:
        raise ValueError("poll_interval must be positive")
    if config["batch_size"] < 1:
        raise ValueError("batch_size must be positive")
    if config["timeout"] <= 0:
        raise ValueError("timeout must be positive")

    email = config.setdefault("email", {})
    email.setdefault("enabled", False)
    if email["enabled"]:
        for key in ("smtp_host", "sender", "recipient"):
            if not email.get(key):
                raise ValueError(f"Missing email configuration: {key}")
        email["smtp_port"] = int(email.get("smtp_port", 25))
        email.setdefault("use_tls", True)
        # Prefer an environment variable over a password stored in the file
        email["password"] = os.environ.get("SMTP_PASSWORD", email.get("password", ""))
        email.setdefault("username", email["sender"])

    return config


def setup_logger():
    logger = logging.getLogger("sender")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        file_handler = logging.FileHandler(BASE_DIR / "sender.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(console)
        logger.addHandler(file_handler)
    return logger


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def connect(db_path):
    db = sqlite3.connect(db_path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def migrate_db(db_path):
    """Add per-channel delivery flags (so a retry never re-sends to a channel
    that already succeeded)."""
    with closing(connect(db_path)) as db:
        existing = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        for column in CHANNEL_COLUMNS.values():
            if column not in existing:
                db.execute(
                    f"ALTER TABLE events ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
        db.commit()


def skip_old_events(db_path, started_at, logger):
    """Everything still 'pending' that happened before this process started is
    marked 'skipped' and will never be sent."""
    with closing(connect(db_path)) as db:
        cursor = db.execute(
            """
            UPDATE events
            SET status = 'skipped'
            WHERE status = 'pending'
              AND julianday(occurred_at) < julianday(?)
            """,
            (started_at.isoformat(),),
        )
        db.commit()
        logger.info("Skipped %d old pending events (before %s)",
                    cursor.rowcount, started_at.isoformat())


def get_pending_events(db_path, batch_size, started_at):
    with closing(connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT id, event_hash, occurred_at, event_type, source, message,
                   bale_sent, email_sent
            FROM events
            WHERE status = 'pending'
              AND julianday(occurred_at) >= julianday(?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (started_at.isoformat(), batch_size),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_channel(db_path, event_id, channel):
    column = CHANNEL_COLUMNS[channel]
    with closing(connect(db_path)) as db:
        db.execute(f"UPDATE events SET {column} = 1 WHERE id = ?", (event_id,))
        db.commit()


def mark_sent(db_path, event_id):
    with closing(connect(db_path)) as db:
        cursor = db.execute(
            "UPDATE events SET status = 'sent' WHERE id = ? AND status = 'pending'",
            (event_id,),
        )
        db.commit()
        return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
def send_bale(config, text):
    endpoint = config["bot_api_base"].rstrip("/") + "/sendMessage"
    payload = json.dumps(
        {"chat_id": config["chat_id"], "text": text}, ensure_ascii=False
    ).encode("utf-8")

    request = Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "LinuxLogCollector/1.0",
        },
        method="POST",
    )

    # urlopen already raises HTTPError for non-2xx responses
    with urlopen(request, timeout=config["timeout"]) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"Bale API did not confirm delivery: {result}")


def send_email(email_cfg, message):
    msg = EmailMessage()
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["recipient"]
    msg["Subject"] = message.subject
    msg.set_content(message.text)                      # plain-text fallback
    msg.add_alternative(message.html, subtype="html")  # styled version

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=15) as smtp:
        smtp.ehlo()
        if email_cfg["use_tls"]:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if email_cfg["username"] and email_cfg["password"]:
            smtp.login(email_cfg["username"], email_cfg["password"])
        smtp.send_message(msg)


def get_server_ip(config):
    """config["server_ip"] wins; otherwise ask the kernel which local address
    would be used for outbound traffic (no packet is actually sent)."""
    if config.get("server_ip"):
        return config["server_ip"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "Unknown"


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def process_events(config, db_path, started_at, server_ip, logger):
    events = get_pending_events(db_path, config["batch_size"], started_at)

    channels = {"bale": lambda m: send_bale(config, m.text)}
    if config["email"]["enabled"]:
        channels["email"] = lambda m: send_email(config["email"], m)

    for event in events:
        message = build_message(event, server_ip, config.get("display_timezone"))
        all_done = True

        for name, send in channels.items():
            if event[CHANNEL_COLUMNS[name]]:
                continue  # already delivered on an earlier attempt
            try:
                send(message)
                mark_channel(db_path, event["id"], name)
                logger.info("Delivered | channel=%s | id=%s | type=%s",
                            name, event["id"], event["event_type"])
            except Exception:
                all_done = False
                logger.exception("Delivery failed | channel=%s | id=%s",
                                 name, event["id"])

        if all_done and not mark_sent(db_path, event["id"]):
            logger.warning("Delivered but status unchanged | id=%s", event["id"])


def main():
    config = load_config()
    logger = setup_logger()
    db_path = BASE_DIR / config["database"]

    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    started_at = datetime.now().astimezone()
    server_ip = get_server_ip(config)

    migrate_db(db_path)
    skip_old_events(db_path, started_at, logger)
    logger.info("Sender started at %s", started_at.isoformat())

    while True:
        try:
            process_events(config, db_path, started_at, server_ip, logger)
        except sqlite3.Error:
            logger.exception("Could not access database")
        except Exception:
            logger.exception("Processing cycle failed")
        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        setup_logger().info("Sender stopped by user")
    except Exception:
        setup_logger().exception("Sender terminated")
        raise