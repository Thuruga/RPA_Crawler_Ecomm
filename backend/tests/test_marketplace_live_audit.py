from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.audit_marketplaces_live import (
    ALLOWED_JOURNEYS,
    AuditReport,
    BrowserFingerprint,
    MarketplaceVerdict,
    CallBudgetExceeded,
    DomainThrottle,
    ExternalCallBudget,
    EngineAdapter,
    RawObservation,
    build_report,
    build_phase10_closeout_report,
    build_freight_gate_results,
    build_search_gate_results,
    choose_fingerprint,
    classify_observation,
    dry_run_payload,
    load_manifest,
    parse_args,
    render_json,
    render_markdown,
    write_report,
    write_final_report_payload,
    run_matrix,
    sanitize_value,
    load_freight_acceptance_matrix,
    AuditConfigurationError,
    derive_phase10_verdicts,
    validate_job_evidence,
)
from services.phase10_acceptance import (
    FreightGateResult,
    FreightMatrixRow,
    StrictProductEvidence,
    StrictSearchGateResult,
)


MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "audits"
    / "marketplaces"
    / "acceptance_manifest.json"
)


def observation(payload=None, **kwargs):
    return RawObservation(payload=payload, **kwargs)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_blocking_http_statuses_win_over_payload(status):
    result = classify_observation(
        "search",
        observation(
            {"products": [{"product_name": "Camiseta", "url": "https://www.amazon.com.br/dp/X", "price_full": 99}]},
            http_status=status,
        ),
        ("amazon.com.br",),
    )
    assert result.status == "blocked"


@pytest.mark.parametrize(
    "signal",
    ["CAPTCHA", "Anubis proof of work", "Access Denied", "login wall", "verify you are human", "HTTP 403"],
)
def test_http_200_challenge_is_blocked(signal):
    result = classify_observation(
        "search", observation({}, http_status=200, body_sample=signal), ("amazon.com.br",)
    )
    assert result.status == "blocked"


def test_http_200_without_expected_structure_is_parser_failure():
    result = classify_observation(
        "search", observation({"products": []}, http_status=200), ("amazon.com.br",)
    )
    assert result.status == "parser_failure"


def test_search_requires_name_https_expected_domain_and_positive_price():
    payload = {
        "products": [
            {"product_name": "", "url": "https://www.amazon.com.br/dp/1", "price_full": 10},
            {"product_name": "wrong", "url": "https://evil.test/item", "price_full": 10},
            {"product_name": "free inferred", "url": "https://www.amazon.com.br/dp/2", "price_full": 0},
            {"product_name": "valid", "url": "https://www.amazon.com.br/dp/3?tag=secret", "price_full": 10},
        ]
    }
    result = classify_observation("search", observation(payload), ("amazon.com.br",))
    assert result.status == "fresh_success"
    assert result.valid_items == 1
    assert result.selected_url.endswith("?tag=secret")


def test_pdp_requires_identity_and_positive_price():
    assert classify_observation(
        "pdp", observation({"product_code": "ABC", "price_full": 1}), ("amazon.com.br",)
    ).status == "fresh_success"
    assert classify_observation(
        "pdp", observation({"product_code": "ABC", "price_full": 0}), ("amazon.com.br",)
    ).status == "parser_failure"


@pytest.mark.parametrize(
    ("journey", "payload"),
    [
        (
            "search",
            {"products": [{"product_name": "bad", "url": "https://www.amazon.com.br/dp/1", "price_full": "Infinity"}]},
        ),
        ("pdp", {"product_code": "ABC", "price_full": "Infinity"}),
        ("shipping", {"status": "available", "shipping_price": "Infinity"}),
    ],
)
def test_live_classifier_rejects_non_finite_money(journey, payload):
    result = classify_observation(journey, observation(payload), ("amazon.com.br",))

    assert result.status == "parser_failure"

def test_shipping_never_accepts_inferred_zero():
    assert classify_observation(
        "shipping", observation({"status": "available", "shipping_price": 0}), ()
    ).status == "parser_failure"
    assert classify_observation(
        "shipping", observation({"is_free_shipping": True, "shipping_price": 0}), ()
    ).status == "fresh_success"
    assert classify_observation(
        "shipping", observation({"status": "Disponível", "shipping_price": 12.5}), ()
    ).status == "fresh_success"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (RawObservation(cache_state="stale"), "stale"),
        (RawObservation(unsupported=True), "unsupported"),
        (RawObservation(timed_out=True), "timeout"),
        (RawObservation(error=OSError("connection reset")), "network_error"),
        (RawObservation(external_calls=0), "not_fresh"),
        (RawObservation(legitimate_empty=True), "legitimate_empty"),
    ],
)
def test_non_success_states_remain_distinct(raw, expected):
    assert classify_observation("search", raw, ("amazon.com.br",)).status == expected


@pytest.mark.asyncio
async def test_budget_is_atomic_global_per_domain_and_single_flight():
    budget = ExternalCallBudget(2, 1)
    active = maximum = 0

    async def work(domain):
        nonlocal active, maximum
        async with budget:
            await budget.reserve(domain)
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(work("amazon.com.br"), work("netshoes.com.br"))
    assert maximum == 1
    assert budget.total == 2
    with pytest.raises(CallBudgetExceeded):
        await budget.reserve("mercadolivre.com.br")
    with pytest.raises(CallBudgetExceeded):
        await ExternalCallBudget(10, 1).reserve("amazon.com.br", 2)


@pytest.mark.asyncio
async def test_throttle_uses_monotonic_domain_deadline():
    now = [10.0]
    slept = []

    async def sleeper(delay):
        slept.append(delay)
        now[0] += delay

    throttle = DomainThrottle(
        2, 4, clock=lambda: now[0], sleeper=sleeper, jitter=lambda _a, _b: 3
    )
    await throttle.wait("amazon.com.br")
    now[0] += 1
    await throttle.wait("amazon.com.br")
    await throttle.wait("netshoes.com.br")
    assert slept == [2]


def test_sanitizer_removes_credentials_queries_headers_and_html():
    cleaned = sanitize_value(
        {
            "url": "https://user:pass@example.test/item?q=secret#fragment",
            "Authorization": "Bearer abc.def",
            "headers": {"Cookie": "sid=secret"},
            "exception": "GET https://proxy-user:proxy-pass@proxy.test/a?api_key=secret failed",
            "html": "<html>secret raw body</html>",
        }
    )
    dumped = json.dumps(cleaned)
    for forbidden in ("user:pass", "q=secret", "abc.def", "sid=secret", "proxy-pass", "raw body"):
        assert forbidden not in dumped


