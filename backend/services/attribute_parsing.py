"""Fallbacks conservadores para opcoes de cor e tamanho malformadas."""

import re


_SIZE_AT_END = re.compile(
    r"(?:^|\s|[/|])(PP|P|M|G|GG|XG|XXG|XGG|U|UN|\d{1,3})\s*$",
    re.IGNORECASE,
)
_MIXED_OPTION = re.compile(r"(?:\s+(?:/|\||-)\s+|\s*[/|]\s*)")


def extract_size_fallback(value: str) -> str:
    """Extrai um tamanho terminal; preserva o valor quando nao ha match seguro."""
    label = str(value or "").strip()
    match = _SIZE_AT_END.search(label)
    return match.group(1).upper() if match else label


def extract_color_fallback(value: str) -> str:
    """Remove apenas um segmento final que seja inequivocamente um tamanho."""
    label = str(value or "").strip()
    parts = _MIXED_OPTION.split(label)
    if len(parts) < 2:
        return label

    trailing = parts[-1].strip()
    match = _SIZE_AT_END.fullmatch(trailing)
    if not match:
        return label
    return " / ".join(part.strip() for part in parts[:-1] if part.strip()) or label
