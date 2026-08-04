import json
import unittest

from server.compatibility import analyze_script_compatibility, device_profile


class ScriptCompatibilityTests(unittest.TestCase):
    def test_reports_coordinate_and_target_warnings_without_running_script(self):
        content = json.dumps({
            "version": 2,
            "target": {
                "device_model": "OtherPhone",
                "screen_size": "1440x2560",
            },
            "steps": [
                {"action": "tap", "click": {"x": 1200, "y": 20}},
                {"action": "swipe", "swipe": {"x1": 10, "y1": 10, "x2": 1100, "y2": 20, "duration_ms": 300}},
            ],
        })

        result = analyze_script_compatibility(content, {
            "serial": "phone-1",
            "model": "CurrentPhone",
            "screen_width": 1080,
            "screen_height": 2400,
        })

        self.assertEqual(result["status"], "warning")
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("model_mismatch", codes)
        self.assertIn("resolution_mismatch", codes)
        self.assertIn("point_out_of_bounds", codes)
        self.assertIn("swipe_out_of_bounds", codes)

    def test_accepts_a_script_with_no_obvious_geometry_risk(self):
        result = analyze_script_compatibility(
            json.dumps({"version": 2, "steps": [{"action": "wait", "seconds": 1}]}),
            {"model": "Phone", "screen_size": "1080x2400"},
        )

        self.assertEqual(result["status"], "compatible")
        self.assertEqual(result["warnings"], [])

    def test_unknown_resolution_is_a_warning_only(self):
        result = analyze_script_compatibility(
            json.dumps({"version": 2, "steps": [{"action": "wait", "seconds": 1}]}),
            {"model": "Phone"},
        )

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["warnings"][0]["code"], "unknown_resolution")

    def test_device_profile_normalizes_display_size(self):
        profile = device_profile({"display_size": "Physical size: 1080x2400"})

        self.assertEqual(profile["screen_width"], 1080)
        self.assertEqual(profile["screen_height"], 2400)


