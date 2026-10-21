# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""
Apply EC2 security group rules inside instance containers with iptables.
"""

from __future__ import annotations

import logging
import os
import shlex

logger = logging.getLogger("ministack.ec2_sg_net")

_ENABLED = os.environ.get("MINISTACK_EC2_SECURITY_GROUPS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def _iptables_script(sg_ids: list[str], security_groups: dict, peer_ips_by_sg: dict[str, list[str]]) -> str:
    """
    Build a shell script that configures SG_IN / SG_OUT inside a netns.

    @param sg_ids - Security group ids attached to the instance
    @param security_groups - EC2 security group store
    @param peer_ips_by_sg - Resolved member IPs for UserIdGroupPairs references
    @returns Shell script body
    """
    lines = [
        "set -e",
        "iptables -w 5 -F SG_IN 2>/dev/null || true",
        "iptables -w 5 -F SG_OUT 2>/dev/null || true",
        "iptables -w 5 -X SG_IN 2>/dev/null || true",
        "iptables -w 5 -X SG_OUT 2>/dev/null || true",
        "iptables -w 5 -N SG_IN",
        "iptables -w 5 -N SG_OUT",
        "iptables -w 5 -F INPUT",
        "iptables -w 5 -F OUTPUT",
        "iptables -w 5 -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "iptables -w 5 -A INPUT -i lo -j ACCEPT",
        "iptables -w 5 -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "iptables -w 5 -A OUTPUT -o lo -j ACCEPT",
        "iptables -w 5 -A OUTPUT -p udp --dport 53 -j ACCEPT",
        "iptables -w 5 -A OUTPUT -p tcp --dport 53 -j ACCEPT",
    ]

    for sg_id in sg_ids:
        sg = security_groups.get(sg_id)
        if not sg:
            continue

        for rule in sg.get("IpPermissions", []):
            lines.extend(_ingress_rule_lines(rule, peer_ips_by_sg))

        for rule in sg.get("IpPermissionsEgress", []):
            lines.extend(_egress_rule_lines(rule, peer_ips_by_sg))

    lines.extend([
        "iptables -w 5 -A INPUT -j SG_IN",
        "iptables -w 5 -A OUTPUT -j SG_OUT",
        "iptables -w 5 -A SG_IN -j DROP",
        "iptables -w 5 -A SG_OUT -j DROP",
        "iptables -w 5 -P INPUT DROP",
        "iptables -w 5 -P OUTPUT DROP",
    ])
    return "\n".join(lines) + "\n"


def _proto_flag(proto: str) -> str:
    if proto in ("-1", "all"):
        return ""
    return f"-p {shlex.quote(proto)}"


def _port_match(rule: dict, direction: str) -> str:
    proto = rule.get("IpProtocol", "-1")
    if proto in ("-1", "all"):
        return _proto_flag(proto)

    from_port = rule.get("FromPort")
    to_port = rule.get("ToPort")
    if from_port is None and to_port is None:
        return _proto_flag(proto)

    from_port = int(from_port)
    to_port = int(to_port if to_port is not None else from_port)
    flag = _proto_flag(proto)
    if proto in ("tcp", "udp"):
        if from_port == to_port:
            port_flag = "--dport" if direction == "ingress" else "--dport"
            return f"{flag} {port_flag} {from_port}".strip()
        multi = ",".join(str(port) for port in range(from_port, to_port + 1))
        port_flag = "--dport" if direction == "ingress" else "--dport"
        return f"{flag} -m multiport {port_flag} {multi}".strip()

    return flag


def _cidr_allow_lines(chain: str, rule: dict, direction: str, cidrs: list[str]) -> list[str]:
    match = _port_match(rule, direction)
    lines = []
    for cidr in cidrs:
        if not cidr:
            continue
        if direction == "ingress":
            lines.append(f"iptables -w 5 -A {chain} -s {shlex.quote(cidr)} {match} -j ACCEPT".strip())
        else:
            lines.append(f"iptables -w 5 -A {chain} -d {shlex.quote(cidr)} {match} -j ACCEPT".strip())
    return lines


def _ingress_rule_lines(rule: dict, peer_ips_by_sg: dict[str, list[str]]) -> list[str]:
    cidrs = [entry.get("CidrIp", "") for entry in rule.get("IpRanges", [])]
    for pair in rule.get("UserIdGroupPairs", []):
        ref = pair.get("GroupId")
        if ref and ref in peer_ips_by_sg:
            for ip in peer_ips_by_sg[ref]:
                cidrs.append(f"{ip}/32")
    if not cidrs and rule.get("IpProtocol") in ("-1", "all"):
        return ["iptables -w 5 -A SG_IN -j ACCEPT"]
    return _cidr_allow_lines("SG_IN", rule, "ingress", cidrs)


def _egress_rule_lines(rule: dict, peer_ips_by_sg: dict[str, list[str]]) -> list[str]:
    cidrs = [entry.get("CidrIp", "") for entry in rule.get("IpRanges", [])]
    for pair in rule.get("UserIdGroupPairs", []):
        ref = pair.get("GroupId")
        if ref and ref in peer_ips_by_sg:
            for ip in peer_ips_by_sg[ref]:
                cidrs.append(f"{ip}/32")
    if not cidrs and rule.get("IpProtocol") in ("-1", "all"):
        return ["iptables -w 5 -A SG_OUT -j ACCEPT"]
    return _cidr_allow_lines("SG_OUT", rule, "egress", cidrs)


def _peer_ips_for_groups(security_groups: dict, instances: dict, sg_ids: set[str]) -> dict[str, list[str]]:
    """
    Resolve referenced security groups to member container IPs.

    @param security_groups - EC2 SG store
    @param instances - EC2 instance records
    @param sg_ids - Group ids referenced in rules
    @returns Map of group id to private IPs
    """
    result: dict[str, list[str]] = {gid: [] for gid in sg_ids}
    values = instances.all_values() if hasattr(instances, "all_values") else instances.values()
    for inst in values:
        ip = inst.get("PrivateIpAddress")
        if not ip:
            continue
        for sg in inst.get("SecurityGroups", []):
            gid = sg.get("GroupId")
            if gid in result:
                result[gid].append(ip)
    return result


def apply_instance_security_groups(
    container,
    inst: dict,
    security_groups: dict,
    instances: dict,
) -> bool:
    """
    Load iptables rules for an instance container. Fail-closed on error.

    @param container - Docker container object
    @param inst - EC2 instance record
    @param security_groups - EC2 security group store
    @param instances - All EC2 instances (for SG references)
    @returns True when rules were applied
    """
    if not _ENABLED:
        return True

    sg_ids = [sg.get("GroupId") for sg in inst.get("SecurityGroups", []) if sg.get("GroupId")]
    if not sg_ids:
        return True

    referenced = set(sg_ids)
    for sg_id in sg_ids:
        sg = security_groups.get(sg_id)
        if not sg:
            continue
        for rule in sg.get("IpPermissions", []) + sg.get("IpPermissionsEgress", []):
            for pair in rule.get("UserIdGroupPairs", []):
                ref = pair.get("GroupId")
                if ref:
                    referenced.add(ref)

    peer_ips = _peer_ips_for_groups(security_groups, instances, referenced)
    script = _iptables_script(sg_ids, security_groups, peer_ips)

    try:
        result = container.exec_run(["/bin/sh", "-c", script], privileged=True)
        exit_code = getattr(result, "exit_code", 1)
        if exit_code != 0:
            output = getattr(result, "output", b"") or b""
            logger.warning(
                "EC2 SG: iptables apply failed for %s (exit %s): %s",
                inst.get("InstanceId"),
                exit_code,
                output.decode("utf-8", errors="replace")[:500],
            )
            _drop_all_traffic(container)
            return False
        return True
    except Exception as err:
        logger.warning("EC2 SG: could not apply rules for %s: %s", inst.get("InstanceId"), err)
        _drop_all_traffic(container)
        return False


def _drop_all_traffic(container) -> None:
    """Fail-closed: block all traffic when rule application fails."""
    script = (
        "iptables -w 5 -F; iptables -w 5 -P INPUT DROP; iptables -w 5 -P OUTPUT DROP; "
        "iptables -w 5 -A INPUT -i lo -j ACCEPT; iptables -w 5 -A OUTPUT -o lo -j ACCEPT"
    )
    try:
        container.exec_run(["/bin/sh", "-c", script], privileged=True)
    except Exception:
        pass


def refresh_security_group(
    sg_id: str,
    security_groups: dict,
    instances: dict,
    get_container_for_instance,
) -> None:
    """
    Re-apply iptables on every running instance that uses ``sg_id``.

    @param sg_id - Security group that changed
    @param security_groups - EC2 security group store
    @param instances - EC2 instance records
    @param get_container_for_instance - Callable(inst) -> container or None
    @returns Nothing.
    """
    if not _ENABLED:
        return

    values = instances.all_values() if hasattr(instances, "all_values") else instances.values()
    for inst in values:
        if inst.get("State", {}).get("Name") != "running":
            continue
        if inst.get("VmManager") != "docker":
            continue
        group_ids = {sg.get("GroupId") for sg in inst.get("SecurityGroups", [])}
        if sg_id not in group_ids:
            continue
        container = get_container_for_instance(inst)
        if container is None:
            continue
        apply_instance_security_groups(container, inst, security_groups, instances)
