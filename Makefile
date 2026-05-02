PYTHON ?= python3
RUFF ?= ruff
MYPY ?= mypy

.PHONY: preflight-secret-manager test-provider-policy-matrix test-provider-policy-cloud-integration smoke-release-nonprod validate-historical-export-example release-readiness ci-local

preflight-secret-manager:
	$(PYTHON) deploy/scripts/secret_manager_preflight.py --require-reachable

test-provider-policy-matrix:
	$(PYTHON) -m pytest tests/test_secret_manager_policy_matrix.py -v

test-provider-policy-cloud-integration:
	$(PYTHON) -m pytest tests/integration/test_secret_manager_cloud_staging.py -m cloud_integration -v

smoke-release-nonprod:
	@echo "Run .github/workflows/nonprod-release-smoke.yml in CI for helm/k8s/systemd smoke checks."

# Same check as uncertainty-recalibration.yml (requires repo parent on PYTHONPATH if not pip-installed).
validate-historical-export-example:
	PYTHONPATH="$$(cd .. && pwd -P)" $(PYTHON) -m coherence_engine uncertainty-profile validate-historical-export \
		--input deploy/ops/uncertainty-historical-outcomes-export.example.json \
		--require-standard-layer-keys

# Deterministic, network-free release readiness checklist (prompt 20).
# Exit 0 = all checks pass, 1 = at least one soft failure, 2 = fixture/loader error.
# See docs/ops/release_readiness.md for per-check failure-mode guidance.
release-readiness:
	mkdir -p artifacts
	PYTHONPATH="$$(cd .. && pwd -P)" $(PYTHON) deploy/scripts/release_readiness_check.py \
		--json-out artifacts/release-readiness.json \
		--markdown-out artifacts/release-readiness.md

# Local mirror of the required-checks subset of .github/workflows/ci.yml.
# Runs lint, type-check, fast unit tests, and the release-readiness checklist.
ci-local:
	@echo "==> ruff"
	$(RUFF) check .
	@echo "==> mypy --strict typed operational services"
	$(MYPY) --strict --follow-imports=skip \
		server/fund/services/policy_parameter_proposals.py \
		server/fund/services/reserve_optimizer.py \
		server/fund/services/governed_historical_dataset.py \
		server/fund/services/calibration_export.py
	@echo "==> pytest (fast)"
	PYTHONPATH="$$(cd .. && pwd -P)" $(PYTHON) -m pytest -q -m "not integration and not e2e and not cloud_integration" --maxfail=5
	@echo "==> release-readiness"
	$(MAKE) release-readiness
	@echo "==> ci-local complete"
