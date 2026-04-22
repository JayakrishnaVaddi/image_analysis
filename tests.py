"""Tests for gene-presence analysis output and MongoDB payload formatting."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np

from color_profiles import COLOR_PROFILES, COLOR_PROFILE_BY_NAME
import db_handler
import live_stream_server
from config import MONGO, PLATE_GEOMETRY
from main import (
    CalibrationError,
    build_backend_gene_results_documents,
    build_backend_mongo_document,
    build_mapped_results_documents,
    build_mapped_mongo_document,
    build_mongo_document,
    build_run_document,
    load_camera_calibration,
    to_json_safe,
    undistort_image,
    validate_binary_data,
)
from plate_analyzer import PlateAnalyzer, WellCandidate, WellDetectionError


class PlateAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = PlateAnalyzer()
        self.image_shape = (1400, 1000, 3)
        self.image = np.zeros(self.image_shape, dtype=np.uint8)
        self.slab_corners = np.array(
            [
                [180, 70],
                [820, 120],
                [760, 1310],
                [140, 1260],
            ],
            dtype=np.float32,
        )

    def test_color_profiles_are_available_from_single_source(self) -> None:
        self.assertTrue(COLOR_PROFILES)
        self.assertEqual(
            {profile.name for profile in COLOR_PROFILES},
            set(COLOR_PROFILE_BY_NAME.keys()),
        )

    def test_each_color_profile_classifies_its_own_midpoint_sample(self) -> None:
        for profile in COLOR_PROFILES:
            threshold = profile.ranges[0]
            lower = threshold["lower"]
            upper = threshold["upper"]
            midpoint = [
                int(round((lower[index] + upper[index]) / 2))
                for index in range(3)
            ]
            self.assertEqual(
                self.analyzer._classify_hsv(midpoint),
                profile.name,
                msg=f"Midpoint sample should classify as {profile.name}",
            )

    def test_gene_mapping_comes_from_color_profiles(self) -> None:
        gene_positive_profiles = [
            profile for profile in COLOR_PROFILES if profile.gene_value == 1
        ]
        gene_negative_profiles = [
            profile for profile in COLOR_PROFILES if profile.gene_value == 0
        ]

        self.assertTrue(gene_positive_profiles)
        self.assertTrue(gene_negative_profiles)
        self.assertEqual(
            self.analyzer._gene_value_from_color(gene_positive_profiles[0].name),
            1,
        )
        self.assertEqual(
            self.analyzer._gene_value_from_color(gene_negative_profiles[0].name),
            0,
        )
        self.assertEqual(self.analyzer._gene_value_from_color(None), 0)
        self.assertEqual(
            COLOR_PROFILE_BY_NAME[gene_positive_profiles[0].name].gene_value,
            1,
        )

    def test_numbering_order_is_top_to_bottom_and_right_to_left(self) -> None:
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=7), 1)
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=6), 2)
        self.assertEqual(self.analyzer._well_number(row_index=0, col_index=0), 8)
        self.assertEqual(self.analyzer._well_number(row_index=1, col_index=7), 9)
        self.assertEqual(self.analyzer._well_number(row_index=1, col_index=0), 16)
        self.assertEqual(self.analyzer._well_number(row_index=11, col_index=0), 96)

    def test_assign_well_ids_maps_detected_points_into_current_output_order(self) -> None:
        candidates = self._make_candidates()
        assigned_wells, _ = self.analyzer._assign_well_ids(
            candidates=candidates,
            slab_corners=self.slab_corners,
            image=self.image,
            accepted_overlay=self.image.copy(),
        )

        self.assertEqual(len(assigned_wells), 96)
        self.assertEqual(assigned_wells[0].well_number, 1)
        self.assertEqual((assigned_wells[0].row_index, assigned_wells[0].col_index), (0, 7))
        self.assertEqual(assigned_wells[-1].well_number, 96)
        self.assertEqual((assigned_wells[-1].row_index, assigned_wells[-1].col_index), (11, 0))
        self.assertEqual(len({well.label for well in assigned_wells}), 96)

    def test_assign_well_ids_fails_when_too_few_candidates_exist(self) -> None:
        candidates = self._make_candidates()[:50]
        with self.assertRaises(WellDetectionError):
            self.analyzer._assign_well_ids(
                candidates=candidates,
                slab_corners=self.slab_corners,
                image=self.image,
                accepted_overlay=self.image.copy(),
            )

    def test_classify_assigned_wells_returns_exactly_96_gene_values(self) -> None:
        candidates = self._make_candidates()
        gene_positive_profiles = [
            profile for profile in COLOR_PROFILES if profile.gene_value == 1
        ]
        self.assertTrue(gene_positive_profiles)
        positive_profile = gene_positive_profiles[0]
        positive_bgr = np.full(self.image_shape, positive_profile.render_bgr, dtype=np.uint8)
        assigned_wells, _ = self.analyzer._assign_well_ids(
            candidates=candidates,
            slab_corners=self.slab_corners,
            image=positive_bgr,
            accepted_overlay=positive_bgr.copy(),
        )
        gene_values, well_colors, _, clean_result, ordered_result, _ = self.analyzer._classify_assigned_wells(
            image=positive_bgr,
            assigned_wells=assigned_wells,
        )

        self.assertEqual(len(gene_values), 96)
        self.assertTrue(all(value == 1 for value in gene_values))
        self.assertTrue(all(color == positive_profile.name for color in well_colors))
        self.assertEqual(clean_result.shape, positive_bgr.shape)
        self.assertEqual(ordered_result.shape, positive_bgr.shape)
        self.assertGreater(int(np.count_nonzero(clean_result != 245)), 0)
        self.assertGreater(int(np.count_nonzero(ordered_result != 245)), 0)

    def _make_candidates(self) -> List[WellCandidate]:
        destination = self.analyzer._helper_destination_corners()
        inverse_transform = cv2.getPerspectiveTransform(destination, self.slab_corners)
        width_step = PLATE_GEOMETRY.warp_width / PLATE_GEOMETRY.cols
        height_step = PLATE_GEOMETRY.warp_height / PLATE_GEOMETRY.rows
        rng = np.random.default_rng(7)

        normalized_points = []
        for row_index in range(PLATE_GEOMETRY.rows):
            for col_index in range(PLATE_GEOMETRY.cols):
                x = ((col_index + 0.5) * width_step) + rng.normal(0.0, 6.0)
                y = ((row_index + 0.5) * height_step) + rng.normal(0.0, 6.0)
                normalized_points.append([[x, y]])

        transformed = cv2.perspectiveTransform(np.array(normalized_points, dtype=np.float32), inverse_transform)
        candidates = [
            WellCandidate(
                center=(float(point[0][0]), float(point[0][1])),
                radius=20.0 + (index % 3),
                score=0.92,
                source="test",
                circularity=0.95,
            )
            for index, point in enumerate(transformed)
        ]
        rng.shuffle(candidates)
        return candidates


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

    def test_build_mapped_results_documents_appends_present_without_changing_source_fields(self) -> None:
        mapped_documents = build_mapped_results_documents(
            [
                {
                    "gene": "K",
                    "allele": ["c.1699G>A"],
                    "rsId": "rs1803274",
                    "assayId": "C__27479669_20",
                    "wellNum": 1,
                },
                {
                    "gene": "BCHE",
                    "allele": ["c.293A>G"],
                    "rsId": "rs1799807",
                    "assayId": "C___2411904_20",
                    "wellNum": "3",
                },
                {
                    "gene": "CYP2B6",
                    "allele": ["*18"],
                    "rsId": "rs28399499",
                    "assayId": "C___7817765_C0",
                    "wellNum": "bad",
                },
            ],
            [1 if index in (0, 2) else 0 for index in range(96)],
        )

        self.assertEqual(len(mapped_documents), 3)
        self.assertEqual(mapped_documents[0]["gene"], "K")
        self.assertEqual(mapped_documents[0]["present"], 1)
        self.assertEqual(mapped_documents[1]["gene"], "BCHE")
        self.assertEqual(mapped_documents[1]["present"], 1)
        self.assertEqual(mapped_documents[2]["gene"], "CYP2B6")
        self.assertEqual(mapped_documents[2]["present"], 0)

    def test_build_backend_gene_results_documents_preserves_duplicate_gene_rows_and_uses_backend_fields(self) -> None:
        gene_documents = build_backend_gene_results_documents(
            [
                {
                    "gene": "CYP2C19",
                    "allele": ["*2"],
                    "wellNum": 1,
                },
                {
                    "gene": "CYP2C19",
                    "allele": ["*17"],
                    "wellNum": 2,
                },
                {
                    "gene": "BCHE",
                    "allele": ["c.293A>G"],
                    "wellNum": "bad",
                },
            ],
            [1, 0] + ([0] * 94),
        )

        self.assertEqual(
            gene_documents,
            [
                {
                    "geneName": "CYP2C19",
                    "genotypes": ["*2"],
                    "testResult": "yellow",
                },
                {
                    "geneName": "CYP2C19",
                    "genotypes": ["*17"],
                    "testResult": "red",
                },
                {
                    "geneName": "BCHE",
                    "genotypes": ["c.293A>G"],
                    "testResult": "red",
                },
            ],
        )

    def test_to_json_safe_stringifies_non_json_types_for_mapped_export(self) -> None:
        class FakeObjectId:
            def __str__(self) -> str:
                return "fake-object-id-123"

        converted = to_json_safe(
            {
                "_id": FakeObjectId(),
                "nested": {"items": (1, FakeObjectId())},
            }
        )

        self.assertEqual(converted["_id"], "fake-object-id-123")
        self.assertEqual(converted["nested"]["items"], [1, "fake-object-id-123"])

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

    def test_mapped_mongo_document_contains_results_array(self) -> None:
        document = build_mapped_mongo_document(
            plate_id="plate-2",
            timestamp="2026-03-24T00:00:00+00:00",
            mapped_results_documents=[
                {"gene": "K", "wellNum": 7, "present": 1},
                {"gene": "BCHE", "wellNum": 3, "present": 0},
            ],
        )

        self.assertEqual(sorted(document.keys()), ["plateId", "results", "timestamp"])
        self.assertEqual(document["plateId"], "plate-2")
        self.assertEqual(len(document["results"]), 2)
        self.assertEqual(document["results"][0]["present"], 1)

    def test_backend_mongo_document_contains_grouped_genes_array(self) -> None:
        document = build_backend_mongo_document(
            plate_id="plate-2",
            timestamp="2026-03-24T00:00:00+00:00",
            gene_results_documents=[
                {
                    "geneName": "CYP2C19",
                    "genotypes": ["*2"],
                    "testResult": "yellow",
                },
                {
                    "geneName": "CYP2C19",
                    "genotypes": ["*17"],
                    "testResult": "red",
                },
                {
                    "geneName": "BCHE",
                    "genotypes": ["c.293A>G"],
                    "testResult": "red",
                },
            ],
        )

        self.assertEqual(sorted(document.keys()), ["genes", "plateId", "timestamp"])
        self.assertEqual(document["plateId"], "plate-2")
        self.assertEqual(len(document["genes"]), 3)
        self.assertEqual(document["genes"][0]["geneName"], "CYP2C19")
        self.assertEqual(document["genes"][0]["testResult"], "yellow")

    def test_test_payload_matches_requested_shape(self) -> None:
        payload = db_handler.build_test_payload()
        self.assertEqual(payload["plateId"], "test_plate_001")
        self.assertEqual(payload["binaryData"], [0, 1, 0, 1, 0, 1, 0, 1])
        self.assertIn("timestamp", payload)

    def test_mongodb_upload_receives_backend_gene_document(self) -> None:
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
            "genes": [
                {"geneName": "K", "genotypes": ["c.1699G>A"], "testResult": "yellow"},
                {"geneName": "BCHE", "genotypes": ["c.293A>G"], "testResult": "red"},
            ],
        }

        with patch.object(db_handler, "MongoClient", FakeClient):
            inserted_id = db_handler.upload_run_document(payload, "mongodb://localhost:27017/")

        self.assertEqual(inserted_id, "fake-id")
        self.assertEqual(len(inserted_documents), 1)
        self.assertEqual(inserted_documents[0]["genes"][0]["geneName"], "K")
        self.assertEqual(inserted_documents[0]["genes"][1]["testResult"], "red")
        self.assertEqual(sorted(inserted_documents[0].keys()), ["genes", "plateId", "timestamp"])

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


class LiveStreamServerTests(unittest.TestCase):
    def test_load_backend_results_payload_returns_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run_20260420T120000"
            run_dir.mkdir()
            payload = {
                "plateId": "run_20260420T120000",
                "timestamp": "2026-04-20T12:00:00Z",
                "genes": [
                    {
                        "geneName": "K",
                        "genotypes": ["c.1699G>A"],
                        "testResult": "yellow",
                    },
                    {
                        "geneName": "BCHE",
                        "genotypes": ["c.293A>G"],
                        "testResult": "red",
                    },
                ]
            }
            (run_dir / "backend_results.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            loaded_payload = live_stream_server.load_backend_results_payload(run_dir)

        self.assertEqual(loaded_payload, payload)

    def test_load_backend_results_payload_returns_none_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run_20260420T120000"
            run_dir.mkdir()

            loaded_payload = live_stream_server.load_backend_results_payload(run_dir)

        self.assertIsNone(loaded_payload)

    def test_find_newest_run_directory_returns_new_run_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            existing_run_dir = output_dir / "run_20260420T120000"
            new_run_dir = output_dir / "run_20260420T120100"
            existing_run_dir.mkdir()
            time.sleep(0.01)
            new_run_dir.mkdir()

            with patch.object(live_stream_server, "OUTPUT_DIR", output_dir):
                newest_run_dir = live_stream_server.find_newest_run_directory({existing_run_dir})

        self.assertEqual(newest_run_dir, new_run_dir)

    def test_find_newest_run_directory_returns_none_when_no_new_run_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            existing_run_dir = output_dir / "run_20260420T120000"
            existing_run_dir.mkdir()

            with patch.object(live_stream_server, "OUTPUT_DIR", output_dir):
                newest_run_dir = live_stream_server.find_newest_run_directory({existing_run_dir})

        self.assertIsNone(newest_run_dir)


class LiveStreamServerSessionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_latest_backend_results_adds_analysis_complete_type(self) -> None:
        coordinator = live_stream_server.SessionCoordinator(camera_index=0)
        session = live_stream_server.ActiveSession(websocket=AsyncMock(), camera_index=0)
        run_dir = Path("/tmp/run_20260420T120000")
        payload = {
            "plateId": "run_20260420T120000",
            "timestamp": "2026-04-20T12:00:00Z",
            "genes": [
                {
                    "geneName": "K",
                    "genotypes": ["c.1699G>A"],
                    "testResult": "yellow",
                }
            ],
        }

        with patch.object(live_stream_server, "find_newest_run_directory", return_value=run_dir), patch.object(
            live_stream_server,
            "load_backend_results_payload",
            return_value=payload,
        ), patch.object(coordinator, "_send_json", new_callable=AsyncMock) as mock_send_json:
            await coordinator._send_latest_backend_results(session, existing_run_dirs=set())

        mock_send_json.assert_awaited_once_with(
            session.websocket,
            {
                "type": "analysis_complete",
                "plateId": "run_20260420T120000",
                "timestamp": "2026-04-20T12:00:00Z",
                "genes": [
                    {
                        "geneName": "K",
                        "genotypes": ["c.1699G>A"],
                        "testResult": "yellow",
                    }
                ],
            },
            session=session,
        )
        self.assertNotIn("type", payload)


class CalibrationTests(unittest.TestCase):
    def test_load_camera_calibration_reads_expected_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calibration_path = Path(temp_dir) / "camera_calibration.json"
            calibration_path.write_text(
                json.dumps(
                    {
                        "camera_matrix": [
                            [1000.0, 0.0, 320.0],
                            [0.0, 1000.0, 240.0],
                            [0.0, 0.0, 1.0],
                        ],
                        "distortion_coefficients": [0.1, -0.2, 0.01, 0.0, 0.05],
                        "image_width": 640,
                        "image_height": 480,
                    }
                ),
                encoding="utf-8",
            )

            with patch("main.Path.resolve", return_value=Path(temp_dir) / "main.py"):
                camera_matrix, distortion_coefficients, image_width, image_height = load_camera_calibration(
                    "camera_calibration.json"
                )

        self.assertEqual(camera_matrix.shape, (3, 3))
        self.assertEqual(distortion_coefficients.shape, (5,))
        self.assertEqual((image_width, image_height), (640, 480))

    def test_load_camera_calibration_rejects_invalid_matrix_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calibration_path = Path(temp_dir) / "camera_calibration.json"
            calibration_path.write_text(
                json.dumps(
                    {
                        "camera_matrix": [[1.0, 0.0], [0.0, 1.0]],
                        "distortion_coefficients": [0.0, 0.0, 0.0, 0.0, 0.0],
                        "image_width": 640,
                        "image_height": 480,
                    }
                ),
                encoding="utf-8",
            )

            with patch("main.Path.resolve", return_value=Path(temp_dir) / "main.py"):
                with self.assertRaises(CalibrationError):
                    load_camera_calibration("camera_calibration.json")

    def test_undistort_image_returns_valid_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calibration_path = Path(temp_dir) / "camera_calibration.json"
            calibration_path.write_text(
                json.dumps(
                    {
                        "camera_matrix": [
                            [600.0, 0.0, 160.0],
                            [0.0, 600.0, 120.0],
                            [0.0, 0.0, 1.0],
                        ],
                        "distortion_coefficients": [0.01, -0.03, 0.001, 0.0, 0.02],
                        "image_width": 320,
                        "image_height": 240,
                    }
                ),
                encoding="utf-8",
            )

            image = np.full((240, 320, 3), 180, dtype=np.uint8)

            with patch("main.Path.resolve", return_value=Path(temp_dir) / "main.py"):
                undistorted = undistort_image(image, "camera_calibration.json")

        self.assertIsInstance(undistorted, np.ndarray)
        self.assertGreater(undistorted.size, 0)
        self.assertEqual(undistorted.ndim, 3)


if __name__ == "__main__":
    unittest.main()