def test_fingerprint_is_internally_consistent_and_sticky():
    first = choose_fingerprint(17)
    second = choose_fingerprint(17)
    assert first == second
    major = first.impersonation.removeprefix("chrome")
    assert f"Chrome/{major}." in first.user_agent
    assert f'v="{major}"' in first.sec_ch_ua
    assert "Safari/605" not in first.user_agent
    assert first.headers == first.headers


class FakeAdapter:
    def __init__(self, marketplace, *, blocked_search=False):
        self.marketplace = marketplace
        self.blocked_search = blocked_search
        self.calls = []

    async def execute(self, journey, **kwargs):
        self.calls.append((journey, kwargs["product_url"]))
        domain = {
            "amazon": "amazon.com.br",
            "mercado_livre": "mercadolivre.com.br",
            "netshoes": "netshoes.com.br",
        }[self.marketplace]
        if journey == "search":
            if self.blocked_search:
                return RawObservation(http_status=403)
            return RawObservation(payload={"products": [{
                "product_name": "Camiseta", "url": f"https://www.{domain}/produto/1?tracking=x",
                "price_full": 99,
            }]})
        if journey == "pdp":
            return RawObservation(payload={"product_code": "SKU1", "price_full": 99})
        return RawObservation(payload={"is_free_shipping": True, "shipping_price": 0})


@pytest.mark.asyncio
async def test_matrix_derives_pdp_from_fresh_search_without_duplicate_calls():
    manifest = load_manifest(MANIFEST_PATH)
    fake = FakeAdapter("amazon")
    adapters = {("current", "amazon"): fake}
    budget = ExternalCallBudget(3, 3)
    throttle = DomainThrottle(0, 0)
    results = await run_matrix(
        manifest, adapters, marketplaces=["amazon"], profiles=["current"],
        journeys=list(ALLOWED_JOURNEYS), budget=budget, throttle=throttle,
    )
    assert [row.status for row in results] == ["fresh_success"] * 3
    assert [call[0] for call in fake.calls] == ["search", "pdp", "shipping"]
    assert fake.calls[1][1].startswith("https://www.amazon.com.br/")
    assert fake.calls[2][1] == fake.calls[1][1]
    assert budget.total == 3


@pytest.mark.asyncio
async def test_search_failure_marks_dependent_journeys_not_reached():
    manifest = load_manifest(MANIFEST_PATH)
    # Remove seed URLs so we can test the fallback to not_reached
    manifest.pop("journey_seed_urls", None)
    
    fake = FakeAdapter("amazon", blocked_search=True)
    results = await run_matrix(
        manifest, {("current", "amazon"): fake}, marketplaces=["amazon"],
        profiles=["current"], journeys=list(ALLOWED_JOURNEYS),
        budget=ExternalCallBudget(3, 3), throttle=DomainThrottle(0, 0),
    )
    assert [row.status for row in results] == ["blocked", "not_reached", "not_reached"]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_coherent_profile_temporarily_replaces_and_restores_engine_headers():
    class Engine:
        headers = {"User-Agent": "legacy", "X-Keep": "yes"}
        async def search(self, *_args, **_kwargs):
            assert self.headers["User-Agent"].startswith("Mozilla/5.0")
            assert self.headers["Sec-CH-UA-Platform"] == '"Windows"'
            assert self.headers["X-Keep"] == "yes"
            return {"products": []}
    engine = Engine()
    adapter = EngineAdapter(engine, tier="coherent_http", fingerprint=choose_fingerprint(17))
    await adapter.execute(
        "search", query="camiseta", product_url=None, zipcode="01310100",
        max_results=1, timeout_seconds=1,
    )
    assert engine.headers == {"User-Agent": "legacy", "X-Keep": "yes"}


def test_serialization_is_deterministic_and_sanitized():
    manifest = load_manifest(MANIFEST_PATH)
    budget = ExternalCallBudget(3, 3)
    freight = FreightGateResult(passed=True, rows=[], rejections=[])
    report = build_report(manifest, [], live=False, fingerprint=None, budget=budget, freight_results=freight, search_results=[], commands=["test"])
    assert render_json(report) == render_json(report)
    assert render_markdown(report).startswith(b"# Marketplace live audit")
    assert b"Cookie" not in render_json(report)



def test_write_report_creates_backend_phase10_companion(tmp_path):
    manifest = load_manifest(MANIFEST_PATH)
    report = build_report(
        manifest,
        [],
        live=True,
        fingerprint=None,
        budget=ExternalCallBudget(0, 0),
        freight_results=FreightGateResult(passed=False, rows=[], rejections=[]),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --live"],
    )

    timestamped_json, timestamped_md = write_report(report, tmp_path)

    assert timestamped_json.is_file()
    assert timestamped_md.is_file()
    companion_json = tmp_path / "phase10_live_report.json"
    companion_md = tmp_path / "phase10_live_report.md"
    assert companion_json.is_file()
    assert companion_md.is_file()
    assert json.loads(companion_json.read_text(encoding="utf-8"))["audit_id"] == report.audit_id

def test_write_report_markdown_uses_sanitized_payload(tmp_path):
    manifest = load_manifest(MANIFEST_PATH)
    report = build_report(
        manifest,
        [],
        live=True,
        fingerprint=None,
        budget=ExternalCallBudget(0, 0),
        freight_results=FreightGateResult(
            passed=False,
            rows=[
                FreightMatrixRow(
                    integration="vtex",
                    state="available",
                    product_url="https://aramis.com.br/produto?token=secret",
                    zipcode="01310100",
                    shipping_price=Decimal("19.90"),
                    evidence={"source": "fixture"},
                )
            ],
            rejections=[],
        ),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --live"],
    )

    _, markdown_path = write_report(report, tmp_path)
    markdown = markdown_path.read_text(encoding="utf-8")

    assert "01310100" not in markdown
    assert "[REDACTED CEP]" in markdown
    assert "?token=secret" not in markdown


