# tome-pulse

The receiving end of [Tome](https://github.com/bndct-devops/tome)'s opt-in telemetry. One small anonymous report per instance per month comes in; public aggregates go out. This is the whole service, so anyone who wants to know what happens to a report can read it in one sitting.

Tome is a self-hosted ebook library server with a KOReader plugin and a native iOS app. If you got here from the telemetry card in Tome, the short version: your instance sent, or is about to send, the JSON shown in that card, to this program, which keeps the JSON and the month it arrived and nothing else.

## What is stored

| Column | What |
|---|---|
| `instance` | a random UUID your instance minted for itself; it relates to nothing |
| `month` | the month the report arrived, UTC |
| `received` | the timestamp it arrived, UTC |
| `body` | the report, exactly as sent |

That is the only table with data in it. There is no column for an address, a header, a hostname or a name, and the access log is switched off in the image so an address is never written anywhere. One row per instance per month; a second report in the same month replaces the first.

Raw rows are kept for 12 months and pruned. Aggregates are computed from the latest report of each instance that has reported in the last 90 days, so an instance that stops reporting leaves the numbers after a quarter.

## What is in a report

Schema 1, as Tome sends it. Anything outside this shape is rejected with a 422 and not stored.

```json
{
  "schema": 1,
  "instance": "79d9d74e-07e5-40e7-a199-b6c863271a7e",

  "version": "2.6.0",
  "plugin_build": 47,
  "platform": "linux/x86_64",
  "docker": true,
  "install_age": "6-12",

  "users": "2-5",
  "readers_30d": "2-5",
  "books": "251-500",
  "libraries": "3-5",

  "book_types": { "manga": 0.55, "light_novels": 0.30, "novels": 0.15 },
  "formats": { "epub": 0.55, "cbz": 0.45 },
  "languages": { "en": 0.9, "de": 0.1 },

  "reading_30d": { "sessions": "101-250", "hours": "26-50", "pages": "2501-5000", "finished": "2-5" },
  "highlights": "101-250",

  "features_30d": {
    "koreader_sync": true, "web_reader": true, "kindle_sync_code": false, "send_to_device": false,
    "bindery": true, "wishlist": true, "hardcover": true, "opds": false, "sso_users": false,
    "api_tokens": true, "reading_goals": false, "series_ratings": true, "arcs": true
  }
}
```

Top to bottom: what runs, how big the install is, what is in it, how much it is read, which features are alive. Every count is a bucket, every feature is a yes or no over the last 30 days, every mix is a share. `install_age` is months since the first account was created. `highlights` is a lifetime count because it describes what exists, not activity. Reading numbers sit under `reading_30d` so the window is stated once. Tome shows this exact document, generated from the instance it runs on, before an admin can agree to send it, and consent is tied to the schema number: if a later Tome changes what is in here, sending pauses until the admin has looked at the new shape.

## Endpoints

- `POST /v1/report`, body as above, answers `204`. Also `204` when a report is dropped by the new-instance ceiling, so a flood learns nothing. `413` over 8 KB, `422` for anything that is not the schema.
- `GET /v1/aggregate`, public, cached five minutes, CORS open. Active instances, how many reported this month, new instances in the last 30 days, an estimate of hours read across all instances in the last 30 days (a sum of bucket midpoints, labelled as an estimate), the share running in Docker, and shares by version, platform, plugin build, install age, feature, every bucket, book type, format and language.
- `GET /healthz`.

## Running it

```bash
docker compose up -d
```

The compose file runs the container read-only, unprivileged, with every capability dropped and a memory and CPU cap, and mounts `./data` for the database. Put a reverse proxy with TLS in front, and a rate limit on `/v1/report` at the proxy or CDN. The app keeps no client addresses, so it cannot rate-limit by itself.

Settings, all optional: `PULSE_DB` (default `/data/pulse.db`), `PULSE_NEW_PER_DAY` (new instances accepted per UTC day, default 500), `PULSE_RETENTION_MONTHS` (default 12), `PULSE_MAX_BODY` (bytes, default 8192).

## Developing

```bash
pip install -e '.[dev]'
pytest
PULSE_DB=./pulse.db uvicorn pulse.app:app --reload --port 8750
```

The tests cover: a report is counted; one row per instance per month; everything outside the schema is rejected; the new-instance ceiling drops silently; nothing but the report is stored; old months are pruned; inactive instances leave the aggregate.

## The rest of Tome

- [Tome](https://github.com/bndct-devops/tome), the server, with the KOReader plugin inside it
- [tome.bndct.sh](https://tome.bndct.sh), docs and the public numbers this service produces
- [Tome Reader for iOS](https://testflight.apple.com/join/JrHyntNU), the native app, on TestFlight

## Licence

AGPL-3.0, same as Tome.
