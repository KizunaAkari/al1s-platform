"""Overlay drawing changes onto a verified snapshot of the current QQ runtime."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'backend' / 'al1s-backend' / 'al1s'
BASE_SOURCE_HASH = 'd5d68d763eae038481f641a99650c8aae7bb6a23c537e73c0dd46d0f09a72dac'
BASE_IMAGE = 'sha256:49d178a7f3be11eec151ce65725aa6da02f003de06d54c0bb3f88dea8b26a66b'
OVERLAYS = (
    'adapters/postgres/bot_ai_history.py', 'adapters/postgres/bot_ai_history_models.py',
    'adapters/postgres/bot_ai_repository.py', 'adapters/postgres/secret_repository.py',
    'adapters/postgres/bot_drawing_models.py', 'adapters/postgres/bot_drawing_repository.py',
    'api/bot_drawing.py', 'bot_ai/configuration.py', 'bot_ai/history.py',
    'bot_ai/managed/comfy_client.py', 'bot_ai/model_manager.py', 'bot_ai/sender.py',
    'bot_ai/drawing_inference.py', 'meme_workshop/qq_worker.py', 'meme_workshop/runtime.py',
    'meme_workshop/runtime_identity.py', 'meme_workshop/runtime_main.py', 'meme_workshop/runtime_probe.py',
    'meme_workshop/transport.py',
)


def source_hash(directory: Path) -> tuple[int, str]:
    files = sorted(directory.rglob('*.py'), key=lambda path: path.relative_to(directory).as_posix())
    return len(files), hashlib.sha256(b''.join(path.read_bytes() for path in files)).hexdigest()


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: prepare_qq_drawing_source.py LIVE_SOURCE_SNAPSHOT BASELINE_MANIFEST OUTPUT_DIRECTORY')
    snapshot, baseline_path, output = (Path(value).resolve() for value in sys.argv[1:])
    if not output.is_relative_to((ROOT / 'artifacts' / 'qq-unified').resolve()) or output.exists():
        raise ValueError('output_must_be_new_qq_unified_artifact')
    if source_hash(snapshot) != (600, BASE_SOURCE_HASH):
        raise ValueError('qq_runtime_snapshot_mismatch')
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    original = Path(baseline['source']).resolve() / 'backend' / 'al1s-backend' / 'al1s'
    overlays = list(OVERLAYS)
    overlays.extend(path.relative_to(SOURCE).as_posix() for path in (SOURCE / 'bot_drawing').rglob('*.py'))
    for name in overlays:
        source = SOURCE / name
        old = original / name
        deployed = snapshot / name
        key = 'backend/al1s-backend/al1s/' + name
        if not source.is_file() or source.is_symlink():
            raise ValueError('qq_overlay_missing:' + name)
        if old.is_file() and old.read_bytes() == source.read_bytes():
            continue
        if key in baseline['files']:
            if not old.is_file() or hashlib.sha256(old.read_bytes()).hexdigest() != baseline['files'][key]:
                raise ValueError('qq_original_source_changed:' + name)
        if deployed.is_file():
            if not old.is_file() or old.read_bytes().replace(b'\r\n', b'\n') != deployed.read_bytes().replace(b'\r\n', b'\n'):
                raise ValueError('qq_overlay_requires_merge:' + name)
        elif not (name.startswith('bot_drawing/') or name in {
                'api/bot_drawing.py', 'bot_ai/drawing_inference.py',
                'adapters/postgres/bot_drawing_models.py', 'adapters/postgres/bot_drawing_repository.py'}):
            raise ValueError('qq_runtime_source_missing:' + name)
    shutil.copytree(snapshot, output / 'al1s', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in overlays:
        target = output / 'al1s' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE / name).read_bytes().replace(b'\r\n', b'\n'))
    count, digest = source_hash(output / 'al1s')
    (output / 'source-manifest.json').write_text(json.dumps({
        'baseline_image_id': BASE_IMAGE, 'baseline_source_sha256': BASE_SOURCE_HASH,
        'python_files': count, 'source_sha256': digest, 'overlays': sorted(overlays),
    }, indent=2), encoding='utf-8')
    print(json.dumps({'python_files': count, 'source_sha256': digest}))


if __name__ == '__main__':
    main()
