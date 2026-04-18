from __future__ import annotations

from app.services.mail.zone_operator import send_zone_email


def send_domain_ownership_verification_email(*, to_email: str, domain: str, workspace_name: str) -> str:
    subject = f"Verify ownership for {domain}"
    body = (
        "Hello,\n\n"
        "A domain ownership verification request was initiated in Lertisento onboarding.\n\n"
        f"Workspace: {workspace_name}\n"
        f"Domain: {domain}\n"
        f"Verification email: {to_email}\n\n"
        "If this request was made by you, no additional action is required at this moment. "
        "The domain is recorded as verified by email for onboarding flow.\n\n"
        "If you did not request this verification, please contact support immediately."
    )
    return send_zone_email(to_email=to_email, subject=subject, body=body)
