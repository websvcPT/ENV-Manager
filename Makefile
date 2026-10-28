.PHONY: up up-app upforce up-bg wait-app stop destroy logs app app-logs lint format tests \
	tests-docker migrate revision seed dev-cleanup dev-reset check-pins e2e e2e-lock e2e-demo demo-up \
	demo-down demo-reset demo-workloads demo-registry-down demo-root-scenarios frontend-build tailwind-download tailwind-build tailwind-watch \
	sso-up sso-down e2e-sso \
	static-fetch help

# Extra arguments for `make tests` (pytest) and `make e2e` (playwright test); empty runs everything.
ARGS ?=
export ARGS
TAILWIND_VERSION ?= 4.3.3
HTMX_VERSION ?= 4.0.0
# The Playwright image tag of docker-compose-e2e.yml takes this value. It must equal the
# @playwright/test pin in playwright/package.json and the default in the compose file, and the
# Tailwind version must equal the one in the Dockerfile (`make check-pins` compares them).
PLAYWRIGHT_VERSION ?= 1.64.1
export PLAYWRIGHT_VERSION
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
# The label of the containers of the local test registry (LABEL in app/real_scenarios/registry.py).
REGISTRY_LABEL := env-manager.test-registry

# The address of this machine that both a browser and the app container reach, for the development
# Keycloak (`make sso-up`). Docker Compose reads it too: it builds the issuer of the Keycloak
# (http://$(DEV_HOST):8180/realms/envmanager-dev) and the realm's redirect address, and, only when
# it is set, the development app's ENVMANAGER_BASE_URL (http://$(DEV_HOST):8080). It stays
# localhost unless you set it, which leaves the development base URL as it always was, but then a
# manual single sign-on sign in cannot work: inside the app container localhost is the container.
DEV_HOST ?= localhost
export DEV_HOST
KEYCLOAK_PORT := 8180
KEYCLOAK_REALM := envmanager-dev
KEYCLOAK_ISSUER := http://$(DEV_HOST):$(KEYCLOAK_PORT)/realms/$(KEYCLOAK_REALM)
# Single sign-on browser proof: the separate app and Keycloak, and the project that holds them.
E2E_SSO_COMPOSE := docker compose -f docker-compose-e2e-sso.yml

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

tests: ## Run tests inside the running app container (ARGS="app/tests/test_x.py -k name" narrows the run)
	docker compose exec -T app sh /app/scripts/test.sh $(ARGS)

tests-docker: ## Run tests in an isolated container
	docker compose -f docker-compose-testing.yml down --remove-orphans --rmi local -v || true
	docker compose -f docker-compose-testing.yml up --force-recreate --build --abort-on-container-exit --exit-code-from app
	docker compose -f docker-compose-testing.yml down --remove-orphans --rmi local -v || true

seed: ## Load the development dataset into the running app (dev-only passwords)
	docker compose exec -T app envmanager dev seed

dev-cleanup: ## Remove the scratch users and projects that browser specs left in the development database
	docker compose exec -T app envmanager dev cleanup

dev-reset: ## Replace the development database with a fresh seeded one (the old one stays as *.before-reset)
	sh scripts/dev-reset.sh
	@$(MAKE) --no-print-directory wait-app
	sh scripts/seed-dev.sh

check-pins: ## Fail when a tool version written in more than one file differs between them
	@package=$$(sed -n 's/.*"@playwright\/test": *"\([^"]*\)".*/\1/p' playwright/package.json); \
	compose=$$(grep -o 'PLAYWRIGHT_VERSION:-[0-9.]*' docker-compose-e2e.yml | sed 's/.*-//' | sort -u); \
	dockerfile=$$(sed -n 's/^ARG TAILWIND_VERSION=//p' Dockerfile); \
	keycloak_dev=$$(sed -n 's/^ *image: keycloak\/keycloak://p' docker-compose.yml); \
	keycloak_sso=$$(sed -n 's/^ *image: keycloak\/keycloak://p' docker-compose-e2e-sso.yml); \
	status=0; \
	[ -n "$$keycloak_dev" ] && [ "$$keycloak_dev" = "$$keycloak_sso" ] || { echo "Keycloak: docker-compose.yml pins '$$keycloak_dev', docker-compose-e2e-sso.yml '$$keycloak_sso'"; status=1; }; \
	[ "$$package" = "$(PLAYWRIGHT_VERSION)" ] || { echo "Playwright: playwright/package.json pins '$$package', the Makefile '$(PLAYWRIGHT_VERSION)'"; status=1; }; \
	[ "$$compose" = "$(PLAYWRIGHT_VERSION)" ] || { echo "Playwright: docker-compose-e2e.yml defaults to '$$compose', the Makefile '$(PLAYWRIGHT_VERSION)'"; status=1; }; \
	[ "$$dockerfile" = "$(TAILWIND_VERSION)" ] || { echo "Tailwind: the Dockerfile pins '$$dockerfile', the Makefile '$(TAILWIND_VERSION)'"; status=1; }; \
	exit $$status

