# URLy

A small URL shortener: paste a long link, get a short one, open it and get redirected.
Python API + Postgres + a static web page, containerized, deployed to AWS with Terraform
and GitHub Actions.

> Sections still to come as the project is built: build and deploy, key technical
> decisions, behavior under load, failure drills, known limitations and improvements.

## Run locally

Requirements: Docker (with Compose v2). Nothing else is needed to run the app.

```bash
cp .env.example .env          # local settings; .env is git-ignored
docker compose up --build     # builds images, starts everything
```

Open <http://localhost:8080>, paste a link, click **Shorten**, then open the short link.

What starts, in order: `db` (Postgres) -> `migrate` (creates the table, then exits) ->
`api` (2 replicas) and `ui` -> `gateway` (nginx, the only service with a published port; it
plays the role the load balancer plays on AWS).

```bash
docker compose down           # stop; links are kept (named volume)
docker compose down -v        # stop and wipe all links
docker compose logs -f api    # follow API logs
```

Try it from the command line:

```bash
curl -s -X POST localhost:8080/api/links -H 'content-type: application/json' \
  -d '{"url":"https://example.com/some/long/path"}'
curl -i localhost:8080/<short_code>        # 302 with the original in Location
```

### Run the tests

The tests use a real Postgres:

```bash
docker run -d --name urly-test-db -e POSTGRES_USER=urly -e POSTGRES_PASSWORD=urly-test \
  -e POSTGRES_DB=urly_test -p 5432:5432 postgres:17
cd api && uv run pytest
```

### Configuration

All settings are environment variables (see `.env.example`): `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD` and `BASE_URL` (the public address used to build short links).
There are no secrets in the repo or in the images; locally they come from `.env`, on AWS from
Secrets Manager.

## Observability

**Health endpoints**

| Endpoint | Meaning | Touches the DB | Used by |
| -- | -- | -- | -- |
| `/healthz` | the process is alive | no | ALB target group, compose healthcheck |
| `/readyz` | the API can do its job | yes (`SELECT 1`, 2s limit) | humans, post-deploy smoke test |

The load balancer checks `/healthz`, not `/readyz`: a health check should fail only when
restarting the container would fix it. A database outage must not make the load balancer drop
every container (users would get a bare 503) or make ECS replace healthy ones in a loop.
During a DB outage the API answers `503` with `Retry-After: 5` and recovers on its own.

**Logs**: one JSON line per request on stdout (`docker compose logs api` locally, CloudWatch
Logs on AWS, 14-day retention). Fields: `timestamp`, `level`, `method`, `path`, `status`,
`duration_ms`, `request_id` (the ALB's `X-Amzn-Trace-Id`), `short_code` on redirects and an
`event` for notable cases: `link_created`, `code_collision`, `db_error`, `unhandled_error`.
The Original URL is never logged, and error messages and tracebacks are scrubbed because
Postgres can include the failing row in an error. `/healthz` is not logged (the ALB calls it
every few seconds).

**Metrics and alarms** (CloudWatch only, no `/metrics` endpoint): ALB and ECS metrics come for
free; metric filters count the `db_error`, `code_collision` and `link_created` events;
alarms on 5xx, latency, unhealthy targets and `db_error` are defined with Terraform.
