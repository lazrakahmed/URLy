# URLy

A small URL shortener: paste a long link, get a short one, open it and get redirected.
Python API + Postgres + a static web page, containerized, deployed to AWS with Terraform
and GitHub Actions.

## Overview and architecture

Paste a long link, get a short one; opening the short link redirects (HTTP 302) to the original.
The application is deliberately small; the effort went into delivery, reliability and operations.

```
                     internet
                        |
              Application Load Balancer  (HTTP :80, path rules)
              /                         \
   /, /app.js, /style.css            /api/*, /{code}
        |                                  |
   ui service (nginx, 2 tasks)       api service (FastAPI, 2-6 tasks, autoscaled)
                                           |
                                    RDS Postgres (private subnets)
```

| Part | Choice |
| -- | -- |
| UI | static HTML/JS served by nginx (no framework, no build step); "your links" kept in the browser's `localStorage` |
| API | Python 3.12, FastAPI, SQLAlchemy, Alembic migrations; 7-character random codes, unique constraint as the arbiter |
| Database | Postgres 17 on RDS `db.t4g.micro`, password managed by RDS in Secrets Manager |
| Compute | ECS Fargate (2 services, one task per availability zone), images in ECR |
| Infrastructure | Terraform (`infra/`, with `infra/bootstrap/` for state bucket, ECR and CI roles) |
| Delivery | GitHub Actions: test, scan, build, push, migrate, deploy, smoke test |
| Observability | JSON logs, CloudWatch metrics, log metric filters, alarms (email via SNS), dashboard |
| Load test | k6 (`loadtest/`), results in `loadtest/results/` |

Repository layout: `api/`, `ui/`, `gateway/` (local stand-in for the ALB), `infra/`, `loadtest/`,
`scripts/`, `.github/workflows/`, `docker-compose.yml`.

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

## Failure drills

Copy-paste runbooks for the demo, each with what to expect. They were rehearsed on the live AWS
deployment on 2026-10-25 (results below are measured, not estimates), except drill 5. Setup used by
every drill:

```bash
URL=http://<alb-dns-name>                     # terraform -chdir=infra output -raw url
CODE=$(curl -s -X POST $URL/api/links -H 'content-type: application/json' \
  -d '{"url":"https://example.com/drill"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["short_code"])')
# keep this running in a second terminal during the drill (one request per second):
while true; do echo "$(date +%T) $(curl -s -o /dev/null -m 4 -w '%{http_code}' $URL/$CODE)"; sleep 1; done
```

### 1. A container dies

```bash
TASK=$(aws ecs list-tasks --cluster urly --service-name api --query 'taskArns[0]' --output text)
aws ecs stop-task --cluster urly --task $TASK --reason "drill 1"
watch -n 5 "aws ecs describe-services --cluster urly --services api --query 'services[0].[runningCount,desiredCount]' --output text"
```

**Result:** the loop never left `302` (120 of 120 requests). The service dropped to 1 of 2 tasks,
ECS started a replacement and it was registered with the load balancer about 2 minutes after the
stop, then the service reported a steady state. Point at: the ECS service events tab, the other
task serving while the replacement starts.

### 2. A bad release is rolled back automatically

Make the container crash on start (tests do not run the container command, so the pipeline still
builds, pushes and starts a deploy):

```bash
sed -i '' 's|"app.main:app"|"app.main:does_not_exist"|' api/Dockerfile
git commit -am "Demo: release that crashes on start" && git push
gh run watch                                   # the deploy job ends red
aws ecs describe-services --cluster urly --services api --query 'services[0].events[0:5].[message]' --output text
git revert --no-edit HEAD && git push          # clean the repo: the pipeline redeploys the good code
```

**Result:** the new tasks failed to start four times; the deployment circuit breaker stopped the
rollout ("tasks failed to start") and rolled back to the previous version. The service stayed on
2 of 2 healthy tasks of the old image, and the loop saw **446 of 446 requests return `302`** during
the 8.5 minutes the bad release was trying to start. Point at: the red deploy job, the ECS events
("deployment failed", "rolling back"), the image tag still being the previous one.

