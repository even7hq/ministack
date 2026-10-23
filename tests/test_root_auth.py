"""ROOT_AUTH requires a real root signature and still leaves health open."""

import asyncio
import json

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials


def _call(app_mod, method, path, query, body, key, secret, sign):
    """Send one ASGI request, signed when ``sign`` is true."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"sts.us-east-1.amazonaws.com")]
    if sign:
        request = AWSRequest(
            method=method,
            url="https://sts.us-east-1.amazonaws.com" + path,
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        SigV4Auth(Credentials(key, secret), "sts", "us-east-1").add_auth(request)
        # botocore signs Host from the URL and does not always copy it into headers.
        signed = {name.lower(): value for name, value in request.headers.items()}
        signed.setdefault("host", "sts.us-east-1.amazonaws.com")
        headers = [(name.encode(), value.encode()) for name, value in signed.items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers,
    }
    asyncio.run(app_mod.app(scope, receive, send))
    status = sent[0]["status"]
    payload = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, payload


def test_fake_root_signature_is_accepted_until_root_auth(monkeypatch):
    """Credential=test with a fake signature keeps working unless ROOT_AUTH is on."""
    from ministack import app as app_mod

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"Action=GetCallerIdentity&Version=2011-06-15", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [
            (b"host", b"sts.us-east-1.amazonaws.com"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (
                b"authorization",
                b"AWS4-HMAC-SHA256 Credential=test/20240101/us-east-1/sts/aws4_request, "
                b"SignedHeaders=host, Signature=fake",
            ),
        ],
    }
    monkeypatch.setattr(app_mod, "ROOT_AUTH", False)
    asyncio.run(app_mod.app(scope, receive, send))
    assert sent[0]["status"] == 200

    sent.clear()
    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    asyncio.run(app_mod.app(scope, receive, send))
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert sent[0]["status"] in (400, 403)
    assert b":root</Arn>" not in body


def test_root_auth_off_still_accepts_unsigned(monkeypatch):
    """The default stays open: a request with no signature is the root caller."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", False)
    status, payload = _call(app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "", "", False)
    assert status == 200
    assert b":root</Arn>" in payload


def test_root_auth_accepts_the_configured_secret(monkeypatch):
    """A signature made with the server secret is the account root."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "devkey")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "devsecret")
    status, payload = _call(
        app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "devkey", "devsecret", True,
    )
    assert status == 200
    assert b":root</Arn>" in payload


def test_root_auth_with_iam_mode_still_accepts_the_root_secret(monkeypatch):
    """ROOT_AUTH keeps checking the root secret when AUTH is also on."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "AUTH", True)
    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "devkey")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "devsecret")
    status, payload = _call(
        app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "devkey", "devsecret", True,
    )
    assert status == 200
    assert b":root</Arn>" in payload


def test_root_auth_accepts_each_configured_key(monkeypatch):
    """Each ROOT_KEYS pair has its own secret. A crossed pair is refused."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("ROOT_KEYS", "alpha=alpha-secret,beta=beta-secret")
    body = b"Action=GetCallerIdentity&Version=2011-06-15"
    status, payload = _call(app_mod, "POST", "/", b"", body, "alpha", "alpha-secret", True)
    assert status == 200
    assert b":root</Arn>" in payload
    status, payload = _call(app_mod, "POST", "/", b"", body, "beta", "beta-secret", True)
    assert status == 200
    assert b":root</Arn>" in payload
    status, payload = _call(app_mod, "POST", "/", b"", body, "alpha", "beta-secret", True)
    assert status == 403
    assert b"SignatureDoesNotMatch" in payload


def test_root_auth_with_no_keys_rejects_the_default_pair(monkeypatch):
    """An empty key list does not accept test/test."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    status, payload = _call(
        app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "test", "test", True,
    )
    assert status == 403
    assert b":root</Arn>" not in payload


