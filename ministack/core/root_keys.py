# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
# Copies or substantial portions, including AI-assisted ports or rewrites, must retain this notice (see LICENSE).
"""Read and write the root access key file used when ROOT_AUTH is on."""

import errno
import getpass
import json
import os
import sys
import tempfile


def resolve_root_keys_path(explicit: str | None = None) -> str:
    """The JSON file to read or write.

    An explicit path wins, then ``ROOT_KEYS_FILE``, then ``./root-keys.json``
    in the current directory. Compose mounts that relative file into the
    container at ``ROOT_KEYS_FILE``. ``explicit`` is the ``--file`` path.
    The returned path may not exist yet.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("ROOT_KEYS_FILE", "").strip()
    if from_env:
        return from_env
    return os.path.join(os.getcwd(), "root-keys.json")


def load_root_keys_file(path: str) -> dict[str, str]:
    """Keys stored at ``path``.

    A missing file is an empty map. The file is a JSON object of access key
    id to secret. Broken JSON, a non-object, or an empty secret raises
    ``ValueError``.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"ROOT_KEYS_FILE {path} is not valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"ROOT_KEYS_FILE {path} must be a JSON object of access key to secret")
    keys: dict[str, str] = {}
    for access_key, secret in loaded.items():
        access_key = str(access_key).strip()
        if not access_key or not isinstance(secret, str) or secret == "":
            raise ValueError(
                f"ROOT_KEYS_FILE {path} entries must map an access key to a non-empty secret"
            )
        keys[access_key] = secret
    return keys


def write_root_keys_file(path: str, keys: dict[str, str]) -> None:
    """Replace ``path`` with ``keys``.

    The write is a temp file in the same directory, then ``os.replace``, so a
    crash does not leave a half-written key list. A Docker bind mount of this
    file alone cannot be renamed over (``EBUSY``), so that case writes the
    mounted file in place. The file mode is ``0600``. ``keys`` maps an access
    key id to its secret.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = json.dumps(keys, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".root-keys.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # The path is the mount point of a single-file bind. rename fails.
            if exc.errno != errno.EBUSY:
                raise
            _overwrite_mounted_file(path, payload)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _overwrite_mounted_file(path: str, payload: str) -> None:
    """Write ``payload`` onto an existing mount, which ``os.replace`` cannot swap."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def add_parser(subparsers) -> None:
    """Register ``ministack root-key`` on the server parser."""
    parser = subparsers.add_parser(
        "root-key",
        help="Write root access keys to the JSON file",
    )
    commands = parser.add_subparsers(dest="key_action", required=True)

    set_key = commands.add_parser("set", help="Add or replace one access key")
    set_key.add_argument("access_key", help="Access key id, stored in the clear")
    set_key.add_argument(
        "--secret",
        help="Secret. Omit this to type it at a prompt. The value shows up in the process list.",
    )
    set_key.add_argument("--file", help="JSON file. Default: ROOT_KEYS_FILE, or ./root-keys.json")

    list_keys = commands.add_parser("list", help="Print access key ids. Secrets stay in the file.")
    list_keys.add_argument("--file", help="JSON file. Default: ROOT_KEYS_FILE, or ./root-keys.json")

    remove_key = commands.add_parser("remove", help="Remove one access key")
    remove_key.add_argument("access_key", help="Access key id to remove")
    remove_key.add_argument("--file", help="JSON file. Default: ROOT_KEYS_FILE, or ./root-keys.json")


def execute(args) -> int:
    """Run one ``root-key`` subcommand. Returns ``0``, or ``1`` when the file or the key is wrong."""
    path = resolve_root_keys_path(getattr(args, "file", None))
    try:
        if args.key_action == "list":
            return _list_keys(path)
        if args.key_action == "remove":
            return _remove_key(path, args.access_key)
        if args.key_action == "set":
            return _set_key(path, args.access_key, args.secret)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"unknown root-key command: {args.key_action}", file=sys.stderr)
    return 1


def _list_keys(path: str) -> int:
    # Ids only. The secret stays in the file.
    for access_key in load_root_keys_file(path):
        print(access_key)
    return 0


def _remove_key(path: str, access_key: str) -> int:
    keys = load_root_keys_file(path)
    if access_key not in keys:
        print(f"{access_key} is not in {path}", file=sys.stderr)
        return 1
    del keys[access_key]
    write_root_keys_file(path, keys)
    print(f"removed {access_key} from {path}")
    return 0


def _set_key(path: str, access_key: str, secret: str | None) -> int:
    access_key = access_key.strip()
    if not access_key:
        print("access key id is empty", file=sys.stderr)
        return 1
    if secret is None:
        secret = _prompt_secret(access_key)
    if secret == "":
        print("secret is empty", file=sys.stderr)
        return 1
    keys = load_root_keys_file(path)
    keys[access_key] = secret
    write_root_keys_file(path, keys)
    print(f"wrote {access_key} to {path}")
    return 0


def _prompt_secret(access_key: str) -> str:
    # A missing tty cannot ask. --secret is the non-interactive path.
    if not sys.stdin.isatty():
        raise ValueError("pass --secret, or run in a terminal to type the secret")
    return getpass.getpass(f"Secret for {access_key}: ")