### 3. The database fails

Two variants. A quick reboot (single-AZ database, about a minute):

```bash
aws rds reboot-db-instance --db-instance-identifier urly
```

A sustained outage, long enough to trigger the alarm (the `db_error` alarm needs errors in 2 of
3 one-minute windows):

```bash
aws rds stop-db-instance  --db-instance-identifier urly
# wait for the alarm email, then:
aws rds start-db-instance --db-instance-identifier urly
```

(With `scripts/first-deploy.sh true` the database has a standby in a second AZ and
`aws rds reboot-db-instance --db-instance-identifier urly --force-failover` is the failover drill;
that variant was not run here.)

**Result, reboot:** 8 of 66 requests (about 10 seconds) answered `503` with `Retry-After: 5` and
then recovered by themselves; `/healthz` stayed `200`, so no container was restarted; each failure
logged a `db_error` event. The outage was too short to fire the alarm, as designed.

**Result, stop/start:** from 17:59:21 every request answered `503` with `Retry-After: 5` (214 of
340 sampled requests) until redirects worked again at 18:07:33, before RDS even reported
`available`. `/healthz` stayed `200` and both services kept their 2 tasks, so nothing was restarted
and no manual step was needed. The `db_error` metric reached 29 and 47 events per minute, the
`urly-db-errors` alarm fired about 2 minutes after the outage began and the email arrived
(the `urly-5xx` alarm fired too, because the 503s count as server errors).

Point at: the UI showing "Service temporarily unavailable" with its countdown, the `db_error`
metric on the dashboard, the alarm email.

### 4. A traffic spike

The load test is the spike drill: see **Behavior under load** (`k6 run` command, three runs,
autoscaling timeline, dashboard screenshot). Point at: the task count rising on the dashboard and
the step-scaling policy reacting about 1.8 minutes after CPU crosses 50%.

### 5. A bad release that passes the health checks (runbook only, not rehearsed)

This is the known limit of the pipeline: the load balancer and ECS only check `/healthz`, which
does not touch the database. A release that breaks the database connection (for example a wrong
`DB_HOST` in the task definition) passes those checks, the rolling update completes and every
request answers `503`. The post-deploy smoke test (`/readyz`, create a link, follow it) turns the
pipeline red, but nothing rolls back automatically. Recover by redeploying a previous image
(Actions -> ci-cd -> Run workflow, `image_tag` = previous git SHA; this reverts the image only) or,
for a configuration change, with `git revert` and a push. Improvement: make the pipeline roll back
when the smoke test fails.

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

## Key technical decisions

Each decision lists what was chosen, why, and what was rejected.

