# Development

Run everything through `make` (see `make help`); nothing runs on the host directly. Application commands, linting and tests all run in containers.

## The development stack

`make up-bg` starts the stack in the background and waits until it is ready: the `app` service
(port 8080, the working tree bind-mounted, running as your host user id) and `smtp4dev`, a mail
catcher whose web interface is at <http://localhost:5080>. `make seed` loads the development
dataset into the running app (`envmanager dev seed`); the seeded users have the address
`<name>@example.test`, so the superadmin signs in at <http://localhost:8080> as
`root@example.test`. The fixed development password is defined in `app/seeds/dataset.py` and is
for development and testing only.

| Target | What it does |
| --- | --- |
| `make up`, `make up-app`, `make up-bg`, `make upforce` | Start the environment (all services, only the app, in the background and wait, or force build and recreate). |
| `make stop`, `make destroy` | Stop the environment, or destroy it and clean up. |
| `make logs`, `make app-logs`, `make app` | Follow the logs, follow the app logs, or open a shell in the app container. |
| `make format`, `make lint` | Auto-format and fix, or lint and type-check. |
| `make tests ARGS="app/tests/test_x.py -k name"` | Run the tests inside the running app container, optionally narrowed. |
| `make tests-docker` | Run the whole test suite in an isolated container built from the `test` stage of the `Dockerfile`. |
| `make e2e ARGS="--project=desktop tests/users.spec.ts"` | Seed the development dataset and run the Playwright specs. |
| `make seed`, `make dev-reset`, `make dev-cleanup` | Load the dataset, replace the development database with a fresh seeded one (the old one stays as `*.before-reset`), or remove the scratch users and projects the browser specs left behind. |
| `make migrate`, `make revision` | Run migrations up, or print the command that creates a new revision. |
| `make tailwind-build`, `make tailwind-watch`, `make frontend-build` | Build the CSS once (minified, in a container), watch it, or build the whole bundle. |
| `make static-fetch`, `make check-pins` | Re-download the pinned vendor assets, or fail when a tool version written in more than one file differs. |
| `make sso-up`, `make sso-down`, `make e2e-sso` | The development Keycloak and the single sign-on browser proof, described below. |
| `make demo-up`, `make demo-reset`, `make demo-workloads`, `make demo-down`, `make e2e-demo` | The demo deployment, described below. |

## Tests

`make tests` runs the pytest suite in the app container; narrow it with
`ARGS="<test files>"`. `make tests-docker` runs everything in a clean container with no running
stack, which is what a CI run does. Tests stand alone: they use temporary files and databases,
fake only the boundaries (filesystem, clock, Docker CLI, git, SMTP) and reuse the seed dataset
in `app/seeds`. Browser specs live under `playwright/tests` and run with `make e2e`. Flows that
write host files also have scenarios under `app/real_scenarios`, run by `make e2e-demo` against
the demo stack.

## Demo deployment

`demo/` holds a small deployment that behaves like a real one managed by the app, for trying
the deploy flow and for real-scenario tests. It is opt in: the default development stack never
mounts the Docker socket.

- `demo/stack/compose.yaml` runs two workloads (`api` and `db`) on a pinned `alpine` image.
  They load their files with `env_file` from `environments/`, only sleep, and print the names
  (never the values) of the `DEMO_` variables they received.
- `demo/stack/environments/` holds the committed dist templates (`.env.*-dist`; the certificate
  has none, as a certificate has no dist file). The real target files next to them are written by the app or restored by `make demo-reset`, and are ignored by
  git, as in a real deployment.
- `demo/baseline/environments/` is the known target state `make demo-reset` restores
  (files written with mode 0600): `.env.api` drifted from its managed version, `.env.db` and
  `.env.db.secrets` identical to theirs, `demo-ca.pem` missing. The dist template of
  `.env.db.secrets` has a key its managed version lacks, so a full deploy of `db` is blocked
  while a deploy limited to `.env.db` is not.
- The seed (`envmanager dev seed` and the test dataset) describes the same deployment in the
  database when the demo root is given: project `demo`, deployment `stack` rooted at
  `demo/stack` (added to the allowed paths), services `api` and `db` with the post-deploy
  commands `docker compose up -d --no-deps --force-recreate api` and `... db` (each names its own
  service), and grants for `project_admin` (the project) and `env_scoped_admin` (only
  `.env.db`). Nothing else changes for the other seeded users. `make e2e` seeds these rows too;
  without the demo override the app container cannot reach the folder, which only matters for
  deploys.

