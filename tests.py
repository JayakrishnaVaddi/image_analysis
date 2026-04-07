"""Tests for gene-presence analysis output and MongoDB payload formatting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import cv2
import numpy as np

import db_handler
from config import HSV_THRESHOLDS, MONGO, PLATE_GEOMETRY
from main import build_mongo_document, build_run_document, validate_binary_data
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
        yellow_bgr = np.full(self.image_shape, (0, 255, 255), dtype=np.uint8)
        assigned_wells, _ = self.analyzer._assign_well_ids(
            candidates=candidates,
            slab_corners=self.slab_corners,
            image=yellow_bgr,
            accepted_overlay=yellow_bgr.copy(),
        )
        gene_values, well_colors, _, _, clean_result = self.analyzer._classify_assigned_wells(
            image=yellow_bgr,
            assigned_wells=assigned_wells,
        )

        self.assertEqual(len(gene_values), 96)
        self.assertTrue(all(value == 1 for value in gene_values))
        self.assertTrue(all(color == "yellow" for color in well_colors))
        self.assertEqual(clean_result.shape, yellow_bgr.shape)
        self.assertGreater(int(np.count_nonzero(clean_result != 245)), 0)

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


if __name__ == "__main__":
    unittest.main()
