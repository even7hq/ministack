# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""
Docker sidecar that binds 169.254.169.254 on the MiniStack network for EC2 IMDS.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger("ministack.imds_linklocal")

LINK_LOCAL = "169.254.169.254"
CONTAINER_NAME = "ministack-imds-linklocal"
LISTENER_IMAGE = os.environ.get("MINISTACK_IMDS_LISTENER_IMAGE", "python:3.12-alpine")
_ENABLED = os.environ.get("MINISTACK_IMDS_LINK_LOCAL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def _listener_script_path() -> str:
    """
    Absolute path to the listener script for a Docker bind mount.

    When MiniStack runs inside a container with a mounted Docker socket, volume
    sources must exist on the **host**. Set MINISTACK_IMDS_LISTENER_HOST_SCRIPT
    to the host path (docker-compose can use ${PWD}/scripts/...).
    """
    host_script = os.environ.get("MINISTACK_IMDS_LISTENER_HOST_SCRIPT", "").strip()
    if host_script:
        # Path is on the Docker daemon host (compose ${PWD}/...); not visible inside MiniStack.
        return host_script

    candidates = [
        "/opt/ministack/scripts/ImdsLinkLocalListener.py",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "ImdsLinkLocalListener.py")
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[-1]


def _gateway_url_for_network(client, network: str, gateway_port: str) -> str:
    """
    Build the gateway URL reachable from a container on ``network``.

    @param client - Docker SDK client
    @param network - Docker network name
    @param gateway_port - MiniStack HTTP port
    @returns Base URL without trailing slash
    """
    explicit = os.environ.get("MINISTACK_IMDS_GATEWAY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    hostname = os.environ.get("HOSTNAME", "")
    if hostname:
        try:
            self_container = client.containers.get(hostname)
            nets = self_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            if network in nets and nets[network].get("IPAddress"):
                ip = nets[network]["IPAddress"]
                return f"http://{ip}:{gateway_port}"
        except Exception:
            pass

    host = os.environ.get("MINISTACK_HOST", "host.docker.internal")
    return f"http://{host}:{gateway_port}"


def _listener_startup_shell() -> str:
    """
    Shell preamble: bind link-local on eth0 and run the Python listener.

    @returns Shell command suffix after package install
    """
    return (
        f"apk add --no-cache iproute2 >/dev/null 2>&1; "
        f"ip addr add {LINK_LOCAL}/32 dev eth0 2>/dev/null || true; "
        f"exec python3 /listener.py"
    )


def _imds_sidecar_run_kwargs(
    name: str,
    script_path: str,
    gateway_url: str,
    *,
    network: str | None = None,
    network_mode: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    """
    Docker SDK kwargs shared by the network proxy and per-instance EC2 sidecars.

    @param name - Container name
    @param script_path - Host path to ImdsLinkLocalListener.py for bind mount
    @param gateway_url - MiniStack gateway base URL
    @param network - Bridge network name (mutually exclusive with network_mode)
    @param network_mode - e.g. container:<ec2-name> for per-instance IMDS
    @param labels - Extra Docker labels
    @returns kwargs for containers.run
    """
    run_kwargs = {
        "image": LISTENER_IMAGE,
        "name": name,
        "detach": True,
        "cap_add": ["NET_ADMIN"],
        "labels": {"ministack": "imds-linklocal", **(labels or {})},
        "volumes": {script_path: {"bind": "/listener.py", "mode": "ro"}},
        "environment": {
            "MINISTACK_IMDS_GATEWAY_URL": gateway_url,
            "MINISTACK_IMDS_BIND": "0.0.0.0",
            "MINISTACK_IMDS_PORT": "80",
        },
        "command": ["sh", "-c", _listener_startup_shell()],
    }
    if network_mode:
        run_kwargs["network_mode"] = network_mode
    elif network:
        run_kwargs["network"] = network
    return run_kwargs


def instance_imds_sidecar_name(ec2_container_name: str) -> str:
    """
    Docker name for the IMDS sidecar sharing an EC2 instance network namespace.

    @param ec2_container_name - EC2 workload container name
    @returns Sidecar container name
    """
    return f"{ec2_container_name}-imds"


def start_instance_imds_sidecar(client, ec2_container, gateway_port: str, docker_network: str | None) -> None:
    """
    Start IMDS at 169.254.169.254 inside an EC2 instance's network namespace.

    @param client - Docker SDK client
    @param ec2_container - Running EC2 instance container
    @param gateway_port - MiniStack HTTP port
    @param docker_network - Docker network MiniStack uses for gateway discovery
    @returns Nothing.
    """
    if not _ENABLED:
        return

    host_script = os.environ.get("MINISTACK_IMDS_LISTENER_HOST_SCRIPT", "").strip()
    script_path = _listener_script_path()
    if not host_script and not os.path.isfile(script_path):
        logger.warning("EC2 IMDS sidecar: listener script missing at %s", script_path)
        return

    gateway_url = _gateway_url_for_network(client, docker_network or "", gateway_port)
    sidecar_name = instance_imds_sidecar_name(ec2_container.name)

    try:
        stale = client.containers.get(sidecar_name)
        stale.remove(force=True)
    except Exception:
        pass

    try:
        client.containers.run(
            **_imds_sidecar_run_kwargs(
                sidecar_name,
                script_path,
                gateway_url,
                network_mode=f"container:{ec2_container.name}",
                labels={"instance_id": ec2_container.labels.get("instance_id", "")},
            )
        )
        logger.info(
            "EC2 IMDS sidecar %s for %s (gateway %s)",
            sidecar_name,
            ec2_container.name,
            gateway_url,
        )
    except Exception as err:
        logger.warning("EC2 IMDS sidecar failed for %s: %s", ec2_container.name, err)


def stop_instance_imds_sidecar(client, ec2_container_name: str) -> None:
    """
    Remove the per-instance IMDS sidecar for an EC2 container.

    @param client - Docker SDK client
    @param ec2_container_name - EC2 workload container name
    @returns Nothing.
    """
    try:
        container = client.containers.get(instance_imds_sidecar_name(ec2_container_name))
        container.remove(force=True)
    except Exception:
        pass


def start_link_local_imds(docker_network: str | None, gateway_port: str) -> None:
    """
    Start or replace the link-local IMDS proxy container.

    @param docker_network - Docker network name instances use
    @param gateway_port - MiniStack gateway port
    @returns Nothing.
    """
    if not _ENABLED or not docker_network:
        return

    try:
        import docker
    except ImportError:
        logger.debug("IMDS link-local: docker SDK not available")
        return

    try:
        client = docker.from_env(timeout=float(os.environ.get("MINISTACK_DOCKER_TIMEOUT", "10")))
        client.ping()
    except Exception as err:
        logger.debug("IMDS link-local: no Docker daemon: %s", err)
        return

    script_path = _listener_script_path()
    host_script = os.environ.get("MINISTACK_IMDS_LISTENER_HOST_SCRIPT", "").strip()
    if not host_script and not os.path.isfile(script_path):
        logger.warning("IMDS link-local: listener script missing at %s", script_path)
        return

    gateway_url = _gateway_url_for_network(client, docker_network, gateway_port)

    try:
        existing = client.containers.get(CONTAINER_NAME)
        existing.remove(force=True)
    except Exception:
        pass

    try:
        client.containers.run(
            **_imds_sidecar_run_kwargs(CONTAINER_NAME, script_path, gateway_url, network=docker_network)
        )
        logger.info(
            "IMDS link-local: listening on %s in network %s (gateway %s)",
            LINK_LOCAL,
            docker_network,
            gateway_url,
        )
    except Exception as err:
        logger.warning("IMDS link-local: failed to start proxy container: %s", err)


def stop_link_local_imds() -> None:
    """Remove the link-local IMDS proxy container."""
    try:
        import docker
    except ImportError:
        return

    try:
        client = docker.from_env(timeout=float(os.environ.get("MINISTACK_DOCKER_TIMEOUT", "10")))
        container = client.containers.get(CONTAINER_NAME)
        container.remove(force=True)
    except Exception:
        pass
