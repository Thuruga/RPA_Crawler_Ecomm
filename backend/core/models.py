"""
Modelo canônico de dados — Camada Bronze.

Contrato único compartilhado por todos os scrapers e pelo orquestrador.
Qualquer campo específico de uma marca que não se aplique a outra fica Optional.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, computed_field
from typing import Optional, Dict, List, Any


def coerce_money_decimal(value: Any, *, field_name: str = "money") -> Decimal | None:
    """Coerce a money value through Decimal(str(value)) and reject invalid values."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite money value")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} must be a finite money value") from None
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be a finite money value")
    return amount


def money_decimal_to_json_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def add_money_values(*values: Any) -> Decimal | None:
    total = Decimal("0")
    has_value = False
    for value in values:
        amount = coerce_money_decimal(value)
        if amount is None:
            continue
        total += amount
        has_value = True
    return total if has_value else None


def resolve_effective_price(
    price_full: float | None,
    price_discount: float | None,
    price_discount_is_delta: bool = False,
) -> float | None:
    full = coerce_money_decimal(price_full, field_name="price_full")
    discount = coerce_money_decimal(price_discount, field_name="price_discount")
    if full is None:
        return None
    if discount is None:
        return money_decimal_to_json_number(full)
    effective = full if price_discount_is_delta else discount
    return money_decimal_to_json_number(effective)


def resolve_original_price(
    price_full: float | None,
    price_discount: float | None,
    price_discount_is_delta: bool = False,
) -> float | None:
    full = coerce_money_decimal(price_full, field_name="price_full")
    discount = coerce_money_decimal(price_discount, field_name="price_discount")
    if full is None:
        return None
    if discount is None:
        return money_decimal_to_json_number(full)
    original = add_money_values(full, discount) if price_discount_is_delta else full
    return money_decimal_to_json_number(original)


class _StringEnum(str, Enum):
    """JSON-friendly base for closed conditional-offer vocabularies."""


def _utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("job timestamp must be an ISO datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("job timestamp must include a timezone")
    return value.astimezone(timezone.utc)


class JobExecutionStatus(_StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            JobExecutionStatus.COMPLETED,
            JobExecutionStatus.FAILED,
            JobExecutionStatus.INTERRUPTED,
        }


class JobProgress(BaseModel):
    stage: Optional[str] = None
    completed_units: int = Field(default=0, ge=0)
    total_units: Optional[int] = Field(default=None, ge=0)
    percent: Optional[float] = Field(default=None, ge=0, le=100)
    message: Optional[str] = None

    @model_validator(mode="after")
    def validate_honest_progress(self):
        if self.total_units is None and self.percent is not None:
            raise ValueError("percent requires total_units")
        if self.total_units is not None and self.completed_units > self.total_units:
            raise ValueError("completed_units cannot exceed total_units")
        return self


class JobAttemptRecord(BaseModel):
    attempt: int = Field(ge=1)
    status: JobExecutionStatus = JobExecutionStatus.PENDING
    progress: JobProgress = Field(default_factory=JobProgress)
    created_at: datetime = Field(default_factory=_utc_now_datetime)
    started_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=_utc_now_datetime)
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None

    @field_validator(
        "created_at", "started_at", "updated_at", "completed_at", mode="before"
    )
    @classmethod
    def validate_utc_timestamps(cls, value: Any) -> datetime | None:
        return _as_utc_datetime(value)


