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
| SMTP | The mail server (host, port, TLS mode, username, password, from address) and a Send test mail button (see Notifications). |
| Notifications | The event matrix: a switch for each event and who receives the events about a project, and the deploy webhook (address, shared secret, switch and Send test event; see Notifications). |
| Git | The private repository the encrypted export is pushed to: address, branch, commit author, export format, deploy key or token, the push switch, and Test connection with the SSH host key (see Git sync). |

- **Allowed paths.** An entry is checked when it is added: absolute and normalized, an existing
  folder inside a host mount, never `/` or the data volume, and not already in the list (compared
  on the real path, so a symlink to an entry is the same entry). The real path is stored. An entry
  on a read-only mount is accepted and shown as Read-only. A stored entry whose folder is gone
  reads Missing, one that now resolves elsewhere reads Moved and is ignored until it is added
  again. Removing an entry deletes nothing on disk; the confirm dialog counts the deployments no
  other entry covers, which are flagged as outside the allowed paths. Every add and remove is
  audited and announced to the superadmins as a system notification (see Notifications).
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

## Observability and error reporting

Error reporting (Sentry) and tracing (OpenTelemetry) are both off. Nothing is exported, no
exporter thread starts and no provider is installed until they are configured in the environment
of the server; the code paths that would record only call a no-op. Neither integration is ever
given a secret value, a file's content, a command or its output or a token.

