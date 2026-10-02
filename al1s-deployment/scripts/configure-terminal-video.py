"""Persist video configuration in the terminal env used by future upgrades."""
import argparse
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('env_file', type=Path)
    parser.add_argument('--url', required=True)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme != 'wss' or not url.hostname or url.username or url.password:
        raise SystemExit('Trusted WSS endpoint required')
    updates = {
        'AL1S_TERMINAL_IMAGE': args.image,
        'AL1S_TERMINAL_SCRCPY_PUBLIC_URL': args.url,
        'AL1S_TERMINAL_SCRCPY_TLS_CERT_FILE': args.cert,
        'AL1S_TERMINAL_SCRCPY_TLS_KEY_FILE': args.key,
    }
    if any('\n' in value or '\r' in value for value in updates.values()):
        raise SystemExit('Invalid multiline setting')
    path = args.env_file.resolve(strict=True)
    lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0].strip() not in updates]
    body = '\n'.join(lines + [f'{key}={value}' for key, value in updates.items()]) + '\n'
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.video-config-')
    try:
        with os.fdopen(handle, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Terminal video/image configuration persisted; other settings retained.')


if __name__ == '__main__':
    main()
