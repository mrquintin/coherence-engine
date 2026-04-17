# Kustomize Prod Overlay

This overlay applies production-level overrides on top of `deploy/k8s`:

- stronger resource requests/limits
- stricter HPA targets
- stricter PDB minimum availability
- stricter NetworkPolicy rules
- production namespace (`coherence-fund-prod`)

## Apply (default broker: Kafka)

Required preflight before apply:

```bash
make preflight-secret-manager
```

```bash
kubectl apply -k deploy/k8s/overlays/prod
```

## Broker-Specific Tuned Defaults

If you want zero-edit apply commands, use:

- `deploy/k8s/overlays/prod-redis`
- `deploy/k8s/overlays/prod-sqs`

This `prod` overlay remains the customizable base.

Additional broker patches are available under `brokers/`:

- `brokers/kafka.yaml`
- `brokers/redis.yaml`
- `brokers/sqs.yaml`

To switch broker defaults, add one of these files to the `patches` list in `kustomization.yaml`.

Example (Redis):

```yaml
patches:
  - path: patch-config.yaml
  - path: patch-secret.yaml
  - path: patch-deployments.yaml
  - path: patch-hpa.yaml
  - path: patch-pdb.yaml
  - path: patch-networkpolicy.yaml
  - path: brokers/redis.yaml
```

## Notes

- Update namespace labels used in `patch-networkpolicy.yaml` (`ingress-nginx`, `database`, `redis`, `kafka`) to match your cluster.
- Replace all placeholder secrets before deployment.