| Variable | Meaning |
| --- | --- |
| `ENVMANAGER_SENTRY_DSN` | Turns error reporting on. No personal data, no local variables and no request bodies are captured, tracing stays at 0 (OpenTelemetry does the tracing), and the release and `ENVMANAGER_ENVIRONMENT` are set. Headers with credentials, cookies, session ids, environment keys and values and SQL parameters are removed from events and breadcrumbs, and the expected outcomes (blocked or halted deploys, failed sign ins, a post-deploy command that exits non-zero) are not reported as errors. |
| `FASTAPI_OTEL_AUTO_CONFIGURE=true` | Turns OpenTelemetry export on. Without it nothing is exported, whatever else is set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where traces, metrics and logs go (OTLP over HTTP, `http/protobuf`); `/v1/traces`, `/v1/metrics` and `/v1/logs` are added. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `..._METRICS_ENDPOINT` and `..._LOGS_ENDPOINT` replace it for one signal and are used as they stand. A signal is not exported when it has no address, when `OTEL_<SIGNAL>_EXPORTER` is `none` or when `OTEL_SDK_DISABLED` is `true`. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Headers sent to the collector, for example its API key. Never shown in diagnostics or by the test command. |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER` | The standard variables. The service name defaults to `env-manager`, and the attributes to the version and the deployment environment name. |

**What is recorded.** FastAPI's own spans and metrics for every request (the method, the route
template, the status, the URL path and the query string as FastAPI records them; never a request
body or a header). The path and query of this application's pages hold ids, names, filters, the
project search text and, for the live path checks of the deployment form, the allowed paths
dialog and the file target check, the host path being typed; values, passwords and tokens travel
in request bodies and headers and are never in a URL. The reveal routes name the environment key
in their path. Besides those, these spans, which carry ids, names, counts and results only (no
path, command, output, address or text of an error):

- `envmanager.deploy` (project, deployment and service ids and names, actor type, dry run, force,
  number of files, result), with child spans `envmanager.dist_check` and `envmanager.drift_check`
  for each file and `envmanager.post_deploy` (result and exit code, never the command or its
  output) for a service that has a command;
- `envmanager.dist_check` and `envmanager.drift_check` when a check or the drift view runs on its
  own, `envmanager.backup` (trigger, result), `envmanager.git_push` (result, tries, why a run did
  nothing), `envmanager.smtp.send` (TLS mode, result, whether trying again can help) and
  `envmanager.webhook.deliver` (attempt, result, HTTP status; the address, which is a secret, is
  not recorded).

Metrics, whose labels are fixed words (never a name or an address): `envmanager.deploys` and
`envmanager.deploy.duration` by result (real deploys only, a refused one counts as `refused`),
`envmanager.post_deploy.runs` and `.duration` by result, `envmanager.drift.detections` by where
the drift was seen (`deploy`, `dry_run` or `view`), `envmanager.dist_check.failures` by problem,
`envmanager.login.failures` by channel (`sign_in` covers sign in and the account screens, `api`),
`envmanager.login.lockouts` by counter kind, `envmanager.backup.runs` by result and trigger,
`envmanager.git_push.failures` by problem and `envmanager.backup.age`, a gauge of the seconds
since the newest backup that produced a snapshot. The gauge reads a time held in memory (read from
the database once at start and moved by every backup the server takes), so a scrape never queries
the database; a backup made by the command line while the server runs is not seen until the
server restarts.

**Logs.** FastAPI records an unhandled exception and a failed request validation as logs linked
to the trace of the request. A processor that runs before the exporter keeps only the exception
class, the route and the number of validation errors: the message, the stack and any request
value are dropped, because a message can repeat a value. Sentry still receives the (scrubbed)
exception.

**Traces and the audit log.** An audit entry stores the id of the trace that was being recorded
when it was written, so it can be matched to its trace (the audit log shows a shortened id when
there is one). With export unconfigured a `traceparent` header is ignored: there is no span, no
span context and no trace id. With export on, the request joins the trace of a sampled
`traceparent` it carries, so the id on its audit entries is then one the client chose.

**Database parameters.** The database engine hides statement parameters, so the text of an error
never holds a bound value, and no database instrumentation is installed.

### Checking the setup

```sh
docker compose exec app envmanager observability test [--timeout 10]
```

Sends one test event to Sentry (when a DSN is set) and one test span to the OTLP traces endpoint
(when OpenTelemetry export is configured) and reports each by host name only, for example
`Error reporting: test event 3f2a... accepted by o1.ingest.example.com (HTTP 200)` and
`OpenTelemetry: test span 9c1f... exported to collector.internal and accepted`. A destination is
reported as sent only when the other end took it: the Sentry result is the HTTP status the
server answered with, the span result is the answer of the OTLP exporter, so an unreachable or
refusing collector reads as failed. A destination that is not configured is reported as such and
nothing is sent. The command exits 0 when everything configured was accepted (or nothing is
configured) and 1 when a configured destination did not take its test. The command line commands
(deploy, backup) do not export spans or metrics; only the running server does.

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
the backup files. A copy of the key next to the backups defeats the encryption. After a key
rotation, keep each old key apart from the backups too, labelled with the backups it opens (see
Key rotation).

- **Where.** Backups are files named `envmanager-<time>-<reason>.db` in the backup folder, inside
  the data volume (`/data/backups`). The folder is mode 0700 and every file 0600 (set when the
  file is created, so there is no moment with wider permissions). A backup is written under a
  temporary name and given its final name only when it is complete, and it is switched out of WAL
  mode so it is one self-contained file.
- **Who and why.** Each backup is recorded with its time, size, why it was taken (manual,
  scheduled, command line, before a migration, before a restore, before a key rotation), its
  outcome and, for a failure, a fixed reason that never holds a path or a secret, and is audited
  (`backup.create`). The automatic backup before a migration is recorded the next time the
  application starts.
- **Outcome.** Success; Failed (no backup was written); or Not copied (the backup exists and can be
  restored, but the copy to the extra destination failed). A failed or not copied newest backup
  raises the banner every superadmin page shows until a later backup succeeds, and every backup,
  whatever its outcome, is announced through the backup notifier (`app.state.backup_notifier`,
  `app/services/backup_events.py`); a failed or not copied backup is a notification (see
  Notifications).
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
   key check record opens with the master key in use now (or, with `--key-file`, the older key of a
   backup made before a key rotation). A backup made with another key, a foreign or damaged file,
   and a backup of a newer version are refused with a plain message and nothing changes.
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
unless the backup is complete, so a cron job notices a failed copy.

A backup made before a key rotation is restored with the key it was made with. The container needs
that key as a file, so run the command as a one-off container with the old key mounted (the
restore is online, so the server may keep running):

```
docker compose run --rm -v "$PWD/master.key.old:/run/oldkey/master.key:ro" envmanager \
  envmanager backup restore <file> --key-file /run/oldkey/master.key
