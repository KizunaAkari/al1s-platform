"""Create a private deploy secret without exposing it in shell history or stdout.

For existing deployments, first verify there are no outstanding offline permits
or executions. This initializer never overwrites an existing key.
"""
import argparse
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('env_file', type=Path)
    args = parser.parse_args()
    path = args.env_file.resolve(strict=True)
    text = path.read_text(encoding='utf-8-sig')
    name = 'AL1S_OFFLINE_PERMIT_SIGNING_KEY'
    if any(line.strip().startswith(name + '=') for line in text.splitlines()):
        raise SystemExit('Existing key retained; key rotation requires a separate review.')
    # Preserve the existing file ACL and all unrelated configuration.
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write('\n' + name + '=' + secrets.token_hex(32) + '\n')
    print('Private offline permit key initialized; value not displayed.')


if __name__ == '__main__':
    main()
