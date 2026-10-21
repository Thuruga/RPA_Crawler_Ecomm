"""
Filtro de categoria — wrapper de compatibilidade sobre `category_policy` (Wave 0, D-02/D-06).

A implementação determinística e fail-closed real vive em `category_policy.py`.
Este módulo é preservado apenas para não quebrar os call sites existentes
(`vtex_catalog.py`, `sfcc_engine.py`, `zara_engine.py`, `vtex_engine.py`), que
continuam importando `is_masculine_category` deste caminho.
"""
from services.category_policy import is_masculine_category as _is_masculine_category


def is_masculine_category(name: str, url: str) -> bool:
    """Filtra as categorias extraídas dos sites para manter apenas
    categorias estritamente masculinas ou infantil masculino.

    Delega para `category_policy.evaluate_category_scope` (determinístico,
    fail-closed, sem NLP/fuzzy). Feminino/menina/infantil-ambíguo/neutro sem
    evidência explícita são rejeitados; ver `category_policy.py` para a
    ordem de precedência completa.
    """
    return _is_masculine_category(name, url)
