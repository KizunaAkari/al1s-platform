import pytest

from al1s.quant.env_bootstrap import main


def test_quant_bootstrap_copies_existing_qq_proxy_without_other_secrets(monkeypatch):
    monkeypatch.setenv("AL1S_QUANT_CONTROL_TOKEN", "x" * 32)
    monkeypatch.setenv("AL1S_QUANT_DATABASE_URL", "postgresql://example/test")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:7897")
    monkeypatch.setenv("AL1S_MEME_DATABASE_URL", "private")
    monkeypatch.setenv("AL1S_QUANT_CREDENTIAL_KEY", "derived-key-" * 6)
    monkeypatch.setenv("AL1S_MEME_MASTER_KEY", "master-secret")
    captured = {}

    def fake_exec(_program, _args, env):
        captured.update(env)
        raise RuntimeError("intercepted")

    monkeypatch.setattr("os.execve", fake_exec)
    with pytest.raises(RuntimeError, match="intercepted"):
        main()
    assert captured["AL1S_QUANT_EASTMONEY_PROXY"] == "http://proxy.example:7897"
    assert "HTTPS_PROXY" not in captured and "AL1S_MEME_DATABASE_URL" not in captured
    assert captured["AL1S_QUANT_CREDENTIAL_KEY"] == "derived-key-" * 6
    assert "AL1S_MEME_MASTER_KEY" not in captured