class JobExecutionRecord(BaseModel):
    """Persisted, attempt-aware lifecycle shared by all durable jobs."""

    model_config = {"extra": "allow"}

    job_id: str
    kind: str = "legacy"
    input: Dict[str, Any] = Field(default_factory=dict)
    status: JobExecutionStatus = JobExecutionStatus.PENDING
    progress: JobProgress = Field(default_factory=JobProgress)
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utc_now_datetime)
    started_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=_utc_now_datetime)
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    attempts: List[JobAttemptRecord] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_status(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        status = payload.get("status")
        if isinstance(status, str):
            payload["status"] = status.casefold()
        return payload

    @field_validator(
        "created_at", "started_at", "updated_at", "completed_at", mode="before"
    )
    @classmethod
    def validate_utc_timestamps(cls, value: Any) -> datetime | None:
        return _as_utc_datetime(value)

    @model_validator(mode="after")
    def seed_attempt_ledger(self):
        if not any(item.attempt == self.attempt for item in self.attempts):
            self.attempts.append(
                JobAttemptRecord(
                    attempt=self.attempt,
                    status=self.status,
                    progress=self.progress,
                    created_at=self.created_at,
                    started_at=self.started_at,
                    updated_at=self.updated_at,
                    completed_at=self.completed_at,
                    result=self.result,
                    error=self.error,
                )
            )
        return self


class OfferConditionType(_StringEnum):
    MEMBERSHIP = "membership"
    PAYMENT_METHOD = "payment_method"
    QUANTITY = "quantity"
    COUPON = "coupon"
    CHECKOUT = "checkout"
    AUTHENTICATION = "authentication"
    OTHER = "other"


class OfferEligibility(_StringEnum):
    PUBLIC = "public"
    REQUIRES_MEMBERSHIP = "requires_membership"
    REQUIRES_PAYMENT_METHOD = "requires_payment_method"
    REQUIRES_QUANTITY = "requires_quantity"
    REQUIRES_COUPON = "requires_coupon"
    REQUIRES_CHECKOUT = "requires_checkout"
    REQUIRES_AUTH = "requires_auth"
    OTHER = "other"


class OfferState(_StringEnum):
    ADVERTISED = "advertised"
    CONFIRMED = "confirmed"
    REQUIRES_AUTH = "requires_auth"
    EXPIRED = "expired"


class OfferSourceAuthority(_StringEnum):
    AUTHORITATIVE = "authoritative"
    STRUCTURED = "structured"
    ADVERTISED = "advertised"


class OfferSourceKind(_StringEnum):
    BRAND_CUSTOM_API = "brand_custom_api"
    VTEX_CATALOG = "vtex_catalog"
    VTEX_CHECKOUT = "vtex_checkout"
    SHOPIFY_PRODUCT = "shopify_product"
    SHOPIFY_CART = "shopify_cart"
    HTML = "html"
    DOM = "dom"
    FIXTURE = "fixture"
    OTHER = "other"


class OfferProbeStatus(_StringEnum):
    FOUND = "found"
    NONE_DETECTED = "none_detected"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    REQUIRES_AUTH = "requires_auth"
    ERROR = "error"


class OfferProbeLevel(_StringEnum):
    FAST = "fast"
    FULL = "full"
    CONFIRM = "confirm"


class ConditionalOfferEventType(_StringEnum):
    STARTED = "started"
    CHANGED = "changed"
    ENDED = "ended"
    STATE_CHANGED = "state_changed"


class OfferScope(BaseModel):
    product_id: Optional[str] = None
    sku_id: Optional[str] = None
    seller_id: Optional[str] = None
    category_ids: List[str] = Field(default_factory=list)
    collection_ids: List[str] = Field(default_factory=list)
    brand_ids: List[str] = Field(default_factory=list)


class OfferSource(BaseModel):
    kind: OfferSourceKind
    ref: str
    authority: OfferSourceAuthority
    observed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _normalized_offer_identity(payload: Dict[str, Any]) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _scope_value(scope: Any, name: str, default: Any = None) -> Any:
    """Read one scope field whether `scope` is an `OfferScope` or a plain dict.

    `ConditionalOfferSnapshot` stores `scope` as an untyped extra field, so it
    round-trips as a plain dict rather than an `OfferScope` instance; a bare
    `getattr(dict_instance, name, default)` silently always returns `default`.
    """
    if isinstance(scope, dict):
        return scope.get(name, default)
    return getattr(scope, name, default)


def _condition_signature_payload(offer: Any) -> Dict[str, Any]:
    """Return the normalized commercial condition, independent of scope/evidence."""
    return {
        "condition_type": _enum_value(getattr(offer, "condition_type", None)),
        "condition_label": _normalized_text(getattr(offer, "condition_label", None)),
        "eligibility": _enum_value(getattr(offer, "eligibility", None)),
        "quantity_required": getattr(offer, "quantity_required", None),
        "coupon_code": _normalized_text(getattr(offer, "coupon_code", None)),
        "payment_method": _normalized_text(getattr(offer, "payment_method", None)),
    }


def condition_signature_key(offer: Any) -> str:
    """Hash the commercial condition alone, without scope, price, state or evidence."""
    return _normalized_offer_identity(_condition_signature_payload(offer))


def _commercial_claim_payload(offer: Any) -> Dict[str, Any]:
    """Return the normalized commercial claim, independent from its evidence."""
    scope = getattr(offer, "scope", None)
    payload = _condition_signature_payload(offer)
    payload["scope"] = {
        "product_id": _normalized_text(_scope_value(scope, "product_id")),
        "sku_id": _normalized_text(_scope_value(scope, "sku_id")),
        "seller_id": _normalized_text(_scope_value(scope, "seller_id")),
    }
    return payload


def commercial_claim_key(offer: Any) -> str:
    """Hash one commercial claim without price, state or evidence identity."""
    return _normalized_offer_identity(_commercial_claim_payload(offer))


_SCALAR_SCOPE_FIELDS = ("product_id", "sku_id", "seller_id")
_LIST_SCOPE_FIELDS = ("category_ids", "collection_ids", "brand_ids")


def scopes_compatible(a: Any, b: Any) -> bool:
    """Return True when two scopes never disagree on a field known to both sides.

    A field absent/empty on either side is treated as unknown, never as a
    conflict. Both `OfferScope` instances and plain dicts (e.g. a persisted
    `ConditionalOfferSnapshot.scope` extra) are supported.
    """
    for field in _SCALAR_SCOPE_FIELDS:
        left = _normalized_text(_scope_value(a, field))
        right = _normalized_text(_scope_value(b, field))
        if left and right and left != right:
            return False
    for field in _LIST_SCOPE_FIELDS:
        left_values = {_normalized_text(item) for item in (_scope_value(a, field, []) or [])}
        right_values = {_normalized_text(item) for item in (_scope_value(b, field, []) or [])}
        left_values.discard("")
        right_values.discard("")
        if left_values and right_values and left_values.isdisjoint(right_values):
            return False
    return True


def merge_scopes(a: Any, b: Any) -> "OfferScope":
    """Reconcile two (assumed-compatible) scopes into one union scope."""
    scalars = {
        field: _scope_value(a, field) or _scope_value(b, field)
        for field in _SCALAR_SCOPE_FIELDS
    }
    lists = {
        field: list(dict.fromkeys([
            *(_scope_value(a, field, []) or []),
            *(_scope_value(b, field, []) or []),
        ]))
        for field in _LIST_SCOPE_FIELDS
    }
    return OfferScope(**scalars, **lists)


def offers_share_commercial_claim(a: Any, b: Any) -> bool:
    """Canonical claim-match predicate: same condition AND compatible scope."""
    scope_a = getattr(a, "scope", a)
    scope_b = getattr(b, "scope", b)
    return condition_signature_key(a) == condition_signature_key(b) and scopes_compatible(
        scope_a, scope_b
    )


class ConditionalOffer(BaseModel):
    offer_key: Optional[str] = None
    condition_type: OfferConditionType
    condition_label: str
    eligibility: OfferEligibility
    price: Optional[Decimal] = None
    effective_unit_price: Optional[Decimal] = None
    discount_value: Optional[Decimal] = None
    discount_unit: Optional[str] = None
    currency: str = "BRL"
    quantity_required: Optional[int] = Field(default=None, ge=1)
    coupon_code: Optional[str] = None
    payment_method: Optional[str] = None
    scope: OfferScope = Field(default_factory=OfferScope)
    state: OfferState
    source: OfferSource
    provenance: List[OfferSource] = Field(default_factory=list)
    raw_text: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    observed_from: Optional[str] = None
    observed_until: Optional[str] = None

    @model_validator(mode="after")
    def set_stable_offer_key(self):
        if not self.offer_key:
            self.offer_key = commercial_claim_key(self)
        return self


class OfferCacheMetadata(BaseModel):
    hit: bool = False
    stale: bool = False
    expires_at: Optional[str] = None


class ConditionalOfferResult(BaseModel):
    status: OfferProbeStatus
    offers: List[ConditionalOffer] = Field(default_factory=list)
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    probe_level: OfferProbeLevel = OfferProbeLevel.FAST
    sources_attempted: List[str] = Field(default_factory=list)
    diagnostics: List[str] = Field(default_factory=list)
    cache: OfferCacheMetadata = Field(default_factory=OfferCacheMetadata)


class ConditionalOfferSnapshot(BaseModel):
    """Persisted monitor view; tolerant of legacy and partially enriched offers."""

    model_config = {"extra": "allow"}

    offer_key: str
    condition_type: Optional[OfferConditionType] = None
    condition_label: str = "Oferta condicional"
    eligibility: Optional[OfferEligibility] = None
    price: Optional[Decimal] = None
    effective_unit_price: Optional[Decimal] = None
    discount_value: Optional[Decimal] = None
    discount_unit: Optional[str] = None
    currency: str = "BRL"
    quantity_required: Optional[int] = None
    coupon_code: Optional[str] = None
    payment_method: Optional[str] = None
    state: OfferState = OfferState.ADVERTISED
    source: Optional[OfferSource] = None
    raw_text: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    observed_from: Optional[str] = None
    observed_until: Optional[str] = None


class ConditionalOfferEvent(BaseModel):
    event_type: ConditionalOfferEventType
    offer_key: str
    before: Optional[ConditionalOfferSnapshot] = None
    after: Optional[ConditionalOfferSnapshot] = None
    observed_from: Optional[str] = None
    observed_until: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ShippingInfo(BaseModel):
    price: float | None = Field(
        default=None, description="Valor numérico do frete. 0.0 significa grátis."
    )
    status: str = Field(
        default="Disponível",
        description="Ex: 'Indisponível', 'Disponível', 'Calculado no checkout'",
    )
    estimated_delivery_days: int | None = None
    raw_text: str | None = Field(
        default=None,
        description="Texto original extraído (ex: 'Grátis', 'Receba amanhã')",
    )
    # Identidade da modalidade (Phase 33 — additive, safe defaults para registros antigos)
    service_name: str | None = Field(
        default=None,
        description="Nome da modalidade de entrega (ex: 'Expresso', 'Normal')",
    )
    service_id: str | None = Field(
        default=None,
        description="ID interno da SLA VTEX (campo 'id' do SLA)",
    )
    # Metadados do prazo parseados por vtex_shipping.parse_estimate
    estimate_display: str | None = Field(
        default=None,
        description="Texto PT do prazo (ex: 'Até 5 dias úteis', 'Até 12 horas')",
    )
    estimate_unit: str | None = Field(
        default=None,
        description="Unidade VTEX do prazo: 'bd', 'd', 'h' ou 'm'",
    )
    is_free_shipping: bool = Field(
        default=False,
        description="True quando price == 0.0 (grátis); False caso contrário",
    )

    @field_validator("price")
    @classmethod
    def price_must_be_valid_money(cls, v: Optional[float]) -> Optional[float]:
        amount = coerce_money_decimal(v, field_name="shipping price")
        if amount is None:
            return None
        if amount < 0:
            raise ValueError("Valor de frete negativo")
        return money_decimal_to_json_number(amount)

    @model_validator(mode="after")
    def align_free_shipping_state(self):
        amount = coerce_money_decimal(self.price, field_name="shipping price")
        if self.is_free_shipping:
            if amount is None:
                self.price = 0.0
            elif amount != 0:
                raise ValueError("Frete gratis deve ter preco zero")
            return self

        status = str(self.status or "").casefold()
        unavailable_tokens = (
            "indispon",
            "bloque",
            "tempor",
            "stale",
            "desatual",
            "unsupported",
            "suport",
        )
        if amount == 0 and not any(token in status for token in unavailable_tokens):
            self.is_free_shipping = True
        return self

class ReviewState(_StringEnum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    ERROR = "error"
    TEMPORARY_FAILURE = "temporary_failure"


class ReviewComment(BaseModel):
    """Comentario de avaliacao normalizado e compacto."""

    review_id: str
    rating: Optional[float] = None
    title: Optional[str] = None
    text: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[str] = None
    source_provider: str
    source_ref: Optional[str] = None


class ReviewCommentsResult(BaseModel):
    """Resultado sob demanda de comentarios de avaliacao."""

    reviews_state: str = "unsupported"
    comments: List[ReviewComment] = Field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    review_product_id: Optional[str] = None
    source_provider: Optional[str] = None
    max_pages: int = 0





class ColorSwatch(BaseModel):
    """Uma variação visual de cor dentro da família de produtos-irmãos
    (engine Shopify: cada cor do modelo é um PRODUTO separado, não uma
    variante — ver Resolution de
    .planning/debug/resolved/ricardo-almeida-cores-vtex.md). `name` usa a
    mesma granularidade genérica de `available_colors` (os nomes
    descritivos do widget de swatches do tema, ex. 'Cinza escuro', NÃO são
    suportados — limitação conhecida documentada na sessão de debug acima).
    `image_url`/`sibling_code` ficam `None` quando a fonte de descoberta de
    família usada não os expõe sem uma chamada de rede adicional (ex.:
    fallback de busca textual legado) — aditivo, nunca introduz um caminho
    de rede novo só para popular estes campos."""

    name: str
    image_url: Optional[str] = None
    sibling_code: Optional[str] = None


class RawProductBronze(BaseModel):
    """Dado bruto de um produto concorrente, sem transformações."""

    url: str
    brand: str
    raw_title: str
    raw_description: str = ""
    price_full: Optional[float] = None
    price_discount: Optional[float] = None
    price_discount_is_delta: bool = False
    stock_availability: Optional[bool] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    breadcrumb: List[str] = Field(
        default_factory=list,
        description="Trilha completa de categorias do produto (ex: ['Masculino', 'Calças', 'Jeans']). "
                    "Aditivo — registros antigos sem o campo continuam válidos.",
    )
    composition: Optional[str] = None
    gender: Optional[str] = None
    age_group: Optional[str] = None
    product_group: Optional[str] = None
    product_code: Optional[str] = None
    # Contexto comercial autoritativo do PDP (GAP-01, Fase 03 Plano 13) — preservado
    # diretamente do item/seller/commertialOffer selecionados, independente de haver
    # oferta condicional (Teaser/Benefit) já detectada. Aditivo: registros antigos
    # sem estes campos continuam válidos com os defaults abaixo.
    sku_id: Optional[str] = None
    seller_id: Optional[str] = None
    price_without_discount: Optional[float] = None
    collection_ids: List[str] = Field(default_factory=list)
    available_colors: List[str] = Field(default_factory=list)
    available_sizes: List[str] = Field(default_factory=list)
    # Proveniência bruta (D-01, Wave 0) — preserva exatamente o valor da fonte
    # (capitalização/ordem), independente de qualquer normalização aplicada a
    # available_colors/available_sizes. Aditivo: registros antigos sem estes
    # campos continuam válidos ([]).
    raw_colors: List[str] = Field(default_factory=list)
    raw_sizes: List[str] = Field(default_factory=list)
    # Um swatch visual por produto-irmão de cor (ver ColorSwatch acima) —
    # reaproveita dados já buscados pela descoberta de família de cores,
    # nunca uma chamada de rede nova. Aditivo: registros antigos sem o
    # campo continuam válidos ([]).
    color_swatches: List[ColorSwatch] = Field(default_factory=list)
    # Grade completa de tamanhos do produto (todas as variantes registradas na
    # fonte, independente de estoque atual) — distinto de available_sizes, que
    # reflete somente o que tem estoque agora. Ausência de dado de grade na
    # fonte produz [] (fail-closed), nunca inferido de available_sizes.
    size_grid: List[str] = Field(default_factory=list)
    raw_size_grid: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    scan_product_id: Optional[str] = None
    reviews_state: Optional[str] = Field(None, description="Estado das avaliações")

    @model_validator(mode="after")
    def _set_reviews_state(self):
        if self.reviews_state is None and (self.rating is not None or self.review_count is not None):
            self.reviews_state = "available"
        return self
    review_comments: List[ReviewComment] = Field(default_factory=list)
    review_product_id: Optional[str] = None
    specifications: Dict[str, str] = Field(default_factory=dict)
    image_url: Optional[str] = None
    shipping: ShippingInfo | None = None
    conditional_offers: List[ConditionalOffer] = Field(default_factory=list)

    is_free_shipping: bool = False
    shipping_price: Optional[float] = None
    landed_price: Optional[float] = None

    @field_validator(
        "price_full",
        "price_discount",
        "price_without_discount",
        "shipping_price",
        "landed_price",
    )
    @classmethod
    def money_fields_must_be_finite(cls, v: Optional[float]) -> Optional[float]:
        amount = coerce_money_decimal(v)
        return money_decimal_to_json_number(amount)

    @model_validator(mode="after")
    def calculate_landed_price(self):
        if self.landed_price is None:
            base_price = resolve_effective_price(
                self.price_full,
                self.price_discount,
                self.price_discount_is_delta,
            )
            if base_price is not None:
                self.landed_price = money_decimal_to_json_number(
                    add_money_values(base_price, self.shipping_price)
                )
        return self

    @field_validator("price_full")
    @classmethod
    def price_must_be_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("Preço zerado ou negativo")
        return v

    @field_validator("image_url")
    @classmethod
    def image_url_must_be_present(cls, v: Optional[str]) -> str:
        if not v or not v.strip() or v == "None":
            raise ValueError("URL da imagem ausente ou inválida")
        return v

    @field_validator("raw_title")
    @classmethod
    def title_must_be_present(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Título do produto ausente")
        return v


# ---------------------------------------------------------------------------
# Modelos de Busca Comparativa
# ---------------------------------------------------------------------------


class SearchProductResult(BaseModel):
    """
    Resultado enxuto de um produto retornado pela busca VTEX.
    Otimizado para exibição na tela comparativa — sem dados pesados de scraping.
    """

    brand: str
    product_name: str
    url: str
    price_full: Optional[float] = None
    price_discount: Optional[float] = None
    price_discount_is_delta: bool = False
    image_url: Optional[str] = None
    category: Optional[str] = None
    available: Optional[bool] = None
    product_code: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    scan_product_id: Optional[str] = None
    reviews_state: Optional[str] = Field(None, description="Estado das avaliações")

    @model_validator(mode="after")
    def _set_reviews_state(self):
        if self.reviews_state is None and (self.rating is not None or self.review_count is not None):
            self.reviews_state = "available"
        return self
    review_comments: List[ReviewComment] = Field(default_factory=list)
    review_product_id: Optional[str] = None
    available_colors: List[str] = Field(default_factory=list)
    available_sizes: List[str] = Field(default_factory=list)
    # Proveniência bruta (D-01, Wave 0) — ver RawProductBronze.raw_colors/raw_sizes.
    raw_colors: List[str] = Field(default_factory=list)
    raw_sizes: List[str] = Field(default_factory=list)
    seller: Optional[str] = None
    # Identidade do SKU/seller usada para a simulação de frete sob demanda (Phase 33.x).
    # Additive, opcional — registros antigos sem estes campos continuam válidos.
    sku_id: Optional[str] = Field(
        default=None,
        description="itemId do SKU selecionado (oferta disponível) — usado no cálculo de frete on-demand.",
    )
    seller_id: Optional[str] = Field(
        default=None,
        description="ID do seller do SKU selecionado — usado no cálculo de frete on-demand.",
    )
    shipping_product_id: Optional[str] = Field(
        default=None,
        description="ID do produto usado por providers nao-VTEX para cotacao de frete.",
    )
    shipping_variant_id: Optional[str] = Field(
        default=None,
        description="ID da variante usado por providers nao-VTEX para cotacao de frete.",
    )
    shipping_sku: Optional[str] = Field(
        default=None,
        description="SKU externo usado por providers nao-VTEX para cotacao de frete.",
    )
    shipping: ShippingInfo | None = None
    shipping_options: List[ShippingInfo] = Field(
        default_factory=list,
        description="Todas as modalidades de entrega domiciliar válidas, "
                    "ordenadas por preço asc depois prazo asc (Phase 33 VTEX).",
    )
    conditional_offers: List[ConditionalOffer] = Field(default_factory=list)

    is_free_shipping: bool = False
    shipping_price: Optional[float] = None
    landed_price: Optional[float] = None

    @field_validator("price_full", "price_discount", "shipping_price", "landed_price")
    @classmethod
    def money_fields_must_be_finite(cls, v: Optional[float]) -> Optional[float]:
        amount = coerce_money_decimal(v)
        return money_decimal_to_json_number(amount)

    @model_validator(mode="after")
    def calculate_landed_price(self):
        if self.landed_price is None:
            base_price = resolve_effective_price(
                self.price_full,
                self.price_discount,
                self.price_discount_is_delta,
            )
            if base_price is not None:
                self.landed_price = money_decimal_to_json_number(
                    add_money_values(base_price, self.shipping_price)
                )
        return self

class BrandSearchResult(BaseModel):
    """Resultado de uma marca individual na busca comparativa."""

    brand_key: str
    brand_name: str
    products: List[SearchProductResult] = Field(default_factory=list)
    error: Optional[str] = None  # Preenchido se a busca da marca falhou
    total_found: int = 0
    average_price: Optional[float] = None
    average_price_original: Optional[float] = None


class ComparisonResult(BaseModel):
    """Resposta completa da busca comparativa multi-brand."""

    query: str
    brands_searched: List[str]
    results: List[BrandSearchResult] = Field(default_factory=list)
    searched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    average_price: Optional[float] = None
    average_price_original: Optional[float] = None
    brand_averages: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Monitoramento de Preços
# ---------------------------------------------------------------------------


class PriceHistoryEntry(BaseModel):
    """Registro de uma variação de preço ou disponibilidade no tempo."""

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    price: float
    price_original: Optional[float] = None
    last_price_discount: Optional[float] = None
    available: bool
    available_colors: List[str] = Field(default_factory=list)
    available_sizes: List[str] = Field(default_factory=list)


class PriceMonitorStatus(_StringEnum):
    """Recurring-entity lifecycle (D-02): the monitor itself, not a cycle."""

    ACTIVE = "active"
    PAUSED = "paused"


class PriceCheckState(_StringEnum):
    """Terminal outcome vocabulary for one price-check cycle (D-02/D-05)."""

    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"
    STALE = "stale"
    INTERRUPTED = "interrupted"


class PriceCheckRun(BaseModel):
    """One durable ledger entry per price-check cycle (D-02, JOB-01/JOB-02).

    `status` is `None` while the cycle is claimed and in flight; it is set
    exactly once, under CAS, to one of OK/BLOCKED/ERROR/STALE/INTERRUPTED
    when the cycle reaches its terminal outcome — mirroring the
    attempt-terminal-is-absorbing contract used by `JsonJobRepository`
    (Plan 07-02) and `CategoryScanRecord` (Plan 07-05), adapted to a cycle
    that is not itself resumed (D-02: the monitor recurs, the cycle does not).
    """

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    logical_cycle_id: str
    scheduled_for: datetime
    status: Optional[PriceCheckState] = None
    created_at: datetime = Field(default_factory=_utc_now_datetime)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    @field_validator(
        "scheduled_for", "created_at", "started_at", "completed_at", mode="before"
    )
    @classmethod
    def validate_utc_timestamps(cls, value: Any) -> datetime | None:
        return _as_utc_datetime(value)


class PriceMonitorConfig(BaseModel):
    """Configuração e estado de um monitoramento ativo."""

    job_id: str
    url: str
    brand: str
    interval_minutes: int = 10
    duration_hours: int = 24
    start_time: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_price: Optional[float] = None
    last_price_original: Optional[float] = None
    last_price_discount: Optional[float] = None
    # Estado da última checagem — alimenta o card do front:
    #   None      = ainda não checado (mostra "Pendente")
    #   "ok"      = preço resolvido
    #   "blocked" = não foi possível ler o produto (anti-bot / 403 / sem dados)
    #   "error"   = erro inesperado no ciclo
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_checked_at: Optional[str] = None
    # Carimbado somente quando last_status vira "ok" (D-01/MON-01) — distinto
    # de last_checked_at, que avança em toda tentativa, sucesso ou não.
    last_success_at: Optional[str] = None
    # Próxima leitura externa liberada pelo coordinator (Fase 8, MKT-03),
    # propagado de product_data["next_retry_at"]; None para marcas próprias.
    next_retry_at: Optional[str] = None
    history: List[PriceHistoryEntry] = Field(default_factory=list)
    conditional_offers_current: List[ConditionalOfferSnapshot] = Field(default_factory=list)
    conditional_offers_status: Optional[str] = None
    conditional_offers_checked_at: Optional[str] = None
    conditional_offers_diagnostics: List[str] = Field(default_factory=list)
    conditional_offer_history: List[ConditionalOfferEvent] = Field(default_factory=list)
    active: bool = True
    image_url: Optional[str] = None
    product_name: Optional[str] = None
    available_colors: List[str] = Field(default_factory=list)
    available_sizes: List[str] = Field(default_factory=list)
    # Additive (Phase 07 Plan 05, D-02, JOB-01/JOB-02): the monitor's own
    # ACTIVE/PAUSED lifecycle, kept in sync with the legacy `active` bool by
    # PriceMonitorService's own mutators (start/stop/resume). `load_monitors`
    # boots strictly off `status`, never off the legacy `active` property.
    status: PriceMonitorStatus = PriceMonitorStatus.ACTIVE
    # Durable per-cycle ledger, no retention cap (T-07-20, accepted).
    check_runs: List[PriceCheckRun] = Field(default_factory=list)
    # The logical_cycle_id currently claimed/in-flight for this monitor, if
    # any. Only a terminal CAS transition clears it and advances the cursor.
    active_cycle_id: Optional[str] = None
    # Durable cursor: the next cycle's due instant. Seeded from start_time for
    # the very first cycle; advanced only on terminal transition, from the
    # previous due_at + interval_minutes (never from wall-clock "now").
    next_check_due_at: Optional[datetime] = None

    @field_validator("next_check_due_at", mode="before")
    @classmethod
    def validate_next_check_due_at(cls, value: Any) -> datetime | None:
        return _as_utc_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_active_to_status(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "status" not in payload:
            payload["status"] = (
                PriceMonitorStatus.ACTIVE.value
                if payload.get("active", True)
                else PriceMonitorStatus.PAUSED.value
            )
        return payload


class CategoryMonitorStatus(_StringEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class CategoryScanOrigin(_StringEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class CategoryScanStatus(_StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CategoryScanRecord(BaseModel):
    """File-backed transition contract for the future category_scans table."""

    scan_id: str = Field(default_factory=lambda: str(uuid4()))
    monitor_id: str
    brand: str
    category_url: str
    origin: CategoryScanOrigin
    status: CategoryScanStatus = CategoryScanStatus.PENDING
    kind: str = "category_scan"
    input: Dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)
    progress: JobProgress = Field(default_factory=JobProgress)
    attempts: List[JobAttemptRecord] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    product_count: int = Field(default=0, ge=0)
    # Additive (Phase 07 Plan 05, D-01/D-02, JOB-01/JOB-02): stable claim identity
    # and the aligned scheduler slot/manual scan_id this attempt was derived from.
    # Legacy rows without these fields deserialize as None; the reconciliation
    # path skips orphans that predate this migration instead of guessing an
    # identity that was never claimed.
    logical_cycle_id: Optional[str] = None
    scheduled_for: Optional[datetime] = None

    @field_validator("scheduled_for", mode="before")
    @classmethod
    def validate_scheduled_for(cls, value: Any) -> datetime | None:
        return _as_utc_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def add_reproducible_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        scheduled_for = payload.get("scheduled_for")
        payload.setdefault(
            "input",
            {
                "monitor_id": payload.get("monitor_id"),
                "brand": payload.get("brand"),
                "category_url": payload.get("category_url"),
                "origin": getattr(payload.get("origin"), "value", payload.get("origin")),
                "scheduled_for": scheduled_for.isoformat()
                if isinstance(scheduled_for, datetime)
                else scheduled_for,
            },
        )
        return payload


# ---------------------------------------------------------------------------
# Marcas Dinâmicas e Mapeamentos
# ---------------------------------------------------------------------------


class CategoryMapping(BaseModel):
    """Mapeamento entre uma categoria canônica e o caminho real na VTEX."""

    canonical_slug: str  # ex: "polos", "camisas"
    vtex_fq_path: str  # ex: "C:/1/2/"
    label: str  # ex: "Polos Masculinas"


class PromotionProbeType(_StringEnum):
    VTEX_STRUCTURED = "vtex_structured"
    SHOPIFY_STRUCTURED = "shopify_structured"
    BRAND_RULE_ENDPOINT = "brand_rule_endpoint"
    PUBLIC_HTML = "public_html"
    PUBLIC_DOM = "public_dom"


class PromotionAdapter(_StringEnum):
    VTEX = "vtex"
    SHOPIFY = "shopify"
    RESERVA_PRIME = "reserva_prime"
    HTML = "html"


class PromotionProbeConfig(BaseModel):
    """Closed, declarative promotion-probe configuration persisted per brand."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    type: PromotionProbeType
    adapter: Optional[PromotionAdapter] = None
    path: Optional[str] = Field(default=None, alias="endpoint")
    level: OfferProbeLevel = OfferProbeLevel.FULL
    options: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_declarative_probe(self):
        allowed_options = {"timeout_seconds", "required", "locale", "channel"}
        forbidden_fragments = (
            "secret", "token", "cookie", "credential", "password", "passwd",
            "authorization", "header", "api_key", "apikey", "javascript",
        )
        unknown = set(self.options) - allowed_options
        sensitive = {
            key for key in self.options
            if any(fragment in key.casefold() for fragment in forbidden_fragments)
        }
        if unknown or sensitive:
            rejected = ", ".join(sorted(unknown | sensitive))
            raise ValueError(f"unsupported or sensitive promotion probe options: {rejected}")
        if self.path is not None:
            from services.promotions.guards import validate_probe_path

            self.path = validate_probe_path(self.path)
        if self.type == PromotionProbeType.BRAND_RULE_ENDPOINT:
            if self.adapter is None or self.path is None:
                raise ValueError("brand_rule_endpoint requires a known adapter and relative path")
        elif self.path is not None:
            raise ValueError("only brand_rule_endpoint may configure a path")
        if self.adapter == PromotionAdapter.RESERVA_PRIME and self.type != PromotionProbeType.BRAND_RULE_ENDPOINT:
            raise ValueError("reserva_prime is only valid for brand_rule_endpoint")
        return self


class DynamicBrandCreate(BaseModel):
    """Dados básicos para cadastrar uma nova marca."""
    model_config = ConfigDict(extra="allow")

    brand_key: str  # ex: "reserva", "aramis"
    brand_name: str  # ex: "Reserva"
    domain: str  # ex: "www.usereserva.com"
    # URL canônica usada para a página inicial do carrossel. Alguns domínios
    # redirecionam para uma rota protegida ou para uma página sem o hero.
    banner_start_url: Optional[str] = None
    color_aliases: List[str] = Field(default_factory=list)
    size_aliases: List[str] = Field(default_factory=list)
    family_aliases: List[str] = Field(default_factory=list)
    domain_aliases: List[str] = Field(default_factory=list)
    extract_via_regex: bool = Field(default=False)

    @field_validator("domain", mode="before")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        if v:
            return v.replace("https://", "").replace("http://", "").strip("/")
        return v

    review_provider: Optional[str] = "none"  # ex: "trustvox", "vtex_native"
    review_store_id: Optional[str] = None  # ex: "78800"
    review_provider_evidence: Optional[str] = None
    review_unsupported_reason: Optional[str] = None
    vtex_account: Optional[str] = None  # ex: "foxton" (se diferente do domínio)
    vtex_sales_channel: Optional[str] = None  # ex: "18" (canal de vendas/política comercial específica)
    engine: Optional[str] = "vtex"  # ex: "vtex", "shopify"
    logo_url: Optional[str] = None  # ex: "https://.../logo.png"
    wake_access_token: Optional[str] = None  # override manual do token público de storefront Wake (D-06)
    # Override por marca da URL de busca. Necessário p/ SFCC Lacoste, cujo host canônico é
    # www.lacoste.com/br/ (o lacoste.com.br redireciona à home e perde o ?q=). {query} = termo URL-encoded.
    search_url_template: Optional[str] = None
    # Egress de IP limpo por marca (proxy Playwright). Lacoste/Akamai bloqueia IP de datacenter/corporativo;
    # None = conexão direta (dev / redes limpas). Formato: http://[user:senha@]host:porta.
    proxy_url: Optional[str] = None
    promotion_probes: List[PromotionProbeConfig] = Field(default_factory=list)

    # Campos específicos de motores (Algolia, Linx, etc.)
    algolia_app_id: Optional[str] = None
    algolia_api_key: Optional[str] = None
    algolia_index: Optional[str] = None
    algolia_origin: Optional[str] = None
    configuration: Optional[Dict[str, Any]] = None


class DynamicBrand(DynamicBrandCreate):
    """Marca completa com seus mapeamentos e estado."""

    mappings: List[CategoryMapping] = Field(default_factory=list)
    is_active: bool = True

    @computed_field
    @property
    def is_marketplace(self) -> bool:
        from core.constants import MARKETPLACE_ENGINE_KEYS
        return (
            (self.engine or "").lower() in MARKETPLACE_ENGINE_KEYS
            or self.brand_key.lower() in MARKETPLACE_ENGINE_KEYS
        )


class BrandActiveUpdate(BaseModel):
    """Payload para PATCH /brands/{brand_key}/active — V5 Input Validation (T-25-03-BODY)."""

    is_active: bool


# ---------------------------------------------------------------------------
# Histórico local de buscas
# ---------------------------------------------------------------------------

class SearchHistory(BaseModel):
    """Registro de uma pesquisa assíncrona (Histórico)."""

    job_id: str
    query: str
    target_sku: Optional[str] = Field(default=None, description="SKU alvo da busca cross (exibido no histórico no lugar do nome do produto)")
    type: str = Field(default="search", description="Tipo de busca: 'search' ou 'cross'")
    brands: List[str] = Field(default_factory=list)
    status: str = Field(default="PENDING", description="PENDING, RUNNING, COMPLETED, FAILED, INTERRUPTED")
    kind: str = "search"
    input: Dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)
    progress: JobProgress = Field(default_factory=JobProgress)
    attempts: List[JobAttemptRecord] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    results: Optional[Any] = None
    error: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def add_reproducible_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload.setdefault("kind", str(payload.get("type") or "search"))
        payload.setdefault(
            "input",
            {
                "query": payload.get("query"),
                "type": payload.get("type", "search"),
                "brands": list(payload.get("brands") or []),
            },
        )
        return payload