def test_write_final_report_payload_sanitizes_json_and_markdown(tmp_path):
    payload = {
        "schema_version": 1,
        "audit_id": "audit-1",
        "generated_at": "2026-08-03T12:00:00Z",
        "live": True,
        "fingerprint": None,
        "total_external_calls": 0,
        "manifest": "backend/data/audits/marketplaces/acceptance_manifest.json",
        "commands": ["python scripts/audit_marketplaces_live.py --output https://example.test/out?token=secret"],
        "requirement_verdicts": {"SHIP-01": "failed"},
        "search_gates": [],
        "freight_gate": {
            "passed": False,
            "rows": [
                {
                    "integration": "vtex",
                    "state": "available",
                    "zipcode": "01310100",
                    "shipping_price": "19.90",
                }
            ],
        },
        "evidence": {
            "raw_log": "CEP 01310100 at https://www.amazon.com.br/dp/X?tag=secret",
            "headers": {"Cookie": "sid=secret"},
        },
        "evidence_sources": {"job_evidence": "backend/data/job-evidence.json?token=secret"},
        "verdicts": [],
    }

    json_path, markdown_path = write_final_report_payload(payload, tmp_path)
    combined = json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")

    assert "01310100" not in combined
    assert "?token=secret" not in combined
    assert "Cookie" not in combined
    assert "sid=secret" not in combined


def test_render_json_serializes_decimal_values_in_strict_search_gate():
    manifest = load_manifest(MANIFEST_PATH)
    budget = ExternalCallBudget(0, 0)
    search = [StrictSearchGateResult(
        query_id="generic_camiseta_masculina",
        query="camiseta masculina",
        passed=False,
        counts_toward_threshold=True,
        required_count=10,
        rejected=[StrictProductEvidence(
            marketplace="amazon",
            title="Camiseta masculina",
            normalized_url="https://www.amazon.com.br/produto/1",
            price=Decimal("99.90"),
            score=Decimal("70.0"),
            reason="score_below_cutoff",
        )],
    )]
    report = build_report(
        manifest,
        [],
        live=True,
        fingerprint=None,
        budget=budget,
        freight_results=FreightGateResult(passed=False, rows=[], rejections=[]),
        search_results=search,
        commands=["python scripts/audit_marketplaces_live.py --live"],
    )

    payload = json.loads(render_json(report))
    rejected = payload["search_gates"][0]["rejected"][0]
    assert rejected["price"] == "99.90"
    assert rejected["score"] == "70.0"


def test_derive_phase10_verdicts_fails_on_degraded_freight():
    freight = FreightGateResult(
        passed=False,
        rows=[FreightMatrixRow(integration="vtex", state="blocked", product_url="https://vtex", zipcode="000")],
        rejections=[]
    )
    verdicts = derive_phase10_verdicts(freight, [])
    assert verdicts["SHIP-01"] == "failed"

def test_derive_phase10_verdicts_fails_on_degraded_search():
    search = [StrictSearchGateResult(
        query_id="generic", query="q", passed=False, counts_toward_threshold=True, required_count=10
    )]
    freight = FreightGateResult(passed=True, rows=[], rejections=[])
    verdicts = derive_phase10_verdicts(freight, search)
    assert verdicts["SEARCH-01"] == "failed"

@pytest.mark.asyncio
async def test_run_freight_matrix_success_all_rows():
    from scripts.audit_marketplaces_live import run_freight_matrix, load_freight_acceptance_matrix
    from services.phase10_acceptance import validate_freight_gate_results
    
    matrix = {
        "integrations": {
            "vtex": {"product_url": "https://aramis.com.br/p", "brand_key": "aramis", "sku_id": "1", "seller_id": "1"},
            "shopify": {"product_url": "https://www.ricardoalmeida.com.br/p", "brand_key": "ricardo_almeida", "engine": "shopify"},
            "wake": {"product_url": "https://www.richards.com.br/p", "brand_key": "richards", "engine": "wake"},
            "mercado_livre": {"product_url": "https://mercadolivre.com.br/p", "engine": "mercado_livre"},
            "amazon": {"product_url": "https://amazon.com.br/p", "engine": "amazon"},
            "netshoes": {"product_url": "https://netshoes.com.br/p", "engine": "netshoes"},
        },
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }
    
    async def fake_quoter(integration: str, config: dict, zipcode: str):
        return {
            "integration": integration,
            "state": "available",
            "product_url": config["product_url"],
            "zipcode": zipcode,
            "shipping_price": "15.90",
            "is_free_shipping": False,
            "evidence": {"source_endpoint": "fake"}
        }
        
    rows = await run_freight_matrix(matrix, fake_quoter)
    assert len(rows) == 30
    gate = validate_freight_gate_results(rows, expected_ceps=matrix["ceps"])
    assert gate.passed is True

@pytest.mark.asyncio
async def test_run_freight_matrix_missing_region_fails():
    from scripts.audit_marketplaces_live import run_freight_matrix
    from services.phase10_acceptance import validate_freight_gate_results
    
    matrix = {
        "integrations": {
            "vtex": {"product_url": "https://aramis.com.br/p", "brand_key": "aramis", "sku_id": "1", "seller_id": "1"}
        },
        "ceps": ["01310100", "90010150"]
    }
    
    async def fake_quoter(integration: str, config: dict, zipcode: str):
        if zipcode == "90010150":
            return {
                "integration": integration,
                "state": "not_reached",
                "product_url": config["product_url"],
                "zipcode": zipcode,
                "shipping_price": None,
                "is_free_shipping": False,
                "evidence": {"error": "blocked"}
            }
        return {
            "integration": integration,
            "state": "available",
            "product_url": config["product_url"],
            "zipcode": zipcode,
            "shipping_price": "15.90",
            "is_free_shipping": False,
            "evidence": {"source_endpoint": "fake"}
        }
        
    rows = await run_freight_matrix(matrix, fake_quoter)
    gate = validate_freight_gate_results(rows, expected_ceps=matrix["ceps"])
    assert gate.passed is False
    assert len(gate.rejections) > 0
    assert any(rej.zipcode == "90010150" and rej.integration == "vtex" for rej in gate.rejections)

@pytest.mark.asyncio
async def test_run_freight_matrix_diagnostic_states_fail():
    from scripts.audit_marketplaces_live import run_freight_matrix
    from services.phase10_acceptance import validate_freight_gate_results
    
    matrix = {
        "integrations": {
            "vtex": {"product_url": "https://aramis.com.br/p", "brand_key": "aramis", "sku_id": "1", "seller_id": "1"}
        },
        "ceps": ["01310100"]
    }
    
    for bad_state in ["blocked", "stale", "temporary_failure", "unsupported", "not_reached", "error"]:
        async def fake_quoter(integration: str, config: dict, zipcode: str, state=bad_state):
            return {
                "integration": integration,
                "state": state,
                "product_url": config["product_url"],
                "zipcode": zipcode,
                "shipping_price": None,
                "is_free_shipping": False,
                "evidence": {"source_endpoint": "fake"}
            }
            
        rows = await run_freight_matrix(matrix, fake_quoter)
        gate = validate_freight_gate_results(rows, expected_ceps=matrix["ceps"])
        assert gate.passed is False
        assert any(rej.state == bad_state for rej in gate.rejections)

