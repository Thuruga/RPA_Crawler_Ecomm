"""
Testes de backend/core/observability.py (OBS-01).

Task 1: primitivas de redação (redact_secrets, SecretRedactingLogProcessor,
hooks de scrub de URL em spans). Nomeados com o prefixo test_redaction_ para
que `-k redaction` selecione exatamente este conjunto (15-VALIDATION.md).

Task 2: builders de provider, bootstrap idempotente, avisos de sanidade de
endpoint e o helper de instrumentação do FastAPI.
"""

import asyncio
import logging

import pytest

import core.observability as observability
from config import Settings
from core.observability import (
    REDACTED,
    SENSITIVE_HEADER_NAMES,
    SecretRedactingLogProcessor,
    known_secret_values,
    redact_secrets,
    scrub_client_span,
    scrub_client_span_async,
    scrub_server_span,
)

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


# ---------------------------------------------------------------------------
# Task 1: test_redaction_*
# ---------------------------------------------------------------------------


def test_redaction_url_credentials_and_query_key():
    text = "GET http://user:pass@proxy.test/a?api_key=super-secret-value&page=2"
    cleaned = redact_secrets(text)
    assert "http://[REDACTED]@proxy.test" in cleaned
    assert "api_key=[REDACTED]" in cleaned
    assert "page=2" in cleaned
    assert "user:pass" not in cleaned
    assert "super-secret-value" not in cleaned


def test_redaction_header_like_pairs():
    text_json = '"X-API-Key": "super-secret-value"'
    text_plain = "x-api-key: super-secret-value"
    cleaned_json = redact_secrets(text_json)
    cleaned_plain = redact_secrets(text_plain)
    assert "super-secret-value" not in cleaned_json
    assert REDACTED in cleaned_json
    assert "super-secret-value" not in cleaned_plain
    assert REDACTED in cleaned_plain


def test_redaction_bearer_and_jwt():
    text = "Authorization: Bearer abcdefghijklmnop123"
    cleaned = redact_secrets(text)
    assert "abcdefghijklmnop123" not in cleaned

    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEifQ.c2lnbmF0dXJlLXZhbHVl"
    cleaned_jwt = redact_secrets(jwt)
    assert jwt not in cleaned_jwt


def test_redaction_secret_keys():
    for key in (
        "ak_N1234567890abcdef",
        "sk_test_abcdefgh12345678",
        "pk_live_abcdefgh12345678",
        "mt_live_abcdefgh12345678",
        "m2m_live_abcdefgh12345678",
    ):
        cleaned = redact_secrets(key)
        assert key not in cleaned


def test_redaction_known_secret_values():
    assert redact_secrets(
        "valor dev-api-key vazou", known_secrets=("dev-api-key",)
    ) == "valor [REDACTED] vazou"

    values = known_secret_values(
        Settings(_env_file=None, INTERNAL_API_KEY="dev-api-key", SCRAPERAPI_KEY=None, )
    )
    assert values == ("dev-api-key",)

    values_with_m2m = known_secret_values(
        Settings(
            _env_file=None,
            INTERNAL_API_KEY="dev-api-key",
            SCRAPERAPI_KEY=None,
            
        )
    )
    
    assert "dev-api-key" in values_with_m2m


def test_redaction_no_false_positives():
    text = "SKU sku_12345678 preço R$ 199,90 categoria camisas"
    assert redact_secrets(text) == text
    assert redact_secrets("desk_abcdefgh") == "desk_abcdefgh"
    assert redact_secrets("tokens=5") == "tokens=5"
    assert redact_secrets("format_12345678") == "format_12345678"


def test_redaction_log_processor_scrubs_ws_access_token():
    provider = LoggerProvider()
    exporter = InMemoryLogRecordExporter()
    provider.add_log_record_processor(SecretRedactingLogProcessor())
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    test_logger = logging.getLogger("test.obs.redaction_ws_log")
    test_logger.propagate = False
    test_logger.setLevel(logging.INFO)
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    test_logger.addHandler(handler)

    jwt_token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEifQ.c2lnbmF0dXJlLXZhbHVl"
    access_line = f'127.0.0.1:51234 - "GET /ws/job-1?token={jwt_token} HTTP/1.1" 101 Switching Protocols'
    try:
        test_logger.info(access_line)
        finished = exporter.get_finished_logs()
        assert len(finished) == 1
        body = finished[0].log_record.body
        assert "token=[REDACTED]" in body
        assert jwt_token not in body
    finally:
        test_logger.removeHandler(handler)
        provider.shutdown()


