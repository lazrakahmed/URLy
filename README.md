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

## Build and deploy

The app runs on AWS in `eu-north-1`: an Application Load Balancer in front of two ECS Fargate
services (`api`, `ui`, 2 tasks each, one per availability zone), Postgres on RDS in private
subnets, images in ECR, CloudWatch for logs, metrics and alarms. Everything is Terraform.

```
internet -> ALB (HTTP :80) -> /, /app.js, /style.css -> ui tasks
                           -> /api/*, /{code}        -> api tasks -> RDS (private subnets)
```

Terraform is in two parts:

| Folder | What it holds | Lifetime |
| -- | -- | -- |
| `infra/bootstrap/` | state bucket (S3), ECR repositories, CI deploy role and the IAM user that can only assume it | created by hand once, removed at teardown |
| `infra/` | VPC, ALB, RDS, ECS, IAM, log metric filters, alarms, dashboard | created and updated by the pipeline |

### First deployment (by hand, once)

A pipeline cannot create its own credentials, the state bucket or the image registry, so the
very first deployment runs from a laptop with an AWS admin session:

```bash
aws login                      # an AWS session with admin rights in the target account
scripts/first-deploy.sh        # ~15 minutes, mostly RDS; prints the public URL at the end
```

It runs bootstrap, builds and pushes both images (tagged with the git SHA, built for
`linux/amd64`), applies `infra/`, runs the database migration as a one-off ECS task, and finishes
with `scripts/smoke-test.sh`. The working tree must be clean (the image tag is the commit).
Pass `true` as the first argument to create the database with a standby in a second AZ.

After the deploy, confirm the SNS subscription email once, or alarm emails will not arrive.

### Every later deployment

Pushing to `main` runs the pipeline (next section). To check any deployment by hand:

```bash
scripts/smoke-test.sh http://<alb-dns-name>
```

### Configuration and secrets

| Setting | Where it comes from on AWS |
| -- | -- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `BASE_URL` | task definition (not secret) |
| `DB_PASSWORD` | secret created and rotated by RDS in Secrets Manager, injected by ECS at start |

The database password is never in the repo, the images or the Terraform state.

### Teardown

The ALB, RDS and Fargate bill by the hour. Remove everything with:

```bash
scripts/teardown.sh            # asks for confirmation; destroys infra/, then bootstrap
```

## CI/CD

`.github/workflows/ci-cd.yml` (GitHub Actions) on every push to `main`:

| Step | What happens | Stops the pipeline when |
| -- | -- | -- |
| Test | `pytest` against a Postgres service container, `ruff`, `terraform fmt`/`validate`, `gitleaks` secret scan | any check fails |
| Build + scan | API and UI images tagged with the commit SHA (never `latest`), Trivy scan | a CRITICAL/HIGH vulnerability with a fix exists |
| Push | images to ECR (tags are immutable; ECR also scans on push) | - |
| Migrate | `alembic upgrade head` as a one-off ECS task with the new image | the migration fails |
| Deploy | `terraform apply -var image_tag=<sha>`: ECS rolling update (100% healthy minimum) with the deployment circuit breaker and automatic rollback, then wait for stability | the new tasks never become healthy |
| Smoke test | `/readyz`, create a link, follow the redirect, unknown code 404, UI page (retries up to 2 minutes) | any check fails |

Pull requests run the test step and build the images but have no AWS access. Only one deploy
runs at a time (the next waits); changes to `*.md` files do not deploy.

**Roll back** by redeploying an older commit: Actions -> ci-cd -> Run workflow, with
`image_tag` set to a previous git SHA (12 characters). Tests and build are skipped; the old
image is already in ECR.

**Credentials.** GitHub OIDC (no stored key) would be the first choice, but this AWS account
cannot create the OIDC provider (the Free plan blocks it). Instead a dedicated IAM user
(`urly-github-deploy`) holds one permission: assume the scoped deploy role. The workflow trades
its key for a 1-hour role session, so the stored key cannot do anything by itself. The key
pair is in two GitHub secrets, the role ARN and region in repository variables; none of it is
in the repo. Switching to OIDC later is a few lines in the workflow. The database password
never touches the pipeline.

