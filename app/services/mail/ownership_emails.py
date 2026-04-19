from __future__ import annotations

from app.services.mail.zone_operator import send_zone_email


def send_domain_ownership_verification_email(
    *,
    to_email: str,
    domain: str,
    workspace_name: str,
    verification_url: str,
) -> str:
    subject = f"Verify ownership for {domain}"
    body = (
        "Hello,\n\n"
        "A domain ownership verification request was initiated in Lertisento onboarding.\n\n"
        f"Workspace: {workspace_name}\n"
        f"Domain: {domain}\n"
        f"Verification email: {to_email}\n\n"
        "To verify domain ownership by email, click this link:\n"
        f"{verification_url}\n\n"
        "The domain will be marked as email-verified only after this link is opened.\n\n"
        "If you did not request this verification, please contact support immediately."
    )
    return send_zone_email(to_email=to_email, subject=subject, body=body)
