"""Local models are listed by Bedrock and forwarded to their own server."""

import errno
import json
import os
import subprocess
import sys

import pytest


def _run(tmp_path, *args):
    """Run one model command against a file under tmp_path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env.pop("LOCAL_MODELS_FILE", None)
    return subprocess.run(
        [sys.executable, "-m", "ministack", "model", *args, "--file", str(tmp_path / "local-models.json")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_set_list_and_remove(tmp_path):
    """set stores the entry, list prints it, remove drops that id."""
    path = tmp_path / "local-models.json"
    added = _run(
        tmp_path, "set", "qwythos-9b",
        "--model", "qwythos-9b", "--name", "Qwythos 9B", "--timeout", "120",
    )
    assert added.returncode == 0, added.stderr
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {
        "qwythos-9b": {"model": "qwythos-9b", "name": "Qwythos 9B", "timeoutSeconds": 120.0},
    }

    listed = _run(tmp_path, "list")
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == ["qwythos-9b\tqwythos-9b\t(LOCAL_MODELS_URL)"]

    missing = _run(tmp_path, "remove", "nope")
    assert missing.returncode == 1

    removed = _run(tmp_path, "remove", "qwythos-9b")
    assert removed.returncode == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_write_falls_back_when_replace_is_busy(tmp_path, monkeypatch):
    """A single-file Docker mount rejects rename. The write lands in place."""
    from ministack.core.local_models import write_local_models_file

    path = tmp_path / "local-models.json"
    path.write_text("{}\n", encoding="utf-8")

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Resource busy")

    monkeypatch.setattr(os, "replace", busy)
    write_local_models_file(str(path), {"qwythos-9b": {"model": "qwythos-9b"}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"qwythos-9b": {"model": "qwythos-9b"}}


def test_bad_file_is_ignored_on_the_request_path(tmp_path, monkeypatch):
    """A broken registry does not fail ListFoundationModels."""
    path = tmp_path / "local-models.json"
    path.write_text("[", encoding="utf-8")
    monkeypatch.setenv("LOCAL_MODELS_FILE", str(path))

    from ministack.core.local_models import catalog_entries, load_local_models

    with pytest.raises(ValueError):
        load_local_models(str(path))
    assert catalog_entries() == []


def test_local_model_is_listed_and_forwarded(tmp_path, monkeypatch):
    """The id shows up in the catalog and Converse posts the upstream name."""
    path = tmp_path / "local-models.json"
    path.write_text(json.dumps({
        "qwythos-9b": {
            "model": "qwythos",
            "url": "http://models.internal:11434",
            "name": "Qwythos 9B",
            "timeoutSeconds": 90,
        },
    }), encoding="utf-8")
    monkeypatch.setenv("LOCAL_MODELS_FILE", str(path))
    monkeypatch.delenv("MINISTACK_BEDROCK_PROXY_URL", raising=False)

    from ministack.services.bedrock import _find_model, _list_foundation_models

    entry = _find_model("qwythos-9b")
    assert entry[0] == "qwythos-9b"
    assert entry[1] == "Qwythos 9B"
    assert entry[2] == "Local"

    _status, _headers, body = _list_foundation_models({})
    ids = [row["modelId"] for row in json.loads(body)["modelSummaries"]]
    assert "qwythos-9b" in ids
    assert "anthropic.claude-3-5-sonnet-20241022-v2:0" in ids

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _n=-1):
            return json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(
        "ministack.services.bedrock_runtime.urllib.request.urlopen",
        fake_urlopen,
    )
    from ministack.services.bedrock_runtime import (
        _proxy_openai_chat_message,
        _proxy_openai_passthrough,
    )

    message, counts = _proxy_openai_chat_message(
        "qwythos-9b",
        [{"role": "user", "content": [{"text": "hi"}]}],
        None,
        None,
    )
    assert captured["url"] == "http://models.internal:11434/v1/chat/completions"
    assert captured["body"]["model"] == "qwythos"
    assert captured["timeout"] == 90
    assert message["content"] == "hi"
    assert counts == (3, 1)

    text = _proxy_openai_passthrough({"model": "qwythos-9b", "messages": []})
    assert text == "hi"
    assert captured["body"]["model"] == "qwythos"


def test_reasoning_fills_an_empty_answer():
    """A reasoning model that spent the budget thinking still returns that text."""
    from ministack.services.bedrock_runtime import _converse_blocks_from_message

    blocks, stop = _converse_blocks_from_message({
        "role": "assistant",
        "content": "",
        "reasoning": "The reply is pong.",
    })
    assert blocks == [{"text": "The reply is pong."}]
    assert stop == "end_turn"

    final, _ = _converse_blocks_from_message({
        "content": "pong",
        "reasoning": "The reply is pong.",
    })
    assert final == [{"text": "pong"}]


def test_unregistered_model_is_not_forwarded(monkeypatch):
    """Without a file entry and without the proxy URL, the call stays a mock."""
    monkeypatch.delenv("LOCAL_MODELS_FILE", raising=False)
    monkeypatch.delenv("MINISTACK_BEDROCK_PROXY_URL", raising=False)
    monkeypatch.delenv("LOCAL_MODELS_URL", raising=False)

    from ministack.services.bedrock_runtime import _chat_completions_target

    assert _chat_completions_target("anthropic.claude-3-5-haiku-20241022-v1:0") is None
