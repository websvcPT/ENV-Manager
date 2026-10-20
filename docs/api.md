# API tokens and the API

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
    {"id": 12, "name": ".env.api", "file_type": "env", "version": 3, "status": "halted",
     "dist": "passed", "drift": "drift",
     "message": "The target changed outside the app and the drift policy halts."}
  ],
  "post_deploy": {"status": "skipped", "exit_code": null,
                  "reason": "Not run because an environment was not deployed."}
}
```

- `status`: `success`, `halted`, `blocked`, `failed` or `post_deploy_failed`. A halted or blocked
  deploy is a result (200), not an error.
- per file `file_type`: `env` or `cert`; `status`: `written`, `unchanged`, `would_write`, `blocked`,
  `halted` or `failed`; `dist`: `passed`, `warning`, `blocked`, `skipped` or `failed` (null when no
  check ran; a certificate is always `skipped`); `drift`: `in_sync`, `drift` or `missing` (null
  when the file was not examined); `message` is a fixed sentence about the outcome.
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
| 429 | `rate_limited` | The client address is locked after failed token attempts; `Retry-After` says for how many seconds. Only the first request refused in a lockout window is audited (`token.auth_failure`, result blocked); the later ones are counted and answered without an entry. |
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
