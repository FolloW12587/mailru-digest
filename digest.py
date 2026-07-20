#!/usr/bin/env python3
"""Дайджест непрочитанных писем mail.ru -> Telegram.

Читает настройки из config.env рядом со скриптом.
Письма НЕ помечаются прочитанными (используется BODY.PEEK).
"""

import email
import email.header
import email.utils
import imaplib
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IMAP_HOST = "imap.mail.ru"
IMAP_PORT = 993
MAX_TELEGRAM_LEN = 4096


def load_config():
    path = Path(__file__).resolve().parent / "config.env"
    if not path.exists():
        sys.exit(f"Нет файла конфигурации: {path}")
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip('"').strip("'")
    required = ["MAIL_USER", "MAIL_PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"В config.env не заполнено: {', '.join(missing)}")
    return cfg


def decode_header(raw):
    """MIME-заголовок (=?utf-8?B?...?=) -> обычная строка."""
    if not raw:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return " ".join("".join(parts).split())


def format_sender(raw):
    """'Иван <i@x.ru>' -> 'Иван'; без имени — оставляем адрес."""
    name, addr = email.utils.parseaddr(decode_header(raw))
    return name or addr or "(без отправителя)"


def format_date(raw):
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d.%m %H:%M")


def fetch_unread(cfg):
    """Возвращает список непрочитанных, новые сверху."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH вернул {status}")
        uids = data[0].split()
        if not uids:
            return [], 0

        limit = int(cfg.get("MAX_MESSAGES", "40"))
        selected = ",".join(uid.decode() for uid in uids[-limit:])
        # PEEK — читаем заголовки, не трогая флаг \Seen
        query = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
        status, data = conn.fetch(selected, query)
        if status != "OK":
            raise RuntimeError(f"IMAP FETCH вернул {status}")

        messages = []
        for item in data:
            if not isinstance(item, tuple):
                continue
            headers = email.message_from_bytes(item[1])
            messages.append(
                {
                    "sender": format_sender(headers.get("From")),
                    "subject": decode_header(headers.get("Subject")) or "(без темы)",
                    "date": format_date(headers.get("Date")),
                }
            )
        messages.reverse()
        return messages, len(uids)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(messages, total):
    today = datetime.now().strftime("%d.%m.%Y")
    if not messages:
        return f"<b>Почта mail.ru — {today}</b>\n\nНепрочитанных нет."

    shown = len(messages)
    head = f"<b>Почта mail.ru — {today}</b>\nНепрочитанных: {total}"
    if shown < total:
        head += f" (показаны последние {shown})"

    lines = [head]
    for i, m in enumerate(messages, 1):
        stamp = f" <i>{escape(m['date'])}</i>" if m["date"] else ""
        lines.append(f"{i}. <b>{escape(m['sender'])}</b>{stamp}\n{escape(m['subject'])}")
    return "\n\n".join(lines)


def split_message(text):
    """Телеграм не принимает >4096 символов — режем по границам писем."""
    if len(text) <= MAX_TELEGRAM_LEN:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > MAX_TELEGRAM_LEN and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['TELEGRAM_TOKEN']}/sendMessage"
    for chunk in split_message(text):
        payload = urllib.parse.urlencode(
            {
                "chat_id": cfg["TELEGRAM_CHAT_ID"],
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        request = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
            if not body.get("ok"):
                raise RuntimeError(f"Telegram: {body}")


def main():
    cfg = load_config()
    try:
        messages, total = fetch_unread(cfg)
    except Exception as exc:
        send_telegram(cfg, f"<b>Дайджест не собрался</b>\n{escape(str(exc))}")
        raise

    if not messages and cfg.get("SEND_IF_EMPTY", "true").lower() != "true":
        print(f"{datetime.now():%Y-%m-%d %H:%M} непрочитанных нет, не отправляю")
        return

    send_telegram(cfg, build_message(messages, total))
    print(f"{datetime.now():%Y-%m-%d %H:%M} отправлено писем: {len(messages)} из {total}")


if __name__ == "__main__":
    main()
