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
| `make e2e-demo` | Runs `make demo-up` and `make demo-reset`, then the real-scenario tests in `app/real_scenarios` inside the app container, then `make demo-reset` and `make demo-down`, whether or not the tests pass. |

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
halts and is audited.
