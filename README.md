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
