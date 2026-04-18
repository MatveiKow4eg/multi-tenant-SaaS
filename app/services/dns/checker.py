"""DNS verification service for sender domain records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecordCheckResult:
    status: str
    actual_value: str | None = None
    error_message: str | None = None


def _resolve_txt(name: str) -> list[str]:
    """Return TXT values for a DNS name. Never raises."""
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(name, "TXT", lifetime=5)
        results: list[str] = []
        for rdata in answers:
            results.append("".join(part.decode("utf-8", errors="replace") for part in rdata.strings))
        return results
    except ImportError:
        pass
    except Exception:
        return []

    import subprocess

    try:
        result = subprocess.run(
            ["nslookup", "-type=TXT", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        values: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split('"')
            if len(parts) >= 2:
                values.append(parts[1])
        return values
    except Exception:
        return []


def _resolve_cname(name: str) -> list[str]:
    """Return CNAME targets for a DNS name. Never raises."""
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(name, "CNAME", lifetime=5)
        return [str(rdata.target).rstrip(".") for rdata in answers]
    except ImportError:
        pass
    except Exception:
        return []

    import subprocess

    try:
        result = subprocess.run(
            ["nslookup", "-type=CNAME", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        values: list[str] = []
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "canonical name" in lower and "=" in line:
                values.append(line.split("=", 1)[1].strip().rstrip("."))
        return values
    except Exception:
        return []


def check_spf(domain: str, expected_value: str) -> RecordCheckResult:
    """Verify SPF and tolerate existing SPF that already includes our domain."""
    from app.services.dns.generator import spf_contains_our_include

    records = [record for record in _resolve_txt(domain) if record.lower().startswith("v=spf1")]
    if not records:
        return RecordCheckResult(status="missing")

    for record in records:
        if spf_contains_our_include(record):
            return RecordCheckResult(status="verified", actual_value=record)

    return RecordCheckResult(
        status="mismatch",
        actual_value=records[0],
        error_message="Existing SPF record does not include our sending domain.",
    )


def check_dkim_txt(domain: str, selector: str, expected_value: str) -> RecordCheckResult:
    """Verify TXT-based DKIM record for a selector."""
    host = f"{selector}._domainkey.{domain}"
    records = _resolve_txt(host)
    if not records:
        return RecordCheckResult(status="missing")

    expected_p = _extract_dkim_p(expected_value)
    for record in records:
        actual_p = _extract_dkim_p(record)
        if expected_p and actual_p and expected_p == actual_p:
            return RecordCheckResult(status="verified", actual_value=record)
        if record.lower().startswith("v=dkim1"):
            return RecordCheckResult(
                status="mismatch",
                actual_value=record,
                error_message="DKIM record found but public key does not match.",
            )

    return RecordCheckResult(status="missing")


def check_cname(hostname: str, expected_target: str) -> RecordCheckResult:
    """Verify a managed DKIM CNAME record."""
    targets = _resolve_cname(hostname)
    if not targets:
        return RecordCheckResult(status="missing")

    for target in targets:
        if target.lower().rstrip(".") == expected_target.lower().rstrip("."):
            return RecordCheckResult(status="verified", actual_value=target)

    return RecordCheckResult(
        status="mismatch",
        actual_value=targets[0],
        error_message="CNAME record found but target does not match the expected managed selector.",
    )


def check_dmarc(domain: str, expected_value: str) -> RecordCheckResult:
    """Verify DMARC TXT record exists and is syntactically valid."""
    records = [record for record in _resolve_txt(f"_dmarc.{domain}") if record.lower().startswith("v=dmarc1")]
    if not records:
        return RecordCheckResult(status="missing")
    return RecordCheckResult(status="verified", actual_value=records[0])


def check_txt_contains(hostname: str, expected_token: str) -> RecordCheckResult:
    """Verify that TXT records for hostname include expected token."""
    records = _resolve_txt(hostname)
    if not records:
        return RecordCheckResult(status="missing")

    normalized_expected = (expected_token or "").strip()
    for record in records:
        if normalized_expected and normalized_expected in record:
            return RecordCheckResult(status="verified", actual_value=record)

    return RecordCheckResult(
        status="mismatch",
        actual_value=records[0],
        error_message="TXT record found but ownership token does not match.",
    )


def _extract_dkim_p(value: str) -> str | None:
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("p="):
            return part[2:].strip()
    return None
