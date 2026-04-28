# Migrations

Numbered, append-only PostgreSQL migrations for the DYC Communications Platform.

- `0001_initial.sql` — initial schema generated from `schema.md` (v1).
- `0002_message_sync_instrumentation.sql` — adds `sync_event`,
  `message_sighting`, and `mailbox_action_event` so the operations
  dashboard can compute real volume / processed / error counts. The API
  also bootstraps these lazily; this file is for environments that prefer
  to apply migrations up front.

Apply manually for now:

```bash
psql "$DATABASE_URL" -f migrations/0001_initial.sql
psql "$DATABASE_URL" -f migrations/0002_message_sync_instrumentation.sql
```

A migration runner (e.g. `sqlx migrate`, `alembic`, `dbmate`) has not been wired
up yet — pick one when the schema starts to evolve. Any change to the schema
should land as a new numbered file rather than editing `0001_initial.sql`.

`schema.md` remains the human-readable design doc; `migrations/0001_initial.sql`
is the executable source of truth.
