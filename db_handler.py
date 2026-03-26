
"""
MongoDB upload and connectivity-test utilities.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from config import MONGO
from project_env import ENV_FILE_PATH, load_local_env

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:  # pragma: no cover - depends on deployment environment.
    MongoClient = None
    PyMongoError = Exception


LOGGER = logging.getLogger(__name__)


def resolve_mongo_uri(mongo_uri: Optional[str] = None) -> Optional[str]:
    """
    Resolve the MongoDB URI from an explicit override or the local `.env`.
    """

    load_local_env()

    if mongo_uri:
        return mongo_uri

    env_uri = os.getenv(MONGO.uri_env_var)
    if env_uri:
        return env_uri

    return None


def resolve_database_name() -> str:
    """
    Resolve the MongoDB database name from `.env` or defaults.
    """

    load_local_env()
    return os.getenv(MONGO.database_name_env_var, MONGO.database_name)


def resolve_collection_name() -> str:
    """
    Resolve the MongoDB collection name from `.env` or defaults.
    """

    load_local_env()
    return os.getenv(MONGO.collection_name_env_var, MONGO.collection_name)


def connect_to_mongo(mongo_uri: Optional[str] = None) -> Tuple[Optional[MongoClient], Optional[str]]:
    """
    Create a MongoDB client using the configured URI.
    """

    if MongoClient is None:
        LOGGER.error("pymongo is not installed; skipping MongoDB connection")
        return None, None

    resolved_uri = resolve_mongo_uri(mongo_uri)
    if not resolved_uri:
        LOGGER.warning(
            "MongoDB URI is not configured; set %s in %s or provide an explicit URI.",
            MONGO.uri_env_var,
            ENV_FILE_PATH,
        )
        return None, None

    client = MongoClient(
        resolved_uri,
        serverSelectionTimeoutMS=MONGO.server_selection_timeout_ms,
    )
    return client, resolved_uri


def upload_run_document(document: Dict[str, Any], mongo_uri: Optional[str]) -> Optional[str]:
    """
    Upload a run document to MongoDB.

    Returns the inserted document ID as a string on success. Returns None if
    the upload fails so local persistence can continue.
    """

    client: Optional[MongoClient]
    client, _ = connect_to_mongo(mongo_uri)
    if client is None:
        return None

    database_name = resolve_database_name()
    collection_name = resolve_collection_name()

    try:
        database = client[database_name]
        collection = database[collection_name]

        # Force a quick connectivity check so failures happen here instead of
        # later when insert_one is called.
        client.admin.command("ping")

        result = collection.insert_one(document)
        inserted_id = str(result.inserted_id)
        LOGGER.info(
            "Uploaded run document to MongoDB collection %s.%s with id %s",
            database_name,
            collection_name,
            inserted_id,
        )
        return inserted_id
    except PyMongoError as exc:
        LOGGER.error("MongoDB upload failed: %s", exc)
        return None
    finally:
        client.close()


def build_test_payload() -> Dict[str, Any]:
    """
    Build the exact test payload requested for Atlas connectivity checks.
    """

    return {
        "plateId": "test_plate_001",
        "binaryData": [0, 1, 0, 1, 0, 1, 0, 1],
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def insert_test_document(mongo_uri: Optional[str] = None) -> Optional[str]:
    """
    Insert the Atlas connectivity-test payload and return the inserted ID.
    """

    payload = build_test_payload()
    LOGGER.info(
        "Testing MongoDB insert into %s.%s",
        resolve_database_name(),
        resolve_collection_name(),
    )
    inserted_id = upload_run_document(payload, mongo_uri)

    if inserted_id:
        LOGGER.info("MongoDB test insert succeeded with id %s", inserted_id)
    else:
        LOGGER.error("MongoDB test insert failed")
    return inserted_id
