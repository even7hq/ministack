"""
LuckyMaker AWSAppInit path — unit and integration coverage for MiniStack extensions.

Unit tests use the EC2 fake-Docker harness (no live daemon).
Integration tests need a running MiniStack (MINISTACK_ENDPOINT) and Docker network (DOCKER_NETWORK).
"""

import asyncio
import os
import re
import socket
import time
import uuid as _uuid_mod

import pytest

from ministack.core.imds_registry import ImdsRegistry
from ministack.services import ec2 as ec2mod
from ministack.services import imds as imds_mod

ENDPOINT = os.environ.get("MINISTACK_ENDPOINT", "http://localhost:4566").rstrip("/")

AWS_TENANT_PRODUCT_ID_TAG_KEY = "e7.dev/tenant/product-id"
AWS_TENANT_APP_ID_TAG_KEY = "e7.dev/tenant/app-id"

requires_docker_network = pytest.mark.skipif(
    not os.environ.get("DOCKER_NETWORK"),
    reason="DOCKER_NETWORK not set - skipping LM boot data-plane tests",
)


def _ministack_up() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_ministack/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


requires_live_ministack = pytest.mark.skipif(
    not _ministack_up(),
    reason="MiniStack not reachable at MINISTACK_ENDPOINT",
)


# ---- fake Docker (same pattern as test_ec2.py) ----


class _FakeContainer:
    def __init__(self, name, kwargs, registry, ip="172.30.0.9", network="testnet", shell=True):
        self.id = f"cid-{name}"
        self.name = name
        self.kwargs = kwargs
        self.status = "running"
        self.attrs = {"NetworkSettings": {"Networks": {network: {"IPAddress": ip}}}}
        self.execs = []
        self._registry = registry
        self._shell = shell

    def reload(self):
        pass

    def exec_run(self, cmd, demux=False, **kw):
        self.execs.append(cmd)

        class _R:
            exit_code = 0
            output = b""

        return _R()


def _fake_docker_sequential_ips(ips):
    registry, runs = {}, []
    ip_iter = iter(ips)

    class _FakeImage:
        attrs = {"Config": {"Entrypoint": None}}

    class _Images:
        def get(self, ref):
            return _FakeImage()

    class _Containers:
        def run(self, image, **kwargs):
            ip = next(ip_iter, ips[-1])
            runs.append({"image": image, **kwargs})
            c = _FakeContainer(kwargs["name"], kwargs, registry, ip=ip)
            registry[c.id] = registry[c.name] = c
            return c

        def get(self, ref):
            if ref not in registry:
                from docker import errors

                raise errors.NotFound("absent")
            return registry[ref]

        def list(self, all=False, filters=None):
            return []

    class _Docker:
        def __init__(self):
            self.containers = _Containers()
            self.images = _Images()

        def ping(self):
            return True

    d = _Docker()
    d._registry, d._runs = registry, runs
    return d


@pytest.fixture
def vm_sequential(monkeypatch):
    """Fake Docker that assigns a distinct IP per RunInstances container."""
    fake = _fake_docker_sequential_ips(["172.30.0.2", "172.30.0.3", "172.30.0.4"])
    monkeypatch.setattr(ec2mod, "_get_docker", lambda: fake)
    monkeypatch.setattr(ec2mod, "_get_ministack_network", lambda _c: "testnet")
    created = []
    yield fake, created
    ImdsRegistry.reset()
    for iid in created:
        ec2mod._instances.pop(iid, None)
    for key in list(ec2mod._images.keys()):
        ec2mod._images.pop(key, None)


def _register_image(location="alpine:3.20", name="lm-boot-box"):
    resp = ec2mod._register_image({"Name": [name], "ImageLocation": [location]})
    return re.search(r"<imageId>(ami-[0-9a-f]+)</imageId>", resp[2].decode()).group(1)


def _run_instances(ami, count=1, tag_params=None):
    params = {
        "ImageId": [ami],
        "MinCount": [str(count)],
        "MaxCount": [str(count)],
    }
    if tag_params:
        params.update(tag_params)
    resp = ec2mod._run_instances(params)
    body = resp[2].decode()
    return re.findall(r"<instanceId>(i-[0-9a-f]+)</instanceId>", body), resp


@pytest.fixture(autouse=True)
def _clear_imds_registry():
    ImdsRegistry.reset()
    yield
    ImdsRegistry.reset()


def test_two_containers_get_distinct_imds_instance_ids(vm_sequential):
    fake, created = vm_sequential
    ami = _register_image()
    iids, _ = _run_instances(ami, count=2)
    created.extend(iids)
    assert len(fake._runs) == 2
    assert ImdsRegistry.lookup("172.30.0.2")["instance_id"] == iids[0]
    assert ImdsRegistry.lookup("172.30.0.3")["instance_id"] == iids[1]
    assert iids[0] != iids[1]


