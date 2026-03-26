"""Tests for gene-presence analysis output and MongoDB payload formatting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from command_handler import CommandHandler
import db_handler
from config import HSV_THRESHOLDS, MONGO, PLATE_GEOMETRY
from main import build_mongo_document, build_run_document, validate_binary_data
from plate_analyzer import PlateAnalyzer
from session_orchestrator import _resolve_session_duration_seconds


class PlateAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = PlateAnalyzer()

    def test_only_three_colors_are_configured(self) -> None:
        self.assertEqual(set(HSV_THRESHOLDS.keys()), {"light_pink", "red", "yellow"})

    def test_only_three_colors_are_recognized(self) -> None:
        self.assertEqual(self.analyzer._classify_hsv([175, 60, 220]), "light_pink")
        self.assertEqual(self.analyzer._classify_hsv([2, 180, 180]), "red")
        self.assertEqual(self.analyzer._classify_hsv([28, 220, 220]), "yellow")

    def test_gene_mapping_is_zero_for_pink_and_red_and_one_for_yellow(self) -> None:
        self.assertEqual(self.analyzer._gene_value_from_color("light_pink"), 0)
        self.assertEqual(self.analyzer._gene_value_from_color("red"), 0)
        self.assertEqual(self.analyzer._gene_value_from_color("yellow"), 1)
        self.assertEqual(self.analyzer._gene_value_from_color(None), 0)

    def test_numbering_order_is_top_to_bottom_and_right_to_left(self) -> None:
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=7), 1)
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=6), 2)
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=0), 8)
        self.assertEqual(self.analyzer._well_number(row_index=1, col_index=7), 9)
        self.assertEqual(self.analyzer._well_number(row_index=1, col_index=0), 16)
        self.assertEqual(self.analyzer._well_number(row_index=11, col_index=0), 96)

    def test_analyze_wells_returns_exactly_96_gene_values(self) -> None:
        yellow_bgr = np.full(
            (PLATE_GEOMETRY.warp_height, PLATE_GEOMETRY.warp_width, 3),
            (0, 255, 255),
            dtype=np.uint8,
        )
        gene_values, _, _, _, _ = self.analyzer._analyze_wells(yellow_bgr)
        self.assertEqual(len(gene_values), 96)
        self.assertTrue(all(value == 1 for value in gene_values))

    def test_visualization_circles_are_larger_than_sampling_circles(self) -> None:
        self.assertGreater(PLATE_GEOMETRY.visualization_radius_ratio, PLATE_GEOMETRY.sample_radius_ratio)

    def test_clean_result_frame_does_not_use_original_image_background(self) -> None:
        warped = np.zeros(
            (PLATE_GEOMETRY.warp_height, PLATE_GEOMETRY.warp_width, 3),
            dtype=np.uint8,
        )
        _, _, _, _, clean_result = self.analyzer._analyze_wells(warped)
        corner_pixel = tuple(int(value) for value in clean_result[5, 5])
        self.assertEqual(corner_pixel, (245, 245, 245))


class PayloadTests(unittest.TestCase):
    def test_resolve_mongo_uri_prefers_explicit_value(self) -> None:
        with patch.dict("os.environ", {MONGO.uri_env_var: "mongodb+srv://env-uri"}, clear=False):
            self.assertEqual(
                db_handler.resolve_mongo_uri("mongodb+srv://explicit-uri"),
                "mongodb+srv://explicit-uri",
            )

    def test_resolve_mongo_uri_falls_back_to_environment(self) -> None:
        with patch.dict("os.environ", {MONGO.uri_env_var: "mongodb+srv://env-uri"}, clear=False):
            self.assertEqual(db_handler.resolve_mongo_uri(None), "mongodb+srv://env-uri")

    def test_load_local_env_reads_project_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "MONGO_URI=mongodb+srv://from-env-file\n"
                "MONGO_DB_NAME=custom_db\n"
                "MONGO_COLLECTION_NAME=custom_collection\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                db_handler.load_local_env(env_path)
                self.assertEqual(db_handler.resolve_mongo_uri(None), "mongodb+srv://from-env-file")
                self.assertEqual(db_handler.resolve_database_name(), "custom_db")
                self.assertEqual(db_handler.resolve_collection_name(), "custom_collection")

    def test_run_document_contains_only_plate_id_binary_data_and_timestamp(self) -> None:
        analysis_result = SimpleNamespace(gene_presence=[0] * 96)
        document = build_run_document(
            plate_id="plate-1",
            timestamp="2026-03-24T00:00:00+00:00",
            analysis_result=analysis_result,
        )
        self.assertEqual(sorted(document.keys()), ["binaryData", "plateId", "timestamp"])
        self.assertEqual(document["plateId"], "plate-1")
        self.assertEqual(len(document["binaryData"]), 96)

    def test_validate_binary_data_enforces_exactly_96_binary_values(self) -> None:
        self.assertEqual(validate_binary_data([0, 1] * 48), [0, 1] * 48)

        with self.assertRaises(ValueError):
            validate_binary_data([0] * 95)

        with self.assertRaises(ValueError):
            validate_binary_data([0] * 95 + [2])

    def test_mongo_document_contains_expected_array_format(self) -> None:
        analysis_result = SimpleNamespace(gene_presence=[1 if index % 2 else 0 for index in range(96)])
        document = build_mongo_document(
            plate_id="plate-2",
            timestamp="2026-03-24T00:00:00+00:00",
            analysis_result=analysis_result,
        )
        self.assertEqual(sorted(document.keys()), ["binaryData", "plateId", "timestamp"])
        self.assertEqual(len(document["binaryData"]), 96)
        self.assertTrue(all(value in (0, 1) for value in document["binaryData"]))

    def test_test_payload_matches_requested_shape(self) -> None:
        payload = db_handler.build_test_payload()
        self.assertEqual(payload["plateId"], "test_plate_001")
        self.assertEqual(payload["binaryData"], [0, 1, 0, 1, 0, 1, 0, 1])
        self.assertIn("timestamp", payload)

    def test_mongodb_upload_receives_result_array_document(self) -> None:
        inserted_documents = []

        class FakeCollection:
            def insert_one(self, document):
                inserted_documents.append(document)
                return SimpleNamespace(inserted_id="fake-id")

        class FakeDatabase:
            def __getitem__(self, name):
                return FakeCollection()

        class FakeAdmin:
            def command(self, name):
                return {"ok": 1}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.admin = FakeAdmin()

            def __getitem__(self, name):
                return FakeDatabase()

            def close(self):
                return None

        payload = {
            "plateId": "plate-3",
            "timestamp": "2026-03-24T00:00:00+00:00",
            "binaryData": [0] * 96,
        }

        with patch.object(db_handler, "MongoClient", FakeClient):
            inserted_id = db_handler.upload_run_document(payload, "mongodb://localhost:27017/")

        self.assertEqual(inserted_id, "fake-id")
        self.assertEqual(len(inserted_documents), 1)
        self.assertEqual(inserted_documents[0]["binaryData"], [0] * 96)
        self.assertEqual(sorted(inserted_documents[0].keys()), ["binaryData", "plateId", "timestamp"])

    def test_mongodb_upload_skips_when_no_uri_is_configured(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch.object(db_handler, "load_local_env", return_value=None):
            inserted_id = db_handler.upload_run_document(
                {
                    "plateId": "plate-4",
                    "timestamp": "2026-03-24T00:00:00+00:00",
                    "binaryData": [1] * 96,
                },
                None,
            )

        self.assertIsNone(inserted_id)

    def test_session_duration_uses_env_override(self) -> None:
        with patch.dict("os.environ", {"SESSION_DURATION_SECONDS": "15"}, clear=True):
            self.assertEqual(_resolve_session_duration_seconds(), 15)


class CommandHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = CommandHandler(camera_index=0)

    def test_detect_colors_maps_placeholder_labels_in_request_order(self) -> None:
        snapshot = SimpleNamespace(
            analysis_result=SimpleNamespace(well_colors=["red", "yellow", None] + [None] * 93)
        )

        with patch("command_handler.capture_frame", return_value="frame"), patch(
            "command_handler.analyze_live_frame",
            return_value=snapshot,
        ):
            response = self.handler.handle_request({"wells": ["test", "demo"]})

        self.assertEqual(
            response,
            {
                "status": "success",
                "colors": {
                    "test": "red",
                    "demo": "yellow",
                },
            },
        )

    def test_detect_colors_supports_explicit_well_numbers(self) -> None:
        analyzed_colors = ["unknown"] * 96
        analyzed_colors[0] = "red"
        analyzed_colors[4] = "light_pink"
        snapshot = SimpleNamespace(
            analysis_result=SimpleNamespace(well_colors=analyzed_colors)
        )

        with patch("command_handler.capture_frame", return_value="frame"), patch(
            "command_handler.analyze_live_frame",
            return_value=snapshot,
        ):
            response = self.handler.handle_request({"wells": ["1", "well_5"]})

        self.assertEqual(
            response,
            {
                "status": "success",
                "colors": {
                    "1": "red",
                    "well_5": "light pink",
                },
            },
        )

    def test_detect_colors_rejects_empty_wells_list(self) -> None:
        with self.assertRaises(ValueError):
            self.handler.handle_request({"wells": []})

    def test_detect_colors_rejects_non_object_requests(self) -> None:
        with self.assertRaises(ValueError):
            self.handler.handle_request(["not", "an", "object"])

    def test_health_action_returns_server_status_without_side_effects(self) -> None:
        with patch("command_handler.SESSION_ORCHESTRATOR.is_session_active", return_value=False):
            response = self.handler.handle_request({"action": "health"})

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["server"], "raspberry-pi-image-analysis")
        self.assertEqual(response["session_active"], False)
        self.assertIn("start_test", response["supported_actions"])

    def test_start_test_uses_existing_session_orchestration(self) -> None:
        orchestrator_response = {
            "status": "completed",
            "session_duration_seconds": 720,
            "analysis_run": {
                "payload": {
                    "plateId": "plate-123",
                    "timestamp": "2026-03-24T00:00:00Z",
                    "binaryData": [1] * 96,
                }
            },
        }

        with patch(
            "command_handler.SESSION_ORCHESTRATOR.run_triggered_session",
            return_value=orchestrator_response,
        ) as run_session:
            response = self.handler.handle_request(
                {
                    "action": "start_test",
                    "plateId": "plate-123",
                    "streamEndpoint": "http://127.0.0.1:8081/api/live-frame",
                    "cameraIndex": 2,
                }
            )

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["session"], orchestrator_response)
        run_session.assert_called_once()

    def test_start_test_returns_busy_when_session_is_already_active(self) -> None:
        with patch(
            "command_handler.SESSION_ORCHESTRATOR.run_triggered_session",
            return_value={"status": "already_active"},
        ):
            response = self.handler.handle_request({"action": "start_test"})

        self.assertEqual(
            response,
            {
                "status": "busy",
                "message": "A timed session is already active",
            },
        )


if __name__ == "__main__":
    unittest.main()
