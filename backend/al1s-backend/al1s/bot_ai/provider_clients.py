from al1s.bot_ai.configuration import existing_https_proxy


def create_client(provider, secret):
    if not secret:
        raise ValueError('ai_provider_unconfigured')
    mode = getattr(provider, 'network_mode', 'direct') or 'direct'
    if mode not in {'direct', 'proxy'}:
        raise ValueError('ai_provider_network_invalid')
    proxy = existing_https_proxy() if mode == 'proxy' else None
    kind = provider.adapter_kind
    timeout = 20 if kind == 'openai_whisper_api' else 15 if kind in {'openai_tts_api', 'gemini_tts'} else 90
    if kind in {'openai_chat_completion', 'openai_responses', 'openai_whisper_api', 'openai_tts_api'}:
        import httpx2
        from openai import AsyncOpenAI

        http = httpx2.AsyncClient(proxy=proxy, trust_env=False, timeout=timeout)
        return AsyncOpenAI(api_key=secret, base_url=provider.base_url or None,
                           timeout=timeout, max_retries=1, http_client=http)
    if kind in {'googlegenai_chat_completion', 'gemini_tts'}:
        from google import genai

        options = {'timeout': timeout * 1000, 'async_client_args': {'trust_env': False}}
        if proxy:
            options['async_client_args']['proxy'] = proxy
        if provider.base_url:
            options['base_url'] = provider.base_url
        return genai.Client(api_key=secret, http_options=options).aio
    raise ValueError('ai_provider_adapter_invalid')