| Decision | Why | Rejected alternatives |
| -- | -- | -- |
| **ECS Fargate behind an ALB** | no servers to patch; per-task health checks, rolling updates, a circuit breaker and autoscaling built in; fits an always-on API with a Postgres connection pool | **Lambda**: a database connection per invocation and cold starts on the redirect path; **ECS on EC2** and **EKS**: nodes to run or a control plane to pay for and operate; **App Runner**: not available on this account's plan |
| **Postgres on RDS** | relational, a unique constraint gives collision-safe codes, managed backups and failover | DynamoDB (a fine fit for the key-value lookups, kept as a scaling option); a Postgres container (no managed durability) |
| **Random 7-character base62 codes, insert-then-retry** | unguessable (the links have no login), no shared counter, no check-then-insert race | sequential IDs (guessable, enumerable), hash of the URL (collisions and dedupe questions) |
| **302 redirects** | every click reaches the service, so links stay controllable | 301 (browsers cache it forever; kept as a load option) |
| **Health check on `/healthz`, not `/readyz`** | a check should fail only when restarting the container would fix it; a database outage must not make the ALB drop every task or trigger a restart storm | checking the database in the ALB check (cascading failure) |
| **Relative `/api` paths, path routing at the ALB** | one address for UI and API: no CORS, no per-environment config | separate API domain (CORS and config), nginx as a front door everywhere (an extra hop on every redirect) |
| **Tasks in public subnets, no NAT gateway** | a NAT gateway costs about $32/month per AZ; protection is the security-group chain (ALB, then tasks, then database) | NAT or VPC endpoints (the production improvement) |
| **Database password managed by RDS** | the password never appears in Terraform state, the repo or an image; ECS injects it at start | a Terraform-generated password (lands in state), a variable in the pipeline |
| **Terraform, flat, remote state in S3 with native locking** | one environment; resources are readable top to bottom | modules (one environment), Terraform Cloud (another vendor), CloudFormation |
| **Bootstrap split from the main config** | the state bucket, image registry and CI roles must exist before and outlive the environment | one root config (a pipeline cannot create its own credentials) |
| **GitHub Actions with an assume-role IAM user** | short-lived (1 hour) role sessions; OIDC is the better design but the Free plan blocks creating the provider | a broad IAM user (long-lived power), CodePipeline (not GitHub Actions) |
| **No `/metrics` endpoint; CloudWatch only** | ALB and ECS metrics come for free; metric filters on JSON logs cover the few app events | Prometheus plus a collector sidecar (too heavy for this scope) |
| **Rolling update with circuit breaker** | automatic rollback of releases that never become healthy (proven in drill 2) | blue/green with CodeDeploy (more machinery than this scale needs) |
| **CPU autoscaling, target tracking plus step scaling** | simple signal needing no calibration; step scaling added after the load test showed a 4-minute reaction lag | requests-per-target scaling (needs measured capacity first), scheduled scaling |
| **HTTP only, no domain** | the app lives under 48 hours; not worth buying a domain | Route 53 + ACM + HTTPS (the upgrade path) |

## Known limitations and improvements

What is not done, why, and what production would add.

**Security and access**
- The app is HTTP only, on the load balancer's generated name (no domain bought for a demo).
- No authentication, WAF or rate limiting: anyone can create unlimited links or call the API.
  Production: WAF rate rules, accounts with per-user lists, URL safety checks.
- Tasks have public IPs and outbound HTTPS to anywhere (no NAT or VPC endpoints). Production:
  private subnets with VPC endpoints.
- CI uses a short-lived access key to assume the deploy role because the Free plan blocks GitHub
  OIDC. The deploy role has service-level wildcards because Terraform manages those services.
  Production: OIDC, a permissions boundary, separate plan and apply roles.
- The database password is an environment variable in the container. Production: IAM database
  authentication and rotation.

**Reliability**
- The database is single-AZ by default (`db_multi_az` enables a standby; the failover drill needs it).
- No automatic rollback when the smoke test fails after a release that passes health checks
  (drill 5). Production: blue/green with alarm-based rollback.
- The DB alarm is derived from logs, so an outage with no traffic raises no alarm; it also needs
  2 of 3 minutes of errors. Production: a synthetic probe on `/readyz`.
- While the database is down, redirects stop (every lookup needs it). Production: a cache in
  front of redirects and a multi-AZ database.

**Scaling** (see Behavior under load for measurements)
- The 300 ms p95 target was not met in any run: small tasks saturate before autoscaling catches up.
  Next steps: larger or more tasks at the start, then a redirect cache, then a bigger
  non-burstable database, a connection pooler, read replicas.
- Autoscaling is capped at 6 tasks so tasks x 10 connections stays below the database limit.
- The load generator is a laptop; no soak test.

**Product and data**
- Links never expire and duplicates create separate links; no per-link click counts; the "your
  links" list lives only in the browser.
- Anyone can shorten the service's own links (redirect loop) or URLs containing credentials.

**Delivery and operations**
- The first deployment is a manual script; later ones are automatic.
- Migrations are forward-only and rely on being backward compatible with the previous release.
- Supply-chain controls are basic (gitleaks, Trivy, pinned tool versions); production adds SBOMs,
  image signing and dependency updates.
- Routing rules exist twice (ALB in Terraform, nginx gateway locally); the smoke test is the guard.
- No tracing; logs go to a single destination; no ALB access logs.
