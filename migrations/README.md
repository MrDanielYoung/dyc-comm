# Migrations

Numbered, append-only PostgreSQL migrations for the DYC Communications Platform.

- `0001_initial.sql` — initial schema generated from `schema.md` (v1).
- `0002_inbox_dry_run_classification.sql` — persisted dry-run classification log.
- `0003_mailbox_move_action.sql` — approved-move audit / idempotency log.

Apply manually for now:

```bash
psql "$DATABASE_URL" -f migrations/0001_initial.sql
psql "$DATABASE_URL" -f migrations/0002_inbox_dry_run_classification.sql
psql "$DATABASE_URL" -f migrations/0003_mailbox_move_action.sql
```

A migration runner (e.g. `sqlx migrate`, `alembic`, `dbmate`) has not been wired
up yet — pick one when the schema starts to evolve. Any change to the schema
should land as a new numbered file rather than editing `0001_initial.sql`.

`schema.md` remains the human-readable design doc; `migrations/0001_initial.sql`
is the executable source of truth.
