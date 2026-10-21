import asyncio

import pytest

from ministack.core.imds_registry import ImdsRegistry
from ministack.services import imds as imds_mod


@pytest.fixture(autouse=True)
def _clear_registry():
    ImdsRegistry.reset()
    yield
    ImdsRegistry.reset()


def test_imds_returns_registered_instance_id_for_client_ip():
    ImdsRegistry.register("10.0.0.42", "i-abc123def45678901", "sa-east-1a")
    headers = {"x-ministack-imds-client-ip": "10.0.0.42", "x-aws-ec2-metadata-token": "unused"}

    imds_mod._tokens["unused"] = imds_mod.time.time() + 60
    status, _hdrs, body = asyncio.run(
        imds_mod.handle_request(
            "GET",
            "/latest/meta-data/instance-id",
            headers,
            b"",
            {},
        )
    )
    assert status == 200
    assert body.decode() == "i-abc123def45678901"


def test_imds_unknown_client_ip_returns_404_on_link_local_only():
    headers = {
        "x-ministack-imds-client-ip": "10.0.0.99",
        "x-ministack-imds-link-local": "1",
        "x-aws-ec2-metadata-token": "tok",
    }

    imds_mod._tokens["tok"] = imds_mod.time.time() + 60
    status, _hdrs, _body = asyncio.run(
        imds_mod.handle_request(
            "GET",
            "/latest/meta-data/instance-id",
            headers,
            b"",
            {},
        )
    )
    assert status == 404


def test_imds_unknown_client_ip_falls_back_on_gateway_path():
    headers = {"x-ministack-imds-client-ip": "10.0.0.99", "x-aws-ec2-metadata-token": "tok2"}

    imds_mod._tokens["tok2"] = imds_mod.time.time() + 60
    status, _hdrs, body = asyncio.run(
        imds_mod.handle_request(
            "GET",
            "/latest/meta-data/instance-id",
            headers,
            b"",
            {},
        )
    )
    assert status == 200
    assert body.decode().startswith("i-")


def test_imds_placement_az_from_registry():
    ImdsRegistry.register("172.18.0.5", "i-registeredinstance1", "us-east-2b")
    headers = {"x-ministack-imds-client-ip": "172.18.0.5", "x-aws-ec2-metadata-token": "t2"}

    imds_mod._tokens["t2"] = imds_mod.time.time() + 60
    status, _hdrs, body = asyncio.run(
        imds_mod.handle_request(
            "GET",
            "/latest/meta-data/placement/availability-zone",
            headers,
            b"",
            {},
        )
    )
    assert status == 200
    assert body.decode() == "us-east-2b"
