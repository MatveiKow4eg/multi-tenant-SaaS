from __future__ import annotations

from app.services.mail.zone_operator import send_zone_email


def send_verification_email(*, to_email: str, verify_url: str) -> str:
    subject = "Confirm your email address"
    body = (
        "Hello,\n\n"
        "Please confirm your email address by clicking the link below:\n"
        f"{verify_url}\n\n"
        "This link expires in 72 hours.\n\n"
        "If you did not register, you can safely ignore this email."
    )
    return send_zone_email(to_email=to_email, subject=subject, body=body)


def send_password_reset_email(*, to_email: str, reset_url: str) -> str:
    subject = "Reset your password"
    body = (
        "Hello,\n\n"
        "We received a request to reset your password. Click the link below:\n"
        f"{reset_url}\n\n"
        "This link expires in 2 hours.\n\n"
        "If you did not request a password reset, you can safely ignore this email."
    )
    return send_zone_email(to_email=to_email, subject=subject, body=body)
