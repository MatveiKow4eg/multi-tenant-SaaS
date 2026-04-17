from fastapi import APIRouter

from app.services.mail.zone_mail import check_zone_connectivity

router = APIRouter()


@router.get("/")
def health_check() -> dict:
    return {"status": "ok"}


@router.get("/mail")
def mail_health_check() -> dict:
    result = check_zone_connectivity()
    return {"smtp_ok": result.smtp_ok, "imap_ok": result.imap_ok}
