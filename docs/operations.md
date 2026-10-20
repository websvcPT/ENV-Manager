# Operations

Backup and restore, key rotation, git sync and notifications.

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
[Key rotation](#key-rotation)).

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
  [Notifications](#notifications)).
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
(the SMTP password, the webhook address and secret, the git deploy key and token), and it keys the hashes that
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
   valid key file is used as it is and may be mounted read-only. The folder must be writable by the
   container (it runs as root) when the file is to be created.

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
- It takes the backup lock for the transaction below, so a `backup create` or `backup restore`
  started in another container at the same moment is refused instead of meeting a half moved
  database, and the rotation itself is refused when one of them holds the lock.
- In one transaction it seals every value again with the new key and recomputes every keyed hash:
  the content of every environment version and its hash, the second factor secrets, every secret
  setting, the hashes of deploy results (the baselines of the drift checks) and the key check
  record. Every value is opened with the new key before the transaction is committed, and the audit
  entry `key.rotate` (counts and the name of the backup, never key material) is part of it. Any
  failure rolls everything back and the database is as it was; the new key file stays and the next
  run uses it.
- Superadmins get the key rotation notification (switch "Master key rotated" in the Notifications
  tab, on by default), sent through the new key because the mail settings are sealed with it now.

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
  `backup restore <file> --key-file <old key file>` (see [Backup and restore](#backup-and-restore)).
  Keep each old key in your secret store, apart from the backup files and labelled with the
  backups it opens, and do not delete an old key while a backup or an exported commit that needs
  it is kept.

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
the master key, so keep that key apart from the repository (see
[Backup and restore](#backup-and-restore)).

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
- **Credentials.** The deploy key (an unencrypted private key, pasted into the multi-line field;
  a key with a passphrase is refused on save, whether it is in the OpenSSH or the PEM format)
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
  address or a secret), announced as a Git push failure notification (see
  [Notifications](#notifications)) and shown as the banner "Git push failing" on every superadmin
  page until a push works. The next attempt waits an hour after the first failed run, then two,
  four, up to a day, and a save of changed git settings gives it a fresh start. Every failed run
  is audited, but the notification goes out at the first failure of a streak and then at most
  once a day while the failure lasts, and one more notice ("Git push works again") when a push
  works after a streak that was announced. The banner does not change.
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
  two-factor sign-in was reset, a failed or not copied backup, a failed git push (see
  [Git sync](#git-sync)) and a master key rotation (counts and the person or command that ran it,
  never key material).
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
  create`, `disable`, `passwd` and `reset-2fa`, `deploy` (a blocked, halted or failed result),
  `backup create` (a failed or not copied backup) and `key rotate`. The command process cannot leave work behind, so
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
- **The body.** The fields of the API's deploy answer (see [Answer](api.md#answer)) for the whole
  run, every file it touched whoever may see it, plus `event` (`deploy.finished`), `event_id`,
  `project` and `deployment`. Never a value, a file's content, a host path, a command or a
  command's output.

  ```json
  {
    "run_id": 41,
    "service": {"id": 7, "name": "api"},
    "dry_run": false,
    "status": "success",
    "started_at": "2026-10-18T12:00:00.123456Z",
    "finished_at": "2026-10-18T12:00:01.234567Z",
    "environments": [
      {"id": 12, "name": ".env.api", "file_type": "env", "version": 3, "status": "written",
       "dist": "passed", "drift": "in_sync", "message": null}
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
  private ones included, because the receiver is often inside the same network. The address and
  the shared secret are both encrypted in the settings store like the SMTP password (chat tools put
  their credential in the address), and neither is put back into the page: the address field shows
  the scheme and host of the stored address as its placeholder and nothing more, an empty field
  keeps what is stored, and a checkbox that appears while one is stored removes it. The shared
  secret has at least 16 characters. The switch can only be on while an address and a secret are
  set. The settings audit entry names the two settings that changed and nothing of their values;
  deliveries and the test event record the host only. A database from an earlier version that holds
  the address as plain text has it sealed in place when the application starts (or when such a
  backup is restored), and the plain text is gone from every table afterwards.
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
