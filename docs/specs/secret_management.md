# Secret management — manifest + pluggable backends

> Wave 8, prompt 27. Companion to ``docs/specs/auth_jwt.md`` (prompt 25)
> and ``docs/specs/db_pooling_and_retries.md`` (prompt 24). Supersedes
> the ad-hoc ``os.environ.get("…")`` pattern previously sprinkled
> across the fund backend.

## Why this exists

Two long-standing gaps motivated this refactor:

1. **Discoverability.** Operators couldn't enumerate "every secret the
   app reads at runtime" without grepping. Onboarding a new
   environment was a discovery exercise.
2. **Pluggability.** Different deployment targets prefer different
   stores: Doppler for some staging environments, HashiCorp Vault for
   on-prem, Supabase Vault when we already have admin access, plain
   environment variables for tests and local dev. The previous
   "managed secret store" abstraction (``ManagedSecretStore``) only
   served the bootstrap-admin token; everything else read directly
   from ``os.environ``.

The runtime resolver consolidates secret reads behind a single
:class:`SecretManager` and a declarative manifest, with three concrete
goals:

* **Boot gate.** ``ENV=production`` deployments must not start with a
  ``prod_required`` secret unresolved.
* **Audit log.** Every resolved secret records *which backend* served
  it (never the value), so unexpected fallbacks (e.g. a Doppler outage
  silently routing to env) are visible in logs.
* **Defense in depth.** Printing a secret value via the CLI requires
  three concurrent acknowledgements: ``--allow-unsafe-print``,
  ``CONFIRM_PRINT_SECRET=YES``, and a non-production env tag.

## Components

| File | Role |
| --- | --- |
| ``data/governed/secret_manifest.yaml`` | Inventory of every named secret (one source of truth). |
| ``server/fund/services/secret_manifest.py`` | Manifest schema, ``ManifestEntry`` / ``ManifestReport``, ``MissingRequiredSecret``. |
| ``server/fund/services/secret_backends.py`` | ``SecretBackend`` Protocol + ``EnvBackend``, ``DopplerBackend``, ``HashicorpVaultBackend``, ``SupabaseVaultBackend``. |
| ``server/fund/services/secret_manager.py`` | ``SecretManager`` resolver (chain over backends, manifest verification, in-memory cache). Also retains the legacy ``ManagedSecretStore`` ABC and the ``get_secret_manager()`` factory for the bootstrap-admin path. |
| ``cli.py`` | ``secrets manifest`` and ``secrets resolve`` verbs. |
| ``server/fund/app.py`` | Boot gate — calls ``SecretManager.from_env().verify_manifest(env)`` during ``@app.on_event("startup")``. |

## The manifest

``data/governed/secret_manifest.yaml`` declares every named secret:

```yaml
schema_version: secret-manifest-v1
secrets:
  - name: SUPABASE_DB_POOLER_URL
    category: db
    policy: prod_required
    owner: platform
  - name: PERSONA_API_KEY
    category: kyc
    policy: prod_optional
    owner: compliance
```

Recognized fields:

* ``name`` (required) — the env var / backend key.
* ``category`` (required) — coarse grouping for human review
  (``db`` | ``auth`` | ``kyc`` | ``bootstrap`` | ``ops``).
* ``policy`` (required) — one of:
    * ``prod_required`` — must resolve when env is ``production``;
      missing values **abort startup**.
    * ``prod_optional`` — looked up if present; absence is logged.
    * ``dev_optional`` — never required (test/dev convenience knob).
* ``owner`` (optional) — team that owns the secret.
* ``description`` (optional) — free-form context.

### Adding a new secret

1. Add an entry to ``secret_manifest.yaml``. Pick the *strictest*
   policy that matches reality — if the secret is only consumed
   when a feature flag is on, prefer ``prod_optional`` and let the
   feature wrapper raise on its own.
2. Read the value via ``get_secret_resolver().get(name)`` in
   application code; do NOT call ``os.environ.get(name)`` for any
   secret listed in the manifest.
3. Add it to ``.env.example`` with a placeholder value and a
   one-line comment.
4. Run ``python -m coherence_engine secrets manifest --env production``
   in your local checkout to confirm the manifest still parses and
   the new entry appears.

### Removing / renaming a secret

* Delete the manifest entry **and** every code reference in the same
  PR. Leaving an unused entry will not crash anything but pollutes
  operator dashboards.
* Renaming: add the new entry first, ship the dual-read code (new
  name preferred, legacy as fallback), then remove the old entry in
  a follow-up.

## Backends

Each backend implements the ``SecretBackend`` Protocol:

```python
class SecretBackend(Protocol):
    name: str
    def get(self, name: str) -> Optional[str]: ...
    def health(self) -> bool: ...
```

* ``EnvBackend`` — reads ``os.environ``. Empty strings are treated as
  missing (secrets are never legitimately ``""``). Always healthy.
* ``DopplerBackend`` — calls ``GET https://api.doppler.com/v3/configs/config/secret?name=…``
  with ``Authorization: Bearer $DOPPLER_TOKEN``. Optional
  ``DOPPLER_PROJECT`` / ``DOPPLER_CONFIG`` for service-token scope.
  Per-name 60s in-memory cache.