def test_redaction_log_processor_scrubs_body_attributes_and_exception():
    provider = LoggerProvider()
    exporter = InMemoryLogRecordExporter()
    provider.add_log_record_processor(
        SecretRedactingLogProcessor(known_secrets=("dev-api-key",))
    )
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    test_logger = logging.getLogger("test.obs.redaction")
    test_logger.propagate = False
    test_logger.setLevel(logging.DEBUG)
    handler = LoggingHandler(level=logging.DEBUG, logger_provider=provider)
    test_logger.addHandler(handler)

    try:
        test_logger.error(
            "falha GET http://u:p@proxy.test/?api_key=super-secret-value",
            extra={
                "x_api_key": "super-secret-value",
                "detalhe": "token=super-secret-value",
            },
        )
        try:
            raise RuntimeError(
                "conexão postgresql://postgres:super-secret-value@db:5432/x"
            )
        except RuntimeError:
            test_logger.exception("erro de conexão")

        finished = exporter.get_finished_logs()
        assert len(finished) == 2
        for record in finished:
            assert "super-secret-value" not in record.to_json()
        assert finished[0].log_record.attributes.get("x_api_key") == REDACTED
    finally:
        test_logger.removeHandler(handler)
        provider.shutdown()


def test_redaction_log_processor_fails_closed(monkeypatch):
    provider = LoggerProvider()
    exporter = InMemoryLogRecordExporter()
    provider.add_log_record_processor(SecretRedactingLogProcessor())
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    test_logger = logging.getLogger("test.obs.redaction_fail_closed")
    test_logger.propagate = False
    test_logger.setLevel(logging.DEBUG)
    handler = LoggingHandler(level=logging.DEBUG, logger_provider=provider)
    test_logger.addHandler(handler)

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(observability, "redact_secrets", _raise)

    try:
        test_logger.error("mensagem qualquer", extra={"a": "b"})
        finished = exporter.get_finished_logs()
        assert len(finished) == 1
        assert finished[0].log_record.body == "[REDACTION FAILED]"
        assert dict(finished[0].log_record.attributes) == {}
    finally:
        test_logger.removeHandler(handler)
        provider.shutdown()


def test_redaction_span_url_hooks():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span(
        "server-span",
        attributes={
            "http.url": "http://testserver/ws/job?api_key=super-secret-value",
            "url.query": "api_key=super-secret-value",
        },
    ) as span:
        scrub_server_span(span, {})

    finished = exporter.get_finished_spans()
    server_span = [s for s in finished if s.name == "server-span"][0]
    for key in ("http.url", "url.query"):
        value = server_span.attributes.get(key)
        assert value is not None
        assert REDACTED in value
        assert "super-secret-value" not in value

    exporter.clear()

    with tracer.start_as_current_span(
        "client-span-sync",
        attributes={"http.url": "http://testserver/x?api_key=super-secret-value"},
    ) as span:
        scrub_client_span(span, None)

    finished = exporter.get_finished_spans()
    client_span = [s for s in finished if s.name == "client-span-sync"][0]
    assert "super-secret-value" not in client_span.attributes.get("http.url")

    exporter.clear()

    async def _run_async_hook():
        with tracer.start_as_current_span(
            "client-span-async",
            attributes={"http.url": "http://testserver/y?api_key=super-secret-value"},
        ) as span:
            await scrub_client_span_async(span, None)

    asyncio.run(_run_async_hook())

    finished = exporter.get_finished_spans()
    client_span_async = [s for s in finished if s.name == "client-span-async"][0]
    assert "super-secret-value" not in client_span_async.attributes.get("http.url")

    provider.shutdown()

    # Nenhum hook levanta exceção quando chamado com trace.INVALID_SPAN.
    scrub_server_span(trace.INVALID_SPAN, {})
    scrub_client_span(trace.INVALID_SPAN, None)
    asyncio.run(scrub_client_span_async(trace.INVALID_SPAN, None))


# ---------------------------------------------------------------------------
# Task 2: helpers + test_build_*, test_endpoint_*, test_bootstrap_*, test_instrument_*
# ---------------------------------------------------------------------------


