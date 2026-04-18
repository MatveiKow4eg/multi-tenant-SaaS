from __future__ import annotations

import json


def parse_feature_toggles(raw_json: str | None) -> dict[str, bool]:
    if raw_json is None:
        return {}

    raw = raw_json.strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    toggles: dict[str, bool] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, bool):
            toggles[key] = value

    return toggles


def active_feature_toggles(raw_json: str | None) -> dict[str, bool]:
    toggles = parse_feature_toggles(raw_json)
    return {name: state for name, state in toggles.items() if state}
