.PHONY: install install-skill test test-py test-sh test-e2e test-integration sandbox-dev dashboard dashboard-test release-image release-builder-clean release-verify

# Version and image coordinates for a release. VERSION is read from
# pyproject.toml — the single source of truth that `ola --version` and
# ola.sh's _ola_image_tag both resolve to.
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
IMAGE_REPO ?= ghcr.io/atineose/ola
PLATFORMS ?= linux/amd64,linux/arm64

install: sandbox-dev ## Install ola CLI globally and build local dev sandbox image
	uv tool install --editable .

install-skill: ## Symlink the ola-plan skill into the Claude Code + OpenHands skill dirs
	bash helper-scripts/install-ola-plan-skill.sh

dashboard: ## Build the ola-dashboard SPA (then run it with: ola-dashboard -f <agent-folder>)
	npm --prefix dashboard install
	npm --prefix dashboard run build

dashboard-test: ## Run the dashboard SPA's lint + unit tests
	npm --prefix dashboard run lint
	npm --prefix dashboard test

release-image: ## Build + push the multi-arch release image ($(IMAGE_REPO):$(VERSION) and :latest)
	@test -n "$(VERSION)" || { echo "Could not read version from pyproject.toml" >&2; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "Working tree is dirty; the image COPYs the tree verbatim. Commit first." >&2; exit 1; }
	@docker buildx inspect ola-release >/dev/null 2>&1 || docker buildx create --name ola-release --driver docker-container >/dev/null
	docker buildx build --builder ola-release --no-cache \
		--platform $(PLATFORMS) \
		-f docker/Dockerfile \
		-t $(IMAGE_REPO):$(VERSION) \
		-t $(IMAGE_REPO):latest \
		--push . ; \
	status=$$? ; \
	$(MAKE) --no-print-directory release-builder-clean ; \
	exit $$status

# Tearing the builder down after every release is what keeps the disk flat. The
# ola-release builder holds its cache in a Docker volume that only ever grows,
# and because the build above passes --no-cache, no build ever reads it: it is
# pure cost. It had reached 73 GB before anyone looked, and it never shows up as
# reclaimable in `docker system df` while the builder container is running — it
# counts as *active*. Recreated by release-image on the next run, so this is
# self-healing, not something a release has to remember.
#
# Runs whether the build succeeded or failed (the build's status is preserved
# and re-raised above): a failed build has already written the cache, and
# --no-cache means a retry gains nothing by keeping it.
#
# Best-effort by design — cleanup must never be what fails a release that has
# already pushed. Note `buildx rm` can report a timeout while still completing:
# deleting tens of GB outlives the API deadline. Hence the check afterwards
# rather than trusting the exit code, and a loud warning instead of a silent
# hardcoded-name fallback if a volume survives.
release-builder-clean: ## Remove the ola-release buildx builder and its cache volume
	@docker buildx rm ola-release >/dev/null 2>&1 || true
	@left="$$(docker volume ls -q --filter name=buildx_buildkit_ola-release)" ; \
	if [ -n "$$left" ]; then \
		echo "warning: builder cache volume(s) survived teardown: $$left" >&2 ; \
		echo "         reclaim with: docker volume rm $$left" >&2 ; \
	else \
		echo "release builder torn down (recreated on the next release-image)" ; \
	fi

release-verify: ## Check the pushed release image exists for every target platform
	@test -n "$(VERSION)" || { echo "Could not read version from pyproject.toml" >&2; exit 1; }
	docker buildx imagetools inspect $(IMAGE_REPO):$(VERSION)
	@for p in $$(echo $(PLATFORMS) | tr ',' ' '); do \
		docker buildx imagetools inspect $(IMAGE_REPO):$(VERSION) | grep -q "$$p" \
			|| { echo "Missing platform $$p in $(IMAGE_REPO):$(VERSION)" >&2; exit 1; }; \
		echo "ok: $$p"; \
	done

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
