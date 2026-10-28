from al1s.quant.health import QuantHealth


def test_health_reports_unavailable_dependencies_without_faking_success():
    health = QuantHealth(database=lambda: False, source=lambda: {"minute": {"last_success": None, "error": "unavailable"}},
                         engine=lambda: {"version": "0.3.71", "entitlement_supported": True})
    result = health.read()
    assert result["database"] == "unavailable"
    assert result["source"]["minute"]["last_success"] is None
    assert result["engine"]["entitlement_supported"] is True
    assert "cpu_percent" in result
