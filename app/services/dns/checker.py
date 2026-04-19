"""DNS verification service for sender domain records."""

from __future__ import annotations

from dataclasses import dataclass


PUBLIC_DNS_RESOLVERS = ("1.1.1.1", "8.8.8.8")


@dataclass
class RecordCheckResult:
    status: str
    actual_value: str | None = None
    error_message: str | None = None


def _resolve_txt(name: str, nameservers: tuple[str, ...] | None = None) -> list[str]:
    """Return TXT values for a DNS name. Never raises."""
    try:
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver(configure=nameservers is None)
        if nameservers:
            resolver.nameservers = list(nameservers)
        answers = resolver.resolve(name, "TXT", lifetime=5)
        results: list[str] = []
        for rdata in answers:
            results.append("".join(part.decode("utf-8", errors="replace") for part in rdata.strings))
        return results
    except ImportError:
        pass
    except Exception:
        return []

    if nameservers:
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


def _resolve_cname(name: str, nameservers: tuple[str, ...] | None = None) -> list[str]:
    """Return CNAME targets for a DNS name. Never raises."""
    try:
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver(configure=nameservers is None)
        if nameservers:
            resolver.nameservers = list(nameservers)
        answers = resolver.resolve(name, "CNAME", lifetime=5)
        return [str(rdata.target).rstrip(".") for rdata in answers]
    except ImportError:
        pass
    except Exception:
        return []

    if nameservers:
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


def _iter_txt_resolution_attempts(name: str):
    seen: set[tuple[str, ...]] = set()

    records = _resolve_txt(name)
    key = tuple(records)
    if key not in seen:
        seen.add(key)
        yield records

    for resolver_ip in PUBLIC_DNS_RESOLVERS:
        records = _resolve_txt(name, nameservers=(resolver_ip,))
        key = tuple(records)
        if key not in seen:
            seen.add(key)
            yield records


def _iter_cname_resolution_attempts(name: str):
    seen: set[tuple[str, ...]] = set()

    records = _resolve_cname(name)
    key = tuple(records)
    if key not in seen:
        seen.add(key)
        yield records

    for resolver_ip in PUBLIC_DNS_RESOLVERS:
        records = _resolve_cname(name, nameservers=(resolver_ip,))
        key = tuple(records)
        if key not in seen:
            seen.add(key)
            yield records


def check_spf(domain: str, expected_value: str) -> RecordCheckResult:
    """Verify SPF and tolerate existing SPF that already includes our domain."""
    from app.services.dns.generator import spf_contains_our_include

    records: list[str] = []
    for resolved in _iter_txt_resolution_attempts(domain):
        spf_records = [record for record in resolved if record.lower().startswith("v=spf1")]
        if not spf_records:
            continue
        records = spf_records

        for record in spf_records:
            if spf_contains_our_include(record):
                return RecordCheckResult(status="verified", actual_value=record)

    if not records:
        return RecordCheckResult(status="missing")

    return RecordCheckResult(
        status="mismatch",
        actual_value=records[0],
        error_message="Existing SPF record does not include our sending domain.",
    )


def check_dkim_txt(domain: str, selector: str, expected_value: str) -> RecordCheckResult:
    """Verify TXT-based DKIM record for a selector."""
    host = f"{selector}._domainkey.{domain}"
    expected_p = _extract_dkim_p(expected_value)

    records: list[str] = []
    for resolved in _iter_txt_resolution_attempts(host):
        if not resolved:
            continue
        records = resolved
        for record in resolved:
            actual_p = _extract_dkim_p(record)
            if expected_p and actual_p and expected_p == actual_p:
                return RecordCheckResult(status="verified", actual_value=record)
            if record.lower().startswith("v=dkim1"):
                return RecordCheckResult(
                    status="mismatch",
                    actual_value=record,
                    error_message="DKIM record found but public key does not match.",
                )

    if not records:
        return RecordCheckResult(status="missing")

    return RecordCheckResult(status="missing")


def check_cname(hostname: str, expected_target: str) -> RecordCheckResult:
    """Verify a managed DKIM CNAME record."""
    targets: list[str] = []
    normalized_expected = expected_target.lower().rstrip(".")

    for resolved in _iter_cname_resolution_attempts(hostname):
        if not resolved:
            continue
        targets = resolved
        for target in resolved:
            if target.lower().rstrip(".") == normalized_expected:
                return RecordCheckResult(status="verified", actual_value=target)

    if not targets:
        return RecordCheckResult(status="missing")

    return RecordCheckResult(
        status="mismatch",
        actual_value=targets[0],
        error_message="CNAME record found but target does not match the expected managed selector.",
    )


def check_dmarc(domain: str, expected_value: str) -> RecordCheckResult:
    """Verify DMARC TXT record exists and is syntactically valid."""
    records: list[str] = []
    for resolved in _iter_txt_resolution_attempts(f"_dmarc.{domain}"):
        dmarc_records = [record for record in resolved if record.lower().startswith("v=dmarc1")]
        if not dmarc_records:
            continue
        records = dmarc_records
        return RecordCheckResult(status="verified", actual_value=dmarc_records[0])

    if not records:
        return RecordCheckResult(status="missing")
    return RecordCheckResult(status="verified", actual_value=records[0])


def check_txt_contains(hostname: str, expected_token: str) -> RecordCheckResult:
    """Verify that TXT records for hostname include expected token."""
    normalized_expected = (expected_token or "").strip()

    def _match_token(values: list[str]) -> str | None:
        for record in values:
            if normalized_expected and normalized_expected in record:
                return record
        return None

    records: list[str] = []
    for resolved in _iter_txt_resolution_attempts(hostname):
        if resolved:
            records = resolved
        matched_record = _match_token(resolved)
        if matched_record is not None:
            return RecordCheckResult(status="verified", actual_value=matched_record)

    if not records:
        return RecordCheckResult(status="missing")

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
