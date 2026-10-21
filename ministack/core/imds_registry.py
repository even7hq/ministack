# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""
Maps container private IPs on the MiniStack Docker network to EC2 instance ids
for link-local IMDS (169.254.169.254).
"""

from __future__ import annotations

import threading
from typing import Optional

_lock = threading.Lock()
_by_ip: dict[str, dict[str, str]] = {}


class ImdsRegistry:
    """Thread-safe registry from Docker bridge IP to instance metadata."""

    @staticmethod
    def register(client_ip: str, instance_id: str, availability_zone: str) -> None:
        """
        Record which instance id and AZ answer for a container source IP.

        @param client_ip - IPv4 of the caller on the MiniStack Docker network
        @param instance_id - EC2 instance id (i-...)
        @param availability_zone - Placement AZ (e.g. sa-east-1a)
        @returns Nothing.
        """
        if not client_ip or not instance_id:
            return

        with _lock:
            _by_ip[client_ip] = {
                "instance_id": instance_id,
                "availability_zone": availability_zone,
            }

    @staticmethod
    def unregister(client_ip: str) -> None:
        """
        Remove a container IP from the registry.

        @param client_ip - IPv4 previously registered
        @returns Nothing.
        """
        if not client_ip:
            return

        with _lock:
            _by_ip.pop(client_ip, None)

    @staticmethod
    def lookup(client_ip: str) -> Optional[dict[str, str]]:
        """
        Resolve instance metadata for a link-local IMDS client.

        @param client_ip - Source IP of the HTTP request to 169.254.169.254
        @returns Instance id and AZ, or None when unknown
        """
        if not client_ip:
            return None

        with _lock:
            entry = _by_ip.get(client_ip)

        if entry is None:
            return None

        return dict(entry)

    @staticmethod
    def reset() -> None:
        """Clear all registrations (tests / reset)."""
        with _lock:
            _by_ip.clear()