e2e: check-pins ## Seed the dev dataset and run the Playwright specs (ARGS="--project=desktop tests/users.spec.ts" narrows the run)
	@docker compose ps --status running --services | grep -qx app \
		|| { echo "the development stack is not running; run make up-bg"; exit 1; }
	sh scripts/e2e-seed.sh
	docker compose -f docker-compose-e2e.yml run --rm -e PLAYWRIGHT_ARGS="$$ARGS" playwright

e2e-lock: ## Regenerate playwright/package-lock.json after changing playwright/package.json
	docker run --rm --user $(USER_IDS) -e HOME=/tmp -v "$(CURDIR)/playwright":/e2e -w /e2e \
		mcr.microsoft.com/playwright:v$(PLAYWRIGHT_VERSION)-noble \
		npm install --package-lock-only --no-audit --no-fund

# --- Single sign-on in development (opt-in, never part of the default stack) ---
sso-up: ## Start the development Keycloak (profile sso), wait for its realm and print how to set up ENV Manager (DEV_HOST=<address> for a real sign in)
	@docker compose ps --status running --services | grep -qx app \
		|| { echo "the development stack is not running; run make up-bg"; exit 1; }
	docker compose --profile sso up -d --wait keycloak
	docker compose up -d --no-deps app
	@$(MAKE) --no-print-directory wait-app
	@echo ""
	@echo "Keycloak is ready: admin console http://$(DEV_HOST):$(KEYCLOAK_PORT) (admin / dev-only-keycloak-admin)"
	@echo ""
	@echo "Settings, Single sign-on, as superadmin (root@example.test):"
	@echo "  Issuer URL      $(KEYCLOAK_ISSUER)"
	@echo "  Client ID       env-manager"
	@echo "  Client secret   dev-only-env-manager-client-secret"
	@echo "  Roles claim     resource_access.env-manager.roles (the default)"
	@echo "  Mapping rows    admin -> Admin, viewer -> Viewer"
	@echo "  Redirect URI    http://$(DEV_HOST):8080/login/sso/callback (shown read only there)"
	@echo ""
	@echo "Test users (username / password, all development and testing only):"
	@echo "  sso-admin / dev-only-sso-admin             role admin, a new account is created"
	@echo "  sso-viewer / dev-only-sso-viewer           role viewer, a new account is created"
	@echo "  sso-root / dev-only-sso-root               role admin, email root@example.test: links to the seeded superadmin"
	@echo "  sso-norole / dev-only-sso-norole           no role: refused"
	@echo "  sso-unverified / dev-only-sso-unverified   viewer@example.test, not verified: refused linking"
	@if [ "$(DEV_HOST)" = "localhost" ]; then \
		echo ""; \
		echo "WARNING: DEV_HOST is localhost. Inside the app container localhost is the container itself, so the"; \
		echo "app cannot reach this Keycloak under that address and a sign in will fail. Run again with an address"; \
		echo "that the browser and the app container both reach, for example: make sso-up DEV_HOST=192.168.10.181"; \
	fi

sso-down: ## Stop and remove the development Keycloak and return the app to the DEV_HOST it is started with
	docker compose --profile sso rm --stop --force keycloak
	@if docker compose ps --status running --services | grep -qx app; then \
		docker compose up -d --no-deps app && $(MAKE) --no-print-directory wait-app; \
	fi

