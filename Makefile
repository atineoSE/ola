.PHONY: test test-py test-sh test-integration

test: test-py test-sh ## Run python + shell tests (default)

test-py: ## Run python unit tests
	uv run --group dev pytest tests/ -v

test-sh: ## Run shell unit tests (requires bats: npm install -g bats bats-support bats-assert)
	bats tests/test_ola_sh.bats

test-integration: ## Run sbx integration tests (requires sbx)
	bats tests/test_sbx_integration.bats
