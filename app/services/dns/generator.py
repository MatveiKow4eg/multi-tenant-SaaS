"""DNS record generation for sender domains.

Generates SPF, DKIM (TXT mode with managed RSA key pair), and DMARC records.
Private DKIM keys are encrypted at rest with Fernet derived from settings.dkim_secret_key.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    """Derive a stable Fernet key from settings.dkim_secret_key."""
    raw = hashlib.sha256(settings.dkim_secret_key.encode()).digest()  # 32 bytes
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


# ---------------------------------------------------------------------------
# DKIM key generation
# ---------------------------------------------------------------------------

def generate_dkim_keypair(selector: str = "s1") -> tuple[str, str, str]:
    """
    Generate RSA-2048 DKIM key pair.

    Returns:
        (selector, private_key_encrypted, public_key_b64)
        - private_key_encrypted: Fernet-encrypted PEM, safe to store in DB
        - public_key_b64: base64(DER SubjectPublicKeyInfo) — goes into DNS TXT record
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    encrypted_pem = _fernet().encrypt(pem).decode()

    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode()
    return selector, encrypted_pem, pub_b64


def decrypt_dkim_private_key(encrypted_pem: str) -> bytes:
    """Return PEM bytes of the decrypted DKIM private key."""
    return _fernet().decrypt(encrypted_pem.encode())


# ---------------------------------------------------------------------------
# DNS record value builders
# ---------------------------------------------------------------------------

def build_spf_value() -> str:
    """Return SPF TXT record value for our sending infrastructure."""
    return f"v=spf1 include:{settings.spf_include_domain} ~all"


def build_dkim_txt_value(pub_b64: str) -> str:
    """Return DKIM TXT record value for a given public key."""
    return f"v=DKIM1; k=rsa; p={pub_b64}"


def build_managed_dkim_target(sender_domain_id: int, selector: str) -> str:
    """Return the managed DKIM target hostname for a customer-facing CNAME record."""
    return f"sd{sender_domain_id}-{selector}.{settings.dkim_managed_domain}"


def build_dmarc_value() -> str:
    """Return DMARC TXT record value (p=none, monitoring mode)."""
    return f"v=DMARC1; p=none; rua=mailto:{settings.dmarc_rua_email}"


# ---------------------------------------------------------------------------
# Public helpers used by the API route
# ---------------------------------------------------------------------------

def spf_contains_our_include(actual_spf: str) -> bool:
    """Check whether an existing SPF record already includes our sending domain."""
    return settings.spf_include_domain.lower() in actual_spf.lower()
