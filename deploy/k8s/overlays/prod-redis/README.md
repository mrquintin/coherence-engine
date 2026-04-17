# Kustomize Prod Redis Overlay

Ready-to-apply production overlay pinned to Redis outbox transport.

## Deploy

Required preflight before apply:

```bash
make preflight-secret-manager
```

```bash
kubectl apply -k deploy/k8s/overlays/prod-redis
```

This overlay includes all `prod` hardening (HPA/PDB/NetworkPolicy/resources) and applies Redis-specific broker tuning.