| Target | What it does |
| --- | --- |
| `make demo-up` | Restores the baseline when no target file exists yet, restarts the app container with `docker-compose-demo.yml` (the Docker socket, the host docker group id in `group_add`, and `demo/` mounted at the same absolute path as on the host), waits for it and seeds the demo project. |
| `make demo-reset` | Restores the demo target files to the baseline. Database state (versions, deploy results) is not touched. |
| `make demo-workloads` | Restores the baseline and starts the demo workloads (Compose project `env-manager-demo`) from `demo/stack/compose.yaml`, waiting until they run and the one with a healthcheck is healthy. `make e2e-demo` runs it before the browser specs, so they do not depend on an earlier spec having started the containers. |
| `make demo-down` | Stops the demo workloads (Compose project `env-manager-demo`) and recreates the app container from the default stack, without the socket. |
| `make demo-registry-down` | Removes the containers (with their volumes) of the local test registry, described below, whichever run left them. |
| `make e2e-demo` | Runs `make demo-up` and `make demo-reset`, then the real-scenario tests in `app/real_scenarios` inside the app container (they manage the containers themselves), `make demo-workloads`, then the browser specs in `playwright/docker-tests` (against the real daemon and a local registry), then `make demo-registry-down`, `make demo-reset` and `make demo-down`, whether or not they pass. |

The real-scenario tests are outside the default test run. Each one seeds a throwaway database,
runs the real `envmanager deploy` command against the demo stack on the mounted host folder and
checks the files it left (mode, owner, content, what was not touched).

Because `demo/` sits at the same absolute path in the container and on the host, a
`docker compose` command run by the app (a post-deploy command works in the deployment root)
resolves the project directory, env files and labels exactly as it does on the host:

```sh
make demo-up
docker compose exec app docker compose -f "$PWD/demo/stack/compose.yaml" config --quiet
docker compose exec app docker compose -f "$PWD/demo/stack/compose.yaml" up -d
docker ps --filter label=com.docker.compose.project=env-manager-demo
make demo-down
```

The Docker socket is root-equivalent on the host; keep the demo override for local runs only.

### Local registry

The registry logins are proved against a real private registry, for tests only. It is the pinned
`registry` image (see `IMAGE` in `app/real_scenarios/registry.py`) with basic authentication,
published on a free port of `127.0.0.1` that Docker picks. The host daemon treats `127.0.0.0/8`
as insecure, so it talks plain http to it and nothing else can reach it. Its only login is a
fixed development and testing one: the user and password are constants of
`app/real_scenarios/registry.py` and the matching bcrypt entry is committed in
`demo/registry/htpasswd`, which is copied into the created container (not mounted, so it works
from inside the app container). Nothing of it is part of the production image or the default
stacks.

- The scenarios in `app/real_scenarios/test_demo_registry.py` start one registry per run, push the
  pinned `alpine` image to it as a private image with the test login in a private Docker config
  folder (removed right after), remove the local tag so that a pull really goes to the registry,
  and remove the container, its volume and the tags afterwards.
- `make e2e-demo` starts another one for the browser specs
  (`python -m app.real_scenarios.registry start` in the app container) and hands its address and
  login to the Playwright container as `E2E_DEMO_REGISTRY`, `E2E_DEMO_REGISTRY_USER` and
  `E2E_DEMO_REGISTRY_PASSWORD`. `make demo-registry-down` removes the containers that carry its
  label, and `make e2e-demo` runs it at the end whether or not the specs passed. The image itself
  stays in the local image cache like the other pinned images (`docker rmi` removes it).

## Single sign-on in development

A Keycloak of the same version as production (`keycloak/keycloak:26.8.1-1`, `start-dev`, plain
http) is part of the development stack under the compose profile `sso`, so `make up` and
`make up-bg` never start it. It imports the realm of `dev/keycloak/realm.json` when its container
is created: realm `envmanager-dev` (*Require SSL* for external requests only, so plain http works
from private addresses), the confidential client `env-manager` (standard flow only, PKCE `S256`
required, a client secret that is a test-only value), the client roles `admin` and `viewer`, a
*User Client Role* mapper on the client that writes them into the ID token as
`resource_access.env-manager.roles`, and the test users below. Every credential in the realm and
in the compose files (the users' passwords, the client secret, the bootstrap admin login
`admin` / `dev-only-keycloak-admin`) is for development and testing only. The realm file holds the
two application addresses it accepts as `${DEV_APP_URL}` and `${E2E_APP_URL}`, which Keycloak
fills from the environment of its container.

