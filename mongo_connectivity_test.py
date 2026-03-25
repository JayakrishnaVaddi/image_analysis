"""
Manual MongoDB Atlas connectivity test entry point.
"""

from __future__ import annotations

import logging

from db_handler import insert_test_document


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    inserted_id = insert_test_document()
    if not inserted_id:
        return 1

    print(f"MongoDB test insert succeeded. Inserted document ID: {inserted_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
