"""ministack root-key writes the JSON file and does not start the server."""

import errno
import os
import stat
import subprocess
import sys


def _run(tmp_path, *args):
    """Run one root-key command against a file under tmp_path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env.pop("ROOT_KEYS_FILE", None)
    return subprocess.run(
        [sys.executable, "-m", "ministack", "root-key", *args, "--file", str(tmp_path / "root-keys.json")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_set_list_and_remove(tmp_path):
    """set stores the secret, list prints only the id, remove drops that id."""
    path = tmp_path / "root-keys.json"
    added = _run(tmp_path, "set", "gio", "--secret", "s3cret")
    assert added.returncode == 0
    assert "s3cret" not in added.stdout
    assert path.read_text(encoding="utf-8") == '{\n  "gio": "s3cret"\n}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    other = _run(tmp_path, "set", "other", "--secret", "kept")
    assert other.returncode == 0
    listed = _run(tmp_path, "list")
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == ["gio", "other"]
    assert "s3cret" not in listed.stdout
    assert "kept" not in listed.stdout

    removed = _run(tmp_path, "remove", "gio")
    assert removed.returncode == 0
    assert path.read_text(encoding="utf-8") == '{\n  "other": "kept"\n}\n'


def test_set_rejects_an_empty_secret(tmp_path):
    """An empty secret does not create the file."""
    result = _run(tmp_path, "set", "gio", "--secret", "")
    assert result.returncode == 1
    assert not (tmp_path / "root-keys.json").exists()


def test_write_falls_back_when_replace_is_busy(tmp_path, monkeypatch):
    """A single-file Docker mount rejects rename. The write still lands."""
    from ministack.core.root_keys import write_root_keys_file

    path = tmp_path / "root-keys.json"
    path.write_text("{}\n", encoding="utf-8")

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Resource busy")

    monkeypatch.setattr(os, "replace", busy)
    write_root_keys_file(str(path), {"gio": "s3cret"})
    assert path.read_text(encoding="utf-8") == '{\n  "gio": "s3cret"\n}\n'
    assert not list(tmp_path.glob(".root-keys.*.tmp"))


def test_remove_of_a_missing_key_fails(tmp_path):
    """remove names the file and leaves it untouched when the id is absent."""
    path = tmp_path / "root-keys.json"
    _run(tmp_path, "set", "gio", "--secret", "s3cret")
    before = path.read_text(encoding="utf-8")
    result = _run(tmp_path, "remove", "missing")
    assert result.returncode == 1
    assert "missing" in result.stderr
    assert path.read_text(encoding="utf-8") == before