| Target | What it does |
| --- | --- |
| `make sso-up` | Starts Keycloak, waits until its realm answers, recreates the app container when `DEV_HOST` changed its base URL, and prints the issuer, the client ID, the test-only client secret to type in Settings, the mapping rows and the test users. It warns when `DEV_HOST` is `localhost`. |
| `make sso-down` | Stops and removes the Keycloak container (a new one imports the realm again) and returns the app container to the `DEV_HOST` it is run with. |
| `make e2e-sso` | The browser proof, described below. |

### `DEV_HOST`

One variable, read by the Makefile and by `docker-compose.yml`, decides the addresses: Keycloak's
`KC_HOSTNAME` is `http://$DEV_HOST:8180`, so its issuer is
`http://$DEV_HOST:8180/realms/envmanager-dev`; the realm accepts the redirect address
`http://$DEV_HOST:8080/login/sso/callback`; and the development app's `ENVMANAGER_BASE_URL`
becomes `http://$DEV_HOST:8080`. Left at its default, `localhost`, the base URL is
`http://localhost:8080` as it always was, so `make e2e` and the other specs behave as before.

The issuer has to be one address that both the browser and the app container reach, and inside
the app container `localhost` is the container itself. For a manual sign in therefore set
`DEV_HOST` to the machine's own address, the same one you type in the browser, for example
`make sso-up DEV_HOST=192.168.10.181`, and open the app at `http://192.168.10.181:8080` (a
different address in the browser makes the redirect address differ from the one the realm
accepts). The container reaches the published Keycloak port through the host address. Then, as
`root@example.test` in Settings, Single sign-on, fill what `make sso-up` printed and turn it on.
Settings are stored in the development database, so after `make sso-down` sign in as the
superadmin with the password and turn single sign-on off again. A sign in as `sso-root` also
renames the seeded superadmin to the name the realm gives (SSO Root), since names follow the
provider, and the other test users stay as accounts; `make dev-reset` puts the database back to a
fresh seeded one.

The test users, by username (the password is `dev-only-` followed by the username, for example
`dev-only-sso-admin`):

| User | Email | In the realm | What the app does |
| --- | --- | --- | --- |
| `sso-admin` | `sso-admin@example.test` | role `admin`, verified | creates an Admin account |
| `sso-viewer` | `sso-viewer@example.test` | role `viewer`, verified | creates a Viewer account |
| `sso-root` | `root@example.test` | role `admin`, verified | links to the seeded superadmin, who stays superadmin |
| `sso-norole` | `sso-norole@example.test` | no role | refused: no role that may use ENV Manager |
| `sso-unverified` | `viewer@example.test` | role `viewer`, not verified | refused: the address is not verified, so no link to the seeded Viewer |

### Reaching the Keycloak from another machine over plain http

Checked on this machine's own LAN address (`192.168.10.181`), not from a second physical machine:
with `DEV_HOST` set to it, the browser (the Playwright container, which reaches the host through
that address like any other machine on the network) opened the app and Keycloak at that address
over plain http and signed in as every user above with the expected results (the 18 tests of the
browser proof, both projects, passed against the development stack). Keycloak's cookies work over
plain http: every cookie it sets on the sign in page and after the sign in (`AUTH_SESSION_ID`,
`KC_AUTH_SESSION_HASH`, `KC_RESTART`, `KEYCLOAK_IDENTITY`, `KEYCLOAK_SESSION`) has `SameSite=Lax` and
no `Secure` flag, which is enough because the sign in is a top-level redirect and a form post.
No certificate is needed, so none is used. Not tried: a browser
address that differs from `DEV_HOST` (the redirect address would not match what the realm
accepts) and a client on a public address (with *Require SSL* on external requests the realm
allows plain http only from localhost and the 10.x, 172.16-31.x and 192.168.x ranges).

### Browser proof

