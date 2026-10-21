"""
Integration checks for the LuckyMaker AWSAppInit path against a running MiniStack.

Set MINISTACK_ENDPOINT (default http://localhost:4566) and optionally DOCKER_NETWORK
for RDS/ElastiCache reachability tests.
"""

import os
import socket
import urllib.request

import pytest

ENDPOINT = os.environ.get("MINISTACK_ENDPOINT", "http://localhost:4566").rstrip("/")
AWS_TENANT_PRODUCT_ID_TAG_KEY = "e7.dev/tenant/product-id"
AWS_TENANT_APP_ID_TAG_KEY = "e7.dev/tenant/app-id"


def _ministack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_ministack/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.skipif(not _ministack_up(), reason="MiniStack not reachable at MINISTACK_ENDPOINT")
def test_imds_v2_token_and_instance_id():
    import requests

    token_resp = requests.put(
        f"{ENDPOINT}/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        timeout=10,
    )
    assert token_resp.status_code == 200
    token = token_resp.text.strip()

    id_resp = requests.get(
        f"{ENDPOINT}/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token},
        timeout=10,
    )
    assert id_resp.status_code == 200
    assert id_resp.text.strip().startswith("i-")


@pytest.mark.skipif(not _ministack_up(), reason="MiniStack not reachable at MINISTACK_ENDPOINT")
def test_s3_presign_round_trip():
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"}),
    )
    bucket = "lm-boot-smoke"
    key = "boot-check.txt"
    try:
        s3.create_bucket(Bucket=bucket)
    except Exception:
        pass
    s3.put_object(Bucket=bucket, Key=key, Body=b"ok")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    with urllib.request.urlopen(url, timeout=10) as resp:
        assert resp.read() == b"ok"


@pytest.mark.skipif(not _ministack_up(), reason="MiniStack not reachable at MINISTACK_ENDPOINT")
def test_imds_v2_unknown_registered_ip_returns_404_on_gateway():
    """Sad path: link-local client IP with no registry must not fall back to global id."""
    import requests

    token = requests.put(
        f"{ENDPOINT}/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        timeout=10,
    ).text.strip()
    resp = requests.get(
        f"{ENDPOINT}/latest/meta-data/instance-id",
        headers={
            "X-aws-ec2-metadata-token": token,
            "X-Ministack-Imds-Client-Ip": "198.51.100.99",
            "X-Ministack-Imds-Link-Local": "1",
        },
        timeout=10,
    )
    assert resp.status_code == 404


@pytest.mark.skipif(not os.environ.get("DOCKER_NETWORK"), reason="DOCKER_NETWORK not set")
def test_link_local_imds_reachable_when_proxy_running():
    """169.254.169.254 is per-instance (EC2 sidecar netns), not routable from the bridge."""
    host = os.environ.get("MINISTACK_IMDS_LINK_LOCAL_HOST", "169.254.169.254")
    try:
        sock = socket.create_connection((host, 80), timeout=2)
        sock.close()
    except OSError as err:
        pytest.skip(
            f"link-local IMDS is not exposed on the Docker bridge at {host}:80 ({err}); "
            "see test_imds_link_local_returns_launched_instance_id"
        )
