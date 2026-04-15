
"""
MongoDB upload and connectivity-test utilities.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import MONGO

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:  # pragma: no cover - depends on deployment environment.
    MongoClient = None
    PyMongoError = Exception


LOGGER = logging.getLogger(__name__)
ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"


def load_local_env(env_path: Path = ENV_FILE_PATH) -> None:
    """
    Load key/value pairs from the project-local `.env` file.

    This keeps MongoDB connectivity self-contained inside `image_analysis`
    without requiring any external app wiring.
    """

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


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


def resolve_source_database_name() -> str:
    """
    Resolve the MongoDB source database for the mock plate records.
    """

    load_local_env()
    return os.getenv(MONGO.source_database_name_env_var, MONGO.source_database_name)


def resolve_source_collection_name() -> str:
    """
    Resolve the MongoDB source collection for the mock plate records.
    """

    load_local_env()
    return os.getenv(MONGO.source_collection_name_env_var, MONGO.source_collection_name)


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


def fetch_mock_plate_documents(mongo_uri: Optional[str]) -> List[Dict[str, Any]]:
    """
    Read mock plate seed documents from MongoDB without modifying them.

    Returns an empty list on connection/read failure so the image-analysis run
    can still complete with its original outputs.
    """

    client: Optional[MongoClient]
    client, _ = connect_to_mongo(mongo_uri)
    if client is None:
        return []

    database_name = resolve_source_database_name()
    collection_name = resolve_source_collection_name()

    try:
        database = client[database_name]
        collection = database[collection_name]
        client.admin.command("ping")
        documents = list(collection.find())
        LOGGER.info(
            "Loaded %s mock-plate documents from %s.%s",
            len(documents),
            database_name,
            collection_name,
        )
        return documents
    except PyMongoError as exc:
        LOGGER.error("MongoDB mock-plate read failed: %s", exc)
        return []
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