`make e2e-sso` proves the whole sign in in a real browser, desktop and phone, without touching the
development database. `docker-compose-e2e-sso.yml` (project `env-manager-e2e-sso`) starts a second
app instance with its own empty data volume that the target seeds, a Keycloak with the same realm
file, and the specs run from the Playwright image of `docker-compose-e2e.yml` (service
`playwright-sso`, config `playwright/playwright.sso.config.ts`, specs in `playwright/sso-tests/`).
All three share one network, and the names are chosen so the browser, the app and Keycloak agree:
the issuer is `http://keycloak:8080/realms/envmanager-dev` (`KC_HOSTNAME`), the app's
`ENVMANAGER_BASE_URL` and the browser's base URL are both `http://envmanager-sso:8080`, and the
realm lists that as a redirect address. The project, with its data volume, is removed at the end
whether the specs pass or not (and once more before starting, in case a run was interrupted), so
a failed run cannot leave password sign in off for `make e2e`. The Python environment is the
development stack's volume, so the stack must have been started once (`make up-bg`); it does not
have to be running. `ARGS="--project=desktop"` narrows the run.

The spec signs in as the superadmin with the password and sets single sign-on up in the Settings
tab (issuer, client ID, secret, the mapping of `admin` to Admin and `viewer` to Viewer, Test
discovery, Save and turn on through its confirmation), then checks each test user above in a
fresh browser context: the Admin and Viewer land on the projects page with the Account page
managed by SSO, the user without a role and the unverified one are refused with the fixed
messages, `sso-root` stays superadmin with a password and two-factor panel that still work,
`admin@example.test` with the right password gets the wrong-password answer while SSO is on, the
superadmin still signs in with the password, and the sign-in card never says "superadmin".

## Continuous integration

`.github/workflows/checks.yml` runs on every pull request and as the first job of the release
workflow: `make check-pins`, the lint and type checks in the test image
(`scripts/lint.sh` through `docker-compose-testing.yml`) and the full pytest suite
(`make tests-docker`). Browser specs and the demo scenarios are not run in CI.

## Releases

Versions are computed by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
from the commit messages, which follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<optional scope>): <summary>`.

| Commit | Release |
| --- | --- |
| `feat` | minor (1.2.0 to 1.3.0) |
| `fix`, `perf` | patch (1.2.0 to 1.2.1) |
| any type with `!` after it (`feat!: ...`) or a `BREAKING CHANGE:` footer | major (1.2.0 to 2.0.0) |
| `refactor`, `docs`, `test`, `build`, `ci`, `chore` | no release |

A push to `main` or `master` (the local branch is `master`, either name works) runs
`.github/workflows/release.yml`: the checks, then the release job, then the image job when a
version was released. The release job updates the version in `pyproject.toml` and in
`app/__init__.py` (the number the sidebar shows), refreshes the root package version in
`uv.lock` (`uv sync --locked` refuses a lock file whose root package version differs), writes `CHANGELOG.md`, pushes the release commit and the `v<version>` tag and creates the
GitHub release. The image job builds the Dockerfile's `runtime` target for `linux/amd64` from that
tag and pushes `websvcpt/env-manager` with the tags `<version>`, `<major>.<minor>`, `<major>` and
`latest`. The release commit is pushed with the workflow token, and a push made with that token
does not start another workflow run.

The first release is 1.0.0: zero versions are disabled, so while the repository has no release
tag, python-semantic-release makes 1.0.0 whatever the commit types are. Either the first push to
the release branch (after the checks pass) or a manual run of the release workflow (Actions,
Release, Run workflow) with `force` set to `major` cuts it; whichever comes first creates `v1.0.0`,
and a second `major` run after that would make 2.0.0. Later, the same `force` input
(`patch`, `minor` or `major`) makes an exceptional bump; leave it at `none` otherwise.

Setup on GitHub and Docker Hub:

- Repository secrets `DOCKERHUB_USERNAME` (the Docker Hub account) and `DOCKERHUB_TOKEN` (an
  access token of that account with read and write permission on `websvcpt/env-manager`).
- Settings, Actions, General, Workflow permissions: allow the workflows to run. The release job
  asks for `contents: write` itself.
- If the release branch is protected, the workflow token must be allowed to push the release
  commit and the tag directly (a ruleset bypass for GitHub Actions), or the workflow must use a
  token that can: a personal access token or an app token stored as a secret, passed to
  `actions/checkout` as `token` and to the release step as `github_token`. A required pull
  request or status check rule without a bypass makes the release job fail at the push.
- The Docker Hub repository `websvcpt/env-manager` must exist or the account must be allowed to
  create it on push.
