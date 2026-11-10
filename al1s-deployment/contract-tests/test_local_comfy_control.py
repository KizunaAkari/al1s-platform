import json
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/Control-ComfyUI.ps1'


def _quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


def _fixture(tmp_path, *, occupied=False, identity_changed=False):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    controller = scripts / SCRIPT.name
    if SCRIPT.exists():
        shutil.copyfile(SCRIPT, controller)
    state = tmp_path / 'packages/components/runtime/local-vision'
    state.mkdir(parents=True)
    root = tmp_path / 'ComfyUI'
    root.mkdir()
    main = root / 'main.py'
    main.write_text('', encoding='utf-8')
    python = tmp_path / 'python.exe'
    python.write_text('', encoding='utf-8')
    (state / 'config.json').write_text(json.dumps({
        'comfy_root': str(root), 'python_executable': str(python),
    }), encoding='utf-8')
    calls = tmp_path / 'calls.txt'
    created = '2026-01-01T00:00:01Z' if identity_changed else '2026-01-01T00:00:00Z'
    harness = tmp_path / 'run.ps1'
    harness.write_text(f"""
param([string]$ActionList)
$ErrorActionPreference = 'Stop'
$global:fakePython = {_quoted(python)}
$global:fakeMain = {_quoted(main)}
$global:fakeCalls = {_quoted(calls)}
function Get-NetTCPConnection {{
    param($LocalPort, $State, $ErrorAction)
    if ({'$true' if occupied else '$false'}) {{ [PSCustomObject]@{{ LocalPort = 8188 }} }}
}}
function Get-CimInstance {{
    param($ClassName, $Filter, $ErrorAction)
    [PSCustomObject]@{{ ProcessId = 4242; ExecutablePath = $global:fakePython;
        CreationDate = [DateTime]::Parse('{created}');
        CommandLine = ('"' + $global:fakePython + '" -s "' + $global:fakeMain + '" --listen 127.0.0.1 --port 8188') }}
}}
function Start-Process {{
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput,
          $RedirectStandardError, [switch]$PassThru)
    Add-Content -LiteralPath $global:fakeCalls -Value 'start'
    [PSCustomObject]@{{ Id = 4242 }}
}}
function Stop-Process {{
    param($Id, $ErrorAction)
    Add-Content -LiteralPath $global:fakeCalls -Value ('stop:' + $Id)
}}
function Wait-Process {{ param($Id, $Timeout, $ErrorAction) }}
foreach ($action in $ActionList.Split(',')) {{ & {_quoted(controller)} -Action $action }}
""", encoding='utf-8')
    return state, calls, harness


@pytest.fixture(params=['pwsh', 'powershell'])
def shell(request):
    return request.param


def _invoke(harness, *actions, shell):
    powershell = shutil.which(shell)
    assert powershell is not None
    return subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(harness), ','.join(actions)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


def test_start_records_identity_and_does_not_start_twice(tmp_path, shell):
    state, calls, harness = _fixture(tmp_path)
    result = _invoke(harness, 'start', 'start', shell=shell)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ['start']
    assert json.loads((state / 'process.json').read_text(encoding='utf-8-sig'))['pid'] == 4242


def test_start_does_not_replace_an_occupied_port(tmp_path, shell):
    _state, calls, harness = _fixture(tmp_path, occupied=True)
    result = _invoke(harness, 'start', shell=shell)
    assert result.returncode != 0
    assert 'local_vision_port_occupied' in result.stderr
    assert not calls.exists()


def test_stop_only_stops_the_identified_process(tmp_path, shell):
    state, calls, harness = _fixture(tmp_path)
    result = _invoke(harness, 'start', 'stop', shell=shell)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ['start', 'stop:4242']
    assert not (state / 'process.json').exists()


def test_stop_refuses_changed_process_identity(tmp_path, shell):
    state, calls, start = _fixture(tmp_path)
    assert _invoke(start, 'start', shell=shell).returncode == 0
    harness = start.read_text(encoding='utf-8').replace('2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z')
    start.write_text(harness, encoding='utf-8')
    result = _invoke(start, 'stop', shell=shell)
    assert result.returncode != 0
    assert 'local_vision_identity_mismatch' in result.stderr
    assert calls.read_text().splitlines() == ['start']
    assert (state / 'process.json').exists()


def test_stop_does_not_touch_an_unmanaged_process(tmp_path, shell):
    _state, calls, harness = _fixture(tmp_path, occupied=True)
    result = _invoke(harness, 'stop', shell=shell)
    assert result.returncode != 0
    assert 'local_vision_process_unmanaged' in result.stderr
    assert not calls.exists()
