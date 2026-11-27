"""Offline negative checks for frozen splits and derivative provenance."""

from __future__ import annotations

import copy
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

MODULE_PATH = Path(__file__).resolve().parents[1] / "evals/step2/dataset.py"
SPEC = importlib.util.spec_from_file_location("step2_dataset_checks", MODULE_PATH)
assert SPEC and SPEC.loader
dataset = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dataset)


class Step2DatasetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.photos = self.root / "photos"
        for key, value in (("ROOT", self.root), ("PHOTOS", self.photos)):
            patched = patch.object(dataset, key, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.records = []
        self.dev = [self.add_case(f"dev_{index}", "dev") for index in range(10)]
        self.holdout = [self.add_case(f"holdout_{index}", "holdout") for index in range(12)]
        self.stress = [
            self.add_case(f"stress_{index}", "stress", parent=self.dev[index // 3])
            for index in range(12)
        ]
        # A valid complete fixture must pass before introducing one defect.
        dataset.validate(self.records)

    def add_case(self, case_id, split, parent=None):
        family = parent["group"] if parent else case_id
        annotation = self.root / "annotations" / f"{family}.json"
        annotation.parent.mkdir(exist_ok=True)
        labels = dataset.encoded({"items": [{"name": family, "price": 80}]})
        annotation.write_bytes(labels)
        folder = self.photos / dataset.SPLITS[split]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{case_id}.expected.json").write_bytes(labels)
        image = io.BytesIO()
        Image.new("RGB", (2, 2), (len(self.records), 0, 0)).save(image, format="PNG")
        (folder / f"{case_id}.png").write_bytes(image.getvalue())
        record = {
            "id": case_id,
            "split": split,
            "group": family,
            "restaurant": f"restaurant-{family}",
            "source": {
                "url": f"https://menus.invalid/{family}.pdf",
                "sourceSha256": dataset.sha(f"original-{family}".encode()),
            },
            "parent": parent["id"] if parent else None,
            "annotation": annotation.relative_to(self.root).as_posix(),
            "annotationSha256": dataset.sha(labels),
            "inputSha256": dataset.sha(image.getvalue()),
            "expectedSha256": dataset.sha(labels),
        }
        self.records.append(record)
        return record

    def test_group_url_or_original_source_cannot_cross_into_holdout(self):
        for field in ("group", "url", "sourceSha256"):
            with self.subTest(field=field):
                records = copy.deepcopy(self.records)
                dev = next(row for row in records if row["id"] == self.dev[0]["id"])
                holdout = next(row for row in records if row["id"] == self.holdout[0]["id"])
                if field == "group":
                    holdout[field] = dev[field]
                else:
                    holdout["source"][field] = dev["source"][field]
                error_key = "origin" if field == "sourceSha256" else field
                with self.assertRaisesRegex(ValueError, f"Cross-split {error_key}:"):
                    dataset.validate(records)

    def test_changed_frozen_files_or_wrong_derivative_parent_are_rejected(self):
        row = self.holdout[0]
        folder = self.photos / dataset.SPLITS[row["split"]]
        for path in (
            folder / f"{row['id']}.png",
            folder / f"{row['id']}.expected.json",
            self.root / row["annotation"],
        ):
            with self.subTest(changed_file=path.name):
                before = path.read_bytes()
                try:
                    path.write_bytes(before + b"tampered")
                    with self.assertRaisesRegex(ValueError, "Hash mismatch:"):
                        dataset.validate(self.records)
                finally:
                    path.write_bytes(before)

        with self.subTest(wrong_parent=True):
            records = copy.deepcopy(self.records)
            derivative = next(row for row in records if row["id"] == self.stress[0]["id"])
            derivative["parent"] = self.dev[1]["id"]
            with self.assertRaisesRegex(ValueError, "Invalid derivative:"):
                dataset.validate(records)


if __name__ == "__main__":
    unittest.main()
