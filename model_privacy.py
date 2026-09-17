"""Fail closed when a writing route cannot enforce provider privacy."""

def private_request_payload(backend, payload):
    if backend.get('url') != 'https://openrouter.ai/api/v1/chat/completions':
        raise ValueError('This model route is disabled until its account privacy is verified')
    result = dict(payload)
    result.update(backend.get('request_options', {}))
    provider = dict(result.get('provider', {}))
    provider.update(zdr=True, data_collection='deny')
    result['provider'] = provider
    return result