```

The backup is checked against that key, and its values and hashes are moved to the key in use now
(the same re-keying as the rotation) in the private working copy before anything is replaced, so
the restore ends like any other. The Settings screen cannot take an older key; such a backup is
restored from the command line.

The command line cannot reach the scheduler of a running server: a restored schedule takes effect
at the next start or the next save of the Backup settings. To recover onto an empty data volume,
start the container with the same key, copy a downloaded backup in and run `backup restore`.

## Key rotation

The master key seals every environment value, the second factor secrets and the secret settings
(the SMTP password, the webhook secret, the git deploy key and token), and it keys the hashes that
drift detection compares. A rotation replaces it with a new key. It is offline: the application
is stopped, the command rewrites the database in one transaction, and the application is started
with the new key.

1. Stop the application, so nothing serves the data folder:

   ```
   docker compose stop envmanager
   ```

2. Run the rotation as a one-off container from the same image with the same volumes, and a
   folder for the new key that is writable and outlives the container. With the sample compose
   file (service `envmanager`, data in `./data`):

   ```
   mkdir -p newkey
   docker compose run --rm -v "$PWD/newkey:/run/newkey" envmanager \
     envmanager key rotate --new-key-file /run/newkey/master.key
   ```

   The command creates the new key file (mode 0600) when the path does not exist, in a folder that
   must already exist, so a key is never written to a folder that vanishes with the container; it
   refuses an existing file that is not a valid key and a key equal to the current one. To use a key
   you made yourself (`openssl rand -base64 32 > newkey/master.key`), pass that file: an existing
   valid key file is used as it is and may be mounted read-only. The container user must be able to
   write to the folder when the file is to be created.

3. Copy the new key into your secret store and keep the old key there too, apart from the backup
   files and labelled with the backups it opens. The output names the backup that was taken first:
   it is sealed with the old key.

4. Point `ENVMANAGER_KEY_FILE` at the new key file (for the sample file, change the source of the
   key mount to `./newkey/master.key`). If the key was given as `ENVMANAGER_KEY`, remove that
   variable: the application refuses to start when both are set.

5. Start the application:

   ```
   docker compose up -d
   ```

   It starts with the new key and refuses to start with the old one.

What the command does, in order:

- It takes the lock of the data folder and refuses to go on while a server holds it (see below).
- It checks that the current key opens the database (the key check record) and that the new key
  file is usable. Nothing has changed if either is refused.
- It takes a backup (reason `rotation`, listed in the Backup tab as "Before a key rotation"). If
  the backup fails, nothing is changed.
- In one transaction it seals every value again with the new key and recomputes every keyed hash:
  the content of every environment version and its hash, the second factor secrets, every secret
  setting, the hashes of deploy results (the baselines of the drift checks) and the key check
  record. Every value is opened with the new key before the transaction is committed, and the audit
  entry `key.rotate` (counts and the name of the backup, never key material) is part of it. Any
  failure rolls everything back and the database is as it was; the new key file stays and the next
  run uses it.

What a rotation leaves behind:

- Remembered sign-in addresses are cleared. They are keyed hashes of addresses that are not stored,
  so they cannot be recomputed; each user's next sign-in is learned silently and not announced.
- The hash of the file found before a deploy (`target_hash_before` of a deploy result, which
  nothing reads) is recomputed when that file held a stored version and dropped when it held
  anything else.
- Sessions stay valid. A second factor setup that was in progress (its secret is in a cookie sealed
  with the old key) must be started again.
- Git sync progress is reset: the next hourly run finds that the sealed manifest in its working
  clone does not open with the new key, writes the whole export again under new paths and pushes
  it. The old files leave the working tree but stay in the history of the repository, readable only
  with the old key, so keep the old key for as long as those commits are kept. A snapshot export
  pushed before the rotation holds a database sealed with the old key.
- Backups taken before the rotation are sealed with the old key. Restore one with
  `backup restore <file> --key-file <old key file>` (see Backup and restore). Keep each old key in
  your secret store, apart from the backup files and labelled with the backups it opens, and do not
  delete an old key while a backup or an exported commit that needs it is kept.

**The server lock.** The running server holds an exclusive lock on `server.lock` in the data folder
for as long as it runs, and the rotation holds the same lock while it works, so a second server
cannot start on the same data folder and the rotation cannot run under a server (nor can a server
start in the middle of a rotation). The kernel releases a lock when its process ends, so a crash
never leaves it held: a file that still holds the line of a holder that did not end cleanly is a
stale lock, reported in the log and taken over. The lock works between containers that share the
data folder on a local filesystem (a bind mount or a Docker volume); a network filesystem that does
not honour locks cannot enforce it.

## Git sync

The application can push an encrypted export of its data to a private git repository, so a copy
exists on a host other than the one that runs it. The direction is push only (nothing is ever
pulled or imported), and nothing readable leaves the application: every file in the repository is
ciphertext sealed with the master key, under paths that are keyed hashes, and a commit carries the
message `Encrypted export <time>` and a fixed address (`env-manager@localhost`) with the author name
of the settings, never the name of a project or a file.
The repository can therefore sit on any private host. Like a backup, the export is useless without
the master key, so keep that key apart from the repository (see Backup and restore).

- **Export format** (Settings, Git). *One file per version* (the default) writes, for every version
  of every file, the ciphertext the database already holds (`versions/<hash>/<number>.enc`, written
  once and never touched again, so the tree only grows) and one sealed metadata file for each file
  (`versions/<hash>/meta.enc`: the project, deployment, service and file names, their paths, and for
  each version its number, author, comment, time and keyed content hash). The `<hash>` is a keyed
  hash of the file's id, so a name cannot be guessed from it without the key. *Encrypted database
  snapshot* replaces one file, `snapshot.enc`, at every push: the same consistent snapshot a backup
  takes, sealed as a whole with the master key, because a plain snapshot holds names, paths and
  user names in the clear. Both layouts also hold `manifest.enc`, which names the layout and tells,
  by whether it opens with the current key, whether the tree belongs to this key. After a key or
  layout change the tree is written again from scratch and the old files are gone from the working
  tree (the history of the repository keeps what was pushed before, sealed with the old key).
- **Reading an export.** Every file is a version-tagged AES-256-GCM token, the format of the
  database itself. The version files are the database ciphertext (no context); `meta.enc`, the
  manifest and the snapshot are bound to a context (`git-export:meta:<hash>`,
  `git-export:manifest`, `git-export:snapshot`). With the key, inside the image:

  ```
  docker compose exec app python -c "
  from pathlib import Path
  from app.core.crypto import CryptoService, load_master_key
  crypto = CryptoService(load_master_key(None, Path('/run/keys/envmanager.key')))
  print(crypto.decrypt_text(Path('versions/<hash>/000001.enc').read_text().strip()))"
  ```

  Nothing in the application reads an export back yet; to recover, restore a backup.
- **Address.** `ssh://git@host:2222/path.git`, `git@host:path.git` or `https://host/path.git`. A
  password or token in the address is refused (it has its own field), so is `http`, an address that
  starts with `-` and any other transport (`ext::`, `file://`, a local path), because a typed path
  would write outside the allowed paths. An address that names a user (`https://ops@host/...`)
  sends that user name with the token (Bitbucket wants `x-token-auth`); otherwise `x-access-token`.
