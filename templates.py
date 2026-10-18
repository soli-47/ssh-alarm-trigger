"""Alert message templates: parse a raw SSH log line and render it as
plain text (Bale / email fallback) and as a responsive HTML email."""

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
AUTH_RE = re.compile(
    r"(?P<result>Accepted|Failed) (?P<method>[\w-]+) for "
    r"(?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port (?P<port>\d+)"
)
HOST_RE = re.compile(r"\s(?P<host>\S+)\s+sshd(?:-session)?(?:\[\d+\])?:")
AUDIT_TIME_RE = re.compile(r"msg=audit\((?P<epoch>\d+(?:\.\d+)?):")
KV_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


@dataclass
class AlertInfo:
    event_type: str
    user: str = "unknown"
    ip: str = "unknown"
    port: str = ""
    method: str = ""
    host: str = ""
    when: datetime = None
    raw: str = ""
    event_id: str = ""


def _parse_log_time(line):
    """Real time of the log line if we can read it, else None."""
    first = line.split(" ", 1)[0]
    try:
        dt = datetime.fromisoformat(first)  # rsyslog ISO format
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        pass
    m = AUDIT_TIME_RE.search(line)
    if m:
        return datetime.fromtimestamp(float(m.group("epoch")), tz=timezone.utc)
    return None


def parse_event(event):
    line = event["message"]
    info = AlertInfo(
        event_type=event["event_type"],
        raw=line,
        event_id=event["event_hash"][:8],
    )

    m = AUTH_RE.search(line)
    if m:
        info.user = m.group("user")
        info.ip = m.group("ip")
        info.port = m.group("port")
        info.method = m.group("method")
    elif "type=USER_" in line:  # auditd record
        kv = {k: v.strip('"') for k, v in KV_RE.findall(line)}
        info.user = kv.get("acct", "unknown")
        info.ip = kv.get("addr", "unknown")
        info.method = kv.get("terminal", "")

    h = HOST_RE.search(line)
    if h:
        info.host = h.group("host")

    when = _parse_log_time(line)
    if when is None:
        try:
            when = datetime.fromisoformat(event["occurred_at"])
        except ValueError:
            when = datetime.now().astimezone()
    info.when = when
    return info


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
STYLES = {
    "ssh_success": {
        "emoji": "🟢", "title": "Successful SSH login",
        "color": "#16a34a", "soft": "#dcfce7",
    },
    "ssh_failed": {
        "emoji": "🔴", "title": "Failed SSH login attempt",
        "color": "#dc2626", "soft": "#fee2e2",
    },
}
DEFAULT_STYLE = {
    "emoji": "⚪", "title": "Security event",
    "color": "#475569", "soft": "#e2e8f0",
}


def format_time(dt, tz_name=None):
    if tz_name and ZoneInfo:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.strftime("%d %b %Y · %H:%M:%S ") + (dt.tzname() or "")


@dataclass
class Message:
    subject: str
    text: str
    html: str


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def _render_text(info, style, when_str, server):
    where = f"{info.ip}:{info.port}" if info.port else info.ip
    lines = [
        f"{style['emoji']} {style['title']}",
        "",
        f"👤 User: {info.user}",
        f"🌐 From: {where}",
    ]
    if info.method:
        lines.append(f"🔑 Method: {info.method}")
    lines += [
        f"🖥 Server: {server}",
        f"🕒 {when_str}",
        "",
        f"#{info.event_id}",
    ]
    return "\n".join(lines)


def _row(label, value, mono=False):
    font = "font-family:Menlo,Consolas,monospace;" if mono else ""
    return (
        '<tr>'
        f'<td style="padding:10px 0;color:#64748b;font-size:13px;width:110px;'
        f'border-bottom:1px solid #f1f5f9;">{label}</td>'
        f'<td style="padding:10px 0;color:#0f172a;font-size:15px;font-weight:600;'
        f'border-bottom:1px solid #f1f5f9;{font}">{value}</td>'
        '</tr>'
    )


def _render_html(info, style, when_str, server):
    esc = html.escape
    where = f"{info.ip}:{info.port}" if info.port else info.ip

    rows = [
        _row("User", esc(info.user), mono=True),
        _row("Source IP", esc(where), mono=True),
    ]
    if info.method:
        rows.append(_row("Method", esc(info.method)))
    rows.append(_row("Server", esc(server)))
    rows.append(_row("Time", esc(when_str)))

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
  {esc(style['title'])}: {esc(info.user)} from {esc(info.ip)}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f1f5f9;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;
                font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                box-shadow:0 1px 3px rgba(15,23,42,.08);">
    <tr><td style="background:{style['color']};height:6px;font-size:0;line-height:0;">&nbsp;</td></tr>
    <tr><td style="padding:28px 28px 8px 28px;">
      <div style="display:inline-block;background:{style['soft']};color:{style['color']};
                  font-size:12px;font-weight:700;letter-spacing:.06em;
                  text-transform:uppercase;padding:5px 10px;border-radius:999px;">
        {style['emoji']}&nbsp; Security Alert
      </div>
      <h1 style="margin:14px 0 4px 0;font-size:22px;line-height:1.3;color:#0f172a;">
        {esc(style['title'])}
      </h1>
      <p style="margin:0;color:#64748b;font-size:14px;">
        Detected on <strong style="color:#334155;">{esc(server)}</strong>
      </p>
    </td></tr>
    <tr><td style="padding:12px 28px 4px 28px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {''.join(rows)}
      </table>
    </td></tr>
    <tr><td style="padding:20px 28px 8px 28px;">
      <div style="font-size:11px;color:#94a3b8;letter-spacing:.06em;
                  text-transform:uppercase;margin-bottom:6px;">Raw log</div>
      <div style="background:#0f172a;color:#e2e8f0;border-radius:8px;padding:12px 14px;
                  font-family:Menlo,Consolas,monospace;font-size:12px;line-height:1.5;
                  word-break:break-all;">{esc(info.raw[:1500])}</div>
    </td></tr>
    <tr><td style="padding:18px 28px 24px 28px;color:#94a3b8;font-size:12px;">
      Event #{esc(info.event_id)} &nbsp;·&nbsp; Linux Log Collector
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def build_message(event, server_ip, tz_name=None):
    info = parse_event(event)
    style = STYLES.get(info.event_type, DEFAULT_STYLE)
    when_str = format_time(info.when, tz_name)

    server = info.host or "server"
    if server_ip and server_ip != "Unknown":
        server = f"{server} ({server_ip})"

    subject = f"{style['emoji']} {style['title']} — {info.user}@{info.host or 'server'}"
    return Message(
        subject=subject,
        text=_render_text(info, style, when_str, server),
        html=_render_html(info, style, when_str, server),
    )