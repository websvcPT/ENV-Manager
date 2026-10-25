# Using ENV Manager

Choosing a deployment path, adding files, deployment files, Compose files, importing from Docker
Compose, file types, command line deploys, the drift view, users, accounts and post-deploy commands. API tokens
and the API are in [api.md](api.md).

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

## Deployment files

The files the whole stack reads, such as the `.env` beside the Compose file (which Compose reads
for `${VAR}` values), belong to the deployment rather than to one service. They are listed in the **Deployment files** panel of the deployment page, below the
services. The superadmin adds the first one with **Add file** there; the Compose import adds them
too.

They work like the files of a service (versions, dist comparison, drift, history, grants) with
their own page, reached from the panel, and their own deploy: **Deploy deployment files** writes
them in a run of their own, apart from the services, and runs their own post-deploy command when
one is set (for example `docker compose -f compose.yaml up -d` after the `.env` changed). A file
that is not in KEY=VALUE format is shown and edited as one text. They cannot be renamed or deleted on their own: they go with the deployment. A grant on the project or
the deployment covers them; a grant on a single file covers that file.

From the command line they are deployed as the service named "Deployment files":
`envmanager deploy "Fancy Proj. Name/prod/Deployment files"`. Through the API they are deployed
by the id in the address of their page, like a service.

## Compose files

The Compose files a deployment runs from are linked to it, read only: they usually live in the
project's repository, so the app never versions, edits or writes them. The **Compose files** panel
of the deployment page lists them, the base file first, and **View** opens the current text of
one, read from the host at that moment. It is also on the Deployment files page. Opening a file
is audited (`deployment.compose_view`) and is open to the superadmin and to the admins whose
grants cover the whole deployment, because a Compose file can hold values under `environment:`;
viewers do not see the panel.

The Compose import links the Compose files it read. The superadmin links them later, or changes
them, without importing again: **Link Compose files** (or **Change links**) lists the Compose
files found in the deployment folder and one folder below it, and takes a typed path for another
one; unticking every file unlinks them. A linked path is checked as the import checks it: inside
the deployment folder and the allowed paths, a regular file of at most 1 MiB. A linked Compose
file that the deployment files held as a managed file is removed from them, with its versions;
the file on the host is not touched. A file an API token is restricted to is kept until the token
is revoked. Upgrading to this version removes such managed copies once, at startup, as the
`upgrade` actor.

## Choosing a deployment path

The deployment form picks the deployment path from a folder browser of the allowed paths. Folders
are sorted by name ignoring case, and the filter field at the top narrows the listed folders as
you type (matching anywhere in the name, ignoring case); Enter opens the folder when only one is
left. Opening a folder clears the filter.

## Importing from Docker Compose

A deployment that has no services yet can get its services and files from its Compose files. The
superadmin starts it with **Import from Docker Compose** on the deployment page, which is offered
only while the deployment has no services and lies inside the allowed paths. It is a one time
action: it is refused as soon as the deployment has a service.

1. **Choose the Compose files.** The files named `compose.yaml`, `compose.yml`,
   `docker-compose.yaml` or `docker-compose.yml`, and their `.override` forms, in the deployment
   folder and in the folders directly below it are listed (hidden folders and symbolic links are
   not). Tick the ones to read, or type another path relative to the deployment folder. The first
   file is the base and the ones after it override it, as Compose merges them.
2. **Review the preview.** It lists the services and the files they read, each to tick or untick,
   with the owner service, the file type and an optional dist file per file. Nothing is changed
   yet.
3. **Confirm.** The confirmation scans the files again and says what will be created. After the
   import the deployment page shows a toast with the counts.

What is found:

- The services of the merged Compose files, including those under a profile. A service name is
  made a lowercase slug, and a service whose name cannot be used (or repeats another one) is
  listed as not importable.
- The files a service reads: each `env_file` entry, a bind mount of a single file, and a secret or
  a config that has a `file`. A bind mount whose source does not exist yet is offered when a dist
  file sits beside it, or, unticked and marked "looks like a file", when its name or the path it
  is mounted at reads like a file name (`.env`, `.env.api`, `nginx.conf`). A folder, a socket,
  another path that does not exist and a file outside the deployment folder or the allowed paths
  are listed as not offered or disabled with the reason, and are never read.
