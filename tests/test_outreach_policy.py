from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.blacklist import Blacklist
from app.services.outreach.policy import (
    get_blacklist_skip_reason,
    is_country_allowed_for_outreach,
    language_for_country,
)
from app.services.outreach.writer import generate_outreach_sequence


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_country_allowlist_lithuania_only(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "outreach_allowed_countries", "Lithuania")
    assert is_country_allowed_for_outreach("Lithuania") is True
    assert is_country_allowed_for_outreach("Estonia") is False


def test_language_mapping_ready_for_estonia():
    assert language_for_country("Lithuania") == "lt"
    assert language_for_country("Estonia") == "et"


def test_blacklist_reason_existing_partner_domain():
    db = _make_db()
    try:
        db.add(Blacklist(entry_type="domain", value="partner.lt", reason="existing partner"))
        db.commit()

        reason = get_blacklist_skip_reason(company_domain="partner.lt", contact_email=None, db=db)
        assert reason == "existing_partner"
    finally:
        db.close()


def test_blacklist_reason_email():
    db = _make_db()
    try:
        db.add(Blacklist(entry_type="email", value="ops@blocked.lt", reason="do not contact"))
        db.commit()

        reason = get_blacklist_skip_reason(
            company_domain="realco.lt",
            contact_email="ops@blocked.lt",
            db=db,
        )
        assert reason == "blacklisted_email"
    finally:
        db.close()


def test_writer_lithuanian_fallback_when_openai_missing(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", None)
    seq = generate_outreach_sequence(
        company_name="RealCo",
        industry="metal fabrication",
        country="Lithuania",
        recipient_role="Operations Manager",
        language="en",
        brief="Signals: welding, subcontracting",
    )

    assert isinstance(seq, dict)
    assert seq["subject"]
    assert "Laba diena" in seq["body_step_1"]
    assert "Hello" not in seq["body_step_1"]


def test_writer_template_selection_is_stable(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", None)

    seq1 = generate_outreach_sequence(
        company_name="StableCo",
        industry="manufacturing",
        country="Lithuania",
        recipient_role="HR",
        language="lt",
        brief="a",
    )
    seq2 = generate_outreach_sequence(
        company_name="StableCo",
        industry="manufacturing",
        country="Lithuania",
        recipient_role="HR",
        language="lt",
        brief="b",
    )

    assert seq1["subject"] == seq2["subject"]
    assert seq1["body_followup_1"] == seq2["body_followup_1"]
