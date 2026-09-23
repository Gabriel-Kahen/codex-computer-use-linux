import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("stock_codex_compat", Path(__file__).resolve().parents[1] / "stock_codex_compat.py")
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


class ImageContractTests(unittest.TestCase):
    def model_request(self, output):
        return {"input": [{"type": "function_call_output", "call_id": "compat-image", "output": output}]}

    def test_content_only_fixture_preserves_metadata_and_image(self):
        compat.assert_image_content(compat.fixture_result())

    def test_structured_media_is_rejected_by_production_contract(self):
        with self.assertRaisesRegex(AssertionError, "omit structuredContent"):
            compat.assert_image_content(compat.fixture_result(structured=True))

    def test_dropped_model_image_fails_even_when_metadata_arrives(self):
        with self.assertRaisesRegex(AssertionError, "replaced by text"):
            compat.assert_model_image(self.model_request(json.dumps(compat.METADATA)))

    def test_metadata_loss_fails_even_when_image_arrives(self):
        with self.assertRaisesRegex(AssertionError, "metadata missing"):
            compat.assert_model_image(self.model_request([
                {"type": "input_image", "image_url": "data:image/png;base64," + compat.PNG}]))

    def test_model_output_handles_codex_timing_text(self):
        compat.assert_model_image(self.model_request([
            {"type": "input_text", "text": "Wall time: 0.1 seconds\nOutput:"},
            {"type": "input_text", "text": json.dumps(compat.METADATA)},
            {"type": "input_image", "image_url": "data:image/png;base64," + compat.PNG}]))


if __name__ == "__main__":
    unittest.main()
