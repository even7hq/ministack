# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""Local models the Bedrock emulator can call.

``local-models.json`` maps a Bedrock model id to an OpenAI-compatible server
(Ollama, llama.cpp, vLLM). The file is the registry: ``ListFoundationModels``
and ``GetFoundationModel`` include every id in it, and Converse / InvokeModel /
``/v1/chat/completions`` send that id to the server named in the entry.

The file is not a secret. It holds model ids and URLs, so it is mode ``0644``.
A missing file means there are no local models. ``MINISTACK_BEDROCK_PROXY_URL``
still forwards every other model id to one server; a local entry wins for its
own id.
"""

import errno
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 180.0


def resolve_local_models_path(explicit: str | None = None) -> str:
    """Path of the local-models file.

    ``explicit`` wins, then ``LOCAL_MODELS_FILE``, then ``./local-models.json``.
    """
    if explicit:
        return explicit
    return os.environ.get("LOCAL_MODELS_FILE", "local-models.json")


def load_local_models(path: str | None = None) -> dict[str, dict]:
    """Return ``{model_id: entry}``. A missing file is an empty registry.

    Raises ``ValueError`` when the file exists and is not the expected object.
    """
    path = resolve_local_models_path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must be a JSON object of model id to settings.")
    models: dict[str, dict] = {}
    for model_id, entry in parsed.items():
        if not isinstance(model_id, str) or not model_id:
            raise ValueError(f"{path} has an empty model id.")
        if not isinstance(entry, dict):
            raise ValueError(f"{path} entry {model_id!r} must be an object.")
        models[model_id] = entry
    return models


def load_local_models_quietly(path: str | None = None) -> dict[str, dict]:
    """Same as ``load_local_models``, but a bad file logs and returns nothing.

    The request path uses this so one broken file does not fail every call.
    """
    try:
        return load_local_models(path)
    except (OSError, ValueError) as exc:
        logger.warning("ignoring local models file: %s", exc)
        return {}


def write_local_models_file(path: str, models: dict[str, dict]) -> None:
    """Replace ``path`` with ``models``.

    Same rename-then-fallback as the root-keys file: a single-file Docker bind
    mount returns ``EBUSY`` on ``os.replace``, and that case writes in place.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    payload = json.dumps(models, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".local-models.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_local_model(model_id: str) -> tuple[str, str, float] | None:
    """Return ``(base_url, upstream_model, timeout_seconds)`` for ``model_id``.

    ``None`` when the id is not in the file, or the entry has no URL and
    neither ``LOCAL_MODELS_URL`` nor ``MINISTACK_BEDROCK_PROXY_URL`` is set.
    The upstream name is the entry's ``model``, or the Bedrock id.
    """
    if not model_id:
        return None
    entry = load_local_models_quietly().get(model_id)
    if entry is None:
        return None
    url = str(entry.get("url") or os.environ.get("LOCAL_MODELS_URL")
              or os.environ.get("MINISTACK_BEDROCK_PROXY_URL") or "").rstrip("/")
    if not url:
        return None
    upstream = str(entry.get("model") or model_id)
    raw_timeout = entry.get("timeoutSeconds")
    if raw_timeout is None:
        raw_timeout = os.environ.get("LOCAL_MODELS_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT_SECONDS
    return url, upstream, timeout


def serves_builtin_catalog() -> bool:
    """Whether ``ListFoundationModels`` still includes the AWS mock ids.

    A registry names the models this process runs. Listing Claude, Nova and
    the rest beside them offers models that are not available. The proxy URL
    forwards every other id, so the mock catalog stays when that URL is set.
    """
    if os.environ.get("MINISTACK_BEDROCK_PROXY_URL", "").strip():
        return True
    return not catalog_entries()


def catalog_entries() -> list[tuple]:
    """Foundation-model rows for ids in the local file.

    Shape matches the built-in catalog tuple:
    ``(model_id, name, provider, inputs, outputs, streaming)``.
    """
    rows = []
    for model_id, entry in load_local_models_quietly().items():
        name = str(entry.get("name") or model_id)
        rows.append((model_id, name, "Local", ["TEXT"], ["TEXT"], True))
    return rows


def add_parser(subparsers) -> None:
    """Register ``ministack model set|list|remove``."""
    model = subparsers.add_parser(
        "model",
        help="Register a local model in local-models.json",
    )
    commands = model.add_subparsers(dest="model_command", required=True)

    set_cmd = commands.add_parser("set", help="Add or replace a local model")
    set_cmd.add_argument("model_id", help="Bedrock model id clients will send")
    set_cmd.add_argument(
        "--model",
        dest="upstream",
        help="Name the OpenAI-compatible server expects. Defaults to the Bedrock id.",
    )
    set_cmd.add_argument(
        "--url",
        help="Server base URL without /v1. Defaults to LOCAL_MODELS_URL at request time.",
    )
    set_cmd.add_argument(
        "--name",
        help="Display name in ListFoundationModels. Defaults to the Bedrock id.",
    )
    set_cmd.add_argument(
        "--timeout",
        type=float,
        help=f"Seconds to wait for one reply. Default {_DEFAULT_TIMEOUT_SECONDS:.0f}.",
    )
    set_cmd.add_argument("--file", help="Path of local-models.json")

    list_cmd = commands.add_parser("list", help="Print registered local models")
    list_cmd.add_argument("--file", help="Path of local-models.json")

    remove_cmd = commands.add_parser("remove", help="Remove a local model")
    remove_cmd.add_argument("model_id")
    remove_cmd.add_argument("--file", help="Path of local-models.json")


def execute(args) -> int:
    """Run the parsed ``model`` command. Returns a process exit code."""
    path = resolve_local_models_path(getattr(args, "file", None))
    if args.model_command == "list":
        return _list(path)
    if args.model_command == "set":
        return _set(path, args)
    if args.model_command == "remove":
        return _remove(path, args.model_id)
    return 2


def _set(path: str, args) -> int:
    try:
        models = load_local_models(path)
    except ValueError as exc:
        print(exc)
        return 1
    entry: dict = {}
    if args.upstream:
        entry["model"] = args.upstream
    if args.url:
        entry["url"] = args.url.rstrip("/")
    if args.name:
        entry["name"] = args.name
    if args.timeout is not None:
        if args.timeout <= 0:
            print("Timeout must be greater than zero.")
            return 1
        entry["timeoutSeconds"] = args.timeout
    models[args.model_id] = entry
    write_local_models_file(path, models)
    print(f"Registered {args.model_id} in {path}")
    return 0


def _list(path: str) -> int:
    try:
        models = load_local_models(path)
    except ValueError as exc:
        print(exc)
        return 1
    if not models:
        print(f"No local models in {path}")
        return 0
    for model_id, entry in models.items():
        upstream = entry.get("model") or model_id
        url = entry.get("url") or "(LOCAL_MODELS_URL)"
        print(f"{model_id}\t{upstream}\t{url}")
    return 0


def _remove(path: str, model_id: str) -> int:
    try:
        models = load_local_models(path)
    except ValueError as exc:
        print(exc)
        return 1
    if model_id not in models:
        print(f"No local model {model_id!r} in {path}")
        return 1
    del models[model_id]
    write_local_models_file(path, models)
    print(f"Removed {model_id} from {path}")
    return 0