- **Credentials.** The deploy key (an unencrypted private key, pasted into the multi-line field)
  is used for an ssh address and the access token for an https address; both can be stored and
  both are sealed like the other secrets, never shown back (an empty field keeps what is stored,
  a checkbox removes it) and never in an audit entry. The application starts the `git` binary with
  an argument list and no shell (git itself passes the ssh command and the credential helper line
  to `sh`, so every path in them is made of safe characters and quoted), in a working clone below
  `/data/git` (mode 0700), with an environment of fixed values only: no variable of the
  application, no agent, no terminal prompt, no configuration of the system or a user, and only
  the ssh and https transports. The key and the
  pinned host key are written to files of mode 0600 in a private folder for the length of one call
  and removed afterwards; the ssh command allows nothing else (`IdentitiesOnly`, no agent or
  forwarding, `StrictHostKeyChecking=yes` against only the pinned key, of the pinned type). A token
  goes to git through its own `store` credential helper pointed at a file of the same kind, so it
  is never in the address, an argument, an environment or the remote configuration. Every git call
  has a deadline, and the output of git is read only to choose one of a few fixed sentences.
- **Test connection** reads from the saved repository (`ls-remote`) and sends nothing, so save a
  change before testing it; only a push proves that writing works. For an ssh address the test
  first asks the server for its host key. With nothing pinned it stops and shows the fingerprint,
  and **Trust this key and test again** pins exactly that key: the server is asked again at that
  moment and the key is pinned only if its type and fingerprint are the ones shown. From then on
  every ssh call checks that key and nothing else, and a server that no longer presents it is
  refused before anything is sent, with both fingerprints shown. Changing the repository address
  clears the pin (and the working clone, the watermark and the failures), so a new server is
  trusted afresh; pinning never replaces a key that is already pinned. A push to an ssh address
  with no pinned key fails with a message that says to test and trust the key.
