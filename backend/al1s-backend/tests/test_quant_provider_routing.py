import pytest

from al1s.quant.provider_routing import eligible_sources, select_source


def test_primary_success_does_not_contact_backup():
    calls = []

    def fetch(provider):
        calls.append(provider)
        return [provider]

    provider, rows = select_source(("sina", "tencent", "eastmoney"), fetch)
    assert (provider, rows) == ("sina", ["sina"])
    assert calls == ["sina"]


def test_unavailable_source_falls_back_in_order_without_swallowing_invalid_request():
    calls = []

    def fetch(provider):
        calls.append(provider)
        if provider == "sina":
            raise ValueError("quant_source_unavailable")
        return [provider]

    assert select_source(("sina", "tencent", "eastmoney"), fetch)[0] == "tencent"
    assert calls == ["sina", "tencent"]
    with pytest.raises(ValueError, match="quant_code_invalid"):
        select_source(("sina", "tencent"), lambda _: (_ for _ in ()).throw(ValueError("quant_code_invalid")))


def test_capability_and_missing_credentials_skip_unusable_providers():
    ordered = ("sina", "tencent", "eastmoney", "tushare", "gugudata")
    assert eligible_sources(ordered, market="BJ", frequency="1m", adjustment="", configured=set()) == ("sina", "eastmoney")
    assert eligible_sources(ordered, market="SH", frequency="1d", adjustment="qfq", configured={"gugudata"}) == ("tencent", "eastmoney", "gugudata")


def test_all_unavailable_returns_failure_without_fabricating_result():
    with pytest.raises(ValueError, match="quant_source_unavailable"):
        select_source(("sina", "tencent"), lambda _: (_ for _ in ()).throw(ValueError("quant_source_unavailable")))