- The deployment environment file (the `.env` beside the base Compose file, which Compose reads
  for `${VAR}` values), offered ticked in a **Deployment files** section when it exists or has a
  dist file and no service names it itself. It goes to the deployment files, which are deployed
  apart from the services with a post-deploy command of their own. The Compose files themselves
  are not offered: the import links them to the deployment, read only (see Compose files above).
- A file used by several services belongs to the owner you choose (the first service in
  alphabetical order by default).
- A file name ending in `.pem`, `.crt`, `.cer` or `.key` is offered as a certificate and any other
  as an ENV file. A type the text does not fit (a certificate without a PEM block, for example) is
  disabled in the type select and the file starts unticked. The type cannot be changed after the
  import.
- While the Docker socket is usable, a post-deploy command per service
  (`docker compose -f <base> [-f <override>] up -d --no-deps <service>`) and one for the
  deployment files (`docker compose -f <base> [-f <override>] up -d`), unticked and editable.
  Only a ticked command is stored, after the safety net accepts it, with the defaults of the
  settings form (the deployment folder as working directory, a 120 second timeout, run only when
  a file changed); the confirmation shows the arguments each line is run with. Without a usable
  socket the preview says why no command is offered.
- A sibling named `<name>-dist`, `<name>.example` or `<name>.dist` is offered as the dist file,
  whether the file exists or not, and the link starts ticked. For a file on disk version 1 is its
  own text and the link only turns the dist comparison on (under the Block policy a dist key the
  file lacks blocks its deploy until it is added). For a file that is not on disk yet "Start from
  the dist file" starts ticked too: version 1 then holds the dist file's text instead of being
  empty. Starting from the dist file needs the link.

What the import creates, in one transaction (all or nothing):

- The ticked services and files, each file with the mode, owner and group it has on the host
  (owner and group are kept only when they differ from the deployment defaults and the app user).
  A file that exists becomes version 1 with its exact text (comment "Imported from Docker
  Compose"); a file that does not exist yet becomes version 1 with its dist file's text when it
  starts from it (comment "Created from the dist file by the Docker Compose import"), else an
  empty version 1 (comment "Created empty from Docker Compose"). Neither is written to the host by
  the import: the first deploy writes it. A certificate cannot be created empty.
- The deployment files are created when one of their files is ticked, or keep the ones made
  earlier from the deployment page.
- For each imported service that had files on the host, one run in Deploy history with the
  trigger shown as **Imported from Compose** and an unchanged result per file. It records version
  1 as the content the file held, which is the starting point of the drift check: a version 2
  saved before the first deploy deploys without a drift halt under the default Halt policy, while
  a file edited on the host after the import still halts it. The run is not a deploy: nothing is
  written, no post-deploy command runs, no webhook is sent and the metrics do not count it, but it
  is the last deployment of the service until a real one follows. The API reports it with
  `trigger` set to `compose`.
- The Compose files are linked to the deployment, and one `deployment.import_compose` audit entry
  lists the choices and what was skipped, never a file's text. Each stored command has its own
  `service.hook_save` entry, whose text only the superadmin sees.

How the files are read, and the limits:

- The real `docker compose config` reads them, with no shell, a minimal environment, no Docker
  daemon or socket and a 30 second limit. Relative paths resolve against the folder of the base
  file. Variables are filled in from the project `.env` file only: a variable that is set only in
  the shell of the operator reads as empty, and a required variable (`${NAME:?message}`) that is
  missing stops the scan with a message naming it.
- Only the structure is kept: paths, names and counts. Values from `environment`, images and
  ports are dropped, and Compose's own messages are never shown, because they can quote a file.
- Every file named by an `include` (all its forms, with its `env_file` and `project_directory`)
  or by an `extends` file, at any depth (8 levels, 50 files), must be a local file inside the
  deployment folder and the allowed paths, symbolic links followed. A remote source (git, http or
  OCI) and a path that holds a variable (`$`, which Compose would expand) are refused, naming the
  entry, before Compose runs.
- At most 8 Compose files of up to 1 MiB each are read. A file to import must be UTF-8 text no
  larger than a file added by hand, and the app must be able to read it.
- An app that does not run as root cannot give a file to another account, so a deploy of an
  imported file whose owner is another account is refused with the usual owner message.

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
  deploy dialog). History and the drift view compare it line by line with the lines aligned
  (as `diff` does), each row named by its line number (`Line 3`), never by content; a line added
  or removed in the middle is one added or removed row and the lines after it stay unchanged.
  A save, restore, upload, import or drift import must contain at least one PEM block
  (`-----BEGIN ...-----` to `-----END ...-----`), otherwise it is refused. The output of a
  post-deploy command has every base64 line of the stored certificate replaced by `****`.

