from __future__ import annotations

import json
from decimal import Decimal

from services.phase10_acceptance import (
    FreightGateResult,
    FreightMatrixRow,
    SearchAcceptanceEvidence,
    StrictProductEvidence,
    StrictSearchGateResult,
    sanitize_phase10_evidence,
    validate_freight_gate_results,
    validate_phase10_report_payload,
    validate_search_acceptance_evidence,
    validate_strict_final_products,
)


EXPECTED_ORIGINS = {
    "amazon.com.br",
    "mercadolivre.com.br",
    "netshoes.com.br",
}

FREIGHT_CEPS = ["01310100", "90010150", "70040010", "40020000", "69010001"]
FREIGHT_URLS = {
    "amazon": "https://www.amazon.com.br/dp/TEST",
    "mercado_livre": "https://www.mercadolivre.com.br/produto/1",
    "netshoes": "https://www.netshoes.com.br/produto/1",
    "shopify": "https://www.ricardoalmeida.com.br/products/1",
    "vtex": "https://aramis.com.br/produto/1",
    "wake": "https://www.richards.com.br/produto/1",
}


def _passing_freight_rows() -> list[FreightMatrixRow]:
    rows: list[FreightMatrixRow] = []
    for integration, url in FREIGHT_URLS.items():
        for zipcode in FREIGHT_CEPS:
            free = integration == "shopify" and zipcode == FREIGHT_CEPS[0]
            rows.append(
                FreightMatrixRow(
                    integration=integration,
                    state="free" if free else "available",
                    product_url=url,
                    zipcode=zipcode,
                    shipping_price=Decimal("0") if free else Decimal("19.90"),
                    is_free_shipping=free,
                    evidence={"source": "fixture"},
                )
            )
    return rows

def _product(index: int, *, marketplace: str = "amazon", **overrides):
    domain = {
        "amazon": "www.amazon.com.br",
        "mercado_livre": "www.mercadolivre.com.br",
        "netshoes": "www.netshoes.com.br",
    }[marketplace]
    payload = {
        "marketplace": marketplace,
        "title": f"Camiseta masculina valid {index}",
        "url": f"https://{domain}/produto/{index}?utm_source=tracker",
        "price": "119.90",
        "sku": f"SKU-{index}",
        "native_code": f"NATIVE-{index}",
        "final_match_score": 90.0,
    }
    payload.update(overrides)
    return payload


def test_d05_d06_d10_generic_query_with_ten_unique_products_passes_search_01():
    products = [_product(index) for index in range(10)]

    result = validate_strict_final_products(
        products,
        query_id="generic_camiseta_masculina",
        query="camiseta masculina",
        expected_origins=EXPECTED_ORIGINS,
        required_count=10,
        min_score=80.0,
    )

    assert isinstance(result, StrictSearchGateResult)
    assert result.passed is True
    assert result.query_id == "generic_camiseta_masculina"
    assert result.counts_toward_threshold is True
    assert result.accepted_count == 10
    assert all(isinstance(item, StrictProductEvidence) for item in result.accepted)
    assert {item.normalized_url for item in result.accepted} == {
        f"https://www.amazon.com.br/produto/{index}" for index in range(10)
    }


def test_d11_duplicates_by_normalized_url_sku_or_native_code_reduce_count():
    products = [_product(index) for index in range(10)]
    products[1] = _product(
        1,
        url="https://www.amazon.com.br/produto/0?utm_source=other",
    )
    products[3] = _product(3, sku="SKU-2")
    products[5] = _product(5, native_code="NATIVE-4")

    result = validate_strict_final_products(
        products,
        query_id="generic_camiseta_masculina",
        query="camiseta masculina",
        expected_origins=EXPECTED_ORIGINS,
        required_count=10,
        min_score=80.0,
    )

    assert result.passed is False
    assert result.accepted_count == 7
    assert {item.reason for item in result.rejected} >= {
        "duplicate_url",
        "duplicate_sku",
        "duplicate_native_code",
        "accepted_count_below_required",
    }


def test_d11_rejects_invalid_title_url_origin_price_and_score():
    invalid_products = [
        _product(1, title=" "),
        _product(2, url="http://www.amazon.com.br/produto/2"),
        _product(3, url="https://evil.example/produto/3"),
        _product(4, price=None),
        _product(5, price="0"),
        _product(6, final_match_score=79.9),
    ]

    result = validate_strict_final_products(
        invalid_products,
        query_id="generic_camiseta_masculina",
        query="camiseta masculina",
        expected_origins=EXPECTED_ORIGINS,
        required_count=1,
        min_score=80.0,
    )

    assert result.passed is False
    assert result.accepted_count == 0
    assert {item.reason for item in result.rejected} >= {
        "empty_title",
        "invalid_url_scheme",
        "unexpected_origin",
        "missing_price",
        "non_positive_price",
        "score_below_cutoff",
    }