def test_imds_placement_az_matches_instance_record(vm_sequential):
    fake, created = vm_sequential
    ami = _register_image()
    iids, _ = _run_instances(ami, count=1)
    created.extend(iids)
    inst = ec2mod._instances[iids[0]]
    az = inst["Placement"]["AvailabilityZone"]
    imds_mod._tokens["tok"] = imds_mod.time.time() + 60
    status, _h, body = asyncio.run(
        imds_mod.handle_request(
            "GET",
            "/latest/meta-data/placement/availability-zone",
            {"x-ministack-imds-client-ip": "172.30.0.2", "x-aws-ec2-metadata-token": "tok"},
            b"",
            {},
        )
    )
    assert status == 200
    assert body.decode() == az


def test_run_instances_persists_lm_tenant_tags(vm_sequential):
    fake, created = vm_sequential
    ami = _register_image()
    product = "prod-lm-test"
    app = "app-lm-test"
    tag_params = {
        "TagSpecification.1.ResourceType": ["instance"],
        "TagSpecification.1.Tag.1.Key": [AWS_TENANT_PRODUCT_ID_TAG_KEY],
        "TagSpecification.1.Tag.1.Value": [product],
        "TagSpecification.1.Tag.2.Key": [AWS_TENANT_APP_ID_TAG_KEY],
        "TagSpecification.1.Tag.2.Value": [app],
    }
    iids, _ = _run_instances(ami, count=1, tag_params=tag_params)
    created.extend(iids)
    tags = ec2mod._tags.get(iids[0], [])
    by_key = {t["Key"]: t["Value"] for t in tags}
    assert by_key[AWS_TENANT_PRODUCT_ID_TAG_KEY] == product
    assert by_key[AWS_TENANT_APP_ID_TAG_KEY] == app


def test_vm_launch_adds_net_admin_for_security_groups(vm_sequential, monkeypatch):
    monkeypatch.setenv("MINISTACK_EC2_SECURITY_GROUPS", "1")
    fake, created = vm_sequential
    ami = _register_image()
    iids, _ = _run_instances(ami, count=1)
    created.extend(iids)
    caps = fake._runs[0].get("cap_add") or []
    assert "NET_ADMIN" in caps


def test_security_group_apply_runs_iptables_in_container(vm_sequential, monkeypatch):
    monkeypatch.setenv("MINISTACK_EC2_SECURITY_GROUPS", "1")
    fake, created = vm_sequential
    ami = _register_image()
    sg_id = ec2mod._DEFAULT_SG_ID
    ec2mod._authorize_sg_ingress({
        "GroupId": [sg_id],
        "IpPermissions.1.IpProtocol": ["tcp"],
        "IpPermissions.1.FromPort": ["443"],
        "IpPermissions.1.ToPort": ["443"],
        "IpPermissions.1.IpRanges.1.CidrIp": ["0.0.0.0/0"],
    })
    iids, _ = _run_instances(ami, count=1)
    created.extend(iids)
    container = fake._registry[fake._runs[0]["name"]]
    assert any("iptables" in str(cmd) for cmd in container.execs)


@requires_live_ministack
@requires_docker_network
def test_describe_tags_for_launched_instance(ec2):
    """Sad path guard: tags from RunInstances must appear in DescribeTags."""
    unique = _uuid_mod.uuid4().hex[:8]
    ami = ec2.register_image(Name=f"lm-tags-{unique}", ImageLocation="alpine:3.20")["ImageId"]
    product = f"product-{unique}"
    try:
        reservation = ec2.run_instances(
            ImageId=ami,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": AWS_TENANT_PRODUCT_ID_TAG_KEY, "Value": product},
                    {"Key": AWS_TENANT_APP_ID_TAG_KEY, "Value": f"app-{unique}"},
                ],
            }],
        )
        instance_id = reservation["Instances"][0]["InstanceId"]
        tags = ec2.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "resource-type", "Values": ["instance"]},
            ],
        )["Tags"]
        keys = {t["Key"]: t["Value"] for t in tags}
        assert keys[AWS_TENANT_PRODUCT_ID_TAG_KEY] == product
    finally:
        ec2.deregister_image(ImageId=ami)


@requires_live_ministack
@requires_docker_network
def test_rds_mysql_endpoint_reachable_from_docker_network(rds):
    db_id = f"lm-boot-{ _uuid_mod.uuid4().hex[:8] }"
    rds.create_db_instance(
        DBInstanceIdentifier=db_id,
        DBInstanceClass="db.t3.micro",
        Engine="mysql",
        MasterUsername="admin",
        MasterUserPassword="secret123",
        AllocatedStorage=20,
    )
    deadline = time.time() + 120
    address = port = None
    while time.time() < deadline:
        instances = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"]
        if instances and instances[0].get("Endpoint"):
            address = instances[0]["Endpoint"]["Address"]
            port = int(instances[0]["Endpoint"]["Port"])
            if address and address not in ("localhost", "127.0.0.1"):
                break
        time.sleep(2)
    assert address, "RDS endpoint address missing"
    assert address not in ("localhost", "127.0.0.1"), f"expected Docker network IP, got {address}"
    while time.time() < deadline:
        try:
            with socket.create_connection((address, port), timeout=3):
                break
        except OSError:
            time.sleep(2)
    else:
        pytest.fail(f"MySQL at {address}:{port} not reachable on DOCKER_NETWORK")


