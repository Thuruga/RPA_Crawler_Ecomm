"""Offline contracts for the dynamic conditional-offer coverage audit."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audit_conditional_offers as audit


def _brand(key, engine="vtex", active=True):
    return SimpleNamespace(
        brand_key=key,
        brand_name=key.title(),
        domain="example.test",
        engine=engine,
        is_active=active,
        promotion_probes=[],
    )


def _payload():
    report = audit.AuditReport(
        captured_at="2026-07-14T23:59:01+00:00",
        expected_brands=["reserva"],
        exclusions={key: "marketplace excluded by D-07" for key in audit.MARKETPLACE_KEYS},
        brands=[
            {
                "brand_key": "reserva",
                "brand_name": "Reserva",
                "engine": "vtex",
                "domain": "www.usereserva.com",
                "status": "none_detected",
                "samples": [
                    {
                        "slot": "ordinary",
                        "selection_method": "fixture",
                        "url": "https://www.usereserva.com/produto",
                        "status": "none_detected",
                        "sources_attempted": ["vtex_structured"],
                    }
                ],
            }
        ],
    )
    return audit.finalize_report(report)


def _destinations(tmp_path: Path):
    output = tmp_path / "audits"
    markdown = tmp_path / "03-COVERAGE-AUDIT.md"
    snapshot = output / "conditional_offers_20260714T235901Z.json"
    latest = output / "conditional_offers_latest.json"
    return output, markdown, snapshot, latest


def _seed_state(paths, exists: bool):
    _output, markdown, snapshot, latest = paths
    expected = {}
    for index, path in enumerate((snapshot, markdown, latest), start=1):
        # The dated snapshot must be absent for publication; an existing one is
        # a collision tested separately.  Markdown/latest exercise rollback of
        # pre-existing destinations.
        should_exist = exists and path != snapshot
        if should_exist:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"before-{index}".encode())
            expected[path] = path.read_bytes()
        else:
            expected[path] = None
    return expected


def _assert_restored(paths, expected):
    output, markdown, snapshot, latest = paths
    for path in (snapshot, markdown, latest):
        if expected[path] is None:
            assert not path.exists()
        else:
            assert path.read_bytes() == expected[path]
    leftovers = [
        path
        for root in (output, markdown.parent)
        if root.exists()
        for path in root.glob(".*.tmp")
    ]
    assert leftovers == []


def test_d06_d07_dynamic_universe_excludes_exactly_three_marketplaces():
    brands = [
        _brand("reserva"),
        _brand("wake", "wake"),
        _brand("mercado_livre", "mercadolivre"),
        _brand("netshoes", "netshoes"),
        _brand("amazon", "amazon"),
        _brand("inactive", active=False),
    ]
    own, excluded = audit.snapshot_active_own_brands(brands)
    assert [brand.brand_key for brand in own] == ["reserva", "wake"]
    assert set(excluded) == {"mercado_livre", "netshoes", "amazon"}


def test_runtime_snapshot_calls_active_only(monkeypatch):
    calls = []
    monkeypatch.setattr(
        audit.brand_service,
        "list_brands",
        lambda *, active_only: calls.append(active_only) or [_brand("reserva")],
    )
    own, excluded = audit.snapshot_active_own_brands()
    assert calls == [True]
    assert [item.brand_key for item in own] == ["reserva"]
    assert excluded == {}


def test_d08_each_brand_has_one_to_three_distinct_samples_or_explicit_limitation():
    report = audit.build_brand_report(
        _brand("reserva"),
        candidates=["https://example.test/a", "https://example.test/a"],
    )
    assert 1 <= len(report.samples) <= 3 or report.limitations
    assert len({sample.url for sample in report.samples}) == len(report.samples)


@pytest.mark.parametrize(
    "status", ["found", "none_detected", "unsupported", "blocked", "requires_auth"]
)
def test_d08_honest_classified_statuses_are_accepted(status):
    sample = {
        "slot": "ordinary",
        "selection_method": "fixture",
        "status": status,
        "limitation": "explicit fixture limitation",
        "limitation_type": "capability_unsupported",
    }
    if status in {"found", "none_detected", "blocked", "requires_auth"}:
        sample.update(
            url="https://example.test/product",
            sources_attempted=["fixture_source"],
        )
        sample.pop("limitation_type")
    report = audit.AuditReport(
        expected_brands=["x"],
        brands=[
            {
                "brand_key": "x",
                "status": status,
                "samples": [sample],
                "limitations": ["explicit fixture limitation"],
            }
        ],
    )
    assert audit.exit_code(report) == 0


def test_nominal_sixteen_of_sixteen_with_zero_sources_is_rejected():
    keys = [f"brand_{index:02d}" for index in range(16)]
    report = audit.AuditReport(
        expected_brands=keys,
        brands=[
            {
                "brand_key": key,
                "status": "none_detected",
                "samples": [
                    {
                        "slot": "ordinary",
                        "selection_method": "fixture",
                        "url": f"https://{key}.example/product",
                        "status": "none_detected",
                    }
                ],
            }
            for key in keys
        ],
    )
    assert report.totals["nominal_brands"] == 16
    assert report.totals["functional_brands"] == 0
    assert audit.exit_code(report) != 0


def test_functional_matrix_and_markdown_separate_nominal_from_executed_sources():
    payload = _payload()
    assert payload["totals"]["nominal_brands"] == 1
    assert payload["totals"]["functional_brands"] == 1
    assert payload["totals"]["executed_samples"] == 1
    markdown = audit.render_markdown(payload).decode("utf-8")
    assert "Cobertura nominal" in markdown
    assert "Cobertura funcional" in markdown
    assert "vtex_structured" in markdown


def test_gap01_insufficient_context_diagnostic_is_executed_but_not_functionally_covered():
    """D-08 (Fase 03 Plano 13): uma fonte que foi de fato chamada mas sem contexto
    comercial suficiente para avaliar a regra conta como `executed=True` e
    `functional_covered=False` — nunca como cobertura funcional honesta."""
    sample = audit.AuditSample(
        slot="ordinary",
        selection_method="fixture",
        url="https://www.usereserva.com/produto",
        status="unsupported",
        sources_attempted=["reserva_prime:/_v/rsv-prime/primepromotion"],
        diagnostics=[
            "reserva_prime:/_v/rsv-prime/primepromotion: insufficient_context: "
            "reserva_prime requires sku_id/seller_id/PriceWithoutDiscount from the PDP; "
            "none resolved for this product"
        ],
    )
    assert sample.executed is True
    assert sample.functional_covered is False


@pytest.mark.parametrize("status", ["found", "none_detected"])
def test_gap01_genuine_status_without_insufficient_context_stays_functionally_covered(status):
    """Mesma fonte tentada, mas com status genuíno (found/none_detected) e sem o
    marcador `insufficient_context` — `functional_covered` permanece True."""
    sample = audit.AuditSample(
        slot="ordinary",
        selection_method="fixture",
        url="https://www.usereserva.com/produto",
        status=status,
        sources_attempted=["reserva_prime:/_v/rsv-prime/primepromotion"],
        diagnostics=["reserva_prime:/_v/rsv-prime/primepromotion: redirects=0"],
    )
    assert sample.executed is True
    assert sample.functional_covered is True


@pytest.mark.parametrize(
    "brands",
    [
        [],
        [{"brand_key": "x", "status": "error", "limitations": ["schema drift"]}],
    ],
)
def test_d08_omitted_brand_or_internal_error_returns_nonzero(brands):
    report = audit.AuditReport(expected_brands=["x"], brands=brands)
    assert audit.exit_code(report) != 0


def test_d09_report_keeps_up_to_three_samples_and_safe_diagnostics_per_brand():
    report = audit.AuditReport(
        expected_brands=["x"],
        brands=[
            {
                "brand_key": "x",
                "status": "blocked",
                "samples": [],
                "limitations": ["storefront protection"],
            }
        ],
    )
    payload = report.model_dump(mode="json")
    assert payload["expected_brands"] == ["x"]
    assert payload["brands"][0]["limitations"]
    assert "cookie=" not in str(payload).lower()
    assert "token=" not in str(payload).lower()


def test_sanitization_removes_query_userinfo_and_sensitive_diagnostics():
    assert audit.sanitize_url(
        "https://user:pass@example.test/product?token=abc&email=x@y.test#frag"
    ) == "https://example.test/product"
    cleaned = audit.sanitize_text("token=abc cookie: xyz email=a@b.test")
    assert "abc" not in cleaned and "xyz" not in cleaned and "a@b.test" not in cleaned


def test_candidate_never_mutates_final_alias_or_markdown(tmp_path):
    output, markdown, _snapshot, latest = _destinations(tmp_path)
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b"stable-latest")
    markdown.write_bytes(b"stable-markdown")
    candidate = audit.write_candidate(_payload(), output / "candidates")
    assert candidate.parent.name == "candidates"
    assert latest.read_bytes() == b"stable-latest"
    assert markdown.read_bytes() == b"stable-markdown"


def test_render_failure_happens_before_any_publication(tmp_path, monkeypatch):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, True)
    monkeypatch.setattr(
        audit,
        "render_markdown",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("render fault")),
    )
    with pytest.raises(RuntimeError, match="render fault"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


@pytest.mark.parametrize(
    "exists,failure_call",
    [(False, call) for call in range(1, 4)] + [(True, call) for call in range(1, 6)],
)
def test_each_staging_or_backup_write_failure_restores_all_destinations(
    tmp_path, monkeypatch, exists, failure_call
):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, exists)
    original = audit._write_temporary
    calls = 0

    def fail(destination, content, label):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError(f"write fault {failure_call}")
        return original(destination, content, label)

    monkeypatch.setattr(audit, "_write_temporary", fail)
    with pytest.raises(OSError, match="write fault"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


@pytest.mark.parametrize(
    "exists,failure_call",
    [(False, call) for call in range(1, 4)] + [(True, call) for call in range(1, 6)],
)
def test_each_file_fsync_failure_restores_all_destinations(
    tmp_path, monkeypatch, exists, failure_call
):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, exists)
    original = audit._fsync_file
    calls = 0

    def fail(handle):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError(f"fsync fault {failure_call}")
        return original(handle)

    monkeypatch.setattr(audit, "_fsync_file", fail)
    with pytest.raises(OSError, match="fsync fault"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_each_replace_failure_restores_all_destinations(
    tmp_path, monkeypatch, exists, failure_call
):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, exists)
    original = audit._replace
    calls = 0

    def fail(source, destination):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError(f"replace fault {failure_call}")
        return original(source, destination)

    monkeypatch.setattr(audit, "_replace", fail)
    with pytest.raises(OSError, match="replace fault"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


@pytest.mark.parametrize("exists", [False, True])
def test_directory_fsync_failure_after_replaces_rolls_back(tmp_path, monkeypatch, exists):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, exists)
    calls = 0

    def fail(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync fault")

    monkeypatch.setattr(audit, "_fsync_directory", fail)
    with pytest.raises(OSError, match="directory fsync fault"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


def test_successful_publication_replaces_latest_last_and_keeps_jsons_equal(
    tmp_path, monkeypatch
):
    paths = _destinations(tmp_path)
    order = []
    original = audit._replace

    def record(source, destination):
        order.append(destination.name)
        return original(source, destination)

    monkeypatch.setattr(audit, "_replace", record)
    snapshot = audit.publish_final(_payload(), paths[0], paths[1])
    assert order[-1] == "conditional_offers_latest.json"
    latest_payload = json.loads(paths[3].read_text(encoding="utf-8"))
    snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert latest_payload == snapshot_payload
    assert latest_payload["snapshot_hash"] in paths[1].read_text(encoding="utf-8")


def test_snapshot_collision_fails_before_touching_other_destinations(tmp_path):
    paths = _destinations(tmp_path)
    expected = _seed_state(paths, True)
    paths[2].parent.mkdir(parents=True, exist_ok=True)
    paths[2].write_bytes(b"existing-snapshot")
    expected[paths[2]] = b"existing-snapshot"
    with pytest.raises(FileExistsError, match="collision"):
        audit.publish_final(_payload(), paths[0], paths[1])
    _assert_restored(paths, expected)


def test_markdown_aliasing_dated_snapshot_fails_fast_without_writing_anything(tmp_path):
    output, _markdown, snapshot, latest = _destinations(tmp_path)
    with pytest.raises(ValueError, match="pairwise distinct"):
        audit.publish_final(_payload(), output, snapshot)
    assert not snapshot.exists()
    assert not latest.exists()


def test_markdown_aliasing_latest_fails_fast_without_writing_anything(tmp_path):
    output, _markdown, snapshot, latest = _destinations(tmp_path)
    with pytest.raises(ValueError, match="pairwise distinct"):
        audit.publish_final(_payload(), output, latest)
    assert not snapshot.exists()
    assert not latest.exists()


@pytest.mark.asyncio
async def test_promote_candidate_is_strictly_offline_and_preserves_exact_bytes(
    tmp_path, monkeypatch
):
    output, markdown, snapshot, latest = _destinations(tmp_path)
    candidate = audit.write_candidate(_payload(), output / "candidates")
    candidate_bytes = candidate.read_bytes()
    monkeypatch.setattr(audit, "_active_universe_keys", lambda: ["reserva"])

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("live/session/browser path was touched")

    monkeypatch.setattr(audit, "run", forbidden)
    monkeypatch.setattr(audit, "run_live", forbidden)
    monkeypatch.setattr(audit, "audit_universe", forbidden)
    monkeypatch.setattr(audit.SessionManager, "close_session", forbidden)
    monkeypatch.setattr(audit.BrowserManager, "close", forbidden)

    code, artifact, report = await audit.dispatch(
        [
            "--promote-candidate",
            str(candidate),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )
    assert code == 0
    assert artifact == snapshot
    assert report.snapshot_hash == _payload()["snapshot_hash"]
    assert candidate_bytes == snapshot.read_bytes() == latest.read_bytes()


@pytest.mark.asyncio
async def test_verify_latest_is_read_only_and_never_touches_live_paths(tmp_path, monkeypatch):
    output, markdown, _snapshot, latest = _destinations(tmp_path)
    candidate = audit.write_candidate(_payload(), output / "candidates")
    monkeypatch.setattr(audit, "_active_universe_keys", lambda: ["reserva"])
    audit.promote_candidate(candidate, output_dir=output, markdown_path=markdown)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (candidate, latest, markdown)
    }

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("live path was touched")

    monkeypatch.setattr(audit, "run", forbidden)
    monkeypatch.setattr(audit, "run_live", forbidden)
    monkeypatch.setattr(audit, "audit_universe", forbidden)
    monkeypatch.setattr(audit, "publish_transaction", forbidden)
    monkeypatch.setattr(audit, "_write_temporary", forbidden)

    code, artifact, _report = await audit.dispatch(
        [
            "--verify-latest",
            str(latest),
            "--markdown",
            str(markdown),
        ]
    )
    assert code == 0
    assert artifact == latest
    for path, (content, modified) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == modified


def test_promote_rejects_noncanonical_hash_timestamp_and_outside_candidate_dir(
    tmp_path, monkeypatch
):
    output, markdown, _snapshot, _latest = _destinations(tmp_path)
    monkeypatch.setattr(audit, "_active_universe_keys", lambda: ["reserva"])
    outside = tmp_path / "conditional_offers_20260714T235901Z.json"
    outside.write_bytes(audit.render_json(_payload()))
    with pytest.raises(ValueError, match="expected directory"):
        audit.promote_candidate(outside, output_dir=output, markdown_path=markdown)

    payload = _payload()
    payload["snapshot_hash"] = "0" * 64
    bad = output / "candidates" / "conditional_offers_20260714T235901Z.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(audit.render_json(payload))
    with pytest.raises(ValueError, match="snapshot_hash"):
        audit.promote_candidate(bad, output_dir=output, markdown_path=markdown)


def test_cli_modes_are_mutually_exclusive_and_offline_modes_require_markdown():
    with pytest.raises(SystemExit):
        audit._parse_args(["--candidate", "--verify-latest", "latest.json"])
    with pytest.raises(SystemExit):
        audit._parse_args(["--promote-candidate", "candidate.json"])
