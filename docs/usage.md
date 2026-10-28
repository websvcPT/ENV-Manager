# Using ENV Manager

Creating a deployment, the Overview of a running stack, choosing a deployment path, adding files, deployment files, Compose files, importing from Docker
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
for `${VAR}` values), belong to the deployment rather than to one service. They are listed in the **Deployment files** panel of the deployment's Files tab, below the
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
of the deployment's Settings tab lists them, the base file first, and **View** opens the current text of
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

## Creating a deployment

The superadmin creates a deployment from **New deployment** on a project, in one of two ways that
the tabs at the top of the page switch between.

**Detected stacks** (the default) lists the Compose stacks of the host. The list loads after the
page, because asking Docker and walking the allowed paths can take a few seconds, and **Scan
again** looks once more. A stack is found in two ways, which are merged by its folder:

- Docker, when it is usable: every Compose project the daemon knows, running or stopped, with its
  containers counted (for example "2 running"), the folder it was started from and the Compose
  files it was started with.
- The allowed paths: every folder up to two levels below an allowed path that holds a Compose file
  (`compose.yaml`, `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`) is a stack, with
  the base file and the override file that goes with it. Hidden folders and symbolic links are not
  entered.

When Docker is not usable the page says why and the folders found in the allowed paths are still
listed. Nothing here changes anything on the host.

Each row ends in one of three things. **Use** fills the form below the list. A stack that a
deployment already uses (the same root folder, or the same Compose project name) links to that
deployment instead. A stack that cannot be used says why: its folder is outside the allowed paths,
its Compose files are not inside its folder, or it uses more than 8 Compose files.

**Use** fills the form with a name made from the project (or the folder) name, which can be
edited, the root folder, the Compose files as chips, and the Compose project, which is read only
when Docker named it and empty for a stack found only in a folder. **Import services and
environment files next** is ticked when there are Compose files. **Create deployment** checks
every value again (the root inside the allowed paths, the Compose files inside the root, the
project name rule), stores the deployment, links the Compose files (the audit log holds
`deployment.create` and `deployment.compose_link`) and then opens the Compose import with those
files ticked. Without the tick it opens the deployment's Files tab instead. A deployment created
from **Enter the folder**, the form of a typed folder, also lands on the Files tab.

### The Compose project

The Compose project is the name Docker Compose gives the containers of a stack (the `name:` of the
Compose file, `COMPOSE_PROJECT_NAME`, or the folder name). It is stored on the deployment so that ENV Manager can find the stack's containers in Docker
by that name, which the features that read a stack from Docker need (the state of its containers,
its networks), and it is never made from the deployment's display name. It is optional: it follows Compose's rule, 1 to 63 lowercase letters, numbers, dashes and
underscores, starting with a letter or number, and the Settings tab of the deployment shows it, or
"Not set". A deployment created from a detected stack gets the name Docker reported; for any other
the superadmin sets it in **Edit deployment**. While the field is empty and exactly one Docker
project has its folder at the deployment's root, the form offers **Fill from the running
containers** with that name. The name is in the audit entries of the create and the edit
(`compose_project`).

## The Overview of a deployment