@requires_live_ministack
@requires_docker_network
def test_elasticache_cluster_arn_and_redis_ping(ec):
    cluster_id = f"lm-boot-{ _uuid_mod.uuid4().hex[:8] }"
    ec.create_cache_cluster(
        CacheClusterId=cluster_id,
        Engine="redis",
        CacheNodeType="cache.t3.micro",
        NumCacheNodes=1,
    )
    deadline = time.time() + 120
    cluster = None
    while time.time() < deadline:
        clusters = ec.describe_cache_clusters(CacheClusterId=cluster_id)["CacheClusters"]
        if clusters:
            cluster = clusters[0]
            if cluster.get("CacheClusterStatus") == "available":
                break
        time.sleep(2)
    assert cluster is not None
    assert cluster.get("ARN") or cluster.get("CacheClusterArn")
    node = cluster["CacheNodes"][0]
    host = node["Endpoint"]["Address"]
    port = int(node["Endpoint"]["Port"])
    if host in ("localhost", "redis"):
        pytest.skip(
            f"ElastiCache fell back to sidecar host '{host}' (Docker spawn failed inside MiniStack)"
        )
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                break
        except OSError:
            time.sleep(2)
    else:
        pytest.fail(f"Redis at {host}:{port} not reachable")


@requires_live_ministack
@requires_docker_network
def test_imds_link_local_returns_launched_instance_id(ec2):
    """LM path: IMDSv2 at 169.254.169.254 returns the EC2 instance id for that container."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("docker CLI not available in test environment")

    unique = _uuid_mod.uuid4().hex[:8]
    ami = ec2.register_image(Name=f"lm-imds-{unique}", ImageLocation="alpine:3.20")["ImageId"]
    instance_id = None
    container_name = None
    try:
        reservation = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)
        instance_id = reservation["Instances"][0]["InstanceId"]
        container_name = f"ministack-ec2-000000000000-us-east-1-{instance_id}"
        wait_deadline = time.time() + 90
        while time.time() < wait_deadline:
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
            )
            if inspect.returncode == 0 and inspect.stdout.strip() == "true":
                break
            time.sleep(1)
        else:
            pytest.fail(f"EC2 container {container_name} did not reach running state")

        script = (
            "apk add --no-cache curl >/dev/null && "
            "TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token "
            "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60') && "
            "curl -sf -H \"X-aws-ec2-metadata-token: $TOKEN\" "
            "http://169.254.169.254/latest/meta-data/instance-id"
        )
        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert result.stdout.strip() == instance_id
    finally:
        if instance_id:
            try:
                ec2.terminate_instances(InstanceIds=[instance_id])
            except Exception:
                pass
        ec2.deregister_image(ImageId=ami)


def _wait_ec2_container_running(container_name: str, timeout_sec: float = 90) -> None:
    """
    Block until the MiniStack EC2 Docker container is running.

    @param container_name - Docker container name for the instance
    @param timeout_sec - Maximum wait time in seconds
    @returns Nothing.
    """
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("docker CLI not available in test environment")

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "true":
            return
        time.sleep(1)
    pytest.fail(f"EC2 container {container_name} did not reach running state")


@requires_live_ministack
@requires_docker_network
def test_security_group_tcp_ingress_blocked_until_authorized(ec2):
    """Live path: default SG drops inbound TCP until AuthorizeSecurityGroupIngress."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("docker CLI not available in test environment")

    unique = _uuid_mod.uuid4().hex[:8]
    listen_port = 20000 + (int(unique[:4], 16) % 8000)
    ami = ec2.register_image(Name=f"lm-sg-{unique}", ImageLocation="alpine:3.20")["ImageId"]
    instance_id = None
    try:
        reservation = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)
        instance_id = reservation["Instances"][0]["InstanceId"]
        container_name = f"ministack-ec2-000000000000-us-east-1-{instance_id}"
        _wait_ec2_container_running(container_name)

        deadline = time.time() + 60
        private_ip = None
        group_id = None
        while time.time() < deadline:
            inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
            private_ip = inst.get("PrivateIpAddress")
            groups = inst.get("SecurityGroups") or []
            if groups:
                group_id = groups[0]["GroupId"]
            if private_ip and group_id:
                break
            time.sleep(0.5)
        assert private_ip and group_id

        def _peer_can_connect() -> bool:
            probe = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", os.environ["DOCKER_NETWORK"],
                    "alpine:3.20", "sh", "-c",
                    f"nc -z -w 2 {private_ip} {listen_port}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return probe.returncode == 0

        assert not _peer_can_connect(), "inbound TCP should be blocked before ingress rule"

        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": listen_port,
                "ToPort": listen_port,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }],
        )
        time.sleep(2)

        subprocess.run(
            [
                "docker", "exec", "-d", container_name, "sh", "-c",
                f"nc -lk -p {listen_port}",
            ],
            check=False,
            timeout=10,
        )
        time.sleep(1)
        assert _peer_can_connect(), "inbound TCP should succeed after ingress rule and listener"
    finally:
        if instance_id:
            try:
                ec2.terminate_instances(InstanceIds=[instance_id])
            except Exception:
                pass
        ec2.deregister_image(ImageId=ami)
