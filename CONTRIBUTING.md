# Contributing to ENV Manager

Thank you for your interest in improving ENV Manager. This guide explains how to set up the
development stack, the conventions the code follows and how to send a change.

## Ground rules

- Open an issue before starting a large change, so the approach can be agreed first.
- Keep each pull request focused on one change, with tests and documentation updated in the same
  pull request.
- Do not add a framework, library or infrastructure component without discussing it in an issue
  first. New dependencies are pinned to an exact version in `pyproject.toml` and `uv.lock`.
- Report security problems privately to the maintainers, not in a public issue.

## Development setup

You need Docker with the Compose plugin and `make`. Everything runs in containers: do not run
Python, uv, pytest, ruff, mypy, Node, npm, Playwright or the `envmanager` CLI on your host.

```sh
make up-bg      # start the app and the smtp4dev mail catcher, wait until ready
make seed       # load the development dataset
```

Open <http://localhost:8080> and sign in as `root@example.test` with the development password
defined in `app/seeds/dataset.py` (development and testing only). Mail sent by the app is visible
at <http://localhost:5080>. `make help` lists every target, and
[docs/development.md](docs/development.md) describes the stack, the tests and the demo
deployment.

## Before you open a pull request

```sh
make format     # auto-format and fix
make lint       # ruff and mypy
make tests      # the pytest suite in the running app container
make e2e        # the Playwright specs, when you changed a screen or a route
```

`make tests ARGS="app/tests/test_x.py"` and `make e2e ARGS="--project=desktop tests/x.spec.ts"`
narrow a run while you work. `make tests-docker` runs the whole suite in a clean container, as
the CI does. Changes to deploys, post-deploy commands, drift or host files also need
`make e2e-demo`, which runs real scenarios against a demo Docker Compose stack.

## Code conventions

- **Architecture.** Routers stay thin and live by feature area in `app/web/` (HTML pages and
  htmx fragments) and `app/api/` (the bearer token API). Use cases live in `app/services/`;
  routers and the CLI call services, and services never import routers. Models are in
  `app/models/`, migrations in `app/alembic/`.
- **Python.** Type hints on every signature, a docstring on every new module, class and
  function, ruff with a line length of 100, imports at the top of the file. Log through
  `app.core.log`, never `print()`. Descriptive names: `snake_case` for files, functions and
  variables, `PascalCase` for classes, `SCREAMING_SNAKE_CASE` for constants.
- **Environment variables.** Every environment variable is documented in the README
  "Environment variables" table (name, default, purpose) in the same change that adds or changes
  it. A setting a production user may set also goes into `deploy/.env.example`.
- **Comments.** Comment complex logic only, describe behavior rather than tickets or plans, and
  do not use the em-dash character or emoji anywhere (a test checks it).
- **Templates and UI.** Jinja2 with autoescape and htmx 4 (not 1.x or 2.x syntax). Tailwind CSS
  v4 with the semantic tokens of the theme only, no raw colors. No inline scripts, styles or
  `onclick` (the Content Security Policy refuses them). Row actions come from the `row_actions`
  macro, destructive actions go through a confirm dialog, and every screen works from 360 px wide
  with 44 px touch targets on phones.
- **Security.** Every route declares an authorization dependency (a test fails otherwise).
  Secret values never reach logs, error pages, audit entries, emails or telemetry. Every host
  path is checked against the allowed paths at the moment it is used. Post-deploy commands run
  without a shell and must start with `docker`.

## Tests

- Fix a bug by writing a failing test first.
- Tests stand alone: they use temporary files and databases, need no running stack or network,
  and do not depend on their order. Fake only the boundaries (filesystem, clock, Docker CLI,
  git, SMTP) and let the code under test run for real.
- Database tests use a real temporary SQLite file and check the rows read back.
- Reuse the seed dataset in `app/seeds` (the `seeded_database` and `seed_data` fixtures) instead
  of building users and structure by hand.
- A change to a user-facing flow adds or extends a Playwright spec under `playwright/tests/`,
  with selectors that do not depend on wording (ids, `data-*` attributes, `href`), for both the
  desktop and phone projects.
- No skipped tests and no loosened assertions to get a green run.

## Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org/), because releases
are computed from them:

```
<type>(<optional scope>): <summary>
```

| Type | Use it for | Release |
| --- | --- | --- |
| `feat` | a new feature | minor |
| `fix` | a bug fix | patch |
| `perf` | a performance improvement | patch |
| `refactor`, `docs`, `test`, `build`, `ci`, `chore` | everything else | none |

A breaking change adds `!` after the type (`feat!: ...`) or a `BREAKING CHANGE:` footer and makes
a major release. Write the summary in the imperative mood and in plain English. Do not edit the
version, `CHANGELOG.md` or the release tags by hand: the release workflow does it.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make your change with its tests and documentation.
3. Run the checks above and make sure they pass.
4. Open a pull request that explains what changed and why. The CI runs the lint and the full
   test suite on every pull request.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
