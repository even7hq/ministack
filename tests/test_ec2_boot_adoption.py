# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
# Copies or substantial portions, including AI-assisted ports or rewrites, must retain this notice (see LICENSE).
"""Boot must not kill running EC2 guests that a persisted record still owns."""

import pytest

from ministack import app
from ministack.core import container_reaper, persistence
from ministack.core.responses import request_scope
from ministack.services import ec2

ACCOUNT = "111111111111"
REGION = "us-east-1"
IID = "i-a2e6340da905adfe6"
AMI = "ami-0bootadopt"


def _persisted_instance(monkeypatch, tmp_path):
    """Save a running, container-backed guest exactly like a shutdown does."""
    monkeypatch.setattr(persistence, "PERSIST_STATE", True)
    monkeypatch.setattr(persistence, "STATE_DIR", str(tmp_path))
    with request_scope(ACCOUNT, REGION):
        ec2._clear_state()
        ec2._instances.set_scoped(ACCOUNT, REGION, IID, {
            "InstanceId": IID,
            "ImageId": AMI,
            "State": {"Code": 16, "Name": "running"},
            "VmManager": "docker",
            "RootDeviceType": "instance-store",
            "PrivateIpAddress": "10.0.0.10",
            "BlockDeviceMappings": [],
        })
        ec2._images.set_scoped(ACCOUNT, REGION, AMI, {
            "ImageId": AMI, "ImageLocation": "debian:bookworm",
        })
        ec2._tags.set_scoped(ACCOUNT, REGION, IID, [
            {"Key": "Name", "Value": "kevin-ai-agent"},
            {"Key": "GuestState", "Value": "on"},
        ])
        snapshot = ec2.get_state()
    persistence.save_state("ec2", snapshot)
    ec2._clear_state()
    monkeypatch.setattr(app, "load_state", persistence.load_state)
    with request_scope(ACCOUNT, REGION):
        return ec2._ec2_container_name(IID)


@pytest.mark.serial
def test_boot_adopts_live_guest_container_and_still_reaps_real_orphans(
        fake_docker, monkeypatch, tmp_path):
    """A boot sweep must adopt a running guest, not delete it as an orphan.

    The 2026-11-27 MiniStack restart called reap_all("at boot any surviving
    container is by definition an orphan"), which removed the containers of two
    instances whose persisted records still said running. The guest files survived
    on their bind mounts, but the records were left pointing at nothing.
    """
    name = _persisted_instance(monkeypatch, tmp_path)
    monkeypatch.setenv("GATEWAY_PORT", "4566")
    monkeypatch.setattr(container_reaper, "_boot_nonce", "run-2")
    monkeypatch.setattr(app, "_reaper_docker_client", lambda: fake_docker)

    with request_scope(ACCOUNT, REGION):
        guest = fake_docker.containers.run(
            "debian:bookworm", name=name,
            labels=container_reaper.own_labels("ec2", instance_id=IID),
        )
        orphan = fake_docker.containers.run(
            "rds", name="rds-from-dead-process",
            labels=container_reaper.own_labels("rds"),
        )
        protected = ec2.boot_protected_container_refs()
        app._stop_docker_containers(True, protected=protected)
        assert guest.name in fake_docker.live(), "boot swept a persisted running guest"
        assert orphan.name not in fake_docker.live(), "boot left a real orphan behind"

        monkeypatch.setattr(ec2, "_get_docker", lambda: fake_docker)
        monkeypatch.setattr(ec2, "_vm_apply_container_ip", lambda *a: None)
        monkeypatch.setattr(ec2, "_probe_shell", lambda c: True)
        monkeypatch.setattr(ec2, "_image_has_entrypoint", lambda c, ref: False)
        app._load_persisted_state()

        inst = ec2._instances[IID]
        assert inst["State"]["Name"] == "running"
        assert inst["_container_id"] == guest.id
        assert inst["_container_name"] == name
        assert guest.name in fake_docker.live()


@pytest.mark.serial
def test_boot_relaunches_missing_guest_with_the_same_bind_mounts(
        fake_docker, monkeypatch, tmp_path):
    """When the container is really gone, boot starts a new one for the record."""
    name = _persisted_instance(monkeypatch, tmp_path)
    guest_root = tmp_path / "guests"
    monkeypatch.setattr(ec2, "EC2_GUEST_STATE_DIR", str(guest_root))
    monkeypatch.setenv("GATEWAY_PORT", "4566")
    monkeypatch.setattr(container_reaper, "_boot_nonce", "run-2")
    monkeypatch.setattr(app, "_reaper_docker_client", lambda: fake_docker)
    with request_scope(ACCOUNT, REGION):
        app._stop_docker_containers(True, protected=ec2.boot_protected_container_refs())
        monkeypatch.setattr(ec2, "_get_docker", lambda: fake_docker)
        monkeypatch.setattr(ec2, "_vm_apply_container_ip", lambda *a: None)
        monkeypatch.setattr(ec2, "_probe_shell", lambda c: True)
        monkeypatch.setattr(ec2, "_image_has_entrypoint", lambda c, ref: False)
        app._load_persisted_state()

        inst = ec2._instances[IID]
        assert inst["State"]["Name"] == "running", "missing guest was reported terminated"
        assert inst["_container_name"] == name
        assert name in fake_docker.live()
        assert (guest_root / "kevin-ai-agent" / "home").is_dir()
        assert (guest_root / "kevin-ai-agent" / "data").is_dir()