## Behavior under load

**Test:** `loadtest/redirect.js` (k6) runs from a laptop against the public ALB address for about
8.5 minutes: 95% redirects (`GET /{code}`, 302 not followed) and 5% link creation, ramping from 10
to 50, 100 and 200 virtual users (2 minutes per step), after creating a pool of 200 links.
Targets: redirect p95 under 300 ms and under 1% errors. Between runs the API service was reset
by hand to its minimum of 2 tasks so every run starts from the same baseline.

```bash
k6 run -e BASE_URL=http://<alb-dns-name> --summary-export loadtest/results/run.json loadtest/redirect.js
```

| | Run 1 | Run 2 | Run 3 (after the change below) |
| -- | -- | -- | -- |
| Autoscaling | CPU target tracking | same | target tracking + step scaling |
| Requests (rate) | 64,332 (130/s) | 61,903 (125/s) | **111,299 (225/s)** |
| Errors | 0% | 0% | 0% |
| Redirect median | 86 ms | 88 ms | 82 ms |
| Redirect p95 / max | 2.45 s / 4.9 s | 2.49 s / 5.0 s | **1.40 s** / 3.2 s |
| CPU above 50% until a new task was requested | ~4.4 min | ~4.4 min | **~1.8 min** |
| p95 < 300 ms target | missed | missed | missed |

Raw numbers are in `loadtest/results/` (k6 summaries, the autoscaling timeline sampled every 20 s,
and a CloudWatch dashboard screenshot of the three runs).

**What happened.** The app never returned an error and most requests stayed fast (median
~85 ms), but the tail latency grew whenever the running tasks were saturated. The API tasks are
small (0.25 vCPU) and reached about 75% CPU at roughly 100 users, so requests queued.
Autoscaling worked but reacted late: with CPU target tracking alone it took about 4.4 minutes
from "CPU over 50%" to a new task being requested (CloudWatch needs three consecutive
one-minute readings, then Fargate needs about a minute to start a task). Runs 1 and 2 behaved the
same, so the result is repeatable.

**What was changed, and what it did.** After two runs I added a step-scaling policy on a
one-minute alarm (add 1, 2 or 3 tasks depending on how far CPU is above 50%); target tracking
stays for the slow scale-in. In run 3 the service asked for 4 tasks about 1.8 minutes after CPU
crossed 50% and reached 6 (the configured maximum) near the end of the test. Throughput nearly
doubled and p95 fell from ~2.45 s to 1.40 s. The target was still missed: the ramp outran the
capacity twice (2 starting tasks, then 4 tasks at 200 users). I stopped tuning there rather than
adjust the test until it passed.

**Where it bends next: the database has headroom, for now.** During run 3 RDS CPU peaked at 17.8%
and open connections at 35 (limit about 100; 6 tasks x 10 connections = 60 is the design ceiling).
The `db.t4g.micro` is burstable and its CPU credit balance stayed near zero (3 to 6 credits), so
sustained higher load would hit the database CPU next, and adding API tasks would not help.

**What I would do next, in order of effort**
1. Bigger or more tasks at the start (0.5 vCPU, or a minimum of 4): the cheapest fix for this
   exact result. Scheduled scale-up if the traffic peak is predictable.
2. Cache redirect lookups: links never change, so an in-process cache, then Redis, takes most
   reads off the database.
3. Bigger or non-burstable database instance; a connection pooler (RDS Proxy or PgBouncer) once
   there are many tasks; read replicas for redirects.
4. Take redirects off the origin: CloudFront with caching (a 301 or `Cache-Control` loses
   per-click visibility); or a key-value store (DynamoDB) for the code-to-URL mapping.
5. Run the load generator inside AWS (a Fargate task) to remove the laptop and home network
   from the measurement.

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
