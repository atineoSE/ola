.PHONY: test test-py test-sh test-integration

test: test-py test-sh ## Run python + shell tests (default)

test-py: ## Run python unit tests
	uv run --group dev pytest tests/ -v

test-sh: ## Run shell unit tests
	bash tests/test_ola_sh.bash

test-integration: ## Run sbx integration tests (requires sbx)
	bash tests/test_sbx_integration.bash
