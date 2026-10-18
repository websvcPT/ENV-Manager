# ENV Manager

Self-hosted web app that stores text config files for Docker Compose deployments and deploys
them to host paths with version history, audit log, dist comparison and drift protection.

Developer conventions are in `AGENTS.md`; live work state is in `PROGRESS.md`. Run everything
through `make` (see `make help`); nothing runs on the host directly.

## Demo deployment

`demo/` holds a small deployment that behaves like a real one managed by the app, for trying
the deploy flow and for real-scenario tests. It is opt in: the default development stack never
mounts the Docker socket.

- `demo/stack/compose.yaml` runs two workloads (`api` and `db`) on a pinned `alpine` image.
  They load their files with `env_file` from `environments/`, only sleep, and print the names
  (never the values) of the `DEMO_` variables they received.
- `demo/stack/environments/` holds the committed dist templates (`.env.*-dist`, plus a fake,
  clearly labelled non-secret certificate for a comparison that is skipped). The real target
  files next to them are written by the app or restored by `make demo-reset`, and are ignored by
  git, as in a real deployment.
- `demo/baseline/environments/` is the known target state `make demo-reset` restores
  (files written with mode 0600): `.env.api` drifted from its managed version, `.env.db` and
  `.env.db.secrets` identical to theirs, `demo-ca.pem` missing. The dist template of
  `.env.db.secrets` has a key its managed version lacks, so a full deploy of `db` is blocked
  while a deploy limited to `.env.db` is not.
- The seed (`envmanager dev seed` and the test dataset) describes the same deployment in the
  database when the demo root is given: project `demo`, deployment `stack` rooted at
  `demo/stack` (added to the allowed paths), services `api` and `db` with the post-deploy command
  `docker compose up -d --no-deps --force-recreate {service_name}`, and grants for
  `project_admin` (the project) and `env_scoped_admin` (only `.env.db`). Nothing else changes for
  the other seeded users. `make e2e` seeds these rows too; without the demo override the app
  container cannot reach the folder, which only matters for deploys.

| Target | What it does |
| --- | --- |
| `make demo-up` | Restores the baseline when no target file exists yet, restarts the app container with `docker-compose-demo.yml` (the Docker socket, the host docker group id in `group_add`, and `demo/` mounted at the same absolute path as on the host), waits for it and seeds the demo project. |
| `make demo-reset` | Restores the demo target files to the baseline. Database state (versions, deploy results) is not touched. |
| `make demo-down` | Stops the demo workloads (Compose project `env-manager-demo`) and recreates the app container from the default stack, without the socket. |
| `make e2e-demo` | Runs `make demo-up` and `make demo-reset`, then the real-scenario tests in `app/real_scenarios` inside the app container, then the browser specs in `playwright/docker-tests` (the post-deploy settings against the real daemon), then `make demo-reset` and `make demo-down`, whether or not they pass. |

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

## Deploying from the command line

```sh
docker compose exec app envmanager deploy <project>/<deployment>/<service> \
    [--env <file name> ...] [--dry-run] [--force]
```

Deploys the service as the actor `cli`: every file of the service, or the named ones. The command
prints one line per file and exits 0 only when every file was written or found unchanged and the
post-deploy step did not fail; a blocked, halted or failed file (or a deploy refused because
another deploy holds the lock of the same deployment folder) exits 1. `--dry-run` runs the same
checks without writing or backing anything up, `--force` overrides missing-key blocks and drift
halts and is audited. A drifted file that a forced deploy replaces is backed up first, next to it
(`<name>.bak-<UTC stamp>-<4 hex>`, mode 0600).

## Drift view

A file whose target changed outside the app has a drift view at `/environments/<id>/drift` (the
Halted result of a deploy links to it). It reads the target from the host when the page is opened
and shows it next to the last deployed version and the managed version, masked and compared key by
key; revealing values is an audited request per column and they disappear after 30 seconds.
Importing the target saves the file as it is on disk as a new version (refused when the file or
the managed version changed since the page was opened). Overwriting the target is a forced deploy
of that one file after a confirmation: the file it replaces is copied next to it first (mode 0600),
whatever the drift policy says, and the post-deploy command of the service runs as for any deploy.

## API tokens