def _config(**overrides):
    defaults = dict(
        _env_file=None,
        OBS_ENABLED=True,
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT=None,
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT=None,
        OBS_LOKI_HOST=None,
        OBS_SERVICE_NAME="rpa-crawler-competitor-catalog",
        OBS_PRODUCT_NAME=None,
        OBS_ENVIRONMENT="development",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def reset_providers():
    """Reseta o guard de idempotência e o estado do root logger entre testes."""
    observability._providers = None
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    added = [h for h in root.handlers if h not in before]
    for handler in added:
        root.removeHandler(handler)
        close = getattr(handler, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass
    observability._providers = None


class _FakeSpanExporter(observability.SpanExporter):
    def __init__(self, *_, endpoint=None, **__):
        super().__init__()
        self.endpoint = endpoint
        _FAKE_SPAN_EXPORTERS.append(self)

    def export(self, spans):
        return observability.SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class _FakeMetricExporter(observability.MetricExporter):
    def __init__(self, *_, endpoint=None, **__):
        super().__init__()
        self.endpoint = endpoint
        _FAKE_METRIC_EXPORTERS.append(self)

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        return observability.MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass

    def force_flush(self, timeout_millis=10_000):
        return True


class _FakeLogExporter(observability.LogRecordExporter):
    def __init__(self, *_, endpoint=None, **__):
        self.endpoint = endpoint
        self.exported = []
        _FAKE_LOG_EXPORTERS.append(self)

    def export(self, batch):
        self.exported.extend(batch)
        return observability.LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30_000):
        return True


_FAKE_SPAN_EXPORTERS = []
_FAKE_METRIC_EXPORTERS = []
_FAKE_LOG_EXPORTERS = []


@pytest.fixture
def fake_exporters(monkeypatch):
    _FAKE_SPAN_EXPORTERS.clear()
    _FAKE_METRIC_EXPORTERS.clear()
    _FAKE_LOG_EXPORTERS.clear()
    monkeypatch.setattr(observability, "OTLPSpanExporter", _FakeSpanExporter)
    monkeypatch.setattr(observability, "OTLPMetricExporter", _FakeMetricExporter)
    monkeypatch.setattr(observability, "OTLPLogExporter", _FakeLogExporter)
    yield {
        "spans": _FAKE_SPAN_EXPORTERS,
        "metrics": _FAKE_METRIC_EXPORTERS,
        "logs": _FAKE_LOG_EXPORTERS,
    }


def _shutdown_providers(providers):
    if providers is None:
        return
    if providers.tracer_provider is not None:
        providers.tracer_provider.shutdown()
    if providers.meter_provider is not None:
        providers.meter_provider.shutdown()
    if providers.logger_provider is not None:
        providers.logger_provider.shutdown()


def test_build_resource_defaults_and_fallbacks():
    config = _config(OBS_SERVICE_NAME="", OBS_PRODUCT_NAME="")
    resource = observability.build_resource(config)
    attrs = resource.attributes
    assert attrs["service.name"] == "rpa-crawler-competitor-catalog"
    assert attrs["deployment.environment"] == "development"
    assert attrs["deployment.environment.name"] == "development"
    assert "service.namespace" not in attrs

    config2 = _config(OBS_PRODUCT_NAME="aramis-intel", OBS_SERVICE_NAME="svc-x")
    resource2 = observability.build_resource(config2)
    attrs2 = resource2.attributes
    assert attrs2["service.namespace"] == "aramis-intel"
    assert attrs2["service.name"] == "svc-x"


def test_build_loki_logs_endpoint():
    assert (
        observability.loki_logs_endpoint("http://loki.test:3100")
        == "http://loki.test:3100/otlp/v1/logs"
    )
    assert (
        observability.loki_logs_endpoint("http://loki.test:3100/")
        == "http://loki.test:3100/otlp/v1/logs"
    )
    assert (
        observability.loki_logs_endpoint("http://loki.test:3100/otlp")
        == "http://loki.test:3100/otlp/v1/logs"
    )
    assert (
        observability.loki_logs_endpoint("http://loki.test:3100/otlp/v1/logs")
        == "http://loki.test:3100/otlp/v1/logs"
    )


def test_build_providers_disabled_or_unconfigured_returns_none(reset_providers, fake_exporters):
    config = _config(
        OBS_ENABLED=False,
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT="http://collector.test:4318/v1/metrics",
        OBS_LOKI_HOST="http://loki.test:3100",
    )
    assert observability.build_providers(config) is None

    config2 = _config(OBS_ENABLED=True)
    assert observability.build_providers(config2) is None


def test_build_providers_routes_each_signal_verbatim(reset_providers, fake_exporters):
    config = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT="http://collector.test:4318/v1/metrics",
        OBS_LOKI_HOST="http://loki.test:3100",
    )
    providers = observability.build_providers(config)
    try:
        assert fake_exporters["spans"][-1].endpoint == "http://collector.test:4318/v1/traces"
        assert fake_exporters["metrics"][-1].endpoint == "http://collector.test:4318/v1/metrics"
        assert fake_exporters["logs"][-1].endpoint == "http://loki.test:3100/otlp/v1/logs"
    finally:
        _shutdown_providers(providers)

    config2 = _config(OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces")
    providers2 = observability.build_providers(config2)
    try:
        assert providers2.tracer_provider is not None
        assert providers2.meter_provider is None
        assert providers2.logger_provider is None
    finally:
        _shutdown_providers(providers2)


def test_build_providers_redacts_before_export(reset_providers, fake_exporters):
    config = _config(OBS_LOKI_HOST="http://loki.test:3100")
    providers = observability.build_providers(config)
    assert providers.logger_provider is not None

    test_logger = logging.getLogger("test.obs.build_providers_redaction")
    test_logger.propagate = False
    test_logger.setLevel(logging.DEBUG)
    handler = LoggingHandler(level=logging.DEBUG, logger_provider=providers.logger_provider)
    test_logger.addHandler(handler)

    try:
        test_logger.error("api_key=super-secret-value")
        providers.logger_provider.force_flush()
        fake_exporter = fake_exporters["logs"][-1]
        assert len(fake_exporter.exported) == 1
        assert "super-secret-value" not in fake_exporter.exported[0].to_json()
    finally:
        test_logger.removeHandler(handler)
        _shutdown_providers(providers)


def test_endpoint_configuration_warnings():
    good = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT="http://collector.test:4318/v1/metrics",
        OBS_ENVIRONMENT="development",
    )
    assert observability.check_endpoint_configuration(good) == []

    swapped = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/metrics",
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_ENVIRONMENT="development",
    )
    messages = observability.check_endpoint_configuration(swapped)
    assert any(
        "OBS_OTEL_TRACES_EXPORTER_ENDPOINT" in m and "/v1/traces" in m for m in messages
    )
    assert any(
        "OBS_OTEL_METRICS_EXPORTER_ENDPOINT" in m and "/v1/metrics" in m for m in messages
    )
    for m in messages:
        assert "collector.test" not in m
        assert "loki.test" not in m

    plain_prod = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_ENVIRONMENT="production",
    )
    prod_messages = observability.check_endpoint_configuration(plain_prod)
    assert any("https" in m and "OBS_OTEL_TRACES_EXPORTER_ENDPOINT" in m for m in prod_messages)

    plain_dev = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_ENVIRONMENT="development",
    )
    dev_messages = observability.check_endpoint_configuration(plain_dev)
    assert not any("https" in m for m in dev_messages)