def test_freight_matrix_metadata_validation(tmp_path):
    from scripts.audit_marketplaces_live import load_freight_acceptance_matrix, AuditConfigurationError
    
    # Missing required metadata
    bad_matrix = {
        "schema_version": 1,
        "integrations": {
            "vtex": {"product_url": "https://vtex.com"}
        },
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad_matrix))
    with pytest.raises(AuditConfigurationError, match="missing integrations"):
        load_freight_acceptance_matrix(p)
        
    # Query strings not allowed
    bad_matrix2 = {
        "schema_version": 1,
        "integrations": {
            "vtex": {"product_url": "https://vtex.com?id=1"},
            "shopify": {"product_url": "https://shopify.test"},
            "wake": {"product_url": "https://wake.test"},
            "mercado_livre": {"product_url": "https://ml.test"},
            "amazon": {"product_url": "https://amazon.test"},
            "netshoes": {"product_url": "https://netshoes.test"}
        },
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }
    p2 = tmp_path / "bad2.json"
    p2.write_text(json.dumps(bad_matrix2))
    with pytest.raises(AuditConfigurationError, match="must not contain query strings"):
        load_freight_acceptance_matrix(p2)

def test_dry_run_freight_matrix(tmp_path, capsys):
    from scripts.audit_marketplaces_live import main
    import json
    
    manifest = {
        "marketplaces": ["amazon", "mercado_livre", "netshoes"],
        "query": "camiseta masculina",
        "zipcode": "01310100",
        "max_results": 10,
        "timeout_seconds": 15,
        "cooldown_seconds": {"min": 3, "max": 6},
        "max_external_calls": 36,
        "max_calls_per_marketplace": 12,
        "search_queries": {
            "generic_camiseta_masculina": {"strict_products": 20, "min_score": 0.5},
            "business_aramis_camisa_masculina": {"strict_products": 10, "min_score": 0.8}
        }
    }
    p_manifest = tmp_path / "manifest.json"
    p_manifest.write_text(json.dumps(manifest))
    
    matrix = {
        "schema_version": 1,
        "integrations": {
            "vtex": {"product_url": "https://vtex.com/p", "brand_key": "aramis", "sku_id": "1", "seller_id": "1"},
            "shopify": {"product_url": "https://shopify.test", "brand_key": "ricardo", "engine": "shopify"},
            "wake": {"product_url": "https://wake.test", "brand_key": "richards", "engine": "wake"},
            "mercado_livre": {"product_url": "https://ml.test", "engine": "mercado_livre"},
            "amazon": {"product_url": "https://amazon.test", "engine": "amazon"},
            "netshoes": {"product_url": "https://netshoes.test", "engine": "netshoes"}
        },
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }
    p_matrix = tmp_path / "matrix.json"
    p_matrix.write_text(json.dumps(matrix))
    
    asyncio.run(main([
        "--manifest", str(p_manifest),
        "--freight-matrix", str(p_matrix),
        "--marketplaces", "amazon",
        "--output", str(tmp_path)
    ]))
    
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["mode"] == "dry-run"
    assert out["network_enabled"] is False



def test_default_cli_is_dry_run_and_does_not_import_engines(monkeypatch, capsys):
    import scripts.audit_marketplaces_live as audit

    monkeypatch.setattr(audit, "build_live_adapters", lambda *_args, **_kwargs: pytest.fail("network adapter resolved"))
    assert asyncio.run(audit.main([])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["network_enabled"] is False
    assert payload["attempts_executed"] == 0
    assert payload["concurrency"] == 1


def test_merge_latest_with_search_evidence_rewrites_without_network_and_merges(tmp_path, monkeypatch):
    import scripts.audit_marketplaces_live as audit
    monkeypatch.setattr(audit, "build_live_adapters", lambda *_args, **_kwargs: pytest.fail("network adapter resolved"))
    monkeypatch.setattr(audit, "DEFAULT_OUTPUT", tmp_path)

    latest_path = tmp_path / "marketplace_audit_2026-08-04T120000+0000.json"
    latest_path.write_text(json.dumps({
        "schema_version": 1,
        "live": True,
        "manifest": {"marketplaces": ["amazon", "mercado_livre", "netshoes"]},
        "commands": ["python ..."],
        "results": [],
        "freight_gate": {"passed": False, "rows": [], "rejections": []},
        "search_gates": [],
        "evidence": {},
        "verdicts": []
    }), encoding="utf-8")
    
    def _product(index):
        return {
            "marketplace": "amazon",
            "title": f"Item {index}",
            "url": f"https://www.amazon.com.br/produto/{index}",
            "price": "100",
            "sku": f"SKU-{index}",
            "final_match_score": 90.0,
        }

    search_path = tmp_path / "10-SEARCH-EVIDENCE.json"
    search_path.write_text(json.dumps({
        "query_id": "generic_camiseta_masculina",
        "query": "camiseta masculina",
        "results": [_product(i) for i in range(10)],
        "generated_at": "2026-08-04T12:00:00Z"
    }), encoding="utf-8")

    dummy_file = tmp_path / "dummy.json"
    dummy_file.write_text(json.dumps({
        "schema_version": 1, 
        "integrations": {
            "vtex": {"product_url": "https://test.com"},
            "shopify": {"product_url": "https://test.com"},
            "wake": {"product_url": "https://test.com"},
            "mercado_livre": {"product_url": "https://test.com"},
            "amazon": {"product_url": "https://test.com"},
            "netshoes": {"product_url": "https://test.com"}
        }, 
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }), encoding="utf-8")
    
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(phase10_job_evidence()), encoding="utf-8")
    
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(json.dumps({
        "checklist_status": "passed",
        "observed_route": "/comparativa/search-acceptance-1",
        "reloaded": True,
        "job_still_visible": True,
        "duplicate_visible": False,
        "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
    }), encoding="utf-8")
    
    result = asyncio.run(audit.main([
        "--merge-latest", 
        "--search-evidence", str(search_path),
        "--freight-matrix", str(dummy_file),
        "--phase-report-output", str(tmp_path),
        "--job-evidence", str(job_file),
        "--frontend-checklist", str(checklist_file),
        "--output", str(tmp_path),
    ]))
    assert result == 0
    
    companion_json = tmp_path / "phase10_live_report.json"
    payload = json.loads(companion_json.read_text(encoding="utf-8"))
    assert payload["search_acceptance"]["query_id"] == "generic_camiseta_masculina"
    assert payload["search_acceptance"]["accepted_count"] == 10
    assert "search_evidence" in payload.get("evidence_sources", {})


