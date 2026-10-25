# Configuration and observability

## Settings

The superadmin opens Settings from the sidebar (`/settings`, which opens the first tab). Every
other group gets 403 and no link. A save is validated on the server, written with one audit entry
that names every changed setting with its old and new value (a secret is never recorded, only
that it changed) and applies to the next request: the settings are read per request, so nothing
needs a restart. Settings never replace startup configuration: the master key, base URL, CSRF
secret, trusted proxies and allowed sources stay in environment variables (see
[Deployment and network configuration](deployment.md)).

| Tab | Content |
| --- | --- |
| Allowed paths | The list with mounted and writable status and the deployments under each entry, add with a live check, remove behind a confirm dialog. |
| Security | Idle and absolute session timeouts, failed logins before lockout per user, per address and for the API per address, lockout duration, minimum password length, and the two-factor policy (Optional, shown read-only). |
| Post-deploy | The Docker socket status and the switch for post-deploy commands. |
| Registries | The Docker registry logins post-deploy commands pull private images with (host, username, token): add, edit and remove in dialogs, and Test login. |
| General | Time zone and date format, audit retention, the defaults of a new deployment, and the read-only diagnostics. |
| Backup | Schedule, retention, an optional extra destination, the list of backups with Run now, Download and a confirmed Restore (see [Backup and restore](operations.md#backup-and-restore)). |
| SMTP | The mail server (host, port, TLS mode, username, password, from address) and a Send test mail button (see [Notifications](operations.md#notifications)). |
| Notifications | The event matrix: a switch for each event and who receives the events about a project, and the deploy webhook (address, shared secret, switch and Send test event; see [Notifications](operations.md#notifications)). |
| Git | The private repository the encrypted export is pushed to: address, branch, commit author, export format, deploy key or token, the push switch, and Test connection with the SSH host key (see [Git sync](operations.md#git-sync)). |

- **Allowed paths.** An entry is checked when it is added: absolute and normalized, an existing
  folder inside a host mount, never `/` or the data volume, and not already in the list (compared
  on the real path, so a symlink to an entry is the same entry). The real path is stored. An entry
  on a read-only mount is accepted and shown as Read-only. A stored entry whose folder is gone
  reads Missing, one that now resolves elsewhere reads Moved and is ignored until it is added
  again. Removing an entry deletes nothing on disk; the confirm dialog counts the deployments no
  other entry covers, which are flagged as outside the allowed paths. Every add and remove is
  audited and announced to the superadmins as a system notification (see
  [Notifications](operations.md#notifications)).
- **Security.** Idle and absolute timeouts, lockout limits and the password length apply to the
  next request; a changed absolute timeout also recomputes the expiry of the sessions that are
  already open. The failure window (15 minutes) is not a setting. The sign-in limit per address
  can be fixed by `ENVMANAGER_LOGIN_IP_MAX_FAILURES` (development and testing only): while it is
  set its field shows that value and cannot be edited, and the stored value is kept.
- **Post-deploy.** The switch defaults to on. It can only be changed while Docker is usable. With
  it off the deploy engine skips a service's stored command, records Skipped with the reason
  "Post-deploy commands are switched off in settings.", and the deploy dialog preview and the
  service page say the same. Force does not override it.
- **Registries.** The logins that let a post-deploy command pull images from a private registry,
  one list for the whole app and one login per registry host (Docker sends each registry only its
  own entry, so deployments that pull from several registries work with the one list). A login is
  a host, a username and a token or password. The host is typed as `registry.example.com`, with a
  port when it has one (`127.0.0.1:5000`), and stored in lowercase without `https://` or `http://`
  and a trailing slash; a path is refused, and `docker.io`, `index.docker.io` and
  `registry-1.docker.io` all mean Docker Hub. The token is required when a login is added (at most
  4096 characters), is sealed with the master key like the other secrets and is never shown
  again: the edit dialog leaves the field empty and an empty token keeps the stored one. Adding,
  changing and removing a login is audited (`registry.add`, `registry.update`, `registry.remove`)
  with the host and the username and, for a change, only whether the token changed. Test login
  runs `docker login <host>` through the Docker socket in a private config folder that holds that
  one login, with no standard input and a limit of 30 seconds, and shows OK or Docker's own
  refusal with the token scrubbed out of it; it needs a usable Docker (socket, daemon and compose
  plugin, as for the Post-deploy tab), otherwise the action is absent and the panel says why.
  Every test is audited as `registry.test` with the host, the username and the result, never the
  output. Removing a login deletes nothing on the host: the images already pulled stay, and the
  next pull of a private image fails with `unauthorized` until the login is added again. How the
  logins reach a command is described under
  [Post-deploy commands](usage.md#post-deploy-commands).
- **General.** The time zone and date format apply to the times on the audit log, project
  activity and user activity (stored times stay UTC; the audit date filters are days of that zone;
  the CSV export stays UTC). The audit retention (90 days by default, 0 keeps every entry) is
  applied by a daily job at 03:30 UTC that deletes the entries older than that in batches and
  writes one `audit.purged` entry with the count and the cutoff. The defaults (missing dist keys,
  drift, missing target, file mode, owner and group) pre-fill the new deployment form; a
  deployment keeps its own values afterwards.
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
template, the status; never a request body or a header). The raw URL path and the query string
are replaced on every request span (`url.path` and `url.query` read `[redacted]`) because they can
hold an environment key name (the reveal routes), the project search text and the host path being
typed in the live path checks; the route template (`http.route`, for example
`/environments/{environment_id}/reveal/{key}`) stays, so a trace still shows which screen was
served. When traces are exported the application owns the tracer provider and sets it as the
global one; its resource, sampler and exporter still come from the standard variables below.
Values, passwords and tokens travel in request bodies and headers and are never in a URL.
Besides those, these spans, which carry ids, names, counts and results only (no
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
`traceparent` only when the peer is one of `ENVMANAGER_TRUSTED_PROXIES`; the headers
`traceparent`, `tracestate` and `baggage` of every other peer are dropped before the span starts,
so the request begins a trace of its own and the id on its audit entries is never one a client
chose.

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
