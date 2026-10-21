import re
from typing import List

_SIZE_MAP = {
    "PEQUENO": "P",
    "MEDIO": "M",
    "MÉDIO": "M",
    "GRANDE": "G",
    "UNICO": "U",
    "ÚNICO": "U",
    "UN": "U",
    "EXTRA GRANDE": "GG",
    "XG": "GG",
}

_SIZE_ORDER = {
    "PP": 1,
    "P": 2,
    "M": 3,
    "G": 4,
    "GG": 5,
    "XG": 6,
    "XGG": 7,
    "XXG": 8,
    "U": 9,
}

def _size_sort_key(size: str):
    if size in _SIZE_ORDER:
        return (0, _SIZE_ORDER[size])
    if size.isdigit():
        return (1, int(size))
    return (2, size)


def sanitize_colors(colors: List[str]) -> List[str]:
    """
    Higieniza a lista de cores, padronizando a capitalização
    e removendo espaços extras.
    """
    sanitized = []
    for c in colors:
        c = c.strip()
        if not c:
            continue
        # Title case, mantendo hifens e afins
        c = " ".join(word.capitalize() for word in c.split())
        if c not in sanitized:
            sanitized.append(c)
    return sanitized

def sanitize_sizes(sizes: List[str]) -> List[str]:
    """
    Higieniza a lista de tamanhos, padronizando para UPPERCASE
    e convertendo tamanhos verbosos em siglas canônicas.
    """
    sanitized = []
    for s in sizes:
        s = s.strip().upper()
        if not s:
            continue
        # Expansões canônicas
        s = _SIZE_MAP.get(s, s)
        
        if s not in sanitized:
            sanitized.append(s)
            
    sanitized.sort(key=_size_sort_key)
    return sanitized