# The app instance has its own fresh seeded data volume, so the development database is never
# touched and a run that fails cannot leave password sign in off for `make e2e`: the project, with
# its volume, is removed in every case, first as well, to clear what an interrupted run left.
e2e-sso: check-pins ## Prove single sign-on in a real browser against a dev Keycloak and a separate app instance (ARGS="--project=desktop" narrows the run)
	@docker volume inspect env-manager_venv > /dev/null 2>&1 \
		|| { echo "the development stack has not been started yet; run make up-bg once"; exit 1; }
	@$(E2E_SSO_COMPOSE) down --volumes --remove-orphans > /dev/null 2>&1; \
	status=0; \
	{ $(E2E_SSO_COMPOSE) up -d --build --wait \
		&& $(E2E_SSO_COMPOSE) exec -T app envmanager dev seed \
		&& docker compose -f docker-compose-e2e.yml run --rm -e PLAYWRIGHT_ARGS="$$ARGS" playwright-sso; } || status=$$?; \
	$(E2E_SSO_COMPOSE) down --volumes --remove-orphans || status=$$?; \
	exit $$status

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

# The test registry of the real scenarios and the Docker browser specs
# (app/real_scenarios/registry.py) labels its containers; this removes them with their volumes,
# whichever run left them behind.
demo-registry-down: ## Remove the local test registry containers and their volumes
	@ids=$$(docker ps --all --quiet --filter label=$(REGISTRY_LABEL)); \
	[ -z "$$ids" ] || docker rm --force --volumes $$ids > /dev/null

# The Docker browser specs read the running demo workloads (the stack overview and the New
# deployment page list them), so the target starts them from the restored target files instead of
# relying on what an earlier spec deployed. The real scenarios manage the containers themselves.
demo-workloads: ## Restore the demo target files and start the demo workloads, waiting until they run and are healthy
	sh scripts/demo-reset.sh
	docker compose -p $(DEMO_PROJECT) --project-directory $(DEMO_ROOT)/stack -f $(DEMO_ROOT)/stack/compose.yaml up -d --wait

# Real-scenario tests (app/real_scenarios, outside the default test run) deploy the demo stack
# through the real CLI onto the host folder mounted at its host path. The browser specs in
# playwright/docker-tests run against the same stack, whose app reaches the host Docker daemon.
# The scenarios start their own local registry and the browser run gets one started by the
# target, both for the registry logins. The demo override is always taken down again, the
# registries removed and the target files left at the baseline, whether or not they pass.
demo-root-scenarios: ## Run the owner and group scenarios as root in a throwaway container of the demo stack
	DOCKER_GID=$$(stat -c %g $(DOCKER_SOCKET)) $(DEMO_COMPOSE) run --rm -T --no-deps --user 0 app \
		sh -c 'sh /app/scripts/test.sh -n 0 app/real_scenarios/test_demo_owner_root.py; \
		status=$$?; chown -R $(APP_UID):$(APP_GID) "$$ENVMANAGER_DEMO_ROOT"; exit $$status'

e2e-demo: ## Bring the demo override up, run the real-scenario tests and the Docker browser specs, take it down
	@status=0; \
	{ $(MAKE) --no-print-directory demo-up \
		&& $(MAKE) --no-print-directory demo-reset \
		&& docker compose exec -T app sh /app/scripts/test.sh -n 0 app/real_scenarios \
			--ignore=app/real_scenarios/test_demo_owner_root.py \
		&& $(MAKE) --no-print-directory demo-root-scenarios \
		&& $(MAKE) --no-print-directory demo-workloads \
		&& sh scripts/e2e-seed.sh \
		&& registry=$$(docker compose exec -T app python -m app.real_scenarios.registry start) \
		&& set -- $$registry \
		&& docker compose -f docker-compose-e2e.yml run --rm \
			-e E2E_DEMO_REGISTRY="$$1" -e E2E_DEMO_REGISTRY_USER="$$2" \
			-e E2E_DEMO_REGISTRY_PASSWORD="$$3" playwright-demo; } || status=$$?; \
	$(MAKE) --no-print-directory demo-registry-down || status=$$?; \
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

tailwind-build: check-pins $(TAILWIND_CLI) ## Build Tailwind CSS once (minified, in a container)
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
