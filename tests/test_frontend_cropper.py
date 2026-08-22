"""main 分支內嵌前端的菜單裁切整合測試。"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendCropperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "web.html").read_text(encoding="utf-8")

    def test_cropper_dependency_is_version_pinned_and_integrity_checked(self):
        self.assertIn("cropperjs@2.1.1/dist/cropper.min.js", self.html)
        self.assertIn(
            'integrity="sha256-J/Kdrjxvp6X2EmkB9NH4y7w2dWGWBGqn6X0urhQTGXk="',
            self.html,
        )

    def test_crop_controls_are_available_in_upload_dialog(self):
        for action in ("rotate-left", "rotate-right", "zoom-out", "zoom-in", "reset"):
            self.assertIn(f'data-vision-crop-action="{action}"', self.html)

    def test_model_receives_prepared_crop_instead_of_original_file(self):
        self.assertIn("uploadFile = await prepareVisionUploadFile()", self.html)
        self.assertIn("form.append('image', uploadFile)", self.html)
        self.assertNotIn("form.append('image', visionFile)", self.html)


if __name__ == "__main__":
    unittest.main()
