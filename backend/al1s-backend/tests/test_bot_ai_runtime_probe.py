from pathlib import Path
import ast

import pytest

from al1s.meme_workshop import runtime_probe


def test_local_ai_probe_rejects_an_active_astrbot_program(tmp_path, monkeypatch):
    supervisor = tmp_path / 'supervisor.conf'
    supervisor.write_text('[program:meme-backend]\n[program:astrbot]\n', encoding='utf-8')
    with pytest.raises(ValueError, match='astrbot'):
        runtime_probe.prove_local_ai(supervisor, legacy_paths=())


def test_local_ai_probe_requires_fixed_sdks_and_no_legacy_runtime(tmp_path, monkeypatch):
    supervisor = tmp_path / 'supervisor.conf'
    supervisor.write_text('[program:meme-backend]\n', encoding='utf-8')
    monkeypatch.setattr(runtime_probe.importlib, 'import_module', lambda _name: object())
    monkeypatch.setattr(runtime_probe, 'package_version', lambda name: {
        'openai':'3.31.0', 'google-genai':'2.33.0', 'websockets':'17.1'}[name])
    monkeypatch.setattr(runtime_probe.shutil, 'which', lambda _name: 'fixture')
    runtime_probe.prove_local_ai(supervisor, legacy_paths=(tmp_path / 'absent',))
    legacy = tmp_path / 'astrbot-venv'
    legacy.mkdir()
    with pytest.raises(ValueError, match='astrbot'):
        runtime_probe.prove_local_ai(supervisor, legacy_paths=(legacy,))


def test_active_ai_modules_do_not_import_astrbot_or_load_legacy_bridge():
    root = Path(__file__).resolve().parents[1] / 'al1s' / 'bot_ai'
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name == 'astrbot' or name.startswith('astrbot.') for name in imports), str(path)
    assert not (root / 'bridge.py').exists() and not (root / 'bootstrap.py').exists()