The Overview tab of a deployment is a read only view of the running stack. It needs Docker, and
the deployment needs its [Compose project](#the-compose-project); the page itself is shown at
once and the container section loads after it, so a slow Docker never holds the page. The section
refreshes by itself every 10 seconds while the browser tab is in front, and when a deploy
finishes. A refresh that fails leaves what is shown and tries again at the next one. ENV Manager
asks Docker for the state, health, times, restart count, published ports and addresses of the
containers and for the networks, never for their environment variables, and keeps the answer for
5 seconds, so several viewers cost one set of Docker commands.

The status strip at the top has the state flags, the last deploy, the drift state, the dist
check and the post-deploy outcome, and, once the containers are read, two more cells:

- **Containers** is how many containers run out of all of them ("3 of 4 running").
- **Applied** is how many containers must be recreated to use the files deployed to them ("1 to
  recreate"), or "All applied".

The **Services** table has one row per Compose service and per service stored in ENV Manager,
matched by name (on a phone it is a list with the same facts):

| Column | Meaning |
| --- | --- |
| Service | The name, a link to the service page when ENV Manager stores files for it, with the image of its container under it. |
| Container | The state of the container (running, exited, restarting, paused, created, dead) and, when the image has a healthcheck, healthy, unhealthy or starting. A service with files but no container says "No container". |
| Uptime | A running container shows "Up 3 days" and when it started. A stopped one shows "Exited 2 hours ago" and its exit code. "restarted 4 times" appears when Docker restarted it. |
| Files | The number of files stored for the service. A Compose service with no stored service says "No files in ENV Manager", and the superadmin gets an **Add service** link that opens the form with that name filled in. |
| Last deploy | The outcome of the last deploy of the service, when and by whom. |

**Recreate to apply** is a warning on the Files column. A container reads its `env_file` files
only when it is created, so restarting it never loads a deploy. A deploy that wrote a file (not a
dry run, not an import from Compose, and not a file found identical on disk) marks every
container of the service that was created before the write, whatever the deploy ended as, since
the files are written one by one. The warning shows when the files were written and when the
container was created. A post-deploy command that recreates the container (such as
`docker compose up -d --no-deps --force-recreate <service>`) runs after the write and clears it.

The **Network** panel, for callers who see the whole deployment, lists the networks of the stack
with their driver, subnet and gateway and a note for an internal network ("Internal, no outside
access") or an external one shared with other stacks, then the addresses of each service on those
networks and its published ports. A port badge says which interfaces listen: **All interfaces**
(shown as a warning, since the port is reachable from the network), **This host only** (loopback)
or one address. The panel is left out when there is nothing to show.

A caller with access to some services only sees the rows, the counts and the status of those
services, and no Network panel. The section also says when there is nothing to list: a deployment
with no Compose project name (the superadmin gets a link to the Settings tab, where the field
is), Docker not usable (with the reason, the same as the Post-deploy settings show), and a
project that has no containers on this host.

### Starting, stopping, restarting and recreating a container

Each service row with a container has actions, each behind a confirmation that says what it does,
shows the exact command (one chip per argument, run without a shell) and says that it is
recorded in the audit log:

| Action | Shown for | What it does |
| --- | --- | --- |
| **Restart** | a container that is not stopped | Stops the container and starts it again with the configuration it already has. It does **not** load files deployed since the container was created; the dialog warns about that when the service is marked Recreate to apply. |
| **Stop** | a container that is not stopped | Stops it and leaves it stopped until someone starts it or a deploy recreates it. The dialog warns that services calling it get errors, and its button is the red one. |
| **Start** | an exited, dead or never started container | Starts it with the configuration it already has, and mentions the exit code when it exited. |
| **Recreate** | every container, when Compose files are linked to the deployment | Compose replaces the container (`up -d --no-deps --force-recreate`), so it loads the deployed files. Volumes are kept, and the service is unavailable for a few seconds. It is the main action of a service marked Recreate to apply. |

Recreate forces the replacement. Without that Compose keeps a container whose definition did not
change, and a deploy usually changes only what the definition does not name: the text of a file
the service reads through a mount or an `env_file` it already had in the same state.

Who can use them: the superadmin, and an admin who has a **project or deployment grant** over
the deployment (the grant that covers all of it). An admin whose grants only reach some of its
services or files, and every viewer, see no action and are refused the routes. The actions are
also absent while Docker is not usable or the deployment has no Compose project name.

How they run:

- With Compose files linked to the deployment every action runs on them
  (`docker compose --project-directory <folder of the first file> -p <project> -f <file> ...`),
  so Compose sees the same configuration as your own commands, and Recreate needs them. Without
  linked files Start, Stop and Restart run on the project alone (`docker compose -p <project>
  <action> <service>`), which Compose resolves from the labels of the containers. If the linked
  files no longer resolve (moved, deleted, outside the allowed paths) the actions are refused
  with that reason until they are linked again.
- The service must be a service of the project that has a container, as Docker reports it now.
- An action takes the same lock as a deploy on the deployment folder, so it never overlaps one:
  while a deploy is running it is refused with "Another deploy is running for this deployment
  folder. Try again when it ends." and the other way round.
- Start, Stop and Restart wait at most 60 seconds, Recreate 180 (it may pull an image, with the
  registry logins of Settings, Registries). A command that exceeds that is stopped and reported
  as failed.
- The dialog closes when the answer comes. A toast says what was done, or that it failed with the
  last lines of the output (with the values of the deployment's files and the registry logins
  replaced by `****`), or why it did not run. The Overview then reads the containers again
  at once.
- Every attempt is an audit entry (`container.start`, `container.stop`, `container.restart`,
  `container.recreate`) on the deployment with the service and the exit code, and with the
  reason when it did not run (the result is blocked). The output is never stored or logged.

## The Logs tab of a deployment

The Logs tab follows the output of the containers of the deployment's [Compose
project](#the-compose-project) in the page. It needs Docker, and like the Overview it says so
when the deployment has no project name, when Docker is not usable and when the project has no
container.

Who can open it: the superadmin, and an admin who has a **project or deployment grant** over the
deployment (the grant that covers all of it), the same people as the container actions. An admin
whose grants only reach some of its services or files, and every viewer, see no tab and are
refused the routes. A service row of the Overview that has a container carries a **Logs** action
for those who may open it, which opens the tab filtered to that service.

What the tab shows:

- A dark box with one line per log line: the time (UTC, to the second, with the full stamp as its
  title), the service in a colour of its own, and the message. Lines that say `error`, `fatal`
  or `panic` are tinted red and those that say `warn` or `warning` amber.
- A chip for **All services** and one for each service that has a container; the text filter
  (**Filter lines**, case-insensitive, applied to the lines read after the masking); **Show**,
  the lines of the first read, the last 200 or 1000 of each container; the **Following** and
  **Paused** button; and a download button.
- A footer that says whether the box follows and how many lines it holds. The box keeps its last
  2000 lines; when it follows and you have not scrolled up it stays at the bottom.

How it reads:

- The first read runs `docker compose -p <project> logs --timestamps --no-color --tail <n>`
  (with the service when a chip is chosen) on the project alone, so no linked Compose file is
  needed. Following, the page asks every 2 seconds for the lines after the last one it has, with
  `--since` that moment (the lines stamped with that very moment, which Docker returns again, are
  dropped). Lines from several containers are sorted by their time.
- **Paused** stops the requests altogether: the box keeps what it shows and the button asks for
  the lines since the last one, then follows again. Changing a chip, the filter or **Show**
  reads again from the start. The tab pauses its requests while the browser tab is hidden.
- Hard limits: 1000 lines in one update (when more arrived, the newest are shown and the footer
  says how many older ones were left out), 2000 characters in one line (a cut line ends with
  `[line cut]`) and 4 MiB read from Docker at once (more is a notice, nothing is cut mid read).
- A read that fails (Docker does not answer, the project is gone) is a short notice in the footer
  and the box keeps its lines; the next request tries again.

Masking: every value of every file of the deployment (the newest version of each environment, of
all its services, including files you may not see) is replaced by a **[masked]** chip in the box
and in the download, before the text filter looks at the line and before it is shortened, so
typing a value into the filter finds nothing and a cut never leaves half of one. Values shorter
than 4 characters are not masked (they would match inside ordinary words), and a value that a
container re-encodes (quoted, escaped, hashed, split over lines) is not recognized. A value that
only an older saved version held is not masked either, so a container that was not recreated
after a change can still print its old value in clear. The values
are opened once per request and kept in memory for it.

Download saves the lines of the current chip, filter and **Show** as `deployment-<id>-logs.txt`
(`-<service>` when a chip is chosen), one `stamp service text` per line, with the same masking.
It reads Docker again, so it is the latest lines, not a copy of the box.

What is recorded: opening the logs (the first read of the tab and every download) is an audit
entry `deployment.logs_view` on the deployment with the service filter, the number of lines
asked for and whether it was a download, never the lines or the text filter. Changing a chip, the
filter or **Show** reads again from the start, which is another first read and another entry. The polls that
follow are not recorded. Nothing is stored and no line is written to the application's own log.

## Choosing a deployment path

The deployment form picks the deployment path from a folder browser of the allowed paths. Folders
are sorted by name ignoring case, and the filter field at the top narrows the listed folders as
you type (matching anywhere in the name, ignoring case); Enter opens the folder when only one is
left. Opening a folder clears the filter.

## Importing from Docker Compose

A deployment that has no services yet can get its services and files from its Compose files. The
superadmin starts it with **Import from Docker Compose** on the deployment's Files tab, which is offered
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
   import the Files tab shows a toast with the counts.

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
  earlier from the Files tab.
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

## Signing in with SSO

When a superadmin has switched single sign-on on and set it up in Settings, the sign-in card
shows a primary **Sign in with SSO** link. It is a plain link to `/login/sso`, which sends the
browser to the identity provider (the sign-in attempt is tied to that browser by a short-lived
HttpOnly cookie, so a link started elsewhere is refused) and back to `/login/sso/callback`, where
the session starts exactly as after a password sign in. Two-factor of ENV Manager is not asked on
this path: the provider is the one that checks the person. Signing out ends the ENV Manager
session only, not the one at the provider.

An account is created at its first SSO sign in when the settings allow it, with no grants (a
superadmin gives them afterwards); a person who has no role that maps to a group, or whose
account is disabled here, is refused. The card then shows one fixed sentence for the reason, never
text from the provider, and the audit log records the failed sign in with its reason. Refused SSO
sign ins count on the same per client address lockout as wrong passwords: while the address is
locked, the sign-in link and the callback both answer with the locked message.

**Password sign in** stays behind **Sign in with password** on the card and works for
superadmin accounts only (with their ENV Manager two-factor, if on). The card does not say so,
and any other account that tries it gets exactly the answer of a wrong password, so nobody can
learn who is a superadmin; the audit entry records `password_sign_in_off`, and the attempt counts like a wrong
password. With single sign-on off, nothing changes: every account signs in with email and
password.

**What an SSO account can change.** While single sign-on is on, an account linked to the provider
that is not a superadmin gets its name and email from the provider at every sign in. Its Account
page shows that profile read only with a **From SSO** badge, a **Manage account in SSO** button
to the provider's account page (opens in a new tab), a Password panel that says the password is
not used, and the Two-factor panel with its state, a **Skipped with SSO** badge and its controls
disabled; the routes that change a password or the second factor answer 403 for it. A superadmin
account linked to the provider keeps everything on the Account page and also gets the note and
the button. In the Users list and on the user page a linked account carries an **SSO** badge
(whether or not single sign-on is on), and while it is on the Reset two-factor action is absent
for linked accounts that are not superadmins (its route answers 403). With single sign-on off,
linked accounts have the Account page and the actions of any other account.

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
