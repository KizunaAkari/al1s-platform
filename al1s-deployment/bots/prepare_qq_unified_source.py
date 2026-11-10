"""Assemble the reviewed QQ source without overwriting local trading work."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / 'artifacts' / 'qq-unified'
SOURCE = ROOT / 'backend' / 'al1s-backend'
BASELINE = ROOT / 'al1s-deployment' / 'bots' / 'qq-unified-baseline.json'
PATCH = ROOT / 'al1s-deployment' / 'bots' / 'qq-unified-baseline.patch'
OVERLAYS = (
    'al1s/api/bot_ai.py',
    'al1s/adapters/postgres/bot_ai_history.py',
    'al1s/adapters/postgres/bot_ai_history_models.py',
    'al1s/adapters/postgres/bot_ai_models.py',
    'al1s/adapters/postgres/bot_ai_repository.py',
    'al1s/meme_workshop/runtime.py',
    'al1s/meme_workshop/runtime_main.py',
    'al1s/meme_workshop/runtime_probe.py',
)


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: prepare_qq_unified_source.py OUTPUT_DIRECTORY')
    output = Path(sys.argv[1]).resolve()
    if not output.is_relative_to(ARTIFACTS.resolve()) or output.exists():
        raise ValueError('output_must_be_new_qq_unified_artifact')
    manifest = json.loads(BASELINE.read_text(encoding='utf-8'))
    if manifest['baseline_image_id'] != 'sha256:9bb1abb8e712ae2d006b8acd64cf1738f2dce58eb86d20073e51d2040ecaa496':
        raise ValueError('qq_baseline_image_invalid')
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != manifest['patch_sha256']:
        raise ValueError('qq_baseline_patch_mismatch')
    for name, record in manifest['files'].items():
        source = (SOURCE / 'al1s' / name).resolve()
        if not source.is_relative_to((SOURCE / 'al1s').resolve()):
            raise ValueError('qq_baseline_path_invalid')
        expected = record['workspace_sha256']
        if expected is None:
            if source.exists():
                raise ValueError('qq_workspace_source_changed:' + name)
            continue
        data = source.read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('qq_workspace_source_changed:' + name)
        target = output / 'al1s' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    directory = output.relative_to(ROOT).as_posix()
    for extra in (['--check'], []):
        subprocess.run(['git', 'apply', *extra, '--directory=' + directory, str(PATCH)], cwd=ROOT, check=True)
    for name, record in manifest['files'].items():
        path = output / 'al1s' / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['baseline_sha256']:
            raise ValueError('qq_baseline_source_mismatch:' + name)
    shutil.copytree(SOURCE / 'al1s' / 'bot_ai', output / 'al1s' / 'bot_ai',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in OVERLAYS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    files = sorted((output / 'al1s').rglob('*.py'), key=lambda path: path.relative_to(output).as_posix())
    digest = hashlib.sha256(b''.join(path.read_bytes() for path in files)).hexdigest()
    (output / 'source-manifest.json').write_text(json.dumps({
        'baseline_image_id': manifest['baseline_image_id'], 'python_files': len(files),
        'source_sha256': digest, 'patch_sha256': manifest['patch_sha256'],
        'overlays': list(OVERLAYS) + ['al1s/bot_ai/'],
    }, indent=2), encoding='utf-8')
    print(json.dumps({'python_files': len(files), 'source_sha256': digest}))


if __name__ == '__main__':
    main()
