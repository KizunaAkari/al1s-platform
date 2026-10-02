from al1s.execution.diagnostic_events import conditional_skip_metadata


def test_skip_metadata_preserves_location_without_capture_or_raw_log():
    result = conditional_skip_metadata(
        {
            "modules": [
                {
                    "result": {
                        "conditional_skips": [
                            {
                                "module_number": 3,
                                "module_step_number": 2,
                                "trigger_step_number": 54,
                                "capture": {"path": "/private/screenshot.png"},
                                "detail": "private log",
                            }
                        ]
                    }
                }
            ]
        }
    )
    assert result == [
        {
            "module_number": 3,
            "module_step_number": 2,
            "step_number": 54,
            "diagnostic_pointer": "/modules/0/result/conditional_skips/0",
        }
    ]


def test_invalid_or_missing_diagnostic_does_not_invent_skips():
    assert conditional_skip_metadata(None) == []
    assert conditional_skip_metadata({"modules": [None, {"result": None}]}) == []