- **When it pushes.** A job on the scheduler runs an hour after the last run, the first two
  minutes after start. A push happens only while the address is saved and the switch is on, at
  most once an hour, and only when something changed since the last push that worked: a
  watermark (a keyed hash of the counts, largest ids and newest update times of the versions and
  of the structure, of the address, branch and layout, and for the snapshot layout of the audit log
  without the entries that git sync and backups write about themselves) is compared with the one
  stored. There is no push after a save or a deploy. The working clone is a cache: before each push
  it is made to match the remote branch (or started empty when the branch does not exist), the
  export is written on top, and the result is pushed without force, so commits that someone else
  made stay and a push never rewrites history.
- **When it fails.** A fault that can pass (the server cannot be reached, a timeout, the
  repository changed during the push) is tried up to four times with waits of 5, 30 and 120
  seconds; a refused credential, a changed host key or a refused push stops at once. A run that
  still fails is recorded with one fixed sentence, audited (`git.push`, the host only, never the
  address or a secret), announced as a Git push failure notification (see Notifications) and shown
  as the banner "Git push failing" on every superadmin page until a push works. The next attempt
  waits an hour after the first failed run, then two, four, up to a day, and a save of changed git
  settings gives it a fresh start. Each failed run sends the notification, so a repository that
  stays broken sends about five mails the first day and then one a day.
- **Audit.** `git.push` (a push that went out or a failed run), `git.test` (each test, with the
  host and the outcome), `git.host_key_pin` and `git.host_key_clear` (host and fingerprint), and
  the usual `settings.update` entry for a save, which keeps the scheme and host of the address and
  only the name of a credential. These entries have no item scope, so only the superadmin sees
  them.
- **Cost and limits.** The snapshot layout adds the whole database to the history of the
  repository at every push, which does not compress; the per-version layout only adds what is new.
  File sizes, the number of files and the times of the commits are visible to anyone who can read
  the repository. Proxy settings are not passed to git. The application runs as one worker; a second
  one would start a second scheduler.