def test_search_evidence_rejects_unsafe_material_during_merge(tmp_path, monkeypatch):
    import scripts.audit_marketplaces_live as audit
    monkeypatch.setattr(audit, "DEFAULT_OUTPUT", tmp_path)
    
    latest_path = tmp_path / "marketplace_audit_2026-08-04T120000+0000.json"
    latest_path.write_text(json.dumps({
        "schema_version": 1, "live": True, "manifest": {"marketplaces": ["amazon", "mercado_livre", "netshoes"]},
        "commands": ["python ..."], "results": [],
        "freight_gate": {"passed": False, "rows": [], "rejections": []},
        "search_gates": [], "evidence": {}, "verdicts": []
    }), encoding="utf-8")
    
    search_path = tmp_path / "10-SEARCH-EVIDENCE.json"
    search_path.write_text(json.dumps({
        "query_id": "generic_camiseta_masculina",
        "query": "camiseta masculina",
        "results": [{"title": "Item 1", "price": "100"}],
        "generated_at": "2026-08-04T12:00:00Z",
        "unsafe_cookie": "secret=token",
        "unsafe_html": "<html><body>unsafe</body></html>"
    }), encoding="utf-8")
    
    dummy_file = tmp_path / "dummy.json"
    dummy_file.write_text(json.dumps({
        "schema_version": 1, 
        "integrations": {
            "vtex": {"product_url": "https://test.com"},
            "shopify": {"product_url": "https://test.com"},
            "wake": {"product_url": "https://test.com"},
            "mercado_livre": {"product_url": "https://test.com"},
            "amazon": {"product_url": "https://test.com"},
            "netshoes": {"product_url": "https://test.com"}
        }, 
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }), encoding="utf-8")

    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(phase10_job_evidence()), encoding="utf-8")
    
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(json.dumps({
        "checklist_status": "passed",
        "observed_route": "/comparativa/search-acceptance-1",
        "reloaded": True,
        "job_still_visible": True,
        "duplicate_visible": False,
        "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
    }), encoding="utf-8")

    with pytest.raises(audit.AuditConfigurationError, match="sensitive material"):
        asyncio.run(audit.main([
            "--merge-latest", 
            "--search-evidence", str(search_path),
            "--freight-matrix", str(dummy_file),
            "--phase-report-output", str(tmp_path),
            "--job-evidence", str(job_file),
            "--frontend-checklist", str(checklist_file),
            "--output", str(tmp_path),
        ]))


@pytest.fixture
def phase10_manifest():
    return {
        "marketplaces": ["amazon", "mercado_livre", "netshoes"],
        "query": "camiseta masculina",
        "zipcode": "01310100",
        "max_results": 10,
        "timeout_seconds": 45,
        "cooldown_seconds": {"min": 3.0, "max": 6.0},
        "max_external_calls": 36,
        "max_calls_per_marketplace": 12,
        "search_queries": {
            "generic_camiseta_masculina": 10,
            "business_aramis_camisa_masculina": 0
        }
    }


def test_load_manifest_validates_phase10_budgets(tmp_path, phase10_manifest):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(phase10_manifest), encoding="utf-8")
    loaded = load_manifest(path)
    assert loaded["query"] == "camiseta masculina"
    assert loaded["zipcode"] == "01310100"
    assert "generic_camiseta_masculina" in loaded["search_queries"]


@pytest.mark.parametrize("overrides", [
    {"zipcode": "01000000"},  # Must be 01310100
    {"max_results": 11},      # Budget exceed
    {"timeout_seconds": 46},  # Budget exceed
    {"cooldown_seconds": {"min": 2.9, "max": 6.0}}, # Budget exceed
    {"max_external_calls": 37}, # Budget exceed
    {"max_calls_per_marketplace": 13}, # Budget exceed
    {"marketplaces": ["amazon", "mercado_livre"]}, # Incomplete
])
def test_load_manifest_fails_on_budget_exceed_or_invalid_zipcode(tmp_path, phase10_manifest, overrides):
    phase10_manifest.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(phase10_manifest), encoding="utf-8")
    with pytest.raises(AuditConfigurationError):
        load_manifest(path)


def test_load_manifest_fails_on_missing_search_queries(tmp_path, phase10_manifest):
    del phase10_manifest["search_queries"]["business_aramis_camisa_masculina"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(phase10_manifest), encoding="utf-8")
    with pytest.raises(AuditConfigurationError):
        load_manifest(path)


@pytest.fixture
def phase10_matrix():
    return {
        "schema_version": 1,
        "integrations": {
            "vtex": {"product_url": "https://aramis.com.br/product"},
            "shopify": {"product_url": "https://www.ricardoalmeida.com.br/product"},
            "wake": {"product_url": "https://www.richards.com.br/product"},
            "mercado_livre": {"product_url": "https://mercadolivre.com.br/product"},
            "amazon": {"product_url": "https://amazon.com.br/product"},
            "netshoes": {"product_url": "https://netshoes.com.br/product"}
        },
        "ceps": ["01310100", "90010150", "70040010", "40020000", "69010001"]
    }


def test_load_freight_acceptance_matrix_validates_shape(tmp_path, phase10_matrix):
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(phase10_matrix), encoding="utf-8")
    loaded = load_freight_acceptance_matrix(path)
    assert "vtex" in loaded["integrations"]


@pytest.mark.parametrize("missing_integration", ["vtex", "shopify", "wake", "mercado_livre", "amazon", "netshoes"])
def test_load_freight_acceptance_matrix_fails_on_missing_integration(tmp_path, phase10_matrix, missing_integration):
    del phase10_matrix["integrations"][missing_integration]
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(phase10_matrix), encoding="utf-8")
    with pytest.raises(AuditConfigurationError):
        load_freight_acceptance_matrix(path)


def test_load_freight_acceptance_matrix_fails_on_query_string(tmp_path, phase10_matrix):
    phase10_matrix["integrations"]["vtex"]["product_url"] = "https://aramis.com.br/product?tag=123"
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(phase10_matrix), encoding="utf-8")
    with pytest.raises(AuditConfigurationError):
        load_freight_acceptance_matrix(path)


def test_load_freight_acceptance_matrix_fails_on_invalid_ceps(tmp_path, phase10_matrix):
    phase10_matrix["ceps"] = ["01310100", "90010150", "70040010", "40020000"] # Missing one
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(phase10_matrix), encoding="utf-8")
    with pytest.raises(AuditConfigurationError):
        load_freight_acceptance_matrix(path)


