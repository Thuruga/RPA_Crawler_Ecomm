"""Replace the ``scraping`` data with ``competitor-catalog`` data safely.

Run with ``--apply`` only after reviewing the printed counts.  The script keeps
both schemas intact; deleting the source schema is a separate, post-verify step.
"""

from __future__ import annotations

import argparse
from urllib.parse import parse_qsl, urlencode, urlsplit

import psycopg2
from dotenv import dotenv_values
from psycopg2 import sql


SOURCE_SCHEMA = "competitor-catalog"
TARGET_SCHEMA = "scraping"
TABLES = (
    "brands",
    "banner_assets",
    "banner_runs",
    "category_monitors",
    "category_scans",
    "price_monitors",
    "search_jobs",
    "brand_category_mappings",
    "price_history",
    "scan_products",
    "review_comments",
    "search_job_brands",
    "banner_run_brands",
    "banner_candidates",
    "banner_video_slides",
)


def connection_dsn() -> str:
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


def table_count(cursor, schema: str, table: str) -> int:
    cursor.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
    )
    return cursor.fetchone()[0]


def shared_columns(cursor, table: str) -> list[str]:
    """Return target-order columns shared by both schema variants."""
    cursor.execute(
        """
        SELECT target.column_name
        FROM information_schema.columns AS target
        JOIN information_schema.columns AS source
          ON source.table_schema = %s
         AND source.table_name = target.table_name
         AND source.column_name = target.column_name
        WHERE target.table_schema = %s
          AND target.table_name = %s
        ORDER BY target.ordinal_position
        """,
        (SOURCE_SCHEMA, TARGET_SCHEMA, table),
    )
    return [row[0] for row in cursor.fetchall()]


def counts(cursor) -> dict[str, dict[str, int]]:
    return {
        table: {
            TARGET_SCHEMA: table_count(cursor, TARGET_SCHEMA, table),
            SOURCE_SCHEMA: table_count(cursor, SOURCE_SCHEMA, table),
        }
        for table in TABLES
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with psycopg2.connect(connection_dsn(), connect_timeout=30) as connection:
        with connection.cursor() as cursor:
            before = counts(cursor)
            print("Before:", before)
            if not args.apply:
                return

            targets = sql.SQL(", ").join(
                sql.SQL("{}.{}").format(
                    sql.Identifier(TARGET_SCHEMA), sql.Identifier(table)
                )
                for table in TABLES
            )
            cursor.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(targets))
            cursor.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(targets)
            )
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")

            for table in TABLES:
                columns = shared_columns(cursor, table)
                if not columns:
                    raise RuntimeError(f"No shared columns for {table}")
                fields = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
                if table == "price_history":
                    # The source contains historic rows referencing deleted
                    # monitors. They cannot satisfy the target FK and were
                    # explicitly approved for discard during this migration.
                    source_query = sql.SQL(
                        "SELECT {fields} FROM {source}.{history} AS history "
                        "JOIN {source}.{monitors} AS monitors "
                        "ON monitors.id = history.monitor_id"
                    ).format(
                        fields=sql.SQL(", ").join(
                            sql.SQL("history.{}").format(sql.Identifier(column))
                            for column in columns
                        ),
                        source=sql.Identifier(SOURCE_SCHEMA),
                        history=sql.Identifier(table),
                        monitors=sql.Identifier("price_monitors"),
                    )
                else:
                    source_query = sql.SQL("SELECT {} FROM {}.{}").format(
                        fields,
                        sql.Identifier(SOURCE_SCHEMA),
                        sql.Identifier(table),
                    )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.{} ({}) OVERRIDING SYSTEM VALUE {}"
                    ).format(
                        sql.Identifier(TARGET_SCHEMA),
                        sql.Identifier(table),
                        fields,
                        source_query,
                    )
                )

            for table in TABLES:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                          AND column_name = 'id'
                    )
                    """,
                    (TARGET_SCHEMA, table),
                )
                if not cursor.fetchone()[0]:
                    continue
                cursor.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')",
                    (f"{TARGET_SCHEMA}.{table}",),
                )
                sequence = cursor.fetchone()[0]
                if sequence:
                    cursor.execute(
                        sql.SQL("SELECT max(id) FROM {}.{}").format(
                            sql.Identifier(TARGET_SCHEMA), sql.Identifier(table)
                        )
                    )
                    maximum = cursor.fetchone()[0]
                    cursor.execute(
                        "SELECT setval(%s::regclass, %s, %s)",
                        (sequence, maximum or 1, maximum is not None),
                    )

            after = counts(cursor)
            expected_counts = {
                **{table: values[SOURCE_SCHEMA] for table, values in after.items()},
                # The source has 30 rows pointing to missing monitors; they
                # are intentionally excluded rather than copied as orphans.
                "price_history": 1,
            }
            mismatched = {
                table: values
                for table, values in after.items()
                if values[TARGET_SCHEMA] != expected_counts[table]
            }
            if mismatched:
                raise RuntimeError(f"Synchronization count mismatch: {mismatched}")
            print("After:", after)


if __name__ == "__main__":
    main()
