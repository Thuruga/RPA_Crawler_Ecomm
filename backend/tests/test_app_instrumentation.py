"""Testes de integração no nível ASGI para a instrumentação OTel (OBS-01).

Provam, na borda real do ASGI (TestClient real do FastAPI/Starlette, sem
mockar o framework), que `instrument_fastapi_app` produz spans/métricas sem
vazar segredos: a query string `?api_key=` da autenticação do WebSocket, os
headers `Authorization`/`X-API-Key`, e o `/health-check` não geram span de
probe nem span por mensagem. Cada teste usa providers OTel em memória
(`TracerProvider`/`MeterProvider` com exporters `InMemory*`) construídos por
`make_instrumented_app`, então nada aqui toca rede nem o estado OTel global
do processo.

O último teste (`test_app_module_instruments_when_enabled`) roda em um
subprocesso dedicado para isolar o estado global do OTel e o objeto
`app.app` módulo-level do `backend/app.py` real, sem afetar os demais
testes deste arquivo nem o restante da suíte.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from core.observability import instrument_fastapi_app

SECRET = "super-secret-value"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEifQ.c2lnbmF0dXJlLXZhbHVl"


def _attrs_blob(spans) -> str:
    """Junta key=value de todos os atributos de todas as spans finalizadas."""
    parts = []
    for span in spans:
        for key, value in (span.attributes or {}).items():
            parts.append(f"{key}={value}")
    return " ".join(parts)


@pytest.fixture
def make_instrumented_app():
    """Fixture-fábrica: builder que monta um FastAPI mínimo já instrumentado.

    Retornada como função (não já construída) para que cada teste possa
    ajustar env vars ANTES de instrumentar — a captura de headers é lida
    quando o middleware ASGI é montado, na primeira requisição.
    """
    created_providers = []

    def _build():
        app = FastAPI()

        @app.get("/items/{item_id}")
        async def get_item(item_id: str):
            return {"item_id": item_id}

        @app.get("/health-check")
        async def health_check():
            return {"status": "ok"}

        @app.websocket("/ws/{job_id}")
        async def ws_endpoint(websocket: WebSocket, job_id: str):
            await websocket.accept()
            text = await websocket.receive_text()
            await websocket.send_text("echo:" + text)
            await websocket.close()

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

        metric_reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[metric_reader])

        instrument_fastapi_app(
            app, tracer_provider=tracer_provider, meter_provider=meter_provider
        )

        created_providers.append((tracer_provider, meter_provider))
        client = TestClient(app)
        return client, span_exporter, metric_reader

    yield _build

    for tracer_provider, meter_provider in created_providers:
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_http_server_span_redacts_query_secret(make_instrumented_app):
    client, span_exporter, _ = make_instrumented_app()

    response = client.get(f"/items/7?api_key={SECRET}&page=2")
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind == SpanKind.SERVER]
    assert len(server_spans) == 1
    assert server_spans[0].name == "GET /items/{item_id}"

    http_url = server_spans[0].attributes.get("http.url", "")
    assert "api_key=[REDACTED]" in http_url
    assert "page=2" in http_url

    assert SECRET not in _attrs_blob(spans)


def test_sensitive_headers_not_captured_by_default(make_instrumented_app):
    client, span_exporter, _ = make_instrumented_app()

    response = client.get(
        "/items/1",
        headers={
            "Authorization": f"Bearer {JWT}",
            "X-API-Key": SECRET,
        },
    )
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    for span in spans:
        for key in (span.attributes or {}):
            assert not key.startswith("http.request.header.")

    blob = _attrs_blob(spans)
    assert SECRET not in blob
    assert JWT not in blob


def test_sensitive_headers_sanitized_when_capture_enabled(make_instrumented_app, monkeypatch):
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
        "authorization,x-api-key,user-agent",
    )
    client, span_exporter, _ = make_instrumented_app()

    response = client.get(
        "/items/1",
        headers={
            "Authorization": f"Bearer {JWT}",
            "X-API-Key": SECRET,
            "User-Agent": "obs-test-agent",
        },
    )
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind == SpanKind.SERVER]
    assert len(server_spans) == 1
    attrs = server_spans[0].attributes

    assert attrs.get("http.request.header.authorization") == ("[REDACTED]",)
    assert attrs.get("http.request.header.x_api_key") == ("[REDACTED]",)
    assert attrs.get("http.request.header.user_agent") == ("obs-test-agent",)

    blob = _attrs_blob(spans)
    assert SECRET not in blob
    assert JWT not in blob


def test_websocket_span_redacts_api_key_and_skips_message_spans(make_instrumented_app):
    client, span_exporter, _ = make_instrumented_app()

    with client.websocket_connect(f"/ws/job-1?api_key={SECRET}") as websocket:
        websocket.send_text("ping")
        message = websocket.receive_text()
        assert message == "echo:ping"

    spans = span_exporter.get_finished_spans()
    assert SECRET not in _attrs_blob(spans)

    urls = [span.attributes.get("http.url", "") for span in spans]
    assert any("api_key=[REDACTED]" in url for url in urls if url)

    assert not any(
        span.name.endswith(" receive") or span.name.endswith(" send") for span in spans
    )


def test_websocket_span_redacts_token_query_and_skips_message_spans(make_instrumented_app):
    client, span_exporter, _ = make_instrumented_app()

    with client.websocket_connect(f"/ws/job-1?token={JWT}") as websocket:
        websocket.send_text("ping")
        message = websocket.receive_text()
        assert message == "echo:ping"

    spans = span_exporter.get_finished_spans()
    assert JWT not in _attrs_blob(spans)

    urls = [span.attributes.get("http.url", "") for span in spans]
    assert any("token=[REDACTED]" in url for url in urls if url)

    assert not any(
        span.name.endswith(" receive") or span.name.endswith(" send") for span in spans
    )


def test_health_check_is_not_traced(make_instrumented_app):
    client, span_exporter, _ = make_instrumented_app()

    response = client.get("/health-check")
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    assert not any("health-check" in span.name for span in spans)


def test_http_metrics_recorded_without_secrets(make_instrumented_app):
    client, _, metric_reader = make_instrumented_app()

    response = client.get(f"/items/1?api_key={SECRET}")
    assert response.status_code == 200

    metrics_json = metric_reader.get_metrics_data().to_json()
    assert "http.server" in metrics_json
    assert SECRET not in metrics_json


def test_app_module_skips_instrumentation_when_disabled():
    assert os.environ.get("OBS_ENABLED") == "false"

    import app as app_module

    assert app_module._observability_enabled is False
    assert getattr(app_module.app, "_is_instrumented_by_opentelemetry", False) is False


def test_app_module_instruments_when_enabled():
    """Prova que app.py chama instrument_fastapi_app quando o bootstrap reporta habilitado.

    Roda em subprocesso dedicado (isola o estado global do OTel e o objeto
    módulo-level app.app do processo pytest atual, que já importou app.py
    com OBS_ENABLED=false no teste anterior).
    """
    repo_root = Path(__file__).resolve().parents[2]
    backend_dir = repo_root / "backend"

    script = (
        "import core.observability as obs\n"
        "obs.bootstrap_observability = lambda *args, **kwargs: True\n"
        "import app\n"
        "print('INSTRUMENTED=' + str(app.app._is_instrumented_by_opentelemetry))\n"
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert "INSTRUMENTED=True" in result.stdout