def test_build_freight_gate_results_records_matrix_rows_as_failed_until_quoted(phase10_matrix):
    gate = build_freight_gate_results(phase10_matrix)

    assert gate.passed is False
    assert len(gate.rows) == 30
    assert {row.integration for row in gate.rows} == set(phase10_matrix["integrations"])
    assert {row.state for row in gate.rows} == {"not_reached"}
    assert {rejection.reason for rejection in gate.rejections} >= {"not_reached_state", "invalid_money"}


def test_build_search_gate_results_does_not_fabricate_missing_scores(phase10_manifest):
    gates = build_search_gate_results(
        phase10_manifest,
        [
            {
                "marketplace": "amazon",
                "title": "Camiseta masculina",
                "url": "https://www.amazon.com.br/produto/1",
                "price": "99.90",
            }
        ],
    )

    generic = next(gate for gate in gates if gate.query_id == "generic_camiseta_masculina")
    assert generic.passed is False
    assert generic.accepted_count == 0
    assert {item.reason for item in generic.rejected} >= {"score_below_cutoff", "accepted_count_below_required"}


def test_derive_phase10_verdicts_includes_mkt04_marketplace_requirement():
    freight = FreightGateResult(passed=True, rows=[], rejections=[])
    search = [StrictSearchGateResult(
        query_id="generic_camiseta_masculina",
        query="camiseta masculina",
        passed=True,
        counts_toward_threshold=True,
        required_count=10,
    )]
    marketplace_verdicts = [
        MarketplaceVerdict(name, "current", "circumvented_in_test_scope", list(ALLOWED_JOURNEYS), {}, 3)
        for name in ("amazon", "mercado_livre", "netshoes")
    ]

    verdicts = derive_phase10_verdicts(freight, search, {"job_types": {}}, marketplace_verdicts)
    assert verdicts["MKT-04"] == "passed"

    missing_netshoes = marketplace_verdicts[:-1]
    verdicts = derive_phase10_verdicts(freight, search, {"job_types": {}}, missing_netshoes)
    assert verdicts["MKT-04"] == "failed"


def phase10_job_evidence():
    return {
        "schema_version": 1,
        "restart_job_kind": "search",
        "backend_restart_command": "python -m uvicorn app:app --host 127.0.0.1 --port 8080",
        "restart_started_at": "2026-08-03T12:00:00Z",
        "restart_completed_at": "2026-08-03T12:02:00Z",
        "pre_restart": {
            "job_id": "search-acceptance-1",
            "status": "running",
            "observed_at": "2026-08-03T12:00:00Z",
        },
        "post_restart": {
            "job_id": "search-acceptance-1",
            "status": "completed",
            "observed_at": "2026-08-03T12:02:00Z",
        },
        "duplicate_check": {
            "job_id": "search-acceptance-1",
            "duplicate_count": 0,
            "disappeared": False,
        },
        "job_types": {
            "search": {
                "job_id": "search-acceptance-1",
                "status": "completed",
                "observed_after_restart": True,
                "duplicate_count": 0,
                "disappeared": False,
            },
            "cross": {
                "job_id": "cross-acceptance-1",
                "status": "completed",
                "observed_after_restart": True,
                "duplicate_count": 0,
                "disappeared": False,
            },
            "category_scan": {
                "job_id": "category-scan-acceptance-1",
                "status": "failed",
                "observed_after_restart": True,
                "duplicate_count": 0,
                "disappeared": False,
                "failure_reason": "external marketplace blocked during evidence run",
            },
            "price_check": {
                "job_id": "price-check-acceptance-1",
                "status": "blocked",
                "observed_after_restart": True,
                "duplicate_count": 0,
                "disappeared": False,
                "failure_reason": "anti-bot blocked the PDP read",
            },
        },
        "frontend_reconnect": {
            "checklist_status": "passed",
            "observed_route": "/historico/search-acceptance-1",
            "reloaded": True,
            "job_still_visible": True,
            "duplicate_visible": False,
            "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
        },
    }


def test_job_evidence_requires_all_durable_job_types():
    evidence = phase10_job_evidence()
    del evidence["job_types"]["price_check"]

    with pytest.raises(AuditConfigurationError, match="missing durable job evidence"):
        validate_job_evidence(evidence)


def test_restart_evidence_requires_search_restart_and_pre_post_snapshots():
    evidence = phase10_job_evidence()
    evidence["restart_job_kind"] = "category_scan"

    with pytest.raises(AuditConfigurationError, match="restart job kind must be search"):
        validate_job_evidence(evidence)

    evidence = phase10_job_evidence()
    del evidence["post_restart"]["job_id"]

    with pytest.raises(AuditConfigurationError, match="missing restart field"):
        validate_job_evidence(evidence)


def test_job_evidence_enforces_terminal_status_rules_by_job_family():
    accepted = validate_job_evidence(phase10_job_evidence())
    assert accepted["job_types"]["search"]["status"] == "completed"
    assert accepted["job_types"]["price_check"]["status"] == "blocked"

    evidence = phase10_job_evidence()
    evidence["job_types"]["cross"]["status"] = "failed"

    with pytest.raises(AuditConfigurationError, match="cross must complete"):
        validate_job_evidence(evidence)

    evidence = phase10_job_evidence()
    del evidence["job_types"]["category_scan"]["failure_reason"]

    with pytest.raises(AuditConfigurationError, match="honest terminal failure"):
        validate_job_evidence(evidence)


def test_job_evidence_rejects_sensitive_material_before_rendering():
    evidence = phase10_job_evidence()
    evidence["frontend_reconnect"]["headers"] = {"Cookie": "sid=secret"}

    with pytest.raises(AuditConfigurationError, match="sensitive"):
        validate_job_evidence(evidence)

def test_parse_args_accepts_job_and_frontend_evidence_paths():
    args = parse_args([
        "--job-evidence", "job-evidence.json",
        "--frontend-checklist", "frontend-checklist.md",
    ])

    assert args.job_evidence == Path("job-evidence.json")
    assert args.frontend_checklist == Path("frontend-checklist.md")