def test_bootstrap_disabled_is_noop(reset_providers, monkeypatch):
    called = {"trace": False, "metrics": False, "logs": False, "httpx": False}

    monkeypatch.setattr(observability.trace, "set_tracer_provider", lambda *a, **k: called.__setitem__("trace", True))
    monkeypatch.setattr(observability.metrics, "set_meter_provider", lambda *a, **k: called.__setitem__("metrics", True))
    monkeypatch.setattr(observability, "set_logger_provider", lambda *a, **k: called.__setitem__("logs", True))

    class _FakeHTTPXInstrumentor:
        def instrument(self, *a, **k):
            called["httpx"] = True

    monkeypatch.setattr(observability, "HTTPXClientInstrumentor", _FakeHTTPXInstrumentor)

    root = logging.getLogger()
    before = list(root.handlers)

    config = _config(OBS_ENABLED=False)
    result = observability.bootstrap_observability(config)

    assert result is False
    assert called == {"trace": False, "metrics": False, "logs": False, "httpx": False}
    assert root.handlers == before


def test_bootstrap_enabled_registers_once(reset_providers, fake_exporters, monkeypatch, caplog):
    import warnings

    set_calls = {"trace": 0, "metrics": 0, "logs": 0}
    monkeypatch.setattr(
        observability.trace,
        "set_tracer_provider",
        lambda *a, **k: set_calls.__setitem__("trace", set_calls["trace"] + 1),
    )
    monkeypatch.setattr(
        observability.metrics,
        "set_meter_provider",
        lambda *a, **k: set_calls.__setitem__("metrics", set_calls["metrics"] + 1),
    )
    monkeypatch.setattr(
        observability,
        "set_logger_provider",
        lambda *a, **k: set_calls.__setitem__("logs", set_calls["logs"] + 1),
    )

    httpx_calls = []

    class _FakeHTTPXInstrumentor:
        def instrument(self, **kwargs):
            httpx_calls.append(kwargs)

    monkeypatch.setattr(observability, "HTTPXClientInstrumentor", _FakeHTTPXInstrumentor)

    root = logging.getLogger()
    before = list(root.handlers)

    config = _config(
        OBS_OTEL_TRACES_EXPORTER_ENDPOINT="http://collector.test:4318/v1/traces",
        OBS_OTEL_METRICS_EXPORTER_ENDPOINT="http://collector.test:4318/v1/metrics",
        OBS_LOKI_HOST="http://loki.test:3100",
    )

    caplog.set_level(logging.WARNING, logger="observability")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first_result = observability.bootstrap_observability(config)

    assert first_result is True
    assert set_calls == {"trace": 1, "metrics": 1, "logs": 1}

    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1
    assert isinstance(added[0], observability.LoggingHandler)
    assert type(added[0]).__module__ == "opentelemetry.instrumentation.logging.handler"

    assert len(httpx_calls) == 1
    assert httpx_calls[0]["request_hook"] is scrub_client_span
    assert httpx_calls[0]["async_request_hook"] is scrub_client_span_async

    for w in caught:
        assert not (
            issubclass(w.category, DeprecationWarning)
            and "LoggingHandler" in str(w.message)
        )

    second_result = observability.bootstrap_observability(config)
    assert second_result is True
    assert set_calls == {"trace": 1, "metrics": 1, "logs": 1}
    assert len(httpx_calls) == 1
    assert [h for h in root.handlers if h not in before] == added


