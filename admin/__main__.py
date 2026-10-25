"""Command line: python3 -m admin set-password"""
import getpass
import json
import logging
import os
import sys

import admin as admin_pkg
import envfile

from .core import hash_password, MIN_PASSWORD


def _cli(argv):
    if argv != ["set-password"]:
        print(admin_pkg.__doc__.split("Command line:")[1].split("\n\n")[0].strip())
        return 2
    import config  # local import: the dashboard's config loader
    cfg = config.load_config(os.path.join(config.BASE_DIR, "config.json"))
    path = os.path.join(config.BASE_DIR, cfg["admin"]["password_file"])
    if not sys.stdin.isatty():
        print("Run this in an interactive terminal so the password can be typed without echo.")
        return 1
    pw = getpass.getpass("New admin password (input hidden, at least %d characters): " % MIN_PASSWORD)
    if len(pw) < MIN_PASSWORD:
        print("Too short. Nothing was saved.")
        return 1
    if getpass.getpass("Type it again: ") != pw:
        print("The two entries differ. Nothing was saved.")
        return 1
    envfile.write_private(path, json.dumps(hash_password(pw)) + "\n")
    print("Saved to %s (mode 600). Existing sessions stay signed in until they expire or the server restarts."
          % path)
    print("Open https://<server>:%s/admin" % (cfg["https_port"] or "<https_port>"))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(_cli(sys.argv[1:]))
