import argparse
import getpass
import os
import sys
from pathlib import Path

from . import __version__
from .engine import (
    EXTENSION, NotLockbox, decrypt_path, describe, encrypt_path, human_size, is_locked, MIN_PASSWORD,
)


def build_parser():
    parser = argparse.ArgumentParser(prog="lockbox", description="Lock and unlock files with a password.")
    parser.add_argument("--version", action="version", version="lockbox " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", aliases=["encrypt"], help="encrypt files or folders")
    lock.add_argument("paths", nargs="+")
    lock.add_argument("-o", "--output", help="save locked files in this folder")
    lock.add_argument("-k", "--keyfile", help="also require this file to unlock")
    lock.add_argument("--hide-name", action="store_true", help="give the locked file a random name")
    lock.add_argument("--delete", action="store_true", help="delete the original once the locked copy checks out")
    lock.add_argument("--shred", action="store_true", help="with --delete, overwrite the original first")

    unlock = commands.add_parser("unlock", aliases=["decrypt"], help="decrypt files")
    unlock.add_argument("paths", nargs="+")
    unlock.add_argument("-o", "--output", help="save unlocked files in this folder")
    unlock.add_argument("-k", "--keyfile", help="the key file used when locking")
    unlock.add_argument("--delete", action="store_true", help="delete the locked file afterwards")

    info = commands.add_parser("info", help="show details of a locked file")
    info.add_argument("paths", nargs="+")
    return parser


def ask_password(confirm):
    password = os.environ.get("LOCKBOX_PASSWORD")
    if password:
        return password
    password = getpass.getpass("Password: ")
    if confirm:
        if getpass.getpass("Repeat password: ") != password:
            print("The passwords don't match", file=sys.stderr)
            sys.exit(2)
    return password


def expand(paths, locking):
    for entry in paths:
        entry = Path(entry)
        if entry.is_dir() and not locking:
            for child in sorted(entry.rglob("*" + EXTENSION)):
                yield child
        else:
            yield entry


def report(result):
    if result.error:
        print("%s: %s" % (result.source, result.error), file=sys.stderr)
        return False
    print("%s -> %s (%s)" % (result.source, result.output, human_size(result.size)))
    return True


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "info":
        failed = 0
        for path in args.paths:
            try:
                details = describe(path)
            except (NotLockbox, OSError) as problem:
                failed += 1
                print("%s: %s" % (path, problem), file=sys.stderr)
                continue
            print(path)
            for key, value in details.items():
                print("  %s: %s" % (key.replace("_", " "), value))
        return 1 if failed else 0

    locking = args.command in ("lock", "encrypt")
    if locking and args.shred and not args.delete:
        print("--shred only works together with --delete", file=sys.stderr)
        return 2
    password = ask_password(locking)
    if locking and len(password) < MIN_PASSWORD:
        print("The password needs at least %d characters" % MIN_PASSWORD, file=sys.stderr)
        return 2

    failed = 0
    for path in expand(args.paths, locking):
        if locking:
            result = encrypt_path(
                path, password, keyfile=args.keyfile, output_dir=args.output, hide_name=args.hide_name,
                delete_original=args.delete, shred=args.shred,
            )
        else:
            if not is_locked(path):
                print("%s: not a Lockbox file" % path, file=sys.stderr)
                failed += 1
                continue
            result = decrypt_path(path, password, keyfile=args.keyfile, output_dir=args.output, delete_original=args.delete)
        if not report(result):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

_credit = "Built by Unkn0wngb"
