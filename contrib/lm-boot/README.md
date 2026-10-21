# LuckyMaker boot on MiniStack

This folder holds a reference Docker Swarm stack for running LuckyMaker replicas against MiniStack with the same paths `AWSAppInit` uses in production.

## Git remotes

Clone upstream and point `origin` at Even7:

```bash
git clone https://github.com/ministackorg/ministack.git packages/ministack
cd packages/ministack
git remote rename origin upstream
git remote add origin https://github.com/even7hq/ministack.git
```

## Environment

| Variable | Purpose |
|----------|---------|
| `DOCKER_NETWORK` | Shared bridge/overlay for RDS, Redis, EC2 instances, and link-local IMDS |
| `MINISTACK_IMDS_LISTENER_HOST_SCRIPT` | Host path to `scripts/ImdsLinkLocalListener.py` when MiniStack runs in Docker with a mounted socket (`${PWD}/scripts/...` in compose) |
| `AWS_ENDPOINT_URL` | EC2, RDS, ElastiCache, Secrets Manager SDK clients |
| `S3_ENDPOINT_URL` | S3 and presigned URLs (`S3Client.ts`) |

Link-local IMDS: each `RunInstances` container gets an IMDS sidecar in its network namespace (`169.254.169.254`). The optional `ministack-imds-linklocal` network proxy is legacy; Swarm still needs a per-task sidecar as below.

## Swarm sidecar

Swarm overlay networks do not deliver the bridge `169.254.169.254` alias. Run an IMDS sidecar in the **same network namespace** as each app task (`network_mode: service:luckymaker`) with `NET_ADMIN`, using the same `scripts/ImdsLinkLocalListener.py` image pattern as `ministack-imds-linklocal`.

## Verification

See [VERIFICATION.md](./VERIFICATION.md) for the AWSAppInit-to-test matrix and the integration command (Docker socket + `docker` CLI required for link-local IMDS and security group live tests).
