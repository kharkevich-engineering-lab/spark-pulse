# State and persistence

## One database

Structured state lives in a single SQLAlchemy-backed database: deployments, nodes, the enrolment ledger, benchmark results, recipe customizations and browser sessions.

SQLite in WAL mode by default — `~/.config/spark-pulse/spark-pulse.db`, mode `0600` because it holds OIDC tokens. SQLAlchemy is there so the scale case is a URL rather than a rewrite:

```yaml
database_url: postgresql+psycopg://spark@db.internal/spark_pulse
```

```bash
pip install spark-pulse[postgres]
```

Every table is compiled for the PostgreSQL dialect in the test suite, so a SQLite-only column type fails in CI rather than in front of an operator.

## Migrating from the JSON files

Earlier builds kept deployments, nodes and benchmarks in JSON files. Each store imports its old file **once**, on first read, recorded in a `meta` table — not inferred from an empty table, because deleting the last row would re-import and resurrect what an operator had removed. The JSON files are left where they are.

This is the one compatibility path kept deliberately: it is somebody's data, not somebody's code.

## What is a file on purpose

| Path | Why |
|---|---|
| `~/.config/spark-pulse/agent/` | The CA key and node certificates, `0600`, read by tooling. |
| `~/.config/spark-pulse/settings.json` | Config, layered under env vars. |
| `~/.config/spark-pulse/secrets.json` | Secrets, `0600`. Never returned unmasked. |
| `~/.config/spark-pulse/custom-recipes/`, `custom-mods/` | Yours to edit by hand. |
| `registries.yaml` | OCI registry list. |
| `~/.cache/spark-pulse/` | Index and metadata caches. |

## Deployment records

A record is a SQLAlchemy row with `id`, `status` and `runtime` promoted to columns and the rest kept as a JSON document — so a new field (`sync`, `sync_reason`, `sync_intent`, `ranks`, `orphans`) needs no migration.

Records are written **per row**, never as "the set is now this": a whole-set save read before a concurrent create would delete the deployment somebody made in between.

Finished records are purged by `job_retention_days`, and the purge writes back only the rows it decided about, for the same reason.

## What is not persisted, deliberately

The engine-metrics window — one hour of five-second readings per deployment — is in memory only and lost on restart. Retention is Prometheus's job; the window exists so you can see the last hour without deploying one, and pretending it survives a restart would be worse than saying it does not.

## Tests

Every test gets its own database through an autouse fixture, which also points every JSON migration source at a temporary directory — without that, a test run would import the developer's real deployments.
