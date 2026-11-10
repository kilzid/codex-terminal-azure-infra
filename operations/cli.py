#!/usr/bin/env python3
"""Operate an Entra-protected Azure terminal; private output stays local."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import urllib.error

from cloud import Failure, atomic, require
from controller import Controller
from identity import bootstrap, local_policy_reader
from adoption import adopt, fresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'adopt', 'plan', 'apply', 'verify', 'rollback', 'rotate-credentials'])
    parser.add_argument('--private', required=True, help='Private recovery directory outside the checkout')
    parser.add_argument('--fresh', action='store_true', help='During bootstrap, provision an empty resource group')
    parser.add_argument('--no-publish', action='store_true', help='Bootstrap an isolated test without replacing GitHub secrets')
    parser.add_argument('--revision', help='Private runtime rollback revision identifier')
    args = parser.parse_args()
    os.umask(0o077)
    directory = Path(args.private).expanduser().resolve()
    from controller import ROOT
    require(directory != ROOT and ROOT not in directory.parents, 'private-inputs-must-be-outside-checkout')
    if args.command == 'bootstrap':
        result = bootstrap(directory, publish=not args.no_publish)
        if args.fresh:
            ctl = Controller(directory)
            with local_policy_reader(ctl.cloud): result = fresh(ctl, publish=not args.no_publish)
    else:
        require(not args.fresh and not args.no_publish, 'bootstrap-flag-used-with-other-command')
        ctl = Controller(directory)
        with local_policy_reader(ctl.cloud):
            if args.command == 'adopt': result = adopt(ctl)
            elif args.command == 'plan':
                plan = ctl.plan()
                result = {'status': 'planned', 'infrastructureChanges': len(plan['infrastructure']),
                          'changedFiles': len(plan['host']['changed']), 'affectedServices': plan['host']['affected'],
                          'budgetChanged': plan['budgetChanged'], 'callbackChanged': plan['callbackChanged']}
            elif args.command == 'apply': result = ctl.apply()
            elif args.command == 'verify': result = ctl.verify()
            elif args.command == 'rollback': result = ctl.rollback(args.revision)
            else: result = ctl.rotate_credentials()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Failure as error:
        # Failure messages are fixed public codes, never API bodies or user values.
        print(json.dumps({'status': 'failed', 'code': str(error)}), file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError as error:
        print(json.dumps({'status': 'failed', 'code': 'cloud-api-failed', 'httpStatus': error.code}), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(json.dumps({'status': 'failed', 'code': 'operation-failed-details-withheld'}), file=sys.stderr)
        sys.exit(1)
