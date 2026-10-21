"""Normalize unambiguous duplicated category labels in Azure and Docker databases.

The source URL is the authority: labels only gain a prefix when the same label
exists more than once for the same brand.  The prefix comes from an explicit
department in that URL; ambiguous paths are deliberately skipped.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import psycopg2
from dotenv import dotenv_values


LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:5433/scrapper_dev"
SKIP_PARENTS = {"", "-", "masculino", "feminino", "roupas", "acessorios", "calcados"}
DISPLAY = {
    "acessorios": "Acessórios", "alfaiataria": "Alfaiataria", "bermudas": "Bermudas",
    "bermudas-e-shorts": "Bermudas e Shorts", "blazers": "Blazers", "blusas": "Blusas",
    "blusoes": "Blusões", "bodies": "Bodies", "calcados": "Calçados", "calcas": "Calças",
    "camisas": "Camisas", "camisetas": "Camisetas", "camisolas": "Camisolas",
    "camisetes": "Camisetes", "casacos": "Casacos", "cardigans": "Cardigans",
    "coletes": "Coletes", "conjuntos": "Conjuntos", "colecoes": "Coleções",
    "infantil": "Infantil", "jaquetas": "Jaquetas", "jardineiras": "Jardineiras",
    "kimonos": "Kimonos", "macacoes": "Macacões", "maios": "Maiôs", "pijamas": "Pijamas",
    "polos": "Polos", "regatas": "Regatas", "roupoes": "Roupões", "saias": "Saias",
    "shorts": "Shorts", "sport": "Sport", "sueteres": "Suéteres", "sutias": "Sutiãs",
    "tops": "Tops", "vestidos": "Vestidos", "vestuario": "Vestuário",
}
LACOSTE_POLO_LABELS = {
    "polos": "Polos — Todos os modelos",
    "golf": "Polos — Golf",
    "l1212": "Polos — Original L.12.12",
    "loose-fit-polos": "Polos — Loose Fit",
    "manga-curta": "Polos — Manga Curta",
    "manga-longa": "Polos — Manga Longa",
    "paris-fit": "Polos — Paris Fit",
    "polos-classic-fit": "Polos — Classic Fit",
    "polos-slim-fit": "Polos — Slim Fit",
    "regular-fit-polos": "Polos — Regular Fit",
    "tenis": "Polos — Tênis",
}
EXACT_LABELS = {
    ("aramis", "/urban/roupas/alfaiataria/calca-alfaiataria"): "Urban — Alfaiataria — Calça Alfaiataria",
    ("aramis", "/roupas/alfaiataria/calca-alfaiataria"): "Alfaiataria — Calça Alfaiataria",
    ("aramis", "/urban/roupas/alfaiataria/paleto"): "Urban — Alfaiataria — Paletó",
    ("aramis", "/roupas/alfaiataria/paleto"): "Alfaiataria — Paletó",
    ("aramis", "/urban/acessorios/cuecas"): "Urban — Cuecas",
    ("foxton", "/acessorios/diversos"): "Acessórios — Diversos",
}


@dataclass(frozen=True)
class Mapping:
    brand: str
    source_path: str
    label: str


def azure_dsn() -> str:
    value = dotenv_values(Path(__file__).parents[1] / ".env")["DATABASE_URL"]
    parsed = urlsplit(value)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query)
                       if k not in {"schema", "connection_limit", "pool_timeout"}])
    return parsed._replace(query=query).geturl()


def path_parts(source_path: str) -> list[str]:
    return [part for part in unquote(urlsplit(source_path).path).lower().split("/") if part]


def title_for(slug: str) -> str:
    return DISPLAY.get(slug, slug.replace("-", " ").title())


def parent_prefix(mapping: Mapping) -> str | None:
    parts = path_parts(mapping.source_path)
    if len(parts) < 2:
        return None

    # Brand divisions need the first URL segment: both have the same
    # department structure below them.
    if mapping.brand == "hugoboss" and parts[0] in {"boss", "hugo"}:
        return parts[0].upper()
    if mapping.brand == "calvinklein" and parts[:2] == ["infantil", "menino"]:
        return "Infantil Masculino"
    if mapping.brand == "levis" and parts[:2] == ["kids", "menino"]:
        return "Infantil Masculino"
    if mapping.brand == "trackfield" and parts[:2] == ["roupas-infantis", "roupas-infantis-masculinas"]:
        return "Infantil Masculino"
    if mapping.brand in {"aramis", "zapalla"} and "outlet" in parts:
        return "Outlet"

    parent = parts[-2]
    if parent in SKIP_PARENTS:
        return None
    rendered = title_for(parent)
    if rendered.casefold() == mapping.label.casefold():
        return None
    return rendered


def proposal(mapping: Mapping, duplicate_count: int) -> str | None:
    parts = path_parts(mapping.source_path)
    decoded_path = unquote(urlsplit(mapping.source_path).path).lower()
    for (brand, suffix), label in EXACT_LABELS.items():
        if mapping.brand == brand and decoded_path.endswith(suffix):
            return label
    if mapping.brand == "lacoste":
        if "/masculino/vestuário/polos" in decoded_path:
            return LACOSTE_POLO_LABELS.get(parts[-1])
        if parts[-3:] == ["masculino", "calcados", "tenis"]:
            return "Calçados — Tênis"
        if parts[-3:] == ["infantil", "infantil---2-8-anos", "vestuário-de-menino"]:
            return "Infantil 2–8 anos — Vestuário de Menino"
        if parts[-3:] == ["infantil", "adolescentes---10-16-anos", "vestuário-de-menino"]:
            return "Adolescentes 10–16 anos — Vestuário de Menino"
    # A normalizer must be idempotent.  Existing contextual labels were
    # already reviewed, so never stack a second parent prefix onto them.
    if " — " in mapping.label:
        return None
    if duplicate_count < 2:
        return None
    prefix = parent_prefix(mapping)
    return f"{prefix} — {mapping.label}" if prefix else None


def load(connection) -> list[Mapping]:
    with connection.cursor() as cur:
        cur.execute("""
            SELECT b.brand_key, m.source_path, m.label
            FROM scraping.brand_category_mappings AS m
            JOIN scraping.brands AS b ON b.id = m.brand_id
            ORDER BY b.brand_key, m.id
        """)
        return [Mapping(*row) for row in cur.fetchall()]


def changes(rows: list[Mapping]) -> list[tuple[Mapping, str]]:
    repeated = Counter((row.brand, row.label) for row in rows)
    return [(row, new) for row in rows if (new := proposal(row, repeated[(row.brand, row.label)])) and new != row.label]


def apply(connection, items: list[tuple[Mapping, str]]) -> None:
    with connection.cursor() as cur:
        for item, label in items:
            cur.execute("""
                UPDATE scraping.brand_category_mappings AS m
                SET label = %s, updated_at = NOW()
                FROM scraping.brands AS b
                WHERE m.brand_id = b.id AND b.brand_key = %s AND m.source_path = %s
            """, (label, item.brand, item.source_path))
            if cur.rowcount != 1:
                raise RuntimeError(f"Expected one row for {item.brand} {item.source_path}; got {cur.rowcount}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist the proposed labels")
    parser.add_argument("--database", choices=("azure", "local", "both"), default="both")
    parser.add_argument("--summary", action="store_true", help="Print counts only")
    args = parser.parse_args()
    targets = [("azure", azure_dsn()), ("local", os.environ.get("LOCAL_DATABASE_URL", LOCAL_DSN))]

    for name, dsn in targets:
        if args.database not in {name, "both"}:
            continue
        with psycopg2.connect(dsn, connect_timeout=30) as connection:
            items = changes(load(connection))
            print(f"{name}: {len(items)} unambiguous changes")
            if args.summary:
                for brand, count in sorted(Counter(row.brand for row, _ in items).items()):
                    print(f"  {brand}: {count}")
            else:
                for row, label in items:
                    print(f"  {row.brand}: {row.label!r} -> {label!r} ({row.source_path})")
            if args.apply:
                apply(connection, items)
                # Be explicit: this script is also run against the Docker
                # Postgres service, where relying on context-manager commit
                # behavior made troubleshooting unnecessarily opaque.
                connection.commit()
                print(f"{name}: applied {len(items)} changes")


if __name__ == "__main__":
    main()
