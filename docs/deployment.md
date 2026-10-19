# Deployment and network configuration

## Deployment

The application is one image. [`deploy/docker-compose.yaml`](../deploy/docker-compose.yaml) is the
example to copy: one service (`envmanager`) with the image, the key file, the data volume, the
host folders the deployments live in and the port on the loopback address. It reads its settings
from `.env` next to it, which you make from [`deploy/.env.example`](../deploy/.env.example). Every
setting is described in the
[Environment variables table of the README](../README.md#environment-variables).

1. Pull or build the image. The published image is `websvcpt/env-manager:latest` on Docker Hub
   (every release is also tagged with its version). From a checkout, `docker build --target
   runtime -t envmanager:1 .` builds the production image (the `runtime` stage of the
   `Dockerfile`; the last stage, `test`, is only for the test run); put the name you used into
   `image:`.
2. Make the folders and the master key, and keep a copy of the key apart from the data and from
   every backup:

   ```
   mkdir -p data/runtime
   openssl rand -base64 32 > data/master.key
   chmod 600 data/master.key
   ```

   The sample mounts `data/runtime` at `/data` and the key read-only at `/run/keys/master.key`,
   and its `environment:` points `ENVMANAGER_KEY_FILE` at it. The key is not inside
   `data/runtime`, so a copy of that folder or of a backup never carries it, but it sits beside
   it in `data`: a copy of the whole `data` folder carries both, so back the key up on its own.
   Store another copy in a password manager or secret store. Without it the data cannot be read
   (see [Backup and restore](operations.md#backup-and-restore) and
   [Key rotation](operations.md#key-rotation)).
3. Copy `deploy/.env.example` to `.env` next to the compose file. Set `ENVMANAGER_BASE_URL` to the
   address people open and `ENVMANAGER_CSRF_SECRET` to a secret of at least 32 characters (for
   example `openssl rand -hex 32`), and uncomment the optional settings you need, such as
   `ENVMANAGER_TRUSTED_PROXIES`. Mount the host folders that hold the deployments
   (`/var/www/vhosts` and `/opt` in the sample) at the same path as on the host. Mounts are
   read-write; the allowed path list in Settings decides which of them the application may use.
   The sample publishes the port on the loopback address only (`127.0.0.1:8080:8080`): put the
   reverse proxy that ends https on the same host, or change the binding on purpose.
4. Start it with `docker compose up -d`, create the first superadmin with `docker compose exec
   envmanager envmanager user create --email you@example.com --name "Your Name" --group
   superadmin` (see [Users](usage.md#users)), and sign in at the base URL. Until the superadmin
   adds an allowed path in Settings a banner says so and nothing can be deployed.

**Why the container runs as root.** A deploy writes files into the deployment folders and gives
them the mode, owner and group of their deployment. Changing the owner of a file, and replacing a
file that belongs to another user, needs root, and the Docker socket (post-deploy commands) is
root equivalent on the host anyway, so a non-root user would add little protection and break the
owner and group fields. The image does not create another user. Root also reads the socket
without any group, so `group_add` in the sample is needed only if you override the user of the
container; then give it the group id of the host's `docker` group.

**Owner, group and mode of what a deploy writes.** A file a deploy creates is mode 0600 and owned
by root unless the deployment or the environment names an owner and a group (a user name or a
numeric id, and a group name or id). A container that Compose starts as another user cannot read
a root owned 0600 file. Set the owner and group of the deployment to the account that container
runs as, or give the file a wider mode, so the file is readable where it is used. The numeric ids
must match the ones inside that container, because names are looked up in this container.

**Base URL and cookies.** The session cookie is sent with the `Secure` flag only when
`ENVMANAGER_BASE_URL` starts with `https://`. Behind a reverse proxy that ends https, set an
https base URL and list the proxy in `ENVMANAGER_TRUSTED_PROXIES` (see
[Network configuration](#network-configuration)), so the scheme is read from `X-Forwarded-Proto`.
With an `http://` base URL outside development the application logs a warning at startup, because
the cookie then has no `Secure` flag.

**Docker socket.** Mounting `/var/run/docker.sock` turns post-deploy commands on; without it the
feature is off. The socket gives root on the host to whoever can reach the application with a
deploy right, so mount it only when you need the commands.

**Upgrades and the automatic backup.** Pull or build the new image and run `docker compose up -d`.
When the new version changes the database, the application takes a backup into `/data/backups`
before it migrates (see [Backup and restore](operations.md#backup-and-restore) for the restore
steps). Take a manual backup before an upgrade you care about.

**One process.** The scheduler and the deploy locks live in the process, so run exactly one
container per data folder and one worker. A second server on the same folder is refused at
start.

**Limits.** Post-deploy output is scrubbed of the managed values, but values shorter than 4
characters are not scrubbed (the output would turn unreadable); keep secrets longer than that.

## Network configuration

Startup configuration comes from environment variables only. Every value is checked when the
application starts, and an invalid one stops it with a message that names the variable and the
entry. Entries are IPv4 and IPv6 addresses or networks in CIDR form, separated by commas; an
IPv4-mapped IPv6 address (`::ffff:10.0.0.5`, what a dual stack socket reports for an IPv4
client) counts as the IPv4 address it carries, and a network with host bits set
(`10.0.0.5/24`) is refused instead of widened.

Two settings shape it, `ENVMANAGER_TRUSTED_PROXIES` (the peers whose `X-Forwarded-For` and
`X-Forwarded-Proto` headers are believed) and `ENVMANAGER_ALLOWED_SOURCES` (the client addresses
that may use the application at all); their defaults and formats are in the
[Environment variables table of the README](../README.md#environment-variables). `*` and networks
that hold every address (`0.0.0.0/0`, `::/0`) are refused as trusted proxies: trusting every peer
would let anyone choose the address that lockouts and the allowed sources rely on. A client
outside the allowed sources gets a bare `403` (JSON in the shape of the API under `/api`).

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
  before that (a trusted hop wrote something unreadable, or a trailing comma) the client becomes
  the address of the trusted hop that wrote it (the proxy you connect to when its right-most
  entry is the unreadable one), so the sign-in and API counters, lockouts, logs and audit entries
  always have an address, and one warning line per proxy address names that proxy (never the
  unreadable text). The request is marked as having an unknown client: with allowed sources set
  it is refused, because the proxy's own address says nothing about where the client is. Fix the
  proxy so it writes valid addresses.

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
value and the Security tab shows the field fixed (see [Settings](configuration.md#settings)).
