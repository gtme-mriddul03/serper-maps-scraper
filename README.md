# Google Maps / Serper Places Scraper

Minimal Python CLI for finding businesses from Serper Places.

The scraper reads ZIP/city/state rows from `us_input_locations_for_maps.csv`, reads search terms from `config/keywords.yaml`, runs ZIP-level Serper Places searches, stores deduped results in SQLite, and exports CSV files.

## What It Runs

Each Serper query uses this format:

```text
<Keyword Query>, <ZIP>, <City>, <State>, <Country>
```

Example:

```text
Civil Engineering, 98610, Carson, WA, US
```

## Setup

```bash
git clone <github-repo-url>
cd gmaps-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```bash
SERPER_API_KEY=your_serper_api_key_here
```

Validate setup:

```bash
python3 -m scraper validate-config
python3 serper_credits.py
```

## Method 1: Clone It And Ask Your Agent

After cloning the repo, open it in your coding agent and use this prompt:

```text
Read README.md and AGENTS.md. I want to run a bounded Serper scrape.
First validate config and check credits. Then propose the exact command before running anything paid.
Use raw_zip_max, workers, and max-serper-queries-per-seed. Do not run paid Serper work until I approve.
```

Example follow-up:

```text
Run a smoke test for Civil Engineering in WA using seed:
Civil Engineering, 98610, Carson, WA, US
Use 25 workers and max 1 Serper query per ZIP. Ask before executing.
```

The agent should propose something like:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 98610, Carson, WA, US" --max-serper-queries-per-seed 1 --workers 25
```

## Method 2: Normal Step-By-Step Guide

Use this if you are running the project yourself without asking an agent.

### 1. Open The Project

```bash
cd gmaps-scraper
source .venv/bin/activate
```

If `.venv` does not exist yet:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Check The Setup

```bash
python3 -m scraper validate-config
```

This prints JSON. Look for:

```json
{
  "serper_api_key_present": true
}
```

If that is `false`, add your key to `.env`:

```bash
SERPER_API_KEY=your_serper_api_key_here
```

### 3. Check Serper Credits

```bash
python3 serper_credits.py
```

This calls Serper's credits endpoint and prints the remaining balance.

Do this before any larger run.

### 4. Run A Small Smoke Test

Start with a cheap test. This example runs the state from the seed, but caps each ZIP to 1 Serper page request:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 98610, Carson, WA, US" --max-serper-queries-per-seed 1 --workers 25
```

While it runs, the terminal prints progress like:

```text
[20260702T120000Z_raw_zip_max_abcd1234] started seed mode=raw_zip_max states=1 keywords=1
[20260702T120000Z_raw_zip_max_abcd1234] success WA civil_engineering raw_zip=98610 queries=1 credits=1 results=10 new=6
[20260702T120000Z_raw_zip_max_abcd1234] finished requests=312 credits=312 new=804 summary=outputs/run_summary_...
```

Each progress line tells you:

- state being run
- keyword id
- ZIP seed
- Serper page requests used
- estimated credits used
- results returned
- new unique businesses added

### 5. Watch The Live Log

During the run, open the newest file matching:

```text
outputs/live_run_*.jsonl
```

Each line is one event. It is safe to open while the scraper is still running.

Example event:

```json
{"event":"request_done","state":"WA","keyword_id":"civil_engineering","seed_type":"raw_zip","seed_value":"98610, Carson","api_request_count":1,"estimated_credit_usage":1,"result_count":10,"new_unique_businesses":6}
```

### 6. Review The Finished Run

After the terminal prints `finished`, open the newest summary:

```text
outputs/run_summary_*.json
```

Useful fields:

```json
{
  "total_requests": 312,
  "estimated_credits": 312,
  "total_results": 1200,
  "new_unique_businesses_added": 804,
  "failed_requests": 0,
  "request_log_path": "outputs/request_log_...",
  "live_log_path": "outputs/live_run_..."
}
```

Also open the request log:

```text
outputs/request_log_*.csv
```

This shows each ZIP request, status, query count, stop reason, and errors if any.

### 7. Run The Larger Job

If the smoke test looks good, increase the per-seed cap:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 98610, Carson, WA, US" --max-serper-queries-per-seed 30 --workers 25
```

### 8. Run Multiple States

The scraper runs one seed-derived state at a time. To run multiple states, run one `seed` command per state.

