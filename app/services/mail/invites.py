from __future__ import annotations

from app.services.mail.zone_operator import send_zone_email


def send_invite_email(*, to_email: str, invite_url: str, tenant_name: str, role: str) -> str:
    subject = f"Invite to {tenant_name} workspace"
    body = (
        "Hello,\n\n"
        f"You were invited to join tenant '{tenant_name}' with role '{role}'.\n"
        f"Open this link to accept the invite and set your password:\n{invite_url}\n\n"
        "If you did not expect this invite, you can ignore this email."
    )
    return send_zone_email(to_email=to_email, subject=subject, body=body)