def test_phase10_closeout_report_merges_job_and_frontend_evidence():
    manifest = load_manifest(MANIFEST_PATH)
    budget = ExternalCallBudget(3, 3)
    freight = FreightGateResult(passed=True, rows=[], rejections=[])
    checklist = {
        "checklist_status": "passed",
        "checklist_path": "backend/data/audits/marketplaces/frontend-reconnect.md",
        "observed_route": "/historico/search-acceptance-1",
        "reloaded": True,
        "job_still_visible": True,
        "duplicate_visible": False,
        "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
    }

    report = build_phase10_closeout_report(
        manifest,
        [],
        live=False,
        fingerprint=None,
        budget=budget,
        freight_results=freight,
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --verify-latest"],
        job_evidence=phase10_job_evidence(),
        frontend_checklist=checklist,
    )

    payload = json.loads(render_json(report))
    assert payload["evidence"]["durable_jobs"]["restart_job_kind"] == "search"
    assert payload["evidence"]["durable_jobs"]["job_types"]["cross"]["status"] == "completed"
    assert payload["evidence"]["frontend_reconnect"]["checklist_status"] == "passed"
    assert payload["requirement_verdicts"]["VERIFY-01"] == "passed"

    markdown = render_markdown(report).decode("utf-8")
    assert "## Durable Job Evidence" in markdown
    assert "Restart job: `search`" in markdown
    assert "price_check" in markdown
    assert "## Frontend Reconnect Evidence" in markdown
    assert "frontend-reconnect.png" in markdown



def test_phase10_closeout_report_fails_verify01_when_frontend_checklist_fails():
    manifest = load_manifest(MANIFEST_PATH)
    budget = ExternalCallBudget(3, 3)
    checklist = {
        "checklist_status": "failed",
        "observed_route": "/historico/search-acceptance-1",
        "reloaded": False,
        "job_still_visible": True,
        "duplicate_visible": False,
        "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
    }

    report = build_phase10_closeout_report(
        manifest,
        [],
        live=False,
        fingerprint=None,
        budget=budget,
        freight_results=FreightGateResult(passed=True, rows=[], rejections=[]),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --verify-latest"],
        job_evidence=phase10_job_evidence(),
        frontend_checklist=checklist,
    )

    payload = json.loads(render_json(report))

    assert payload["evidence"]["frontend_reconnect"]["reloaded"] is False
    assert payload["requirement_verdicts"]["VERIFY-01"] == "failed"


def test_phase10_closeout_report_fails_verify01_without_job_evidence():
    manifest = load_manifest(MANIFEST_PATH)
    budget = ExternalCallBudget(3, 3)
    report = build_phase10_closeout_report(
        manifest,
        [],
        live=False,
        fingerprint=None,
        budget=budget,
        freight_results=FreightGateResult(passed=True, rows=[], rejections=[]),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --verify-latest"],
    )

    payload = json.loads(render_json(report))
    assert payload["requirement_verdicts"]["VERIFY-01"] == "failed"
    assert payload["evidence"]["durable_jobs"] is None