## Notifications

The application sends mail about what the superadmin and the admins need to know. Nothing is sent
until the SMTP tab has a server and a from address, and until then every superadmin page shows the
banner "SMTP is not configured". Mail goes through fastapi-mail, which is configured from the
stored settings each time a mail is sent, never at start-up, so a save applies to the next mail.

- **SMTP tab.** Server, port, TLS mode (None, STARTTLS or SSL/TLS), username, password and from
  address. Emptying the server and the from address switches mail off. The password is encrypted in
  the settings store and never put back into the page: an empty field keeps the stored password,
  and a checkbox that appears while one is stored removes it. Send test mail goes through the
  saved settings (so a working test means notifications work), shows how it went in words that
  never repeat what the server said (cannot connect, no answer in time, refused login, refused
  address, TLS failure) and is audited as `smtp.test`. Addresses are checked as plain addresses,
  so names on private networks such as `ops@corp.local` work.
- **Events (Notifications tab).** Each has a switch. On by default: a sign-in from an address that
  user has not used before, a lockout (a name, an address, or the API from an address), a failed,
  blocked or halted deploy (a failed post-deploy command included), a user created, disabled, given
  a new password (changed by the user or reset by a superadmin or the command line) or whose
  two-factor sign-in was reset, a failed or not copied backup, and a failed git push (see Git sync).
  Off by default: every successful deploy and every file
  change (a new version saved, restored, imported from the host, created, or keys added from a dist
  report). Changes to the allowed paths are always announced to the superadmins; there is no
  switch for them.
- **Recipients.** Events about the whole system go to the active superadmins. Events about a
  project (deploy results and file changes) go to the project admins by default, or to the
  superadmins, or to both, as chosen per event: a project admin is an active admin whose grants
  reach at least one file the event is about, and each of them is told only about the files they
  can reach, so a mail never names a file outside a user's access. Every recipient gets a message
  of their own, so addresses are not shared, and an address is written to once.
- **Extra addresses per project.** The superadmin can name extra addresses on the project's
  Settings tab (a team mailbox, a pager gateway), one per line, at most 20, each checked as a plain
  address and kept once whatever its case. They receive every notification about that project that
  is switched on in the matrix (deploy results and file changes), whatever the recipients choice
  is, each with every file of the event. An address that is also an account's address is told about
  every file of the project, because the superadmin chose it. Events about the whole system never
  go to them. A change is audited as `project.notification_addresses` with the addresses added and
  removed, and only the superadmin sees that entry.
- **New address.** Each user has a small record of the addresses they signed in from, kept only as
  keyed hashes (an HMAC under a subkey of the master key), at most the 20 seen most recently. The
  first sign-in of a user, who has no record yet, is learned without a mail, which also means a
  database that had users before this existed does not mail for all of them. A sign-in with no
  client address is neither recorded nor announced. A key rotation clears the records (the hashes
  cannot be recomputed, the addresses are not stored), so the next sign-in of each user after a
  rotation is learned without a mail again.
- **Delivery.** A mail is sent after the change that caused it was committed, in the background:
  the request that causes an event never waits for the mail server and never fails because of it.
  A mail that fails for a reason that can pass (no connection, no answer, a closed connection) is
  tried again after 5 and 30 seconds, and each retry reads the settings again, so a server that was
  fixed in the meantime is used; a refusal that cannot pass (login, address) is tried once. Up to
  500 events wait at once; more are dropped and counted. A failure is logged with a fixed reason
  and counted in the database, and the superadmin banner "Notification emails are failing" shows
  how many mails could not be sent since one last worked (a save of the SMTP settings or a working
  test mail starts the count again). Events still waiting when the server stops are lost.
