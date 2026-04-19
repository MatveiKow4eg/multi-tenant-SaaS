from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.sender_domain import SenderDomain


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_onboarding_email_ownership_requires_link_click_before_step3(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    sent_email: dict[str, str] = {}

    def _fake_send_domain_ownership_verification_email(*, to_email: str, domain: str, workspace_name: str, verification_url: str) -> str:
        sent_email["to_email"] = to_email
        sent_email["domain"] = domain
        sent_email["workspace_name"] = workspace_name
        sent_email["verification_url"] = verification_url
        return "msg-domain-verify-1"

    monkeypatch.setattr(
        "app.ui.routes.send_domain_ownership_verification_email",
        _fake_send_domain_ownership_verification_email,
    )

    try:
        reg_owner = client.post(
            "/api/auth/register",
            json={
                "email": "owner@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Owner Team",
            },
        )
        assert reg_owner.status_code == 200
        token = reg_owner.json()["token"]
        tenant_id = reg_owner.json()["tenant_id"]

        client.cookies.set("auth_session", token)

        onboarding = client.get("/ui/onboarding/start", follow_redirects=False)
        assert onboarding.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        step1 = client.post(
            "/ui/onboarding/start",
            data={
                "step": "1",
                "action": "next",
                "workspace_name": "Owner Team",
                "company_website": "https://example.com",
                "company_name": "Example",
                "sender_name": "Owner",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert step1.status_code == 200
        assert step1.json()["success"] is True

        sender_domain = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id).first()
        assert sender_domain is not None

        sender_domain.spf_status = "verified"
        sender_domain.dkim_status = "verified"
        sender_domain.dmarc_status = "verified"
        db.add(sender_domain)
        db.commit()

        request_verify = client.post(
            "/ui/onboarding/start",
            data={
                "step": "2",
                "action": "verify_ownership_email",
                "domain_id": str(sender_domain.id),
                "ownership_email": "admin@example.com",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert request_verify.status_code == 200
        verify_payload = request_verify.json()
        assert verify_payload["success"] is True
        assert verify_payload["reload"] is True
        assert sent_email["to_email"] == "admin@example.com"

        db.refresh(sender_domain)
        assert sender_domain.ownership_email_status == "pending"
        assert sender_domain.ownership_status == "pending"

        blocked_next = client.post(
            "/ui/onboarding/start",
            data={
                "step": "2",
                "action": "next",
                "domain_id": str(sender_domain.id),
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert blocked_next.status_code == 200
        blocked_payload = blocked_next.json()
        assert blocked_payload["success"] is False
        assert "Domain setup is not complete yet" in blocked_payload["error"]

        verification_path = urlsplit(sent_email["verification_url"])
        click_verify = client.get(f"{verification_path.path}?{verification_path.query}", follow_redirects=False)
        assert click_verify.status_code == 303
        assert click_verify.headers.get("location", "").startswith("/ui/onboarding/start?step=2")

        db.refresh(sender_domain)
        assert sender_domain.ownership_email_status == "verified"
        assert sender_domain.ownership_status == "verified"
        assert sender_domain.ownership_verified_via == "email"

        complete_step2 = client.post(
            "/ui/onboarding/start",
            data={
                "step": "2",
                "action": "next",
                "domain_id": str(sender_domain.id),
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert complete_step2.status_code == 200
        done_payload = complete_step2.json()
        assert done_payload["success"] is True
        assert done_payload["next_step"] == 3
    finally:
        app.dependency_overrides.clear()
        db.close()
