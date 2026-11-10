import sys
from types import SimpleNamespace

import pytest

from al1s.bot_ai.provider_clients import create_client


def spec(kind='openai_chat_completion', network='direct'):
    return SimpleNamespace(adapter_kind=kind, model_id='model', base_url='https://example.invalid/v1',
                           network_mode=network)


def test_openai_direct_never_inherits_proxy_and_proxy_mode_is_explicit(monkeypatch):
    seen = []
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setitem(sys.modules, 'httpx2', SimpleNamespace(AsyncClient=lambda **kw: seen.append(kw) or kw))
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(AsyncOpenAI=lambda **kw: kw))
    direct = create_client(spec(), 'fixture-key')
    assert direct['http_client']['trust_env'] is False and direct['http_client']['proxy'] is None
    proxied = create_client(spec(network='proxy'), 'fixture-key')
    assert proxied['http_client']['proxy'] == 'http://proxy.invalid:8080'
    assert proxied['http_client']['trust_env'] is False and len(seen) == 2


def test_gemini_proxy_options_do_not_depend_on_process_environment(monkeypatch):
    created = []
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    fake = SimpleNamespace(Client=lambda **kw: created.append(kw) or SimpleNamespace(aio=kw))
    google = SimpleNamespace(genai=fake)
    monkeypatch.setitem(sys.modules, 'google', google)
    monkeypatch.setitem(sys.modules, 'google.genai', fake)
    result = create_client(spec('googlegenai_chat_completion'), 'fixture-key')
    assert result['http_options']['async_client_args'] == {'trust_env': False}
    assert result['http_options']['base_url'] == 'https://example.invalid/v1'
    assert len(created) == 1


def test_missing_key_and_unusable_proxy_fail_before_sdk_creation(monkeypatch):
    with pytest.raises(ValueError, match='unconfigured'):
        create_client(spec(), '')
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.delenv('https_proxy', raising=False)
    with pytest.raises(ValueError, match='proxy_unavailable'):
        create_client(spec(network='proxy'), 'fixture-key')