def _write_merge_latest_inputs(tmp_path: Path):
    manifest = load_manifest(MANIFEST_PATH)
    output = tmp_path / "out"
    phase_output = tmp_path / "phase"
    output.mkdir()
    report = build_phase10_closeout_report(
        manifest,
        [],
        live=True,
        fingerprint=None,
        budget=ExternalCallBudget(0, 0),
        freight_results=FreightGateResult(passed=False, rows=[], rejections=[]),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --live"],
    )
    write_report(report, output)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(MANIFEST_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    matrix_path = tmp_path / "freight.json"
    matrix_path.write_text(
        (MANIFEST_PATH.parent / "freight_acceptance_matrix.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    job_path = tmp_path / "job-evidence.json"
    job_path.write_text(json.dumps(phase10_job_evidence()), encoding="utf-8")
    checklist_path = tmp_path / "frontend-checklist.md"
    checklist_path.write_text(
        "# Frontend reconnect\n\nRoute `/comparativa/search-acceptance-1` reloaded and terminal status stayed visible.\n",
        encoding="utf-8",
    )
    search_path = tmp_path / "10-SEARCH-EVIDENCE.json"
    search_path.write_text(json.dumps({
        "query_id": "generic_camiseta_masculina",
        "query": "camiseta masculina",
        "results": [],
        "generated_at": "2026-08-04T12:00:00Z"
    }), encoding="utf-8")
    return output, phase_output, manifest_path, matrix_path, job_path, checklist_path, search_path


def test_merge_latest_writes_final_reports_without_live_or_network(monkeypatch, tmp_path):
    import scripts.audit_marketplaces_live as audit

    output, phase_output, manifest_path, matrix_path, job_path, checklist_path, search_path = _write_merge_latest_inputs(tmp_path)

    async def fail_run_matrix(*_args, **_kwargs):
        pytest.fail("marketplace matrix should not run during --merge-latest")

    monkeypatch.setattr(audit, "build_live_adapters", lambda *_args, **_kwargs: pytest.fail("live adapters resolved"))
    monkeypatch.setattr(audit, "run_matrix", fail_run_matrix)

    assert asyncio.run(audit.main([
        "--merge-latest",
        "--manifest", str(manifest_path),
        "--freight-matrix", str(matrix_path),
        "--output", str(output),
        "--phase-report-output", str(phase_output),
        "--job-evidence", str(job_path),
        "--frontend-checklist", str(checklist_path),
        "--search-evidence", str(search_path),
    ])) == 0

    backend_json = output / "phase10_live_report.json"
    backend_md = output / "phase10_live_report.md"
    phase_json = phase_output / "10-LIVE-REPORT.json"
    phase_md = phase_output / "10-LIVE-REPORT.md"
    for path in (backend_json, backend_md, phase_json, phase_md):
        assert path.is_file()

    payload = json.loads(backend_json.read_text(encoding="utf-8"))
    assert payload["job_evidence"]["job_types"]["price_check"]["status"] == "blocked"
    assert payload["frontend_reconnect"]["checklist_path"].endswith("frontend-checklist.md")
    assert any("--merge-latest" in command for command in payload["commands"])
    assert payload["requirement_verdicts"]["VERIFY-01"] == "passed"
    assert payload["evidence_sources"]["job_evidence"].endswith("job-evidence.json")
    assert payload["evidence_sources"]["frontend_checklist"].endswith("frontend-checklist.md")
    assert json.loads(phase_json.read_text(encoding="utf-8"))["audit_id"] == payload["audit_id"]



def test_merge_latest_marks_verify_failed_when_frontend_checklist_fails(monkeypatch, tmp_path):
    import scripts.audit_marketplaces_live as audit

    output, phase_output, manifest_path, matrix_path, job_path, _checklist_path, search_path = _write_merge_latest_inputs(tmp_path)
    failed_checklist_path = tmp_path / "frontend-checklist.json"
    failed_checklist_path.write_text(
        json.dumps(
            {
                "checklist_status": "failed",
                "observed_route": "/comparativa/search-acceptance-1",
                "reloaded": True,
                "job_still_visible": False,
                "duplicate_visible": False,
                "screenshot_path": "backend/data/audits/marketplaces/frontend-reconnect.png",
            }
        ),
        encoding="utf-8",
    )

    async def fail_run_matrix(*_args, **_kwargs):
        pytest.fail("marketplace matrix should not run during --merge-latest")

    monkeypatch.setattr(audit, "build_live_adapters", lambda *_args, **_kwargs: pytest.fail("live adapters resolved"))
    monkeypatch.setattr(audit, "run_matrix", fail_run_matrix)

    assert asyncio.run(audit.main([
        "--merge-latest",
        "--manifest", str(manifest_path),
        "--freight-matrix", str(matrix_path),
        "--output", str(output),
        "--phase-report-output", str(phase_output),
        "--job-evidence", str(job_path),
        "--frontend-checklist", str(failed_checklist_path),
        "--search-evidence", str(search_path),
    ])) == 0

    payload = json.loads((output / "phase10_live_report.json").read_text(encoding="utf-8"))
    assert payload["frontend_reconnect"]["job_still_visible"] is False
    assert payload["requirement_verdicts"]["VERIFY-01"] == "failed"


def test_parse_args_rejects_live_merge_latest_combination():
    with pytest.raises(SystemExit):
        parse_args(["--merge-latest", "--live"])


def test_verify_latest_rejects_companion_missing_job_type_or_sensitive_material(tmp_path):
    import scripts.audit_marketplaces_live as audit

    manifest = load_manifest(MANIFEST_PATH)
    report = build_phase10_closeout_report(
        manifest,
        [],
        live=False,
        fingerprint=None,
        budget=ExternalCallBudget(0, 0),
        freight_results=FreightGateResult(passed=False, rows=[], rejections=[]),
        search_results=[],
        commands=["python scripts/audit_marketplaces_live.py --merge-latest"],
        job_evidence=phase10_job_evidence(),
    )
    payload = json.loads(render_json(report))
    payload["job_evidence"] = dict(payload["job_evidence"])
    del payload["job_evidence"]["job_types"]["price_check"]
    payload["evidence"]["durable_jobs"] = payload["job_evidence"]
    output = tmp_path / "out"
    output.mkdir()
    (output / "phase10_live_report.json").write_text(json.dumps(payload), encoding="utf-8")
    (output / "phase10_live_report.md").write_text("# Marketplace live audit\n", encoding="utf-8")

    with pytest.raises(AuditConfigurationError, match="missing durable job evidence"):
        audit.verify_latest(output)

    payload = json.loads(render_json(report))
    payload["commands"].append("curl -H 'Cookie: sid=secret'")
    (output / "phase10_live_report.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AuditConfigurationError, match="forbidden sensitive material"):
        audit.verify_latest(output)

@pytest.mark.asyncio
async def test_journey_seed_urls_fallback_and_evidence():
    import scripts.audit_marketplaces_live as audit
    manifest = {
        "marketplaces": ["amazon"],
        "query": "camiseta masculina",
        "zipcode": "01310100",
        "max_results": 10,
        "timeout_seconds": 15,
        "cooldown_seconds": {"min": 3, "max": 6},
        "max_external_calls": 36,
        "max_calls_per_marketplace": 12,
        "search_queries": {
            "generic_camiseta_masculina": {"strict_products": 20, "min_score": 0.5},
            "business_aramis_camisa_masculina": {"strict_products": 10, "min_score": 0.8}
        },
        "journey_seed_urls": {
            "amazon": {
                "pdp": "https://www.amazon.com.br/dp/TEST",
                "shipping": "https://www.amazon.com.br/dp/TEST"
            }
        }
    }
    
    # Fake adapter that fails search but succeeds PDP and shipping because of seed URL
    fake = FakeAdapter("amazon", blocked_search=True)
    results = await audit.run_matrix(
        manifest, {("current", "amazon"): fake}, marketplaces=["amazon"],
        profiles=["current"], journeys=list(audit.ALLOWED_JOURNEYS),
        budget=audit.ExternalCallBudget(3, 3), throttle=audit.DomainThrottle(0, 0),
    )
    
    assert len(results) == 3
    search_result = results[0]
    pdp_result = results[1]
    shipping_result = results[2]
    
    assert search_result.status == "blocked"
    
    # Since search was blocked, it used the seed URL for PDP/shipping!
    assert pdp_result.status == "fresh_success"
    assert pdp_result.attempts[0].evidence["product_url_source"] == "seed"
    assert pdp_result.attempts[0].evidence["seed_url_used"] is True
    
    assert shipping_result.status == "fresh_success"
    assert shipping_result.attempts[0].evidence["product_url_source"] == "seed"
    assert shipping_result.attempts[0].evidence["seed_url_used"] is True

def test_journey_seed_urls_validation(tmp_path):
    import scripts.audit_marketplaces_live as audit
    manifest = {
        "marketplaces": ["amazon", "mercado_livre", "netshoes"],
        "query": "camiseta masculina",
        "zipcode": "01310100",
        "max_results": 10,
        "timeout_seconds": 15,
        "cooldown_seconds": {"min": 3, "max": 6},
        "max_external_calls": 36,
        "max_calls_per_marketplace": 12,
        "search_queries": {
            "generic_camiseta_masculina": {"strict_products": 20, "min_score": 0.5},
            "business_aramis_camisa_masculina": {"strict_products": 10, "min_score": 0.8}
        }
    }
    p = tmp_path / "manifest.json"
    
    # Test valid
    manifest["journey_seed_urls"] = {"amazon": {"pdp": "https://www.amazon.com.br/dp/TEST"}}
    p.write_text(json.dumps(manifest))
    assert audit.load_manifest(p)
    
    # Test query strings not allowed
    manifest["journey_seed_urls"] = {"amazon": {"pdp": "https://www.amazon.com.br/dp/TEST?q=1"}}
    p.write_text(json.dumps(manifest))
    with pytest.raises(audit.AuditConfigurationError, match="must not contain query strings"):
        audit.load_manifest(p)
        
    # Test bad host
    manifest["journey_seed_urls"] = {"amazon": {"pdp": "https://evil.com/dp/TEST"}}
    p.write_text(json.dumps(manifest))
    with pytest.raises(audit.AuditConfigurationError, match="not allowed for marketplace amazon"):
        audit.load_manifest(p)

