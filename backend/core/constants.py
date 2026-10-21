"""
Constantes centralizadas do backend.

Este modulo e a UNICA fonte de verdade para listas e mapeamentos de
marketplaces que eram anteriormente duplicados em multiplos arquivos,
causando divergencias silenciosas quando novos marketplaces eram adicionados.
"""

# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------

# Identificadores canonicos dos engines de marketplace.
# Adicionar um novo marketplace aqui e suficiente para que toda a validacao
# (rotas, monitores, promocoes) funcione corretamente.
MARKETPLACE_CANONICAL_KEYS: frozenset[str] = frozenset({
    "amazon",
    "mercado_livre",
    "netshoes",
    "dafiti",
    "privalia",
})

# Aliases alternativos aceitos nas APIs (ex: frontend enviando "mercadolivre").
# Mapeia variante -> chave canonica.
MARKETPLACE_KEY_ALIASES: dict[str, str] = {
    "mercadolivre": "mercado_livre",
}

# Uniao de chaves canonicas + todos os aliases.
# Usar este set para validar se uma entrada e um marketplace em qualquer forma.
MARKETPLACE_ENGINE_KEYS: frozenset[str] = (
    MARKETPLACE_CANONICAL_KEYS | frozenset(MARKETPLACE_KEY_ALIASES.keys())
)

# Marketplaces excluidos da sondagem generica de promocoes condicionais (/conditional-offers).
# A Privalia possui engine dedicada com suporte a extracao de ofertas Pix diretamente da PDP,
# portanto e a unica excecao de marketplace com cobertura ativa de conditional-offers.
CONDITIONAL_OFFER_EXCLUDED_MARKETPLACES: frozenset[str] = (
    MARKETPLACE_ENGINE_KEYS - {"privalia"}
)