- **Command line.** The commands send the same notifications when SMTP is configured: `user
  create`, `disable`, `passwd` and `reset-2fa`, `deploy` (a blocked, halted or failed result) and
  `backup create` (a failed or not copied backup). The command process cannot leave work behind, so
  it sends before it ends, once and without retries, within 30 seconds, and a mail that cannot be
  sent never changes the output or the exit code of the command. It needs the master key to read the
  SMTP password; without it nothing is sent.
- **What a mail holds.** Names, ids, results and fixed sentences, in a plain text part and a simple
  HTML part made from Jinja templates with autoescape on, with times in the time zone and date
  format of the General settings. Never a value, a file's content, a password, a token or a command's
  output. A name typed into the sign-in field that belongs to no user is never repeated in a lockout
  mail. The password of a new or reset user is shown once on screen or in the command output and is
  not in the mail.
- **Development.** The development stack has a mail catcher, smtp4dev (`rnwood/smtp4dev:3.15.0`),
  with its web interface at <http://localhost:5080>. `envmanager dev seed` points the SMTP settings
  at it (`smtp4dev`, port 25, no TLS, no sign in) when `ENVMANAGER_ENVIRONMENT` is `development`
  and SMTP is not configured yet, so every event of a development session appears there. Its API
  (`/api/messages`) is how the browser specs read mail back.

### Deploy webhook

The Notifications tab can also post every deploy result to a web address, for chat tools and
automation. It has an address, a shared secret, a switch and a Send test event button. It works
without SMTP.

- **Which deploys.** Every real deploy that was recorded, whatever its result (success, halted,
  blocked, failed, post-deploy failed), and from every way to deploy: the web, the API and the
  command line. Dry runs are never sent, and neither is a deploy that was refused because the
  deployment folder was locked, since no run was recorded for it.
- **The request.** A `POST` with `Content-Type: application/json`, a `User-Agent` of
  `ENV-Manager/<version>` and two headers that let the receiver trust it:
  `X-EnvManager-Timestamp` (Unix time in seconds) and `X-EnvManager-Signature` (`sha256=` and the
  hex HMAC-SHA256 of the timestamp, a dot and the body bytes exactly as received, made with the
  shared secret).
- **The body.** The fields of the API's deploy answer (see Answer below) for the whole run, every
  file it touched whoever may see it, plus `event` (`deploy.finished`), `event_id`, `project` and
  `deployment`. Never a value, a file's content, a host path, a command or a command's output.

  ```json
  {
    "run_id": 41,
    "service": {"id": 7, "name": "api"},
    "dry_run": false,
    "status": "success",
    "started_at": "2026-10-18T12:00:00.123456Z",
    "finished_at": "2026-10-18T12:00:01.234567Z",
    "environments": [
      {"id": 12, "name": ".env.api", "version": 3, "status": "written", "dist": "passed",
       "drift": "in_sync", "message": null}
    ],
    "post_deploy": {"status": "ran", "exit_code": 0, "reason": null},
    "event": "deploy.finished",
    "event_id": "5b0f3a52-7d0c-4f6e-9a1f-2c1d9a3e8b44",
    "project": {"id": 2, "name": "billing"},
    "deployment": {"id": 5, "name": "main"}
  }
  ```

  The test event has the same shape with `event` set to `webhook.test` and a made-up run
  (`run_id` 0, a project called "Test project"), so a receiver can check how it reads a document.
  `event_id` is new for every event and stays the same when a delivery is tried again, so a
  receiver can ignore a repeat.
- **Checking the signature.** Compute the HMAC over the raw body bytes (not a parsed and
  re-serialized copy) with the same shared secret, compare in constant time, and refuse a
  timestamp that is too old so a captured request cannot be replayed:

  ```python
  import hashlib
  import hmac
  import time

  def verify(secret: str, timestamp: str, body: bytes, signature: str, tolerance: int = 300) -> bool:
      """True when the request came from ENV Manager and is recent."""
      if abs(time.time() - int(timestamp)) > tolerance:
          return False
      expected = "sha256=" + hmac.new(
          secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
      ).hexdigest()
      return hmac.compare_digest(expected, signature)

  # headers["X-EnvManager-Timestamp"], headers["X-EnvManager-Signature"] and the raw request body
  ```

