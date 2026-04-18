from __future__ import annotations

import email
import html
import imaplib
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime, make_msgid, parsedate_to_datetime

from app.core.config import settings

# ---------------------------------------------------------------------------
# Plain-text → HTML converter
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)

# Heuristic: signature block starts after a line that is exactly "Pagarbiai,"
# or the name "Jevgeni Reinas" appears standalone, or after a separator like
# "-- ".  We split on the first matching boundary.
_SIG_MARKERS = re.compile(
    r"^(Pagarbiai,|With best regards,|Best regards,|--\s*)$",
    re.MULTILINE | re.IGNORECASE,
)


def _linkify(text: str) -> str:
    """Replace bare URLs in already-escaped text with <a href> tags."""
    return _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" style="color:#1a73e8;">{m.group(1)}</a>',
        text,
    )


def _text_to_html(plain: str) -> str:
    """Convert plain-text email body to an HTML equivalent.

    * Escapes all HTML entities.
    * Converts blank-line-separated paragraphs to <p> blocks.
    * Detects the signature block and renders it smaller + italic.
    * Makes all URLs clickable.
    """
    # Split body from signature
    sig_match = _SIG_MARKERS.search(plain)
    if sig_match:
        body_part = plain[: sig_match.start()]
        sig_part = plain[sig_match.start() :]
    else:
        body_part = plain
        sig_part = ""

    def paragraphs_html(text: str, style: str = "") -> str:
        paras = re.split(r"\n{2,}", text.strip())
        parts = []
        for p in paras:
            if not p.strip():
                continue
            escaped = html.escape(p.strip())
            linked = _linkify(escaped)
            # Convert single newlines within a paragraph to <br>
            linked = linked.replace("\n", "<br>\n")
            tag = f'<p style="margin:0 0 12px 0;{style}">{linked}</p>'
            parts.append(tag)
        return "\n".join(parts)

    body_html = paragraphs_html(body_part)

    sig_html = ""
    if sig_part:
        sig_html = (
            '<div style="margin-top:24px;border-top:1px solid #e0e0e0;padding-top:12px;">\n'
            + paragraphs_html(
                sig_part,
                style="font-size:0.85em;font-style:italic;color:#555555;margin:0 0 4px 0;",
            )
            + "\n</div>"
        )

    return f"""\
<!DOCTYPE html>
<html lang="lt">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#222222;max-width:680px;margin:0 auto;padding:20px;">
{body_html}
{sig_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Send helper
# ---------------------------------------------------------------------------


def send_zone_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    from_email: str | None = None,
    dkim_selector: str | None = None,
    dkim_domain: str | None = None,
    dkim_private_key_pem: bytes | None = None,
) -> str:
    actual_from = from_email or settings.zone_email
    message_id = make_msgid(domain=actual_from.split("@")[-1])

    msg = MIMEMultipart("alternative")
    msg["From"] = actual_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(_text_to_html(body), "html", "utf-8"))

    raw_message = msg.as_bytes()
    if dkim_selector and dkim_domain and dkim_private_key_pem:
        import dkim  # noqa: PLC0415 — lazy import to avoid hard dependency at module load
        raw_message = dkim.sign(
            raw_message,
            selector=dkim_selector.encode("ascii"),
            domain=dkim_domain.encode("ascii"),
            privkey=dkim_private_key_pem,
            include_headers=[b"from", b"to", b"subject", b"date", b"message-id", b"in-reply-to", b"references"],
        ) + raw_message

    smtp = smtplib.SMTP(settings.zone_smtp_host, settings.zone_smtp_port, timeout=20)
    try:
        smtp.ehlo()
        if settings.zone_use_starttls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(settings.zone_email, settings.zone_password)
        smtp.sendmail(actual_from, [to_email], raw_message)
    finally:
        smtp.quit()

    return message_id


def _parse_plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return ""

    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")


def fetch_unseen_zone_messages(limit: int = 30) -> list[dict]:
    imap = imaplib.IMAP4_SSL(settings.zone_imap_host, settings.zone_imap_port)
    items: list[dict] = []
    try:
        imap.login(settings.zone_email, settings.zone_password)
        imap.select("INBOX")
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            return []

        ids = (data[0] or b"").split()
        if not ids:
            return []
        ids = ids[-limit:]

        for raw_id in ids:
            status, payload = imap.fetch(raw_id, "(RFC822)")
            if status != "OK" or not payload:
                continue
            raw_email = payload[0][1]
            msg = email.message_from_bytes(raw_email)

            date_raw = msg.get("Date")
            parsed_date = None
            if date_raw:
                try:
                    parsed_date = parsedate_to_datetime(date_raw)
                except Exception:
                    parsed_date = None

            has_attachments = False
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_disposition() == "attachment":
                        has_attachments = True
                        break

            items.append(
                {
                    "imap_id": raw_id.decode(errors="ignore"),
                    "from": msg.get("From", ""),
                    "subject": msg.get("Subject", ""),
                    "body": _parse_plain_body(msg),
                    "date": parsed_date,
                    "message_id": msg.get("Message-ID"),
                    "in_reply_to": msg.get("In-Reply-To"),
                    "references": msg.get("References"),
                    "has_attachments": has_attachments,
                }
            )

            imap.store(raw_id, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()

    return items
