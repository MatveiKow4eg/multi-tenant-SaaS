"""Tests for the production DNS Wizard: key gen, checker, API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.services.dns.checker import check_cname, check_dkim_txt, check_dmarc, check_spf
from app.services.dns.generator import (
    build_dkim_txt_value,
    build_managed_dkim_target,
    build_spf_value,
    decrypt_dkim_private_key,
    generate_dkim_keypair,
)

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

ENGINE = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(bind=ENGINE)
Base.metadata.create_all(ENGINE)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with ENGINE.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


def _register_owner(email="owner@dnstest.com"):
    reg = client.post("/api/auth/register", json={
        "email": email,
        "password": "Passw0rd!",
        "tenant_name": "DNS Test Tenant",
    })
    assert reg.status_code == 200, reg.text
    data = reg.json()
    return data["tenant_id"], data["token"]


# ---------------------------------------------------------------------------
# Unit: DKIM key generation
# ---------------------------------------------------------------------------

def test_generate_dkim_keypair_returns_real_key():
    selector, encrypted_pem, pub_b64 = generate_dkim_keypair("s1")
    assert selector == "s1"
    assert encrypted_pem  # non-empty encrypted blob
    assert pub_b64        # non-empty base64

    # Decrypt and verify it's valid PEM
    pem = decrypt_dkim_private_key(encrypted_pem)
    assert pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----") or b"PRIVATE KEY" in pem


def test_build_dkim_txt_value_real():
    _, _, pub_b64 = generate_dkim_keypair("s1")
    txt = build_dkim_txt_value(pub_b64)
    assert txt.startswith("v=DKIM1; k=rsa; p=")
    assert pub_b64 in txt
    assert "<" not in txt  # no placeholders


def test_build_spf_value_no_placeholder():
    spf = build_spf_value()
    assert spf.startswith("v=spf1")
    assert "include:" in spf
    assert "<" not in spf


# ---------------------------------------------------------------------------
# Unit: DNS checker
# ---------------------------------------------------------------------------

def test_check_spf_verified():
    with patch("app.services.dns.checker._resolve_txt") as mock:
        mock.return_value = ["v=spf1 include:zonemx.eu ~all"]
        result = check_spf("example.com", "v=spf1 include:zonemx.eu ~all")
    assert result.status == "verified"


def test_check_spf_missing():
    with patch("app.services.dns.checker._resolve_txt") as mock:
        mock.return_value = []
        result = check_spf("example.com", "v=spf1 include:zonemx.eu ~all")
    assert result.status == "missing"


def test_check_spf_mismatch():
    with patch("app.services.dns.checker._resolve_txt") as mock:
        mock.return_value = ["v=spf1 include:other.eu ~all"]
        result = check_spf("example.com", "v=spf1 include:zonemx.eu ~all")
    assert result.status == "mismatch"
    assert result.actual_value


def test_check_dkim_txt_verified():
    _, _, pub_b64 = generate_dkim_keypair("s1")
    expected = build_dkim_txt_value(pub_b64)
    with patch("app.services.dns.checker._resolve_txt") as mock:
        mock.return_value = [expected]
        result = check_dkim_txt("example.com", "s1", expected)
    assert result.status == "verified"


def test_check_dkim_cname_verified():
    expected = build_managed_dkim_target(sender_domain_id=12, selector="s1")
    with patch("app.services.dns.checker._resolve_cname") as mock:
        mock.return_value = [expected]
        result = check_cname("s1._domainkey.example.com", expected)
    assert result.status == "verified"


def test_check_dmarc_verified():
    with patch("app.services.dns.checker._resolve_txt") as mock:
        mock.return_value = ["v=DMARC1; p=none; rua=mailto:dmarc@example.com"]
        result = check_dmarc("example.com", "v=DMARC1; p=none")
    assert result.status == "verified"


# ---------------------------------------------------------------------------
# API: domain lifecycle
# ---------------------------------------------------------------------------

def test_add_domain_generates_real_records():
    tenant_id, token = _register_owner("api1@dnstest.com")
    r = client.post(
        "/api/sender-domains/",
        json={"domain": "example.com"},
        headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["domain"] == "example.com"
    assert data["status"] == "pending"
    assert data["dkim_mode"] == "cname"
    assert data["send_enabled"] is False
    assert data["is_default"] is True
    assert data["send_from_email"] == "hello@example.com"
    assert len(data["dns_records"]) == 4

    purposes = [rec["purpose"] for rec in data["dns_records"]]
    assert purposes.count("spf") == 1
    assert purposes.count("dkim") == 2
    assert purposes.count("dmarc") == 1

    dkim_records = [r for r in data["dns_records"] if r["purpose"] == "dkim"]
    assert all(r["record_type"] == "CNAME" for r in dkim_records)
    assert all(r["value"].endswith(".dkim.lertisento.com") for r in dkim_records)
    assert all("<" not in r["value"] for r in dkim_records)


def test_domain_duplicate_rejected():
    tenant_id, token = _register_owner("api2@dnstest.com")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}
    client.post("/api/sender-domains/", json={"domain": "dup.com"}, headers=headers)
    r = client.post("/api/sender-domains/", json={"domain": "dup.com"}, headers=headers)
    assert r.status_code == 409


def test_check_dns_updates_statuses():
    tenant_id, token = _register_owner("api3@dnstest.com")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}

    # Add domain
    r = client.post("/api/sender-domains/", json={"domain": "checktest.com"}, headers=headers)
    domain_id = r.json()["id"]

    # Mock DNS to return all records as passing
    db = TestSession()
    from app.models.sender_domain import SenderDomain
    sd = db.query(SenderDomain).filter(SenderDomain.id == domain_id).first()
    spf_val = next(rec.value for rec in sd.dns_records if rec.purpose == "spf")
    dmarc_val = next(rec.value for rec in sd.dns_records if rec.purpose == "dmarc")
    dkim_records = [rec for rec in sd.dns_records if rec.purpose == "dkim"]
    dkim_targets = {f"{rec.host}.checktest.com": rec.value for rec in dkim_records}
    db.close()

    def mock_resolve_txt(name):
        if name == "checktest.com":
            return [spf_val]
        if name == "_dmarc.checktest.com":
            return [dmarc_val]
        return []

    def mock_resolve_cname(name):
        return [dkim_targets[name]] if name in dkim_targets else []

    with patch("app.services.dns.checker._resolve_txt", side_effect=mock_resolve_txt), patch("app.services.dns.checker._resolve_cname", side_effect=mock_resolve_cname):
        r = client.post(f"/api/sender-domains/{domain_id}/check-dns", headers=headers)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["spf_status"] == "verified"
    assert data["dkim_status"] == "verified"
    assert data["dmarc_status"] == "verified"
    assert data["status"] == "verified"
    assert data["send_enabled"] is True
    assert data["verified_at"] is not None


def test_regenerate_managed_dkim_keeps_customer_cnames_stable():
    tenant_id, token = _register_owner("api4@dnstest.com")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}

    r = client.post("/api/sender-domains/", json={"domain": "regen.com"}, headers=headers)
    domain_id = r.json()["id"]
    old_dkim_values = [rec["value"] for rec in r.json()["dns_records"] if rec["purpose"] == "dkim"]

    from app.models.sender_domain import DkimKeyPair, SenderDomain
    db = TestSession()
    sender_domain = db.query(SenderDomain).filter(SenderDomain.id == domain_id).first()
    old_key_count = db.query(DkimKeyPair).filter(DkimKeyPair.sender_domain_id == domain_id).count()
    db.close()

    r2 = client.post(f"/api/sender-domains/{domain_id}/regenerate-dkim", headers=headers)
    assert r2.status_code == 200
    new_dkim_values = [rec["value"] for rec in r2.json()["dns_records"] if rec["purpose"] == "dkim"]

    db = TestSession()
    new_key_count = db.query(DkimKeyPair).filter(DkimKeyPair.sender_domain_id == domain_id).count()
    db.close()

    assert new_dkim_values == old_dkim_values
    assert new_key_count == old_key_count + 1


def test_delete_domain():
    tenant_id, token = _register_owner("api5@dnstest.com")
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}
    r = client.post("/api/sender-domains/", json={"domain": "todelete.com"}, headers=headers)
    domain_id = r.json()["id"]
    r2 = client.delete(f"/api/sender-domains/{domain_id}", headers=headers)
    assert r2.status_code == 200
    r3 = client.get(f"/api/sender-domains/{domain_id}", headers=headers)
    assert r3.status_code == 404
