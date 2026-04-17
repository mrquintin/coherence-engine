.PHONY: preflight-secret-manager test-provider-policy-matrix test-provider-policy-cloud-integration smoke-release-nonprod validate-historical-export-example release-readiness

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_PARENT := $(abspath $(ROOT)/..)

preflight-secret-manager:
	python deploy/scripts/secret_manager_preflight.py --require-reachable

test-provider-policy-matrix:
	python -m pytest tests/test_secret_manager_policy_matrix.py -v

test-provider-policy-cloud-integration:
	python -m pytest tests/integration/test_secret_manager_cloud_staging.py -m cloud_integration -v

smoke-release-nonprod:
	@echo "Run .github/workflows/nonprod-release-smoke.yml in CI for helm/k8s/systemd smoke checks."

# Same check as uncertainty-recalibration.yml (requires repo parent on PYTHONPATH if not pip-installed).
validate-historical-export-example:
	PYTHONPATH="$(REPO_PARENT)" python3 -m coherence_engine uncertainty-profile validate-historical-export \
		--input deploy/ops/uncertainty-historical-outcomes-export.example.json \
		--require-standard-layer-keys

# Deterministic, network-free release readiness checklist (prompt 20).
# Exit 0 = all checks pass, 1 = at least one soft failure, 2 = fixture/loader error.
# See docs/ops/release_readiness.md for per-check failure-mode guidance.
release-readiness:
	PYTHONPATH="$(REPO_PARENT)" python3 deploy/scripts/release_readiness_check.py \
		--json-out artifacts/release-readiness.json \
		--markdown-out artifacts/release-readiness.md