- **The settings.** The address must be `http` or `https`, with a host, at most 2048 plain
  characters and no user name or password (put the trust in the secret). Any host is allowed,
  private ones included, because the receiver is often inside the same network. The shared secret
  has at least 16 characters, is encrypted in the settings store like the SMTP password, and is
  never put back into the page: an empty field keeps the stored one, and a checkbox that appears
  while one is stored removes it. The switch can only be on while an address and a secret are set.
  The settings audit entry keeps the scheme, host and port of the address and not its path or
  query, which often hold a token, and for the secret only that it changed.
- **Send test event.** Posts the test event once through the saved settings (so save a change
  before testing it), also while the switch is off, and shows how it went in fixed words that never
  repeat the address or what the receiver answered: it could not be reached, no answer in time, a
  TLS certificate that cannot be verified, a redirect (never followed), or the HTTP status. Audited
  as `webhook.test`.
- **Delivery.** The post is made in the background after the deploy finished, so a slow receiver
  never delays or fails a deploy. It waits 10 seconds at most (5 to connect), does not follow
  redirects and counts any 2xx answer as delivered. A refused connection, a timeout, a 408, a 429
  and a 5xx answer are tried again after 5, 30 and 120 seconds (four attempts at most) with a fresh
  timestamp and signature on the same body; any other answer, a redirect and a certificate failure
  are final. Each retry reads the settings again, so a receiver that was fixed or a webhook that was
  switched off in the meantime is honoured. Every attempt is audited as `webhook.deliver` (the host
  only, the event id, the run, the attempt, the HTTP status and a fixed reason, never the secret),
  and only the superadmin sees those entries. An event that was not delivered after its last attempt
  is counted once, and the superadmin banner "The deploy webhook is failing" shows how many events
  failed since one was last delivered and links to the Notifications tab; a delivered event, a
  delivered test event or saving the webhook settings starts the count again. Events still waiting
  when the server stops are lost. The command line posts once, without retries, within 30 seconds.

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

A project name is a display name (letters in any case, numbers, spaces and `. - _`, 1 to 100
characters, unique ignoring case), so quote the path when it holds spaces:
`envmanager deploy "Fancy Proj. Name/prod/web"`. Deployment and service names stay lowercase
slugs (letters, numbers and dashes), as they usually mirror the Compose project and service.

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
  (an unquoted operator is refused), and the first word must be exactly `docker`. Commands run
  exactly as stored: nothing in them is replaced (there are no placeholders), so a command names
  its service literally (`docker compose up -d --no-deps --force-recreate api`) and text in braces
  such as Docker's `--format '{{.Names}}'` templates reaches Docker as written.
- Safety net, checked when the settings are saved and again before every run, with the reason
  shown (a field error on save, the reason of a failed run at deploy time): at most 10 commands
  of at most 500 characters each; `docker compose down`, every `prune` (`docker system prune`,
  `image`, `volume`, `container`, `network`, `builder` and `buildx` prune) and the `-v` and
  `--volumes` options of `rm` are refused. The rules read the parsed words and err on the side
  of refusing: a word `down` after `compose`, a word `prune` anywhere, and `--volumes` or a short
  option holding `v` (`-v`, `-fv`) anywhere after a word `rm` are refused even where a name
  happens to look like one. Every other docker command is allowed.
- Saving changed commands is a review first: the first Save shows the exact arguments each line
  was split into (one chip per argument, so quoting is visible) and stores nothing; Confirm and
  save stores exactly the text that was shown, and editing the commands after the review asks
  again. Saving other settings, or commands that did not change, needs no review. The audit entry
  of a change to the commands keeps the old and the new command text, which only the superadmin
  sees (the audit log, its single entry view and its CSV export leave the two fields out for
  everyone else), like the command itself.
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
