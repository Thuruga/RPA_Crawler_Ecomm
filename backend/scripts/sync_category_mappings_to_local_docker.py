"""Synchronize category mappings from the Azure source into local Docker Postgres."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

import psycopg2
from dotenv import dotenv_values
from psycopg2.extras import execute_values


def azure_dsn() -> str:
    value = dotenv_values("backend/.env")["DATABASE_URL"]
    parsed = urlsplit(value)
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query)
            if key not in {"schema", "connection_limit", "pool_timeout"}
        ]
    )
    return parsed._replace(query=query).geturl()


LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:5433/scrapper_dev"


def mapping_rows(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT b.brand_key, m.canonical_slug, m.source_path, m.label
            FROM scraping.brand_category_mappings AS m
            JOIN scraping.brands AS b ON b.id = m.brand_id
            ORDER BY b.brand_key, m.id
            """
        )
        return cursor.fetchall()


def main() -> None:
    with psycopg2.connect(azure_dsn(), connect_timeout=30) as source:
        source_rows = mapping_rows(source)

    by_brand: dict[str, list[tuple[str, str, str]]] = {}
    for brand_key, slug, source_path, label in source_rows:
        by_brand.setdefault(brand_key, []).append((slug, source_path, label))

    with psycopg2.connect(LOCAL_DSN, connect_timeout=30) as target:
        with target.cursor() as cursor:
            cursor.execute("LOCK TABLE scraping.brand_category_mappings IN ACCESS EXCLUSIVE MODE")
            cursor.execute("SELECT id, brand_key FROM scraping.brands")
            local_brands = dict(cursor.fetchall())
            local_ids = {key: value for value, key in local_brands.items()}

            missing = sorted(set(by_brand) - set(local_ids))
            if missing:
                raise RuntimeError(f"Source brands absent locally: {missing}")

            brand_ids = [local_ids[key] for key in by_brand]
            cursor.execute(
                "DELETE FROM scraping.brand_category_mappings WHERE brand_id = ANY(%s)",
                (brand_ids,),
            )

            rows_to_insert = [
                (local_ids[key], slug, source_path, label)
                for key, mappings in by_brand.items()
                for slug, source_path, label in mappings
            ]
            execute_values(
                cursor,
                """
                INSERT INTO scraping.brand_category_mappings
                    (brand_id, canonical_slug, source_path, label)
                VALUES %s
                """,
                rows_to_insert,
                page_size=500,
            )

            cursor.execute(
                """
                SELECT b.brand_key, count(m.id)
                FROM scraping.brands AS b
                LEFT JOIN scraping.brand_category_mappings AS m ON m.brand_id = b.id
                WHERE b.brand_key = ANY(%s)
                GROUP BY b.brand_key
                ORDER BY b.brand_key
                """,
                (list(by_brand),),
            )
            copied = dict(cursor.fetchall())
            expected = {key: len(items) for key, items in by_brand.items()}
            if copied != expected:
                raise RuntimeError(f"Mapping count mismatch: expected={expected}, copied={copied}")

    print(f"Synchronized {sum(len(items) for items in by_brand.values())} category mappings.")
    print(f"Lacoste mappings: {expected.get('lacoste')}")


if __name__ == "__main__":
    main()
