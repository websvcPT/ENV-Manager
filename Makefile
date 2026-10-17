.PHONY: up up-app upforce up-bg stop destroy logs app app-logs lint format tests tests-docker \
	migrate revision e2e frontend-build tailwind-download tailwind-build tailwind-watch \
	static-fetch help

TAILWIND_VERSION ?= 4.3.3
HTMX_VERSION ?= 4.0.0
TAILWIND_CLI := ./scripts/bin/tailwindcss
TAILWIND_INPUT := ./app/assets/app.css
TAILWIND_OUTPUT := ./app/static/style.css
USER_IDS := $(shell id -u):$(shell id -g)
export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)
# The standalone Tailwind CLI is a glibc binary; run it in a glibc container as the
# invoking user so generated files are not root-owned.
TAILWIND_RUN := docker run --rm --user $(USER_IDS) -v "$(CURDIR)":/w -w /w debian:bookworm-slim
CURL_RUN := docker run --rm --user $(USER_IDS) -v "$(CURDIR)":/w -w /w curlimages/curl:8.22.0
TAILWIND_CMD := $(TAILWIND_RUN) $(TAILWIND_CLI) -i $(TAILWIND_INPUT) -o $(TAILWIND_OUTPUT) --minify

# --- Environment lifecycle ---
up: dev-dirs $(TAILWIND_OUTPUT) ## Start environment
	docker compose up

up-app: dev-dirs $(TAILWIND_OUTPUT) ## Start only the app service
	docker compose up app

upforce: dev-dirs $(TAILWIND_OUTPUT) ## Start environment, force build and recreate
	docker compose up --build --force-recreate

up-bg: dev-dirs $(TAILWIND_OUTPUT) ## Start environment in background and wait until ready
	docker compose up -d --build
	@echo "Waiting for the app to answer /healthz"
	@for i in $$(seq 1 90); do \
		docker compose exec -T app python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)" >/dev/null 2>&1 && exit 0; \
		sleep 2; \
	done; echo "app did not become ready"; docker compose logs --tail 50 app; exit 1

dev-dirs:
	@mkdir -p dev/data dev/deployments

stop: ## Stop environment
	docker compose stop

destroy: ## Destroy environment and cleanup
	docker compose down --rmi local --volumes --remove-orphans

logs: ## Follow logs
	docker compose logs --follow

# --- App container access ---
app: ## Enter the app container shell
	docker compose exec app sh

app-logs: ## Follow app logs
	docker compose logs -f --tail 500 app

# --- Quality gates (run inside the app container) ---
lint: ## Lint and type-check
	docker compose exec -T app sh /app/scripts/lint.sh

format: ## Auto-format and fix
	docker compose exec -T app sh /app/scripts/format.sh

tests: ## Run tests inside the running app container
	docker compose exec -T app sh /app/scripts/test.sh

tests-docker: ## Run tests in an isolated container
	docker compose -f docker-compose-testing.yml down --remove-orphans --rmi local -v || true
	docker compose -f docker-compose-testing.yml up --force-recreate --build --abort-on-container-exit --exit-code-from app
	docker compose -f docker-compose-testing.yml down --remove-orphans --rmi local -v || true

e2e: ## Run Playwright specs against the running stack
	@test -d playwright || { echo "playwright project does not exist yet"; exit 1; }
	docker compose -f docker-compose-e2e.yml up --build --abort-on-container-exit --exit-code-from playwright

# --- Alembic migrations ---
migrate: ## Run migrations up
	docker compose exec -T app alembic upgrade head

revision: ## Print the command to create a new alembic revision
	@echo "From the host:"
	@echo "  docker compose exec app alembic revision --autogenerate -m \"Revision description\""

# --- Frontend asset pipeline (no npm required) ---
frontend-build: tailwind-download tailwind-build ## Build the CSS bundle

tailwind-download: $(TAILWIND_CLI) ## Download the standalone Tailwind CLI

$(TAILWIND_CLI):
	@mkdir -p $(dir $@)
	$(CURL_RUN) -fsSL -o $@ https://github.com/tailwindlabs/tailwindcss/releases/download/v$(TAILWIND_VERSION)/tailwindcss-linux-x64
	@chmod +x $@

# Bootstrap build so a fresh checkout is styled; template edits are not prerequisites (use
# `make tailwind-build` to force a rebuild or `make tailwind-watch` while editing).
$(TAILWIND_OUTPUT): $(TAILWIND_INPUT) app/assets/theme.css | $(TAILWIND_CLI)
	$(TAILWIND_CMD)

tailwind-build: $(TAILWIND_CLI) ## Build Tailwind CSS once (minified, in a container)
	$(TAILWIND_CMD)

tailwind-watch: $(TAILWIND_CLI) ## Watch Tailwind CSS for local development (in a container)
	$(TAILWIND_RUN) $(TAILWIND_CLI) -i $(TAILWIND_INPUT) -o $(TAILWIND_OUTPUT) --watch

# The vendored file is committed and its name carries the version; run after a version bump.
static-fetch: ## Re-download the pinned vendor assets (run after a version bump)
	@mkdir -p app/static/htmx
	$(CURL_RUN) -fsSL -o app/static/htmx/htmx-$(HTMX_VERSION).min.js https://unpkg.com/htmx.org@$(HTMX_VERSION)/dist/htmx.min.js

# --- Help ---
help: ## This help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help
