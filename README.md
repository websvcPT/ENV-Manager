# ENV Manager

Self-hosted web app that stores text config files for Docker Compose deployments and deploys
them to host paths with version history, audit log, dist comparison and drift protection.

Developer conventions are in `AGENTS.md`; live work state is in `PROGRESS.md`. Run everything
through `make` (see `make help`); nothing runs on the host directly.

## Network configuration

Startup configuration comes from environment variables only. Every value is checked when the
application starts, and an invalid one stops it with a message that names the variable and the
entry. Entries are IPv4 and IPv6 addresses or networks in CIDR form, separated by commas; an
IPv4-mapped IPv6 address (`::ffff:10.0.0.5`, what a dual stack socket reports for an IPv4
client) counts as the IPv4 address it carries, and a network with host bits set
(`10.0.0.5/24`) is refused instead of widened.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENVMANAGER_TRUSTED_PROXIES` | empty | Peers whose `X-Forwarded-For` and `X-Forwarded-Proto` headers are believed, for example `172.18.0.2` or `172.18.0.0/16,fd00::/8`. `*` and networks that hold every address (`0.0.0.0/0`, `::/0`) are refused: trusting every peer would let anyone choose the address that lockouts and the allowed sources rely on. |
| `ENVMANAGER_ALLOWED_SOURCES` | empty (everyone) | Client addresses that may use the application at all, for example `203.0.113.0/24,2001:db8::/32`. Everything else gets a bare `403` (JSON in the shape of the API under `/api`). |

The client address is resolved once, before anything else runs, and is the address that the audit
log, the sign-in and API lockout counters, sessions, the last used address of API tokens and the
server's access log all use:

- A peer that is not a trusted proxy keeps its own address, whatever headers it sends.
- From a trusted proxy the client is the right-most address in `X-Forwarded-For` that is not
  itself a trusted proxy (several header lines count as one list), so a client cannot prepend an
  address of its own. `X-Forwarded-Proto` supplies the scheme (`http` or `https` only, the
  right-most value). Entries may carry a port.
- The list is read from the right and the walk stops at the first address that is not a trusted
  proxy, so whatever a client wrote to the left of it (an `unknown` or any other junk it
  prepended) is never read and changes nothing. If the walk meets an entry that is not an address
  before that (a trusted hop wrote something unreadable, or a trailing comma) the request has no
  client address. It never falls back to the proxy's own address, which may be on the allowed
  list; with allowed sources set such a request is refused.

The allowed sources check runs right after the security headers, before CORS, CSRF, sessions and
any database access: a refused request opens no database session, writes no audit entry and sets
no cookie, so bots outside the list cost nothing. Refusals are logged as `request refused, client
address <address> is not an allowed source`, at most ten lines a minute with a count of the rest
(never the path, query or headers). `/healthz` stays reachable from everywhere so container health
checks keep working. Behind a reverse proxy, list the proxy in `ENVMANAGER_TRUSTED_PROXIES` as well,
or the proxy's address is the one that is checked and counted for every client.

`ENVMANAGER_LOGIN_IP_MAX_FAILURES` (a number from 1 to 100000) replaces the limit of failed
sign ins per client address (20 in 15 minutes unless Settings changes it). It exists for the
development stack, whose browser specs all share one address, and is refused unless
`ENVMANAGER_ENVIRONMENT` is `development` or `testing`. While it is set it wins over the stored
value and the Security tab shows the field fixed (see Settings).

## Settings

The superadmin opens Settings from the sidebar (`/settings`, which opens the first tab). Every
other group gets 403 and no link. A save is validated on the server, written with one audit entry
that names every changed setting with its old and new value (a secret is never recorded, only
that it changed) and applies to the next request: the settings are read per request, so nothing
needs a restart. Settings never replace startup configuration: the master key, base URL, CSRF
secret, trusted proxies and allowed sources stay in environment variables.

| Tab | Content |
| --- | --- |
| Allowed paths | The list with mounted and writable status and the deployments under each entry, add with a live check, remove behind a confirm dialog. |
| Security | Idle and absolute session timeouts, failed logins before lockout per user, per address and for the API per address, lockout duration, minimum password length, and the two-factor policy (Optional, shown read-only). |
| Post-deploy | The Docker socket status and the switch for post-deploy commands. |
| General | Time zone and date format, the defaults of a new deployment, and the read-only diagnostics. |
| Backup | Schedule, retention, an optional extra destination, the list of backups with Run now, Download and a confirmed Restore (see Backup and restore). |
| SMTP, Git, Notifications | Not available yet: each shows its panel and says so. They arrive with the features they configure. |

- **Allowed paths.** An entry is checked when it is added: absolute and normalized, an existing
  folder inside a host mount, never `/` or the data volume, and not already in the list (compared
  on the real path, so a symlink to an entry is the same entry). The real path is stored. An entry
  on a read-only mount is accepted and shown as Read-only. A stored entry whose folder is gone
  reads Missing, one that now resolves elsewhere reads Moved and is ignored until it is added
  again. Removing an entry deletes nothing on disk; the confirm dialog counts the deployments no
  other entry covers, which are flagged as outside the allowed paths. Every add and remove is
  audited and announced through the settings notification seam, which delivers nothing until
  notifications exist.
- **Security.** Idle and absolute timeouts, lockout limits and the password length apply to the
  next request; a changed absolute timeout also recomputes the expiry of the sessions that are
  already open. The failure window (15 minutes) is not a setting. The sign-in limit per address
  can be fixed by `ENVMANAGER_LOGIN_IP_MAX_FAILURES` (development and testing only): while it is
  set its field shows that value and cannot be edited, and the stored value is kept.
- **Post-deploy.** The switch defaults to on. It can only be changed while Docker is usable. With
  it off the deploy engine skips a service's stored command, records Skipped with the reason
  "Post-deploy commands are switched off in settings.", and the deploy dialog preview and the
  service page say the same. Force does not override it.
- **General.** The time zone and date format apply to the times on the audit log, project
  activity and user activity (stored times stay UTC; the audit date filters are days of that zone;
  the CSV export stays UTC). The defaults (missing dist keys, drift, missing target, file mode,
  owner and group) pre-fill the new deployment form; a deployment keeps its own values afterwards.
  The diagnostics show whether error reporting and OpenTelemetry are on, the OTLP endpoint host
  (the host name only), the version, the environment, the trusted proxies, the allowed sources and
  the client address the application resolved for the request. No secret is shown.

## Backup and restore

A backup is a consistent copy of the SQLite database taken with SQLite's online backup, so the
application keeps running and writing while it is made. It holds the database as it is: environment
values, SMTP and git credentials are encrypted fields, and the master key is never in the database,
so it is never in a backup. A backup is not secret-free, though: it also holds user names, email
addresses, password hashes, structure and the audit log in the clear. Treat the file like the
database.

**Store the master key separately from the backups.** A backup is useless without the key it was
made with: every value in it is unreadable, and a restore refuses a backup that the key in use
cannot open. Keep the key (the `ENVMANAGER_KEY` value or the file behind `ENVMANAGER_KEY_FILE`) in a
password manager or secret store that is not on the same disk or in the same backup destination as
the backup files. A copy of the key next to the backups defeats the encryption.

- **Where.** Backups are files named `envmanager-<time>-<reason>.db` in the backup folder, inside
  the data volume (`/data/backups`). The folder is mode 0700 and every file 0600 (set when the
  file is created, so there is no moment with wider permissions). A backup is written under a
  temporary name and given its final name only when it is complete, and it is switched out of WAL
  mode so it is one self-contained file.
- **Who and why.** Each backup is recorded with its time, size, why it was taken (manual,
  scheduled, command line, before a migration, before a restore), its outcome and, for a failure,
  a fixed reason that never holds a path or a secret, and is audited (`backup.create`). The
  automatic backup before a migration is recorded the next time the application starts.
- **Outcome.** Success; Failed (no backup was written); or Not copied (the backup exists and can be
  restored, but the copy to the extra destination failed). A failed or not copied newest backup
  raises the banner every superadmin page shows until a later backup succeeds, and every backup,
  whatever its outcome, is announced through the backup notifier (`app.state.backup_notifier`,
  `app/services/backup_events.py`), which delivers nothing until notifications exist.
- **Schedule and retention** (Settings, Backup). The schedule is Off, Daily at 02:00, Every 6 hours
  or Weekly (Sunday at 02:00), in the display time zone of the General settings, so changing the
  zone moves the run. Retention is a number of backups or a number of days; the stored settings keep
  both limits and honour both when both are set. After every backup the oldest of our own files
  beyond the limits are removed with their records; the newest backup is never removed, a failure
  never removes anything, and the copies in the extra destination are never removed. A backup file
  that cannot be removed keeps its record.
- **Extra destination.** An optional folder that must lie inside the allowed paths, exist and be
  writable; the real path is stored. It is checked again for every backup, with the allowed paths
  read at that moment, and the file is created inside the folder opened without following symlinks
  (the same confined write as deploy targets), so a link swapped in later cannot lead the copy
  outside the allowed paths. A destination that left the allowed paths gets nothing and the
  backup is recorded as Not copied. Nothing is ever deleted there.
- **Scheduler.** APScheduler 3.11 runs inside the application with its default in-memory job store.
  Nothing is persisted by APScheduler: the schedule is read from the settings at startup, after the
  Backup or General settings are saved and after a restore, and replaces the job. The application is
  one worker process (the start script and the deploy locks assume it); a second worker would run
  every job twice, which the backup lock (shared between processes) turns into a skipped run. A run
  that was due while the application was down is not made up. The same scheduler removes expired
  pending sign-in steps every hour.
- **Download.** Superadmin only, streamed from the backup folder without following links, never
  cached, audited as `backup.download`.
- **Run now** takes a manual backup at once. One backup or restore runs at a time (a lock file in
  the data volume that also holds against the command line); another request is refused with a
  message.

### Restore

Restore is offered for a backup that has a file, from the Settings screen (a confirm dialog that
names the backup and says that current data is replaced) and from the command line. In order:

1. The file is copied to a private working file and checked there: an intact SQLite database
   that this application made (its migration record names a revision this version knows) whose
   key check record opens with the master key in use now. A backup made with another key, a foreign
   or damaged file, and a backup of a newer version are refused with a plain message and nothing
   changes.
2. A safety backup of the current data is taken (`pre-restore`); if that fails nothing is
   restored.
3. The working copy is prepared: brought to the current schema when the backup is older, stripped
   of every session and pending sign-in so everyone must sign in again, given the records of the
   backups that exist in the folder now, and given the audit entry of the restore (the audit log of
   the replaced database is replaced with it, so entries made after the backup are kept only in the
   safety backup).
4. It is switched in through SQLite's online backup into the live database: one write
   transaction, so the live data is replaced whole or not at all, the connections of the running
   application stay valid and the journal mode stays WAL. A failure leaves the current data as it
   was.

Everyone is signed out, the person who restored included; the screen answer sends the browser to
the sign-in page. Users and passwords are those of the backup, so a person created after it must be
created again.

```
docker compose exec app envmanager backup create
docker compose exec app envmanager backup restore envmanager-20261018T020000000000Z-scheduled.db
docker compose exec app envmanager backup restore /path/to/downloaded.db --yes
```

`backup restore` takes a path, or a bare file name that is looked up in the backup folder, asks
for confirmation (`--yes` skips it) and exits 1 when nothing was restored. `backup create` exits 1
unless the backup is complete, so a cron job notices a failed copy. The command line cannot reach
the scheduler of a running server: a restored schedule takes effect at the next start or the next
save of the Backup settings. To recover onto an empty data volume, start the container with the
same key, copy a downloaded backup in and run `backup restore`.

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

## Account

Every signed-in user, in any group, has an Account page (`/account`, from the account menu) with
four panels. Change password asks for the current password, a new one of at least the minimum
length (confirmed by typing it twice) that differs from the current one; the user's other
sessions and any unfinished second factor sign in end with the change, the session that made it
stays, and the audit entry holds counts only. Two-factor authentication is optional: setup shows a
QR code drawn by the server (an SVG that is never cached or logged) and the key as text, and
turns on only when the current password and a valid code from the authenticator are entered (so a
session that was taken over cannot enrol another authenticator). The secret reaches the
database, encrypted, only then; until then it lives in a short-lived HttpOnly cookie. Removing it
needs the current password and a fresh code (the code that turned it on cannot be reused). Wrong
passwords and wrong codes on these screens count on the same lockout counters as sign in; a wrong
code while turning it on is refused but not counted, because the secret is the one just shown.
Active sessions lists the user's live sessions (device from the browser's user agent, address,
when signed in and last seen, this one marked) with a confirmed revoke on each other session and
Sign out other sessions. The theme preference (light, dark or match the system, which is the
default) is kept in the browser and shared with the theme buttons.

A user who lost their authenticator is recovered by a superadmin: Reset two-factor on the user
detail page (after a confirm dialog) or `docker compose exec app envmanager user reset-2fa <name>`
turns the second factor off, ends that user's sessions and sign ins waiting for a code, and is
audited as `user.totp_reset` (counts only). The web action refuses a superadmin's own account,
which has the Remove button on its Account page.

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
changes the socket path (read by the web app and by the command line). The superadmin can also
switch post-deploy commands off for the whole app in Settings (the Post-deploy tab), which skips
every stored command with the reason.

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
