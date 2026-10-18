.PHONY: up up-app upforce up-bg wait-app stop destroy logs app app-logs lint format tests \
	tests-docker migrate revision seed e2e e2e-lock e2e-demo demo-up demo-down demo-reset \
	frontend-build tailwind-download tailwind-build tailwind-watch static-fetch help

TAILWIND_VERSION ?= 4.3.3
HTMX_VERSION ?= 4.0.0
# Must equal the @playwright/test pin in playwright/package.json and the image tag in
# docker-compose-e2e.yml.
PLAYWRIGHT_VERSION ?= 1.64.1
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
# Demo deployment (opt-in): the demo folder of this checkout, the Compose project of its
# workloads and the group that owns the Docker socket (read only when the demo runs).
export DEMO_ROOT := $(CURDIR)/demo
DEMO_PROJECT := env-manager-demo
DEMO_COMPOSE := docker compose -f docker-compose.yml -f docker-compose-demo.yml
DOCKER_SOCKET := /var/run/docker.sock

# --- Environment lifecycle ---
up: dev-dirs $(TAILWIND_OUTPUT) ## Start environment
	docker compose up

up-app: dev-dirs $(TAILWIND_OUTPUT) ## Start only the app service
	docker compose up app

upforce: dev-dirs $(TAILWIND_OUTPUT) ## Start environment, force build and recreate
	docker compose up --build --force-recreate

up-bg: dev-dirs $(TAILWIND_OUTPUT) ## Start environment in background and wait until ready
	docker compose up -d --build
	@$(MAKE) --no-print-directory wait-app

wait-app:
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

seed: ## Load the development dataset into the running app (dev-only passwords)
	docker compose exec -T app envmanager dev seed

e2e: ## Seed the dev dataset and run the Playwright specs against the running stack
	@docker compose ps --status running --services | grep -qx app \
		|| { echo "the development stack is not running; run make up-bg"; exit 1; }
	sh scripts/e2e-seed.sh
	docker compose -f docker-compose-e2e.yml run --rm playwright

e2e-lock: ## Regenerate playwright/package-lock.json after changing playwright/package.json
	docker run --rm --user $(USER_IDS) -e HOME=/tmp -v "$(CURDIR)/playwright":/e2e -w /e2e \
		mcr.microsoft.com/playwright:v$(PLAYWRIGHT_VERSION)-noble \
		npm install --package-lock-only --no-audit --no-fund

# --- Demo deployment (opt-in, never part of the default stack) ---
demo-up: dev-dirs $(TAILWIND_OUTPUT) ## Start the dev stack with the Docker socket and demo folder mounted, seed the demo project
	@[ -S $(DOCKER_SOCKET) ] || { echo "$(DOCKER_SOCKET) is not a socket; the demo needs a local Docker daemon"; exit 1; }
	sh scripts/demo-reset.sh --if-missing
	DOCKER_GID=$$(stat -c %g $(DOCKER_SOCKET)) $(DEMO_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory wait-app
	sh scripts/seed-dev.sh
	@echo "Demo ready: project 'demo' is seeded and the app can run docker compose for $(DEMO_ROOT)/stack"
	@echo "Restore the target files with make demo-reset, stop everything with make demo-down"

demo-down: ## Stop the demo workloads and return the app to the default stack (no Docker socket)
	docker compose -p $(DEMO_PROJECT) down --remove-orphans
	docker compose up -d
	@$(MAKE) --no-print-directory wait-app

demo-reset: ## Restore the demo target files to the baseline (drift, identical, missing)
	sh scripts/demo-reset.sh

# Real-scenario tests (app/real_scenarios, outside the default test run) deploy the demo stack
# through the real CLI onto the host folder mounted at its host path. The browser specs in
# playwright/docker-tests run against the same stack, whose app reaches the host Docker daemon.
# The demo override is always taken down again, and the target files left at the baseline,
# whether or not they pass.
e2e-demo: ## Bring the demo override up, run the real-scenario tests and the Docker browser specs, take it down
	@status=0; \
	{ $(MAKE) --no-print-directory demo-up \
		&& $(MAKE) --no-print-directory demo-reset \
		&& docker compose exec -T app sh /app/scripts/test.sh -n 0 app/real_scenarios \
		&& sh scripts/e2e-seed.sh \
		&& docker compose -f docker-compose-e2e.yml run --rm playwright-demo; } || status=$$?; \
	$(MAKE) --no-print-directory demo-reset || status=$$?; \
	$(MAKE) --no-print-directory demo-down || status=$$?; \
	exit $$status

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
