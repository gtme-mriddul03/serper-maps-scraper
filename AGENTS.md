# AGENTS.md

## Purpose

This repo is a minimal Python 3.12 CLI scraper for Serper Places.

It:

- reads `us_input_locations_for_maps.csv` as the only location source
- reads manual keywords from `config/keywords.yaml`
- runs raw ZIP Serper Places seed jobs
- stores deduped businesses and provenance in SQLite
- writes live run visibility to `outputs/live_run_<run_id>.jsonl`
- exports a deduped `outputs/master.csv` plus chunked CSV parts

There is no UI, dashboard, queue, plugin system, or service layer.

## Read This First

Start in this order:

1. `AGENTS.md`
2. `scraper/__main__.py`
3. `scraper/core.py`
4. Only then read `scraper/serper.py`, `scraper/store.py`, or `scraper/export.py` if your task touches them.

## Repo Map

- `scraper/__main__.py`
  - CLI entrypoint
  - commands: `validate-config`, `seed`, `export`

- `scraper/core.py`
  - config loading
  - CSV parsing
  - legacy seed parsing
  - deterministic raw ZIP seed building
  - seed-run orchestration
  - worker and per-ZIP budget validation
  - live progress logging
  - request logs and run summaries

- `scraper/serper.py`
  - stdlib `urllib` Serper Places client
  - retries and pagination
  - response normalization

- `scraper/store.py`
  - SQLite schema and persistence
  - request claiming and concurrent-safe writes
  - stores runs, requests, businesses, and provenance

- `scraper/export.py`
  - writes `outputs/master.csv`
  - writes `outputs/master.part_0001.csv`, etc.

- `scraper/progress.py`
  - writes JSONL live progress events
  - prints default terminal progress to stderr

- `serper_credits.py`
  - checks remaining Serper credits

## Runtime Assumptions

- Python 3.12
- only third-party dependency is `PyYAML`
- repo-root `.env` is auto-loaded with stdlib parsing
- shell environment values take precedence over `.env`
- `SERPER_API_KEY` is required for `seed`
- `SERPER_BASE_URL` is optional and defaults to `https://google.serper.dev`

## Seeds And Modes

Only one seed type exists:

- `raw_zip`

One seed is created per unique (ZIP, city) location, so a single ZIP that
holds multiple cities produces one seed per city. Seeds are ordered by
(ZIP, city) ascending. A seed's `seed_value` is the `"<ZIP>, <City>"`
composite, which keeps request fingerprints and provenance unique per
location.

Only one mode exists:

- `raw_zip_max`

`raw_zip_max` queries every ZIP seed for the seed-derived state and keyword.

Serper query text is built as:

```text
<Keyword Query>, <ZIP>, <City>, <State>, <Country>
```

Example:

```text
Civil Engineering, 98610, Carson, WA, US
```

## Important Behaviors

Canonical business identity is:

1. `cid` if present
2. fallback hash of normalized title + address + phone

Completed requests are fingerprinted by:

- mode
- keyword id
- state
- seed type
- seed value

Successful requests are skipped on reruns. There is no force flag.

## Seed Limits And Concurrency

- `--max-serper-queries-per-seed` caps underlying Serper page requests per seed (per unique ZIP+city location).
- `--workers > 1` runs raw ZIP seeds concurrently.
- `--workers > 1` is not supported together with `--max-serper-queries`.
- There is no built-in CLI flag to run only part of a state.
- For a tiny state-subset smoke test, temporarily narrow `us_input_locations_for_maps.csv` with explicit approval and then restore it.

## Serper Pagination

Serper Places is page-aware:

- page 1 is always fetched first
- additional pages are fetched only when the previous page looks full
- pagination stops on empty, repeated, no-new-business, short, or budget-exhausted pages

## Outputs

Generated artifacts:

- `data/scrape.db`
- `outputs/live_run_*.jsonl`
- `outputs/master.csv`
- `outputs/master.part_*.csv`
- `outputs/run_summary_*.json`
- `outputs/request_log_*.csv`

While a run is active, inspect `outputs/live_run_<run_id>.jsonl`.
After a run completes, inspect the newest `outputs/run_summary_*.json` and `outputs/request_log_*.csv`.

## Utah Architect Workflow

Default Utah architect seed:

```bash
architects, 84003, American Fork, UT, US
```

Before broader Utah work, prefer a bounded 25-worker smoke test and ask for approval before paid Serper work:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id architect --seed "architects, 84003, American Fork, UT, US" --max-serper-queries-per-seed 1 --workers 25
```

Default full Utah worker run to propose after a successful smoke test:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id architect --seed "architects, 84003, American Fork, UT, US" --max-serper-queries-per-seed 30 --workers 25
```

## First Commands To Run

Validation:

```bash
python3 -m scraper validate-config
```

Credits:

```bash
python3 serper_credits.py
```

Tests:

```bash
python3 -m unittest discover -s tests -v
```

Seed smoke test:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id architect --seed "architects, 84003, American Fork, UT, US" --max-serper-queries-per-seed 1
```

Export:

```bash
python3 -m scraper export --chunk-size 50000
```

## Safe Editing Notes

- Keep the system lean.
- Prefer stdlib unless a new dependency is clearly necessary.
- Preserve deterministic ZIP ordering and resume behavior.
- Avoid adding configuration knobs unless necessary.
- If persistence or export behavior changes, update tests in `tests/`.
