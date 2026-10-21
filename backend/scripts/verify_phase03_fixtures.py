"""Offline integrity and privacy gate for Phase 03 fixtures."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


SENSITIVE_QUERY_NAMES = {"access_token", "api_key", "apikey", "auth", "code", "key", "session", "token"}
PAYLOAD_PATTERNS = {
    "bearer credential": re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    "API token": re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\"']?[a-z0-9._~+/=-]{8,}"),
    "cookie value": re.compile(r"(?i)\b(?:set-cookie|cookie)\s*[:=]\s*[^\s<>{}]{4,}"),
    "session identifier": re.compile(r"(?i)\b(?:session(?:[_-]?id)?|sid|jsessionid)\s*[:=]\s*[\"']?[a-z0-9._~-]{6,}"),
    "email address": re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b"),
    "CPF": re.compile(r"(?<!\d)(?:\d{3}[.-]?){3}\d{2}(?!\d)"),
}


class FixtureViolation(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_source_url(name: str, source_url: str) -> None:
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FixtureViolation(f"{name}: invalid source URL")
    if parsed.username or parsed.password:
        raise FixtureViolation(f"{name}: source URL contains userinfo")
    if parsed.query or parsed.fragment:
        raise FixtureViolation(f"{name}: source URL contains query or fragment")
    lowered_path = parsed.path.lower()
    if any(re.search(rf"(?:^|[/_-]){re.escape(key)}(?:[/_.=-]|$)", lowered_path) for key in SENSITIVE_QUERY_NAMES):
        raise FixtureViolation(f"{name}: source URL contains sensitive parameter name")


def _scan_payload(name: str, payload: str) -> None:
    for category, pattern in PAYLOAD_PATTERNS.items():
        if pattern.search(payload):
            raise FixtureViolation(f"{name}: payload contains {category}")


def validate_manifest(manifest_path: Path) -> None:
    manifest_path = manifest_path.resolve()
    fixture_dir = manifest_path.parent
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = document.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise FixtureViolation("manifest: fixtures must be a non-empty list")

    referenced: set[str] = set()
    required = {"file", "source_url", "captured_at", "sanitization", "sha256", "parser", "expected", "scope"}
    for entry in entries:
        missing = required - set(entry)
        if missing:
            raise FixtureViolation(f"manifest entry: missing fields {sorted(missing)}")
        name = entry["file"]
        if name in referenced or Path(name).name != name or name == manifest_path.name:
            raise FixtureViolation(f"{name}: invalid or duplicate fixture reference")
        referenced.add(name)
        _validate_source_url(name, entry["source_url"])
        payload_path = fixture_dir / name
        if not payload_path.is_file():
            raise FixtureViolation(f"{name}: referenced fixture is missing")
        if _sha256(payload_path) != entry["sha256"]:
            raise FixtureViolation(f"{name}: SHA-256 mismatch")
        _scan_payload(name, payload_path.read_text(encoding="utf-8"))

    actual = {path.name for path in fixture_dir.iterdir() if path.is_file() and path.name != manifest_path.name}
    extra = actual - referenced
    if extra:
        raise FixtureViolation(f"fixture directory: unmanifested files {sorted(extra)}")


def _expect_failure(manifest_path: Path, mutation, expected_class: str) -> None:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="phase03-fixtures-") as temp:
        target = Path(temp)
        for entry in document["fixtures"]:
            (target / entry["file"]).write_bytes((manifest_path.parent / entry["file"]).read_bytes())
        candidate = copy.deepcopy(document)
        mutation(target, candidate)
        candidate_path = target / "manifest.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        try:
            validate_manifest(candidate_path)
        except FixtureViolation as exc:
            if expected_class.lower() not in str(exc).lower():
                raise AssertionError(f"expected {expected_class!r}, got {exc!s}") from exc
        else:
            raise AssertionError(f"self-test failed to detect {expected_class}")


def run_self_test(manifest_path: Path) -> None:
    validate_manifest(manifest_path)

    def alter_hash(_target: Path, candidate: dict) -> None:
        candidate["fixtures"][0]["sha256"] = "0" * 64

    _expect_failure(manifest_path, alter_hash, "SHA-256")

    def source_with_query(_target: Path, candidate: dict) -> None:
        candidate["fixtures"][0]["source_url"] += "?token=redacted"

    _expect_failure(manifest_path, source_with_query, "query")

    samples = {
        "bearer": "Authorization: Bearer abcdefghijklmnop",
        "API token": "api_key=abcdefghijklmno",
        "cookie": "Cookie: cart=abcdefghijk",
        "session": "session-id=abcdefghijk",
        "email": "person@example.test",
        "CPF": "123.456.789-09",
    }
    for expected_class, value in samples.items():
        def inject(target: Path, candidate: dict, payload=value) -> None:
            entry = candidate["fixtures"][0]
            path = target / entry["file"]
            path.write_text(payload, encoding="utf-8")
            entry["sha256"] = _sha256(path)

        _expect_failure(manifest_path, inject, expected_class)

    with tempfile.TemporaryDirectory(prefix="phase03-metadata-") as temp:
        target = Path(temp)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in document["fixtures"]:
            (target / entry["file"]).write_bytes((manifest_path.parent / entry["file"]).read_bytes())
        document["fixtures"][0]["sanitization"] = ["removed cookie, bearer token, session id, email and CPF fields"]
        candidate = target / "manifest.json"
        candidate.write_text(json.dumps(document), encoding="utf-8")
        validate_manifest(candidate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).parents[1] / "tests/fixtures/phase03/manifest.json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            run_self_test(args.manifest)
        else:
            validate_manifest(args.manifest)
    except (FixtureViolation, AssertionError, json.JSONDecodeError) as exc:
        print(f"phase03 fixture verification failed: {exc}")
        return 1
    print("phase03 fixture verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
