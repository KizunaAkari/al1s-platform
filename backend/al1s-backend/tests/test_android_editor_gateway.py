"""The deployed TLS gateway must upgrade both sides of the fixed editor transport."""
from pathlib import Path
import re


def test_reverse_phone_and_browser_channels_have_websocket_upgrade_routes():
    root=Path(__file__).resolve().parents[3]
    text=(root/'al1s-deployment/compose/tls/nginx.conf').read_text(encoding='utf-8')
    routes=re.findall(r'location ~ ([^\n]+) \{([\s\S]*?)\n    \}',text)
    identity='189393ea-fc8f-41c2-8bdd-dabb0be26fe6'
    paths=[f'/api/v1/editor-sessions/{identity}/channels/{channel}' for channel in ('video','control','screenshot','foreground')]
    paths += [f'/api/v1/terminal/editor-sessions/{identity}/uplink/{channel}' for channel in ('video','control','screenshot','foreground','ocr','app_icon')]
    for path in paths:
        body=next((body for pattern,body in routes if re.search(pattern,path)),None)
        assert body is not None,path
        assert 'proxy_set_header Upgrade $http_upgrade;' in body
        assert 'proxy_set_header Connection "upgrade";' in body
        assert 'proxy_buffering off;' in body