The add form selects Certificate by itself while a file name ending in `.pem`, `.crt`, `.cer` or
`.key` is typed, until you choose the type yourself. The type shows in the Details panel of a
file and under the file name in the environments table of the service page, and the API answers
carry it as `file_type`.

## Deploying from the command line

```sh
docker compose exec app envmanager deploy <project>/<deployment>/<service> \
    [--env <file name> ...] [--dry-run] [--force]
```

Project and deployment names are display names (letters in any case, numbers, spaces and
`. - _`, with at least one letter or number and repeated spaces made one; 3 to 100 characters for
a project, unique ignoring case, and 1 to 100 for a deployment, unique in its project ignoring
case), so quote the path when it holds spaces:
`envmanager deploy "Fancy Proj. Name/Web EU/web"`. Service names stay lowercase slugs (letters,
numbers and dashes), as they usually mirror the Compose service.

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
  happens to look like one. `docker login`, `docker logout` and the global `--config` option
  (`docker --config <folder> ...`) are refused too, because the registry logins are the app's own
  (see [Registry logins](#registry-logins)); the message points to Settings, Registries. Every
  other docker command is allowed.
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
- The command gets a minimal environment (`PATH`, `HOME`, `LANG`, `DOCKER_HOST` pinned to the
  checked socket and `DOCKER_CONFIG`, a private folder of that run that holds the stored registry
  logins and is removed afterwards, see [Registry logins](#registry-logins)), never the
  environment of the app, so the master key, the CSRF secret and the error reporting DSN do not
  reach it.
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

### Registry logins

A private image needs credentials, and the Docker daemon keeps none: the `docker` command sends
the ones it reads from its config folder. The app starts every command with a config folder of
its own, so a command such as `docker compose up -d --no-deps api` would fail with `unauthorized`
on an image from a private registry. The superadmin stores the logins in Settings, Registries
(see [Configuration](configuration.md#settings)), and the app makes them available to every
command it runs.

- **How a login reaches a command.** For each run of a service's commands the app makes a new
  private folder (readable by the app's user only), writes the stored logins into it as a Docker
  `config.json`, points `DOCKER_CONFIG` at it and removes the folder when the run ends, however it
  ended (success, failure, timeout or cancellation). The folder holds every stored login and
  Docker sends a registry only its own entry, so one service can pull from several registries.
  With no login stored nothing is written. The token is sealed in the database
  (see [Operations](operations.md#key-rotation)) and opened in memory for the run only. A token
  that cannot be opened (a different master key) fails the post-deploy step with a message that
  asks for it to be entered again in Settings, Registries.
- **Which account to use.** Best is a pull-only bot account: a service or robot account of the
  registry whose only permission is to read the images the deployments use, so a leaked token can
  pull and nothing else. Next is a read-only personal access token (a scope such as read packages
  or read registry), which stops working when the person leaves or revokes it. A personal
  password is the last choice: it opens everything that person can reach, including writing, and
  it changes whenever they change it. The app stores them all the same way (a username and a
  secret), so the choice is yours to make well.
- **Not supported.** Registries that only issue short-lived tokens through a credential helper,
  such as AWS ECR or `gcloud` for Google Artifact Registry, because the stored login is a fixed
  username and token and the app does not run credential helpers.
- **The safety net.** `docker login`, `docker logout` and `--config` are refused when the
  commands are saved and again before a run, so a command can neither replace the logins of the
  app nor write a credential of its own.
- **Output.** Every stored token, and its base64 `user:token` form that a Docker config file
  holds, is replaced with `****` in the output of a run before it is stored or shown, like the
  managed values. A token shorter than four characters is not scrubbed by itself (its base64 form
  is), like any value that short.
- **Test login.** The button of a row in Settings, Registries runs `docker login` for that one
  login and says whether the registry accepted it, with the registry's own reason when it did not
  (for example `401 Unauthorized`).