Admins and superadmins create tokens for themselves on the API tokens page (viewers get 403 and
no link). A token has a name (unique among the owner's tokens that are not revoked), the
operations it may use (Deploy, Read last deployment), an optional restriction to one project,
deployment or service the owner reaches, and an optional expiry date (the token works through the
end of that day, UTC). The secret (`envm_` and 48 hex characters) is shown once, with a Copy
button and example requests that read it from `ENVM_TOKEN`; only its SHA-256 hash and a 13
character prefix (shown in the table to recognise the token) are stored, and neither the secret
nor the hash reaches the audit log or the application log. The table shows the status (Active,
Revoked, Expired, Stopped), when and from which address the token was last used.

Owners revoke their own tokens after a confirmation; the superadmin sees every token and revokes
any of them, also from the tokens panel of a user. A revoked token stops at its next use and its
entries stay in the audit log. Disabling a user, or moving them to viewer, revokes all of their
tokens for good: enabling or promoting them later does not bring a token back, they create new
ones. A revoked or expired token no longer keeps its project from being deleted: its restriction rows
are removed with the project; a token that can still be used refuses the deletion until it is
revoked.

## API

A token has two things it can do, and the API has exactly two operations to match. A token never
reads or reveals values, edits files, restores versions or touches structure, users, grants,
settings or the audit log, and it never forces: a blocked or halted file is resolved and forced by
a person in the web interface.

| Token operation | Request |
| --- | --- |
| Deploy | `POST /api/v1/services/{service_id}/deploy` |
| Read last deployment | `GET /api/v1/services/{service_id}/deployments/last` |

The service id is the number in the service page address. Every request carries
`Authorization: Bearer <secret>`; nothing else authenticates (cookies are ignored, no CSRF cookie
is set on API answers, and CORS applies only to the origins configured for the API prefix).

```sh
curl -X POST http://localhost:8080/api/v1/services/7/deploy \
  -H "Authorization: Bearer $ENVM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"environments": [".env.api"], "versions": {".env.api": 3}, "dry_run": true}'
```

### Deploy

The body is a JSON object whose three fields are all optional; any other key is refused with 422,
and `force` (whatever its value) is refused with a message that says so. An empty body means
`{}`. The body is limited to 64 KiB.

| Field | Meaning |
| --- | --- |
| `environments` | The files to deploy: a list of file names (strings) or ids (numbers). Left out: every file of the service in the token's scope. |
| `versions` | The version number to write per file, as an object whose key is the file name or its id written as text. Left out: the newest version. A file named here must be one of the files deployed. |
| `dry_run` | `true` runs every check and reports what would happen without writing, backing up or running the command. Default `false`. |

A file outside the token's scope reads exactly like a file that does not exist. The deploy runs the
same engine as the web interface (dist check, drift and missing-target policies, atomic write,
file mode, post-deploy command once, deploy lock) as the actor `token <name>`.

The request stays open until the deploy has finished, which includes the post-deploy command of
the service (up to its timeout, 3600 seconds at most), so a client needs a timeout longer than
that. A client that gives up early does not stop the deploy: it runs to the end and its result is
read afterwards from the last deployment request.

### Answer

Deploy and last deployment answer the same shape (a deploy answers the run it made):

```json
{
  "run_id": 41,
  "service": {"id": 7, "name": "api"},
  "dry_run": false,
  "status": "halted",
  "started_at": "2026-10-18T12:00:00.123456Z",
  "finished_at": "2026-10-18T12:00:01.234567Z",
  "environments": [
    {"id": 12, "name": ".env.api", "version": 3, "status": "halted", "dist": "passed",
     "drift": "drift", "message": "The target changed outside the app and the drift policy halts."}
  ],
  "post_deploy": {"status": "skipped", "exit_code": null,
                  "reason": "Not run because an environment was not deployed."}
}
```

- `status`: `success`, `halted`, `blocked`, `failed` or `post_deploy_failed`. A halted or blocked
  deploy is a result (200), not an error.
- per file `status`: `written`, `unchanged`, `would_write`, `blocked`, `halted` or `failed`;
  `dist`: `passed`, `warning`, `blocked`, `skipped` or `failed` (null when no check ran);
  `drift`: `in_sync`, `drift` or `missing` (null when the file was not examined); `message` is a
  fixed sentence about the outcome.
- `post_deploy.status`: `none`, `skipped` (with the reason), `ran`, `failed` or `running` (the
  command has not ended, `finished_at` is null); `exit_code` is set when a command ended.
- No answer holds a value, file content, host path, command text or command output.

`GET .../deployments/last` answers the newest real run (dry runs do not count) that has a result
for a file in the token's scope, and lists only the files in the token's scope. It answers 404 when
there is none, when the service does not exist and when it is outside the scope, with one and the
same body.

### Refusals

Every refusal under `/api` has one JSON shape, and no answer repeats what the client sent:

```json
{"error": {"code": "not_found", "message": "Service not found.", "fields": [], "request_id": null}}
```

| Status | `code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | No token, a malformed one, an unknown, revoked or expired one, or one whose owner is disabled or a viewer: one body and `WWW-Authenticate: Bearer` for all of them. |
| 403 | `forbidden` | The token lacks the operation. Checked before the service, so it says nothing about it. |
| 404 | `not_found` | The service does not exist, is not a number or is outside the token's scope. |
| 405 | `method_not_allowed` | Wrong method (with `Allow`). |
| 409 | `deploy_locked` | Another deploy holds the lock of the same deployment folder. |
| 422 | `validation_error` | The body is not usable; `fields` lists `{field, message}` per problem. |
| 429 | `rate_limited` | The client address is locked after failed token attempts; `Retry-After` says for how many seconds. |
| 500 | `internal_error` | Unexpected error; only a `request_id` is given. |
| 503 | `lock_unavailable` | The deploy lock could not be taken. |

Ten failed token attempts (any kind of 401) from one client address within 15 minutes lock that
address for 15 minutes; during the lock every request from it is a 429, a valid token included.
The counters are stored in the database, so a restart does not clear them, and the address is the
one after forwarded headers from the configured trusted proxies (`ENVMANAGER_TRUSTED_PROXIES`).
A 403, 404 or 422 never counts.

### Scope and audit

The scope is the owner's grants narrowed by the token's restriction, loaded again for every call:
a revoked token, a disabled or demoted owner and a removed grant apply on the next request.
Every call is recorded in the audit log with the token, its owner and the client address: the deploy
engine writes the entries of the deploys it runs, the API those of reads and refusals. A 404 names
only the id that was asked for, and no entry holds the secret or any part of what was presented.
Calls with a token that cannot be identified are recorded without an actor. Each call that
authenticates updates the token's last used time and address.

## Post-deploy commands

A service can store commands that run once after its files were deployed, for example to
recreate the Compose service that loads them. They need the Docker socket mounted into the
container (`/var/run/docker.sock`, with the host docker group id in `group_add`); the docker CLI
and compose plugin of the image do the rest. Without a reachable daemon and compose plugin the
step is skipped with the reason and the deploy still counts as successful. `ENVMANAGER_DOCKER_SOCKET`
changes the socket path (read by the web app and by the command line).

The superadmin edits the settings on the service page: working directory, commands, timeout,
run only if changed, a Test command button (`docker compose config --quiet` in the typed working
directory) and Save settings. The panel header shows the socket status (mounted, daemon
reachable, compose version); while Docker is not usable the fields are disabled with the reason
and a save is refused.

- One command per line, split into words and run without a shell: no pipes, redirects or `&&`
  (an unquoted operator is refused), and the first word must be exactly `docker`. The
  placeholders `{deployment_path}`, `{deployment_name}` and `{service_name}` are replaced inside
  the words with validated values; nothing else is interpolated.
- Commands run in order in the working directory (default the deployment root, always inside an
  allowed path, checked again when the command runs) and stop at the first non-zero exit. One
  timeout (default 120 seconds) covers all of them and the deploy lock stays held until they
  end; at the deadline the whole process group is killed and the run counts as failed.
- The command gets a minimal environment (`PATH`, `HOME`, `LANG`, `DOCKER_CONFIG` and a
  `DOCKER_HOST` pinned to the checked socket), never the environment of the app, so the master
  key, the CSRF secret and the error reporting DSN do not reach it.
- The run records the status (ran, skipped with a reason, failed), the exit code and the first
  and last 4 KB of the output, in the run and in the audit entry. While the command executes the
  run reads `running`. A failing command makes the deploy "deployed, post-deploy failed". Status is
  visible to everyone with access to the service, the output to admins in scope and the
  superadmin, the command text to the superadmin only.
- Only the superadmin stores commands, and saving is refused while Docker is not usable.
- Output is scrubbed before it is stored (run and audit entry), returned or shown: every occurrence
  of a value from the newest saved version of every file of the deployment, and of the versions
  the deploy wrote, is replaced with `****`, longest values first. Compose prints the files of its
  whole project (`docker compose config` prints them all), so the scrub set covers the deployment
  and includes files the deploying user has no access to. The values are decrypted in memory only.
  Known limits: values shorter than four characters are not scrubbed (they would match inside
  ordinary words and numbers), a value that a command re-encodes (quoted or escaped differently
  than Compose prints it, base64, hashed) is not recognized, a value that exists only on disk (a
  drifted target file) is not in the set, and a value that only appears in a comment line of a
  file is not a value. The output of a failed Test command is scrubbed the same way.