def test_d05_d06_d10_aramis_query_is_business_evidence_not_search_threshold():
    products = [
        _product(
            index,
            marketplace="mercado_livre",
            title=f"Camisa masculina Aramis valid {index}",
        )
        for index in range(10)
    ]

    result = validate_strict_final_products(
        products,
        query_id="business_aramis_camisa_masculina",
        query="camisa masculina aramis",
        expected_origins=EXPECTED_ORIGINS,
        required_count=10,
        min_score=80.0,
    )

    assert result.query_id == "business_aramis_camisa_masculina"
    assert result.counts_toward_threshold is False
    assert result.accepted_count == 10
    assert result.passed is False
    assert {item.reason for item in result.rejected} == {
        "business_query_not_counted_for_search_threshold"
    }


def test_search_acceptance_evidence_validates_10_accepted_products():
    payload = {
        "query_id": "generic_camiseta_masculina",
        "query": "camiseta masculina",
        "results": [_product(i) for i in range(10)],
        "generated_at": "2026-08-04T12:00:00Z"
    }
    
    result = validate_search_acceptance_evidence(payload, EXPECTED_ORIGINS)
    
    assert isinstance(result, SearchAcceptanceEvidence)
    assert result.query_id == "generic_camiseta_masculina"
    assert result.passed is True
    assert result.counts_toward_threshold is True
    assert result.accepted_count == 10
    assert len(result.accepted_products) == 10
    assert len(result.rejected_products) == 0


def test_search_acceptance_evidence_fails_with_raw_snippets():
    payload = {
        "query_id": "generic_camiseta_masculina",
        "query": "camiseta masculina",
        "results": [{"title": f"Item {i}", "price": "100"} for i in range(10)],
        "generated_at": "2026-08-04T12:00:00Z"
    }
    
    result = validate_search_acceptance_evidence(payload, EXPECTED_ORIGINS)
    
    assert result.passed is False
    assert result.accepted_count == 0
    assert any(item.reason == "missing_product_url" for item in result.rejected_products)
    assert any(item.reason == "accepted_count_below_required" for item in result.rejected_products)


def test_search_acceptance_evidence_for_business_query_is_reportable():
    payload = {
        "query_id": "business_aramis_camisa_masculina",
        "query": "camisa masculina aramis",
        "results": [_product(i) for i in range(10)],
        "generated_at": "2026-08-04T12:00:00Z"
    }
    
    result = validate_search_acceptance_evidence(payload, EXPECTED_ORIGINS)
    
    assert result.query_id == "business_aramis_camisa_masculina"
    assert result.passed is False
    assert result.counts_toward_threshold is False
    assert result.accepted_count == 10
    assert any(item.reason == "business_query_not_counted_for_search_threshold" for item in result.rejected_products)



