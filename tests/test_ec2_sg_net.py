from ministack.services import ec2_sg_net


def test_sg_script_allows_configured_tcp_port():
    security_groups = {
        "sg-test": {
            "IpPermissions": [{
                "IpProtocol": "tcp",
                "FromPort": 3306,
                "ToPort": 3306,
                "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                "Ipv6Ranges": [],
                "UserIdGroupPairs": [],
            }],
            "IpPermissionsEgress": [{
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                "Ipv6Ranges": [],
                "UserIdGroupPairs": [],
            }],
        },
    }
    script = ec2_sg_net._iptables_script(["sg-test"], security_groups, {})
    assert "SG_IN" in script
    assert "--dport 3306" in script
    assert "-s 10.0.0.0/8" in script
    assert "SG_IN -j DROP" in script


def test_sg_script_fail_closed_policy():
    security_groups = {
        "sg-empty": {
            "IpPermissions": [],
            "IpPermissionsEgress": [],
        },
    }
    script = ec2_sg_net._iptables_script(["sg-empty"], security_groups, {})
    assert "-P INPUT DROP" in script
    assert "-P OUTPUT DROP" in script
