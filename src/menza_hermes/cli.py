"""Only fixed subcommands; no command execution or arbitrary file access."""
import argparse
from datetime import datetime
import logging
import os
import time

from .auth import AuthError
from .models import PRAGUE
from .runtime import account
from .watcher import Watches, POLL_SECONDS


def worker(once=False):
    while True:
        try:
            with account() as client:
                watches = Watches(client.state_dir)
                try:
                    before = {w['id']: w['status'] for w in watches.list()}
                    results = watches.tick(client, datetime.now(PRAGUE))
                    for w in results:
                        if before.get(w['id']) != w['status']:
                            logging.info('Watch %s became %s', w['id'], w['status'])
                finally:
                    watches.close()
        except AuthError:
            # Don't keep hammering Microsoft with a rejected password/MFA flow.
            logging.error('Authentication requires operator attention; watcher stopped')
            return 78
        except Exception:
            logging.warning('Read/poll failed; pending watches retained; private error details omitted')
        if once:
            return 0
        time.sleep(POLL_SECONDS)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='TUL Menza MCP server and bounded burza watcher')
    parser.add_argument('command', choices=['serve', 'watcher'], nargs='?', default='serve')
    parser.add_argument('--once', action='store_true', help='Run one watcher pass')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == 'serve':
        from .server import mcp
        mcp.run()
    else:
        raise SystemExit(worker(args.once))


if __name__ == '__main__':
    main()
