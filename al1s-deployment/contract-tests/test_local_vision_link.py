import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/Start-From-AL1S.ps1'


@pytest.mark.parametrize('action', ['start', 'stop'])
def test_fixed_actions_call_only_the_comfy_controller(tmp_path, action):
    shutil.copyfile(SCRIPT, tmp_path / SCRIPT.name)
    (tmp_path / 'runtime').mkdir()
    body = "param([string]$Action); [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'called.txt'),$Action)"
    (tmp_path / 'Control-ComfyUI.ps1').write_text(body, encoding='utf-8')
    result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-File', str(tmp_path / SCRIPT.name),
                             '-Uri', f'al1s-vision://{action}/'], capture_output=True)
    assert result.returncode == 0
    assert (tmp_path / 'called.txt').read_text() == action


@pytest.mark.parametrize('uri,extra', [
    ('al1s-vision://stop?pid=1', []), ('al1s-vision://stop/other', []),
    ('al1s-vision://stop', ['extra']), ('other://stop', []),
])
def test_other_links_and_extra_arguments_are_rejected(tmp_path, uri, extra):
    shutil.copyfile(SCRIPT, tmp_path / SCRIPT.name)
    (tmp_path / 'runtime').mkdir()
    result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-File', str(tmp_path / SCRIPT.name),
                             '-Uri', uri, *extra], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / 'called.txt').exists()


def test_controller_failure_is_recorded_in_the_installed_state_directory(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    (scripts / 'Control-ComfyUI.ps1').write_text("param([string]$Action); throw 'example_start_failure'", encoding='utf-8')
    state = tmp_path / 'packages/components/runtime/local-vision'
    state.mkdir(parents=True)
    result = subprocess.run([shutil.which('powershell'), '-NoProfile', '-File', str(scripts / SCRIPT.name),
                             '-Uri', 'al1s-vision://start'], capture_output=True)
    assert result.returncode != 0
    assert (state / 'link-status.txt').read_text(encoding='utf-8-sig') == 'failed: example_start_failure'
