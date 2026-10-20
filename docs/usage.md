# Using ENV Manager

Adding files, file types, command line deploys, the drift view, users, accounts and post-deploy
commands. API tokens and the API are in [api.md](api.md).

## Adding a file

The superadmin adds a file to a service from its page (Add environment). The form takes the
service (or a new one), the file name, its type, the target path below the deployment root, an
optional dist file path, the mode, owner and group, and where version 1 comes from:

- **Paste text** or **Upload a text file** (UTF-8, up to 256 KiB).
- **Import the current target file** reads the file that is already at the target path.
- **Copy the dist file** reads the dist file of the dist path field and saves it as version 1
  (comment "Initial content (copied from the dist file)"). It is available while the dist path
  field holds a path, and not for a certificate.

The target and dist paths are read through the same confined reader as a deploy: they must stay
inside the deployment root and the allowed paths.

## File types

Every file has a type, chosen when it is added and never changed afterwards: the file settings show
it as plain text and the server refuses a different type. To change it, delete the file and add it
again:

- **ENV file** is KEY=VALUE text. It has the key and value editor and the raw text editor, is
  compared with its dist file key by key, and history, drift and deploy output scrubbing work per
  variable.
- **Certificate** is PEM text: a certificate, a key or a chain. It is edited and revealed as raw
  text only, as one value. It has no dist file: the field is hidden, a dist path is refused and
  the dist check reports the file as not applicable (also in the deployment overview and the
  deploy dialog). History and the drift view compare it line by line, each row named by its
  position (`Line 3`), never by content; a line added or removed in the middle shifts the lines
  after it. A save, restore, upload, import or drift import must contain at least one PEM block
  (`-----BEGIN ...-----` to `-----END ...-----`), otherwise it is refused. The output of a
  post-deploy command has every base64 line of the stored certificate replaced by `****`.

The add form selects Certificate by itself while a file name ending in `.pem`, `.crt`, `.cer` or
`.key` is typed, until you choose the type yourself. The type shows in the Details panel of a
file and as a column of the service page, and the API answers carry it as `file_type`.

## Deploying from the command line

```sh
docker compose exec app envmanager deploy <project>/<deployment>/<service> \
    [--env <file name> ...] [--dry-run] [--force]
```

A project name is a display name (letters in any case, numbers, spaces and `. - _`, 3 to 100
characters with at least one letter or number, repeated spaces made one, unique ignoring case),
so quote the path when it holds spaces:
`envmanager deploy "Fancy Proj. Name/prod/web"`. Deployment and service names stay lowercase
slugs (letters, numbers and dashes), as they usually mirror the Compose project and service.

Deploys the service as the actor `cli`: every file of the service, or the named ones. The command
prints one line per file and exits 0 only when every file was written or found unchanged and the
post-deploy step did not fail; a blocked, halted or failed file (or a deploy refused because
another deploy holds the lock of the same deployment folder) exits 1. `--dry-run` runs the same
checks without writing or backing anything up, `--force` overrides missing-key blocks and drift
halts and is audited. A drifted file that a forced deploy replaces is backed up first, next to it
(`<name>.bak-<UTC stamp>-<4 hex>`, mode 0600). After a deploy that wrote a backup, only the newest
five backups of that file are kept: older ones of exactly that name pattern, plain files with mode
0600, are deleted (a file with a similar name, a symlink or another mode is never touched) and the
result says how many went. A dry run, a blocked, halted or failed file prunes nothing.

## Drift view

A file whose target changed outside the app has a drift view at `/environments/<id>/drift` (the
Halted result of a deploy links to it). It reads the target from the host when the page is opened
and shows it next to the last deployed version and the managed version, masked and compared key by
key; revealing values is an audited request per column and they disappear after 30 seconds.
Importing the target saves the file as it is on disk as a new version (refused when the file or
the managed version changed since the page was opened). Overwriting the target is a forced deploy
of that one file after a confirmation: the file it replaces is copied next to it first (mode 0600),
whatever the drift policy says, and the post-deploy command of the service runs as for any deploy.

## Users

The email address is the unique key of a user (unique ignoring case) and the only thing that
signs a person in: the sign-in field reads Email, and a name typed there never matches a user,
whatever the password, and fails like any wrong password. The name is a display name such as
`John Doe`: trimmed, runs of spaces made one, 1 to 100 characters, letters of any language,
digits, spaces and the characters `.`, `'` and `-`, no control or format characters. Names are
not unique, so wherever users are listed or picked (the users list, the user page, the access
grid, the Access tabs and their add dialog, the token owner column and the audit log filter) the
email shows next to the name, and filters and pickers key on the user id.

The first superadmin and every account made from the command line use the email, which the
other user commands take as their argument too:

```
docker compose exec app envmanager user create --email jane@example.com --name "Jane Doe" --group superadmin
docker compose exec app envmanager user list
docker compose exec app envmanager user disable jane@example.com
docker compose exec app envmanager user enable jane@example.com
docker compose exec app envmanager user passwd jane@example.com
docker compose exec app envmanager user reset-2fa jane@example.com
```

The development dataset (`envmanager dev seed`, `make seed`) gives every seeded user the address
`<name>@example.test`, so the superadmin signs in as `root@example.test` (see
[development.md](development.md)).

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
detail page (after a confirm dialog) or `docker compose exec app envmanager user reset-2fa <email>`
turns the second factor off, ends that user's sessions and sign ins waiting for a code, and is
audited as `user.totp_reset` (counts only). The web action refuses a superadmin's own account,
which has the Remove button on its Account page.

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
and a save is refused. The status shown on the service page, in its post-deploy panel and in
the Post-deploy tab comes from a probe at most five seconds old, so it can lag a change of the
daemon by that long; saving a command, the Test command button, the Post-deploy switch and the
check right before a command runs always probe the daemon.

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