* ``HashicorpVaultBackend`` — KV v2 read against
  ``$VAULT_ADDR/v1/$VAULT_KV_MOUNT/data/$VAULT_KV_PATH`` with
  ``X-Vault-Token: $VAULT_TOKEN``. Caches the bundle for the lease
  duration (capped at 5 minutes).
* ``SupabaseVaultBackend`` — calls ``client.rpc('get_secret', {…}).execute()``
  via a service-role Supabase client. Useful when the app already
  has Supabase admin access. Requires a server-side
  ``vault.get_secret(name)`` RPC.

The active primary backend is selected by ``SECRETS_BACKEND``
(``env`` | ``doppler`` | ``vault`` | ``supabase_vault``). The resolver
always falls back to ``EnvBackend`` if the primary cannot resolve a
name. A backend that fails to construct (e.g. missing ``DOPPLER_TOKEN``)
degrades to env-only with a warning — startup itself is the manifest
gate, not backend-availability.

## Resolution + caching semantics

The resolver caches resolved values in-memory for the process
lifetime:

* **No on-disk cache.** Restart is the rotation primitive.
* **No TTL.** A rotated secret is picked up only on next process
  start. This is intentional: long-running workers do not silently
  flip identity mid-flight.
* **Order.** The active primary backend is consulted first; on
  ``None``, the resolver falls through to ``EnvBackend``. Each
  resolution emits an ``INFO`` log line ``secrets: resolved name=X
  backend=Y`` — value never logged.

## Boot gate

In ``server/fund/app.py`` the FastAPI startup hook calls
``SecretManager.from_env().verify_manifest(env)`` after the legacy
secret-manager probe:

* If ``env=="production"`` and any ``prod_required`` entry is
  unresolved, ``MissingRequiredSecret`` propagates and uvicorn exits
  with a non-zero code.
* Other environments produce a status report; missing required
  entries are logged at ``WARNING`` but do not abort.

This is checked once per process at boot. There is no periodic
re-check — pages should come from observability on missing-secret
log lines, not from a runtime probe.

## CLI

```
python -m coherence_engine secrets manifest [--env <env>] [--json]
python -m coherence_engine secrets resolve --name <NAME> --allow-unsafe-print
```

* ``secrets manifest`` prints every entry along with ``status``
  (``resolved`` / ``missing``) and the resolving backend. **Values
  never appear in this output.** Use ``--json`` to machine-parse.

* ``secrets resolve`` is the diagnostic-only "give me one value"
  escape hatch. It refuses unless ALL THREE conditions hold:
  1. the ``--allow-unsafe-print`` CLI flag is passed,
  2. the ``CONFIRM_PRINT_SECRET`` env var equals exactly ``YES``,
  3. the runtime env (``COHERENCE_FUND_ENV`` / ``APP_ENV``) is **not** ``production``.

  All three must be set to print the value to stdout. This is
  defense in depth — operators who need a value in production should
  fetch it directly from the backing store with operator credentials,
  not via the application CLI.

## Rotation playbook

Per category:

| Category | Cadence | Procedure |
| --- | --- | --- |
| ``db`` (Supabase URLs) | Quarterly or after any compromise. | Rotate via Supabase dashboard → update ``SUPABASE_DB_POOLER_URL`` / ``SUPABASE_DB_URL`` in primary backend → trigger rolling restart. |
| ``auth`` (JWT / JWKS) | Annually; immediate on compromise. | Rotate Supabase project signing keys → JWKS cache TTL elapses (3600s default) → no restart needed for ``SUPABASE_JWKS_URL`` because URL is stable. ``SUPABASE_SERVICE_ROLE_KEY`` rotation requires restart. |
| ``kyc`` (Persona / Onfido) | Per provider policy (typically 90 days). | Provider dashboard rotates → new value pushed to backend → restart → verify webhook signing still passes via test event. |
| ``bootstrap`` | Never on a schedule; on operator-departure events. | New token → ``put_secret`` to managed store → restart. ``COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF`` itself is a path, not a secret. |
| ``ops`` | Annually. | New HMAC key → push to backend → rolling restart → confirm receipt verifier accepts both old + new during overlap (if the receipt format supports key-id; otherwise schedule a window). |

**Always rotate by issuing a new value first, then restarting.** Do
not delete the old value before the rolling restart finishes.

## Never-log invariant

The codebase MUST NOT print or log secret values. Concretely:

* The resolver logs only ``name`` + ``backend`` of resolutions.
* The manifest output (``secrets manifest``) lists only metadata.
* ``probe_secret_manager_reachability`` includes a 6-character
  fingerprint prefix only — it must not be lengthened.
* Tests assert that ``ManifestReport.to_dict()`` does not contain
  any resolved value.

If a future code change adds logging in this area, the corresponding
test in ``tests/test_secret_manifest.py``
(``test_manifest_report_to_dict_has_no_secret_values``) must continue
to pass.

## Coexistence with the legacy ``ManagedSecretStore``

The original ``SecretManager`` ABC was renamed to
``ManagedSecretStore`` so the new resolver class could take the more
natural name. Existing code paths
(``get_secret_manager()``, ``validate_secret_manager_policy``,
``probe_secret_manager_reachability``) are unchanged — they continue
to power the bootstrap-admin token flow. The runtime resolver and the
managed store are independent: the resolver does not read from the
managed store, and the managed store does not consult the manifest.
