import imaplib
import smtplib
from dataclasses import dataclass

from app.core.config import settings


@dataclass
class ConnectivityResult:
    smtp_ok: bool
    imap_ok: bool


def check_zone_connectivity() -> ConnectivityResult:
    smtp_ok = False
    imap_ok = False

    smtp = smtplib.SMTP(settings.zone_smtp_host, settings.zone_smtp_port, timeout=15)
    try:
        smtp.ehlo()
        if settings.zone_use_starttls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(settings.zone_email, settings.zone_password)
        smtp_ok = True
    finally:
        smtp.quit()

    imap = imaplib.IMAP4_SSL(settings.zone_imap_host, settings.zone_imap_port)
    try:
        imap.login(settings.zone_email, settings.zone_password)
        imap_ok = True
    finally:
        imap.logout()

    return ConnectivityResult(smtp_ok=smtp_ok, imap_ok=imap_ok)
