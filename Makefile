.PHONY: install test test-py test-sh test-e2e test-integration sandbox-dev

install: sandbox-dev ## Install ola CLI globally and build local dev sandbox image
	uv tool install --editable .

test: test-py test-sh ## Run python + shell tests (default; test-py includes e2e)

test-py: ## Run python unit + e2e tests
	uv run --group dev pytest tests/ -v

test-e2e: ## Run only the hermetic end-to-end pipeline tests
	uv run --group dev pytest tests/e2e/ -v

test-sh: ## Run shell unit tests (requires bats: npm install -g bats bats-support bats-assert)
	bats tests/test_ola_sh.bats

test-integration: ## Run sbx integration tests (requires sbx)
	bats tests/test_sbx_integration.bats

sandbox-dev: ## Build local dev image and load into sbx (use with: OLA_SBX_IMAGE=ola:dev ola-sandbox <name>)
	@old=$$(docker image inspect -f '{{.Id}}' ola:dev 2>/dev/null || true); \
	docker build --no-cache -f docker/Dockerfile -t ola:dev . && \
	new=$$(docker image inspect -f '{{.Id}}' ola:dev) && \
	{ [ -z "$$old" ] || [ "$$old" = "$$new" ] || docker image rm "$$old" 2>/dev/null || true; }
	docker save ola:dev -o /tmp/ola-dev.tar
	sbx template rm ola:dev 2>/dev/null || true
	sbx template load /tmp/ola-dev.tar
	rm /tmp/ola-dev.tar
