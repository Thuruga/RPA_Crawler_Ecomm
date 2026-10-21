"""
Bootstrap de observabilidade (OBS-01).

Este é o único módulo que fala diretamente com o SDK do OpenTelemetry.
Segue D-01 (backend greenfield para observabilidade — sem OTel/structlog
antes desta phase) e D-02 (exportação OTLP/HTTP direta para os endpoints
da infraestrutura).

O pipeline de logs redige segredos ANTES de qualquer exportação: nenhuma
string logada pela aplicação (corpo, atributos extra, texto de exceção)
chega ao Loki sem passar primeiro pelo SecretRedactingLogProcessor. Os
hooks de span (FastAPI/httpx) fazem o mesmo para http.url/url.full/
url.query/http.target, removendo segredos de query string antes das spans
saírem para o Tempo.

`core/` neste projeto usa módulos simples com estado global e funções
soltas (ver core/db.py) — não uma classe "Manager".
"""

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    Resource,
    SERVICE_NAME,
    SERVICE_NAMESPACE,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from config import DEFAULT_OBS_SERVICE_NAME, Settings, settings

logger = logging.getLogger("observability")

REDACTED = "[REDACTED]"

# Nomes de headers sensíveis: nunca capturados por instrumentação HTTP,
# mesmo que a captura seja habilitada via env var padrão do OTel.
SENSITIVE_HEADER_NAMES: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
)

# Chaves de atributo cujo NOME sugere conteúdo sensível — o valor inteiro
# vira REDACTED independentemente do formato (ex.: extra={"x_api_key": ...}).
_SENSITIVE_ATTRIBUTE_KEY_RE = re.compile(
    r"authorization|cookie|api[-_]?key|token|secret|password|passwd|senha",
    re.IGNORECASE,
)

# user:pass@ em URLs (DATABASE_URL, RABBITMQ_URL, proxies) — mantém o
# esquema e o host, redige só as credenciais embutidas.
_URL_USERINFO_RE = re.compile(
    r"\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@",
    re.IGNORECASE,
)

# "Authorization: Bearer <token>" — mantém a palavra Bearer, redige o token.
_BEARER_RE = re.compile(
    r"\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)

# JWT completo solto no texto (header.payload.signature).
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")

# Chaves e tokens no formato Clerk (ak_/sk_/pk_... e tokens M2M mt_/m2m_...).
_CLERK_KEY_RE = re.compile(r"\b(?:key|token|sec)_[A-Za-z0-9_]{8,}")

# pares chave=valor / chave: "valor" para nomes sensíveis — mantém a chave
# e o separador, redige só o valor.
_KEY_VALUE_SECRET_RE = re.compile(
    r"""\b(x-api-key|api[-_]?key|access[-_]?token|token|secret|password|passwd|senha|authorization)"""
    r"""(["']?\s*[:=]\s*["']?)([^\s"'&;,]+)""",
    re.IGNORECASE,
)


def redact_secrets(text: str, known_secrets: Iterable[str] = ()) -> str:
    """Remove segredos conhecidos e padrões comuns de credencial de uma string.

    Função pura: não lança, não faz I/O. Retorna a entrada inalterada se
    não for uma string (defesa contra corpos de log não textuais).
    """
    if not isinstance(text, str):
        return text

    cleaned = text
    # Segredos conhecidos primeiro (valores exatos do INTERNAL_API_KEY/
    # SCRAPERAPI_KEY), do mais longo para o mais curto, para que um valor
    # que seja substring de outro não deixe sobras.
    known = sorted((s for s in known_secrets if s and len(s) >= 6), key=len, reverse=True)
    for secret in known:
        cleaned = cleaned.replace(secret, REDACTED)

    cleaned = _URL_USERINFO_RE.sub(r"\1" + REDACTED + "@", cleaned)
    cleaned = _BEARER_RE.sub(r"\1 " + REDACTED, cleaned)
    cleaned = _JWT_RE.sub(REDACTED, cleaned)
    cleaned = _CLERK_KEY_RE.sub(REDACTED, cleaned)
    cleaned = _KEY_VALUE_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, cleaned)
    return cleaned


def known_secret_values(config: Settings) -> tuple[str, ...]:
    """Valores de segredo conhecidos (INTERNAL_API_KEY, SCRAPERAPI_KEY, INTERNAL_API_KEY) com >= 6 chars."""
    candidates = (config.INTERNAL_API_KEY, config.SCRAPERAPI_KEY, getattr(config, "INTERNAL_API_KEY", None))
    return tuple(
        value.strip()
        for value in candidates
        if value and len(value.strip()) >= 6
    )