def test_root_keys_parse_pairs_and_reject_a_bad_entry(monkeypatch):
    """The default list is empty, a secret may contain '=', and a bad entry fails."""
    from ministack.core.iam_evaluator import configured_root_keys

    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    monkeypatch.delenv("ROOT_KEYS_FILE", raising=False)
    assert configured_root_keys() == {}
    monkeypatch.setenv("ROOT_KEYS", "dev=abc=def, ci=other")
    assert configured_root_keys() == {"dev": "abc=def", "ci": "other"}
    monkeypatch.setenv("ROOT_KEYS", "noseparator")
    with pytest.raises(ValueError, match="access_key=secret"):
        configured_root_keys()


def test_root_keys_file_loads_and_env_overrides_the_same_id(monkeypatch, tmp_path):
    """The JSON file is the key list. ROOT_KEYS replaces the same id."""
    from ministack.core.iam_evaluator import configured_root_keys

    path = tmp_path / "root-keys.json"
    path.write_text('{"gio": "from-file", "other": "stays"}', encoding="utf-8")
    monkeypatch.setenv("ROOT_KEYS_FILE", str(path))
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    assert configured_root_keys() == {"gio": "from-file", "other": "stays"}

    monkeypatch.setenv("ROOT_KEYS", "gio=from-env")
    assert configured_root_keys() == {"gio": "from-env", "other": "stays"}

    missing = tmp_path / "missing.json"
    monkeypatch.setenv("ROOT_KEYS_FILE", str(missing))
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    assert configured_root_keys() == {}

    path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("ROOT_KEYS_FILE", str(path))
    with pytest.raises(ValueError, match="JSON object"):
        configured_root_keys()


def test_root_auth_accepts_a_key_from_the_file(monkeypatch, tmp_path):
    """A signature made with the secret in ROOT_KEYS_FILE is the account root."""
    from ministack import app as app_mod

    path = tmp_path / "root-keys.json"
    path.write_text('{"gio": "file-secret"}', encoding="utf-8")
    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("ROOT_KEYS_FILE", str(path))
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    status, payload = _call(
        app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "gio", "file-secret", True,
    )
    assert status == 200
    assert b":root</Arn>" in payload


def test_no_root_auth_command_accepts_unsigned_calls(monkeypatch):
    """`--no-root-auth` is what `docker run ... ministack --no-root-auth` executes."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("ROOT_AUTH", "true")
    app_mod.disable_root_auth()
    assert app_mod.ROOT_AUTH is False
    status, payload = _call(app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "", "", False)
    assert status == 200
    assert b":root</Arn>" in payload


def test_root_auth_rejects_a_missing_signature(monkeypatch):
    """A request with no signature is not the root caller."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "devsecret")
    status, payload = _call(app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "", "", False)
    assert status == 403
    assert b"MissingAuthenticationToken" in payload


def test_root_auth_rejects_the_wrong_secret(monkeypatch):
    """A signature made with another secret does not pass."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "devkey")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "devsecret")
    status, payload = _call(
        app_mod, "POST", "/", b"", b"Action=GetCallerIdentity&Version=2011-06-15", "devkey", "wrong-secret", True,
    )
    assert status == 403
    assert b"SignatureDoesNotMatch" in payload


def test_root_auth_leaves_health_unsigned(monkeypatch):
    """The container healthcheck calls health with no credentials."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    status, payload = _call(app_mod, "GET", "/_ministack/health", b"", b"", "", "", False)
    assert status == 200
    assert json.loads(payload)["root_auth"] == "enabled"


def test_root_auth_rejects_unsigned_reset(monkeypatch):
    """Reset is not a public path when root signatures are required."""
    from ministack import app as app_mod

    monkeypatch.setattr(app_mod, "ROOT_AUTH", True)
    status, payload = _call(app_mod, "POST", "/_ministack/reset", b"", b"", "", "", False)
    assert status == 403
    assert b"MissingAuthenticationToken" in payload