def test_d12_d14_supported_freight_rows_pass_only_on_available_or_explicit_free():
    passing = validate_freight_gate_results(_passing_freight_rows())

    assert isinstance(passing, FreightGateResult)
    assert passing.passed is True
    assert passing.rejections == []

    failing = validate_freight_gate_results(
        [
            FreightMatrixRow("vtex", "blocked", "https://aramis.com.br/p/1", "01310100", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("shopify", "stale", "https://shop.example/p/1", "01310100", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("wake", "temporary_failure", "https://wake.example/p/1", "01310100", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("mercado_livre", "unsupported", "https://www.mercadolivre.com.br/p/1", "01310100", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("amazon", "available", "https://www.amazon.com.br/dp/X", "01310100", "not-money", evidence={"source": "fixture"}),
            FreightMatrixRow("netshoes", "available", "https://www.netshoes.com.br/p/1", "01310100", Decimal("0"), evidence={"source": "fixture"}),
            FreightMatrixRow("vtex", "available", "https://aramis.com.br/p/2", "", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("shopify", "available", "", "01310100", Decimal("1"), evidence={"source": "fixture"}),
            FreightMatrixRow("wake", "available", "https://wake.example/p/2", "01310100", Decimal("1"), evidence={}),
        ]
    )

    assert failing.passed is False
    assert {item.reason for item in failing.rejections} >= {
        "blocked_state",
        "stale_state",
        "temporary_failure_state",
        "unsupported_supported_integration",
        "invalid_money",
        "false_zero_shipping",
        "missing_zipcode",
        "missing_product_url",
        "missing_integration_evidence",
    }


def test_freight_gate_requires_expected_cep_coverage_and_integration_origin():
    missing_rows = [
        row for row in _passing_freight_rows()
        if not (row.integration == "amazon" and row.zipcode == FREIGHT_CEPS[-1])
    ]
    missing = validate_freight_gate_results(missing_rows)
    missing_reasons = {
        (rejection.integration, rejection.zipcode, rejection.reason)
        for rejection in missing.rejections
    }

    assert missing.passed is False
    assert ("amazon", FREIGHT_CEPS[-1], "missing_region_evidence") in missing_reasons

    wrong_origin_rows = _passing_freight_rows()
    first = wrong_origin_rows[0]
    wrong_origin_rows[0] = FreightMatrixRow(
        integration=first.integration,
        state=first.state,
        product_url="https://evil.example/produto",
        zipcode=first.zipcode,
        shipping_price=first.shipping_price,
        is_free_shipping=first.is_free_shipping,
        evidence=first.evidence,
    )
    wrong_origin = validate_freight_gate_results(wrong_origin_rows)

    assert wrong_origin.passed is False
    assert (first.integration, "unexpected_origin") in {
        (rejection.integration, rejection.reason) for rejection in wrong_origin.rejections
    }

def test_d20_d21_d23_d24_report_validation_rejects_unsafe_or_non_reproducible_payload():
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-30T16:00:00Z",
        "manifest": "backend/data/audits/marketplaces/acceptance_manifest.json?token=secret",
        "commands": [],
        "evidence": {
            "raw_log": "shipping CEP 01310100 returned <html>secret body</html>",
            "headers": {"Cookie": "sid=secret"},
            "proxy_url": "https://user:pass@proxy.example",
            "product_url": "https://www.amazon.com.br/dp/X?tag=secret",
        },
    }

    result = validate_phase10_report_payload(payload)

    assert result.passed is False
    assert {
        "missing_reproducible_commands",
        "unsafe_manifest_reference",
        "unsafe_evidence_material",
    } <= set(result.rejections)



def test_report_validation_rejects_unsafe_material_in_recursive_sections():
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-30T16:00:00Z",
        "manifest": "backend/data/audits/marketplaces/acceptance_manifest.json",
        "commands": ["python scripts/audit_marketplaces_live.py --output https://example.test/out?token=secret"],
        "evidence_sources": {"job_evidence": "backend/data/job-evidence.json?token=secret"},
        "results": [
            {
                "attempts": [
                    {"evidence": {"product_url": "https://www.amazon.com.br/dp/X?tag=secret"}}
                ]
            }
        ],
        "freight_gate": {
            "passed": True,
            "rows": [{"integration": "vtex", "zipcode": "01310100", "state": "available"}],
        },
        "evidence": {},
    }

    result = validate_phase10_report_payload(payload)

    assert result.passed is False
    assert {
        "unsafe_commands_material",
        "unsafe_evidence_sources_material",
        "unsafe_results_material",
        "unsafe_freight_gate_material",
    } <= set(result.rejections)



def test_report_validation_treats_malformed_url_as_unsafe_material():
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-30T16:00:00Z",
        "manifest": "backend/data/audits/marketplaces/acceptance_manifest.json",
        "commands": ["python scripts/audit_marketplaces_live.py --verify-latest"],
        "evidence": {"raw_log": "browser saw http://[::1 during validation"},
    }

    result = validate_phase10_report_payload(payload)

    assert result.passed is False
    assert "unsafe_evidence_material" in result.rejections


def test_d20_d21_d23_d24_sanitized_report_payload_can_pass_closeout_validation():
    evidence = sanitize_phase10_evidence(
        {
            "raw_log": "shipping CEP 01310100 for https://www.amazon.com.br/dp/X?tag=secret",
            "headers": {"Cookie": "sid=secret"},
            "product_url": "https://www.amazon.com.br/dp/X?tag=secret",
            "observed_state": "fresh_success",
        }
    )

    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-30T16:00:00Z",
        "manifest": "backend/data/audits/marketplaces/acceptance_manifest.json",
        "commands": [
            "python scripts/audit_marketplaces_live.py --verify-latest --output data/audits/marketplaces"
        ],
        "evidence": evidence,
    }

    result = validate_phase10_report_payload(payload)
    dumped = json.dumps(result.sanitized_payload, ensure_ascii=False)

    assert result.passed is True
    assert result.rejections == []
    assert "01310100" not in dumped
    assert "?tag=secret" not in dumped
    assert "Cookie" not in dumped
    assert "<html" not in dumped