def test_bootstrap_handler_filters_otel_internal_records(reset_providers, fake_exporters, monkeypatch):
    monkeypatch.setattr(observability.trace, "set_tracer_provider", lambda *a, **k: None)
    monkeypatch.setattr(observability.metrics, "set_meter_provider", lambda *a, **k: None)
    monkeypatch.setattr(observability, "set_logger_provider", lambda *a, **k: None)

    class _FakeHTTPXInstrumentor:
        def instrument(self, **kwargs):
            pass

    monkeypatch.setattr(observability, "HTTPXClientInstrumentor", _FakeHTTPXInstrumentor)

    root = logging.getLogger()
    before = list(root.handlers)

    config = _config(OBS_LOKI_HOST="http://loki.test:3100")
    observability.bootstrap_observability(config)

    added = [h for h in root.handlers if h not in before][0]

    otel_record = logging.LogRecord(
        name="opentelemetry.exporter.otlp.proto.http",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=None,
        exc_info=None,
    )
    app_record = logging.LogRecord(
        name="App", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=None, exc_info=None,
    )
    assert not added.filter(otel_record)
    assert added.filter(app_record)


def test_instrument_fastapi_app_passes_security_options(monkeypatch):
    calls = {}

    class _FakeFastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, **kwargs):
            calls["app"] = app
            calls["kwargs"] = kwargs

    monkeypatch.setattr(observability, "FastAPIInstrumentor", _FakeFastAPIInstrumentor)

    app_sentinel = object()
    tp = object()
    mp = object()
    observability.instrument_fastapi_app(app_sentinel, tracer_provider=tp, meter_provider=mp)

    assert calls["app"] is app_sentinel
    kwargs = calls["kwargs"]
    assert kwargs["server_request_hook"] is scrub_server_span
    assert kwargs["excluded_urls"] == "health-check"
    assert kwargs["http_capture_headers_sanitize_fields"] == list(SENSITIVE_HEADER_NAMES)
    assert kwargs["exclude_spans"] == ["receive", "send"]
    assert kwargs["tracer_provider"] is tp
    assert kwargs["meter_provider"] is mp