class SecretRedactingLogProcessor(LogRecordProcessor):
    """Processor de log que redige segredos do corpo e dos atributos.

    Deve ser registrado ANTES do processor de exportação em lote — os
    processors do LoggerProvider rodam em ordem de registro, então isso
    garante que o dado já sai limpo quando chega ao processor que exporta.
    """

    def __init__(self, known_secrets: Iterable[str] = ()):
        self._known_secrets = tuple(known_secrets)

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        try:
            if isinstance(record.body, str):
                record.body = redact_secrets(record.body, self._known_secrets)

            attributes = record.attributes
            if attributes is not None:
                for key in list(attributes.keys()):
                    value = attributes[key]
                    if _SENSITIVE_ATTRIBUTE_KEY_RE.search(key):
                        attributes[key] = REDACTED
                    elif isinstance(value, str):
                        attributes[key] = redact_secrets(value, self._known_secrets)
                    elif isinstance(value, (list, tuple)):
                        attributes[key] = type(value)(
                            redact_secrets(item, self._known_secrets)
                            if isinstance(item, str)
                            else item
                            for item in value
                        )
        except Exception:
            # Fail-closed: nunca deixamos um corpo/atributo não-limpo escapar
            # e nunca propagamos a exceção para o código que chamou logging.
            try:
                record.body = "[REDACTION FAILED]"
            except Exception:
                pass
            try:
                record.attributes.clear()
            except Exception:
                try:
                    record.attributes = {}
                except Exception:
                    pass

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_URL_ATTRIBUTE_KEYS = ("http.url", "url.full", "url.query", "http.target")


def _scrub_url_attributes(span) -> None:
    """Redige segredos de query string dos atributos de URL de uma span."""
    if not span.is_recording():
        return
    attributes = getattr(span, "attributes", None)
    if not attributes:
        return
    for key in _URL_ATTRIBUTE_KEYS:
        value = attributes.get(key)
        if isinstance(value, str):
            cleaned = redact_secrets(value)
            if cleaned != value:
                span.set_attribute(key, cleaned)


def scrub_server_span(span, scope) -> None:
    """server_request_hook do FastAPIInstrumentor: roda após os atributos iniciais."""
    _scrub_url_attributes(span)


def scrub_client_span(span, request_info) -> None:
    """request_hook síncrono do HTTPXClientInstrumentor."""
    _scrub_url_attributes(span)


async def scrub_client_span_async(span, request_info) -> None:
    """async_request_hook do HTTPXClientInstrumentor (usado pelo Prisma via httpx.AsyncClient)."""
    _scrub_url_attributes(span)


@dataclass(frozen=True)
class ObservabilityProviders:
    tracer_provider: Optional[TracerProvider]
    meter_provider: Optional[MeterProvider]
    logger_provider: Optional[LoggerProvider]


# Guard de idempotência: bootstrap_observability só registra os providers
# uma vez por processo (protege contra --reload e re-import em testes).
_providers: Optional[ObservabilityProviders] = None


def _non_empty(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_resource(config: Settings) -> Resource:
    """Monta o Resource OTel com os atributos service.name/namespace/deployment.environment."""
    attributes = {
        SERVICE_NAME: _non_empty(config.OBS_SERVICE_NAME) or DEFAULT_OBS_SERVICE_NAME,
    }
    environment = _non_empty(config.OBS_ENVIRONMENT) or "development"
    attributes[DEPLOYMENT_ENVIRONMENT] = environment
    # Chave legada mantida porque o Grafana Application Observability ainda
    # indexa deployment.environment (sem .name) em alguns dashboards.
    attributes["deployment.environment.name"] = environment
    product_name = _non_empty(config.OBS_PRODUCT_NAME)
    if product_name:
        attributes[SERVICE_NAMESPACE] = product_name
    return Resource.create(attributes)


def loki_logs_endpoint(host: str) -> str:
    """Deriva a URL de ingestão OTLP nativa do Loki (Loki 3+) a partir do host base."""
    stripped = host.strip().rstrip("/")
    if stripped.endswith("/v1/logs"):
        return stripped
    if stripped.endswith("/otlp"):
        return f"{stripped}/v1/logs"
    return f"{stripped}/otlp/v1/logs"


def check_endpoint_configuration(config: Settings) -> list[str]:
    """Avisos de sanidade de endpoint — nunca revela o valor, só o nome da variável."""
    messages: list[str] = []

    traces_endpoint = _non_empty(config.OBS_OTEL_TRACES_EXPORTER_ENDPOINT)
    metrics_endpoint = _non_empty(config.OBS_OTEL_METRICS_EXPORTER_ENDPOINT)
    loki_host = _non_empty(config.OBS_LOKI_HOST)

    if traces_endpoint and not traces_endpoint.rstrip("/").endswith("/v1/traces"):
        messages.append(
            "OBS_OTEL_TRACES_EXPORTER_ENDPOINT deveria terminar em /v1/traces "
            "(os valores do D-02 podem estar trocados com o endpoint de métricas); "
            "o valor não foi alterado."
        )
    if metrics_endpoint and not metrics_endpoint.rstrip("/").endswith("/v1/metrics"):
        messages.append(
            "OBS_OTEL_METRICS_EXPORTER_ENDPOINT deveria terminar em /v1/metrics "
            "(os valores do D-02 podem estar trocados com o endpoint de traces); "
            "o valor não foi alterado."
        )

    environment = (config.OBS_ENVIRONMENT or "").strip().lower()
    if environment not in {"development", "dev", "local"}:
        for var_name, endpoint in (
            ("OBS_OTEL_TRACES_EXPORTER_ENDPOINT", traces_endpoint),
            ("OBS_OTEL_METRICS_EXPORTER_ENDPOINT", metrics_endpoint),
            ("OBS_LOKI_HOST", loki_host),
        ):
            if endpoint and endpoint.lower().startswith("http://"):
                messages.append(
                    f"{var_name} usa http:// em texto claro no ambiente '{config.OBS_ENVIRONMENT}'; "
                    "configure https em ambiente de producao."
                )

    return messages


def build_providers(config: Settings) -> Optional[ObservabilityProviders]:
    """Constrói TracerProvider/MeterProvider/LoggerProvider a partir da config.

    Retorna None quando OBS_ENABLED é False ou quando nenhum dos três
    endpoints está configurado (nada a exportar). Cada sinal individual
    fica None quando o endpoint correspondente está vazio.
    """
    traces_endpoint = _non_empty(config.OBS_OTEL_TRACES_EXPORTER_ENDPOINT)
    metrics_endpoint = _non_empty(config.OBS_OTEL_METRICS_EXPORTER_ENDPOINT)
    loki_host = _non_empty(config.OBS_LOKI_HOST)

    if not config.OBS_ENABLED:
        return None
    if not traces_endpoint and not metrics_endpoint and not loki_host:
        return None

    resource = build_resource(config)

    tracer_provider = None
    if traces_endpoint:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint))
        )

    meter_provider = None
    if metrics_endpoint:
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint)
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    logger_provider = None
    if loki_host:
        logger_provider = LoggerProvider(resource=resource)
        # A ordem importa: redação PRIMEIRO, exportação em lote DEPOIS.
        logger_provider.add_log_record_processor(
            SecretRedactingLogProcessor(known_secret_values(config))
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=loki_logs_endpoint(loki_host))
            )
        )

    return ObservabilityProviders(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )


def _is_application_record(record: logging.LogRecord) -> bool:
    """Filtro do handler OTel: nunca reexporta logs internos do próprio OTel.

    Evita um loop de feedback quando o próprio exportador falha e loga o erro.
    """
    return not record.name.startswith("opentelemetry")


def bootstrap_observability(config: Settings = settings) -> bool:
    """Registra os providers OTel uma única vez por processo.

    Chamado a partir de app.py, antes de `FastAPI()`, ao lado de
    `logging.basicConfig` (D-01) — nunca o substitui. Retorna True quando
    os providers foram (ou já tinham sido) registrados, False quando
    OBS_ENABLED é False ou nenhum endpoint está configurado.
    """
    global _providers

    if _providers is not None:
        return True

    if config.OBS_ENABLED:
        for message in check_endpoint_configuration(config):
            logger.warning(message)

    providers = build_providers(config)
    if providers is None:
        logger.info(
            "Observabilidade OTel desabilitada (OBS_ENABLED=false ou nenhum endpoint configurado)."
        )
        return False

    if providers.tracer_provider is not None:
        trace.set_tracer_provider(providers.tracer_provider)
    if providers.meter_provider is not None:
        metrics.set_meter_provider(providers.meter_provider)
    if providers.logger_provider is not None:
        set_logger_provider(providers.logger_provider)
        handler = LoggingHandler(level=logging.INFO, logger_provider=providers.logger_provider)
        handler.addFilter(_is_application_record)
        logging.getLogger().addHandler(handler)

    if providers.tracer_provider is not None or providers.meter_provider is not None:
        # Instrumenta httpx (o Prisma fala com o query engine via
        # httpx.AsyncClient — isso dá visibilidade de queries Postgres/Prisma).
        # aiohttp/curl_cffi de scraping NÃO são instrumentados de propósito.
        HTTPXClientInstrumentor().instrument(
            tracer_provider=providers.tracer_provider,
            meter_provider=providers.meter_provider,
            request_hook=scrub_client_span,
            async_request_hook=scrub_client_span_async,
        )

    _providers = providers
    logger.info(
        "Observabilidade OTel inicializada (traces=%s, metrics=%s, logs=%s).",
        providers.tracer_provider is not None,
        providers.meter_provider is not None,
        providers.logger_provider is not None,
    )
    return True


def instrument_fastapi_app(app, *, tracer_provider=None, meter_provider=None) -> None:
    """Instrumenta o app FastAPI com as opções de segurança do OBS-01."""
    FastAPIInstrumentor.instrument_app(
        app,
        # Todo atributo de URL de span passa pelo scrub antes da span fechar.
        server_request_hook=scrub_server_span,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        # Mantém /health-check fora do Tempo/Mimir (ruído de probe, sem valor).
        excluded_urls="health-check",
        # Redige headers de auth mesmo se a captura de headers for ligada via
        # env var padrão do OTel (OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*).
        http_capture_headers_sanitize_fields=list(SENSITIVE_HEADER_NAMES),
        # Evita uma span por mensagem WebSocket em /ws/{job_id}.
        exclude_spans=["receive", "send"],
    )
