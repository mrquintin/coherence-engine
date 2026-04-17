# Kustomize Prod SQS Overlay

Ready-to-apply production overlay pinned to SQS outbox transport.

## Deploy

Required preflight before apply:

```bash
make preflight-secret-manager
```

```bash
kubectl apply -k deploy/k8s/overlays/prod-sqs
```

This overlay includes all `prod` hardening (HPA/PDB/NetworkPolicy/resources) and applies SQS-specific broker tuning.