Example: Washington, South Carolina, and New York:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 98610, Carson, WA, US" --max-serper-queries-per-seed 30 --workers 25
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 29945, Yemassee, SC, US" --max-serper-queries-per-seed 30 --workers 25
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 10001, New York, NY, US" --max-serper-queries-per-seed 30 --workers 25
```

Those commands run all ZIPs for `WA`, `SC`, and `NY` that exist in `us_input_locations_for_maps.csv`.

For a cheaper selected-state smoke test:

```bash
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 98610, Carson, WA, US" --max-serper-queries-per-seed 1 --workers 25
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 29945, Yemassee, SC, US" --max-serper-queries-per-seed 1 --workers 25
python3 -m scraper seed --mode raw_zip_max --keyword-id civil_engineering --seed "Civil Engineering, 10001, New York, NY, US" --max-serper-queries-per-seed 1 --workers 25
```

### 9. Run All States

There is no separate `all states` command. Use a loop that picks one seed row per state from `us_input_locations_for_maps.csv`, then runs `seed` for each state.

First, print the commands without running them:

```bash
python3 - <<'PY'
import csv

keyword_id = "civil_engineering"
keyword_query = "Civil Engineering"

seen_states = set()
with open("us_input_locations_for_maps.csv", newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        zip_code, city, state, country = [part.strip() for part in row["Locations"].split(",")]
        if state in seen_states:
            continue
        seen_states.add(state)
        seed = f"{keyword_query}, {zip_code}, {city}, {state}, {country}"
        print(
            "python3 -m scraper seed "
            "--mode raw_zip_max "
            f"--keyword-id {keyword_id} "
            f'--seed "{seed}" '
            "--max-serper-queries-per-seed 1 "
            "--workers 25"
        )
PY
```

If the printed commands look right, run all states with a smoke-test cap:

```bash
python3 - <<'PY'
import csv
import subprocess

keyword_id = "civil_engineering"
keyword_query = "Civil Engineering"

seen_states = set()
with open("us_input_locations_for_maps.csv", newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        zip_code, city, state, country = [part.strip() for part in row["Locations"].split(",")]
        if state in seen_states:
            continue
        seen_states.add(state)
        seed = f"{keyword_query}, {zip_code}, {city}, {state}, {country}"
        subprocess.run(
            [
                "python3", "-m", "scraper", "seed",
                "--mode", "raw_zip_max",
                "--keyword-id", keyword_id,
                "--seed", seed,
                "--max-serper-queries-per-seed", "1",
                "--workers", "25",
            ],
            check=True,
        )
PY
```

For a larger all-state run, change:

```text
--max-serper-queries-per-seed 1
```

to:

```text
--max-serper-queries-per-seed 30
```

This can spend a lot of Serper credits. Check credits first.

### 10. Export Results

```bash
python3 -m scraper export --chunk-size 50000
```

Open:

```text
outputs/master.csv
```

If the file is large, also use:

```text
outputs/master.part_0001.csv
outputs/master.part_0002.csv
```

### 11. Common Problems

`SERPER_API_KEY is required for seed runs.`

Add the key to `.env`, then rerun validation.

`workers > 1 is not supported together with max_serper_queries.`

Use this instead:

```bash
--max-serper-queries-per-seed 1 --workers 25
```

The run says many requests were skipped.

That usually means the ZIPs were already completed before. The scraper resumes automatically and skips successful past requests.

## Important Flags

`--seed`

The seed chooses the state to run. If the seed is in Washington, the scraper runs all Washington ZIPs in `us_input_locations_for_maps.csv` for that keyword.

`--max-serper-queries-per-seed`

Maximum Serper page requests per seed (one seed per unique ZIP+city location). Works with workers.

`--workers`

Runs ZIP searches concurrently. Use this with `--max-serper-queries-per-seed`.

`--max-serper-queries`

Global cap for the whole run. Do not combine this with `--workers > 1`.

## Outputs

Live progress while a run is active:

```text
outputs/live_run_<run_id>.jsonl
```

Final run files:

```text
outputs/run_summary_<run_id>.json
outputs/request_log_<run_id>.csv
```

Exported business data:

```text
outputs/master.csv
outputs/master.part_0001.csv
```

Internal database:

```text
data/scrape.db
```

Keep the database if you want resume/dedupe to work across reruns.

## Current Commands

```bash
python3 -m scraper validate-config
python3 -m scraper seed --mode raw_zip_max --keyword-id <keyword_id> --seed "<Keyword>, <ZIP>, <City>, <State>, US" --max-serper-queries-per-seed 1 --workers 25
python3 -m scraper export --chunk-size 50000
```

Only one mode is supported:

```text
raw_zip_max
```

Only one seed type is used internally:

```text
raw_zip
```
