# LuckyMaker boot on MiniStack - verification matrix

This document maps what `AWSAppInit` needs at boot to automated tests and known manual gaps.

## AWSAppInit data path (luckymaker)

| Need | MiniStack surface | Automated test |
|------|-------------------|----------------|
| Tenant product/app tags | EC2 `DescribeTags` / `RunInstances` tag specs | `test_describe_tags_for_launched_instance`, `test_run_instances_persists_lm_tenant_tags` |
| Instance identity / AZ | Link-local IMDSv2 (`169.254.169.254`) | `test_imds_link_local_returns_launched_instance_id`, `test_imds_per_instance.py` |
| RDS endpoints | `CreateDBInstance` + Docker MySQL on `DOCKER_NETWORK` | `test_rds_mysql_endpoint_reachable_from_docker_network` |
| ElastiCache ARN + Redis | `CreateCacheCluster` (container IP on network, no host port bind) | `test_elasticache_cluster_arn_and_redis_ping` |
| S3 presigned URLs | S3 API on gateway | `test_s3_presign_round_trip` |
| EC2 security groups (deploy networking) | iptables in instance netns | `test_ec2_sg_net.py` (unit), `test_security_group_tcp_ingress_blocked_until_authorized` (live) |
| Node public IP (`whatsMyIpAddress`) | Not emulated by MiniStack | **Manual / out of scope** - LM uses external HTTP in prod; set `LM_SKIP_AWS` or mock in dev |

## Run the LM boot test suite

Prerequisites: `docker compose up -d --build` in `packages/ministack` (compose sets `DOCKER_NETWORK` and `MINISTACK_IMDS_LISTENER_HOST_SCRIPT`).

Unit only (no live MiniStack):

```bash
pip install '.[test]'
pytest tests/test_imds_per_instance.py tests/test_ec2_sg_net.py tests/test_lm_boot_path.py -q -o addopts=''
```

Full integration (recommended):

```bash
docker run --rm --network ministack_default \
  -e MINISTACK_ENDPOINT=http://ministack:4566 \
  -e DOCKER_NETWORK=ministack_default \
  -v "$PWD:/work" -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro \
  -w /work python:3.12-slim bash -c \
  'pip install -q ".[test]" pytest-xdist && pytest tests/test_lm_boot_path.py tests/test_lm_boot_integration.py tests/test_imds_per_instance.py tests/test_ec2_sg_net.py -o addopts=""'
```

Expected: all tests pass; `test_link_local_imds_reachable_when_proxy_running` skips (link-local is per-instance netns, not the bridge).

## Manual / not automated

| Item | Reason |
|------|--------|
| Docker Swarm stack (`docker-stack.yml`) | Needs a Swarm cluster and per-task IMDS sidecar; compose covers the same APIs |
| `checkip.amazonaws.com` | Not part of MiniStack; LM IP discovery is orthogonal to AWS API emulation |
| Full LuckyMaker `AWSAppInit` inside a replica | E2E belongs to luckymaker integration; MiniStack tests prove the AWS surfaces it calls |

Last verified: run the docker integration command above against a fresh `docker compose up -d --build`.
