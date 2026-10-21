"""Pure normalization and evidence-merging helpers for promotion adapters."""

from __future__ import annotations

from typing import Iterable

from core.models import (
    ConditionalOffer,
    OfferSourceAuthority,
    OfferState,
    commercial_claim_key,
    condition_signature_key,
    merge_scopes,
    scopes_compatible,
)

_AUTHORITY_RANK = {
    OfferSourceAuthority.ADVERTISED: 0,
    OfferSourceAuthority.STRUCTURED: 1,
    OfferSourceAuthority.AUTHORITATIVE: 2,
}


class _ScopeCluster:
    """One reconciled-scope bucket within a condition_signature_key group."""

    __slots__ = ("members", "scope")

    def __init__(self, offer: ConditionalOffer):
        self.members: list[ConditionalOffer] = [offer]
        self.scope = offer.scope


def _cluster_by_scope(candidates: Iterable[ConditionalOffer]) -> list[_ScopeCluster]:
    """Group same-condition offers whose scopes are pairwise compatible.

    An absent scope field (e.g. HTML only knows `product_id`, VTEX checkout
    only knows `sku_id`/`seller_id`) never blocks clustering — only a field
    known and divergent on both sides does. The cluster's reconciled scope
    accumulates as members are added, so later members are matched against
    the union seen so far, not just the first member.
    """
    clusters: list[_ScopeCluster] = []
    for offer in candidates:
        target = next(
            (cluster for cluster in clusters if scopes_compatible(offer.scope, cluster.scope)),
            None,
        )
        if target is None:
            clusters.append(_ScopeCluster(offer))
            continue
        target.members.append(offer)
        target.scope = merge_scopes(target.scope, offer.scope)
    return clusters


def _resolve_cluster(cluster: _ScopeCluster) -> tuple[ConditionalOffer, str | None]:
    """Pick the winning offer for one scope cluster, with reconciled scope/provenance."""
    candidates = cluster.members
    winner = max(candidates, key=lambda item: _AUTHORITY_RANK[item.source.authority])
    values = {(item.price, item.state.value) for item in candidates}
    diagnostic = None
    if len(values) > 1:
        diagnostic = (
            f"conflicting evidence for {winner.condition_label}; selected {winner.source.authority.value}"
        )
    winner = winner.model_copy(update={"scope": cluster.scope})
    winner.offer_key = commercial_claim_key(winner)
    winner_identity = (
        winner.source.kind.value,
        winner.source.ref,
        winner.source.authority.value,
    )
    seen = {winner_identity}
    provenance = []
    for item in candidates:
        for source in [item.source, *item.provenance]:
            identity = (source.kind.value, source.ref, source.authority.value)
            if identity not in seen:
                seen.add(identity)
                provenance.append(source)
    return winner.model_copy(update={"provenance": provenance}), diagnostic


def deduplicate_offers(
    offers: Iterable[ConditionalOffer],
) -> tuple[list[ConditionalOffer], list[str]]:
    """Prefer authority, reconcile partial scope, and diagnose conflicting values.

    Offers are grouped first by `condition_signature_key` (the commercial
    condition, excluding scope/price/state/evidence), then sub-clustered by
    scope compatibility so real, complementary evidence for the same claim
    (e.g. HTML advertising only `product_id`, checkout confirming only
    `sku_id`/`seller_id`) reconciles into one offer, while genuinely
    conflicting scopes (known-divergent SKU, disjoint category ids) remain
    separate offers.
    """
    signature_order: list[str] = []
    signature_groups: dict[str, list[ConditionalOffer]] = {}
    for offer in offers:
        key = condition_signature_key(offer)
        if key not in signature_groups:
            signature_order.append(key)
        signature_groups.setdefault(key, []).append(offer)

    merged: list[ConditionalOffer] = []
    diagnostics: list[str] = []
    for key in signature_order:
        for cluster in _cluster_by_scope(signature_groups[key]):
            winner, diagnostic = _resolve_cluster(cluster)
            merged.append(winner)
            if diagnostic:
                diagnostics.append(diagnostic)
    return merged, diagnostics


def merge_offer_evidence(offers: Iterable[ConditionalOffer]):
    """Compatibility alias used by probes in later plans."""
    return deduplicate_offers(offers)


def can_confirm(authority: OfferSourceAuthority | str) -> bool:
    value = OfferSourceAuthority(authority)
    return value is OfferSourceAuthority.AUTHORITATIVE


def normalize_state(
    requested: OfferState | str, authority: OfferSourceAuthority | str
) -> OfferState:
    state = OfferState(requested)
    if state is OfferState.CONFIRMED and not can_confirm(authority):
        return OfferState.ADVERTISED
    return state
