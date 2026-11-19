# nav-data

Navigation expert-data production. It submits tasks to the
[sim-engine](https://github.com/AhernResearch/vln-sim-engine) collection service, calls the
annotation model, lands and checks the evidence, compacts episodes and freezes a **release**
for nav-train.

nav-data produces model-independent physical facts: trajectories, poses, media,
instructions, provenance and review state. It does not select history frames, preprocess
images, encode training targets or train models; those steps belong to nav-train.
Simulation, planning and expert execution belong to sim-engine.

| Repository | Owns |
|---|---|
| sim-engine | How the world runs: simulation, planning, `serve-collection` / `serve-planner` |
| **nav-data** | What was collected: task submission, model annotation, landing, compaction, release |
| nav-train | How the model learns: history, images, prompts, targets, splits, SFT, training, eval |

The **only** interface to nav-train is the frozen release directory (`manifest.json` +
`episodes.jsonl.gz`), read by `nav_train.episodes.inventory_rows` after verifying
`files_sha256`. Neither repository imports the other. Format: [release format](docs/release-format.md).

## Pipeline at a glance

```
prepare-*            freeze inputs + source snapshot     no model, no movement
   │
collect-*            submit to sim-engine /v1/collections, call the model, land receipts
   │                 (needs serve-collection running, shared filesystem, model endpoint)
   │
compaction           inline (compact_completed_episodes) or ops/compaction_runner.py
   │                 → conversion root with result.json, plan.json, process.json, …
inventory            frozen release: manifest.json + episodes.jsonl.gz
   │
nav-train            inventory_rows(release) → history / SFT / training
```

Two collection mainlines:

| Mainline | Task kind | Model role | Commands |
|---|---|---|---|
| Short instructions | `short_instructions` | Draws a target box on four views, then writes the instruction | `prepare-short`, `collect-short` |
| R2R chunks | `r2r_chunks` | Annotates an assigned local R2R instruction segment | `prepare-r2r[-full]`, `collect-r2r`, `replenish-r2r`, … |

## 1. Installation

Python 3.10, CPU only. nav-data never loads Habitat or a GPU model.

```bash
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/nav-data --help
```

**Prerequisites for collection** (not for prepare/compaction/inventory):

1. A running sim-engine `serve-collection` (GPU + Habitat; see the sim-engine README,
   section 5) whose `training_datasets` include your dataset and whose `cameras` include your
   camera.
2. The collection root of that service must be visible here at the **same absolute path**.
   nav-data links the service's immutable artifacts into its own run directory.
3. One or more OpenAI-compatible model endpoints for annotation (`model_profiles`).

## 2. Configuration

Configs are frozen JSON. The maintained mainline configs are
[`annotations/planner_chunks/short_instructions.mainline.json`](annotations/planner_chunks/short_instructions.mainline.json)
and [`annotations/planner_chunks/r2r_chunk.mainline.json`](annotations/planner_chunks/r2r_chunk.mainline.json).
Copy one and edit it; never edit a config that a run has already used.

Every collection config needs a `collection_service` block:

```json
{
  "collection_service": {
    "url": "http://127.0.0.1:18812",
    "shared_root": "/data/nav_storage/sim-engine/collections/train-v1",
    "dataset": "train_pilot",
    "camera": "visible_88cm_hfov120",
    "max_pending": 64,
    "poll_interval_s": 0.1
  }
}
```

| Key | Meaning |
|---|---|
| `url` | sim-engine collection service |
| `shared_root` | Must equal the service's `collection.root` |
| `dataset`, `camera` | Must be registered on the service; checked against `/v1/health` before any submission |
| `max_pending` | Client in-flight limit, capped by the service's advertised capacity |
| `per_scene_pending` | Optional per-scene in-flight limit (default 8) |
| `poll_interval_s`, `http_timeout_s` | Polling cadence and HTTP timeout |
| `compact_completed_episodes` | Short instructions only: compact each `completed_audit` episode inline |

The rest of the config describes the task. For example, `cases` (start poses) and
`model_profiles` for short instructions, or `source_collection`, `source_review`,
`snapshot_root`, `base_request` and the chunking parameters for R2R. `prepare-*` validates
these keys and rejects unknown ones.

## 3. Short-instruction mainline

```bash
# Freeze: new owner id, normalized visible-region policy, nav-data source snapshot.
.venv/bin/nav-data prepare-short \
  --source-config annotations/planner_chunks/short_instructions.mainline.json \
  --output /data/runs/short-20261119

# Collect: verifies config + source hashes, checks service health, submits all cases.
.venv/bin/nav-data collect-short --root /data/runs/short-20261119
```

- `prepare-short` writes `config.json`, `source/` (a copy of `src/nav_data`) and
  `source-provenance.json` with `status=prepared_not_launched`. It makes no model or
  movement calls.
- `collect-short` refuses to run if `config.json` or any nav-data source file changed since
  prepare, so prepare with the same checkout you will collect with. Each case is one
  attempt: observe → model box → plan → execute once → audit.

## 4. R2R chunk mainline

```bash
# Freeze tasks from a reviewed parent collection (snapshot_root = a pinned sim-engine tree).
.venv/bin/nav-data prepare-r2r --config my-r2r.json --output /data/runs/r2r-20261119
# Or freeze from the pinned full FG / continuous R2R train sources (hash-checked; eval data rejected).
.venv/bin/nav-data prepare-r2r-full --config my-r2r.json \
  --episodes train.json.gz --fg-train fg_train.json --output /data/runs/r2r-full-20261119

# Collect: one attempt per manifest dir, written to <manifest-dir>/attempt.
.venv/bin/nav-data collect-r2r --manifest-dir /data/runs/r2r-20261119
```

To fill coverage gaps after one or more attempts, freeze a **new**, deduplicated replenishment
instead of re-running:

```bash
.venv/bin/nav-data replenish-r2r --base /data/runs/r2r-20261119 \
  --prior /data/runs/r2r-20261119/attempt --output /data/runs/r2r-replenish-1
.venv/bin/nav-data annotate-r2r-replenishment --manifest-dir /data/runs/r2r-replenish-1   # reused geometry, no movement
.venv/bin/nav-data collect-r2r                --manifest-dir /data/runs/r2r-replenish-1
.venv/bin/nav-data finalize-r2r-replenishment --manifest-dir /data/runs/r2r-replenish-1   # final coverage index
```

`prepare-r2r` writes `manifest.json` + `manifest.sha256.json`. Every later command verifies
that hash first.

### Exit status

`collect-short` and `collect-r2r` print a JSON summary. They exit **2** if anything was
aborted, `failed_or_uncertain` or `not_started`, or if the success target was not met.
Uncertain tasks keep their evidence and are **never** replayed automatically. To collect more,
prepare a new run (or a replenishment).

## 5. Compaction → conversion root

`inventory` reads a **conversion root**: a directory with `result.json` (`passed`,
`status=completed`, `total_converted`, `catalogs`), `plan.json`, `process.json` and
`source-provenance.json`, plus the per-client `episodes.sqlite3` catalogs and request
templates.

- **Inline:** with `collection_service.compact_completed_episodes=true`, `collect-short`
  compacts each `completed_audit` episode into the client catalog as it lands. This is
  supported for short instructions only.
- **Batch:** [`ops/compaction_runner.py`](ops/compaction_runner.py) compacts many client
  directories with up to 128 processes. Copy it into a new operation directory next to an
  `identity.json` (`owner`, `latest`, `current_shared`, `workers`, `target`,
  `previous_operation`), then run it there with the nav-data interpreter. That directory
  becomes the conversion root. It stops cleanly on SIGINT/SIGTERM and writes `error.json` on
  failure, with no automatic retry.

> **Limitation:** the batch runner was written for the free-instruction v5 production layout
> (it reads that run's `reconciliation.json`). It is not yet a generic `nav-data compact`
> subcommand. Treat it as a template, and check its plan against your collection layout
> before using it on a new one.

Inspect any single compacted episode:

```bash
.venv/bin/nav-data inspect-episode --inspect /path/to/episode/meta.json --full-media
```

## 6. Release (inventory) → nav-train

```bash
.venv/bin/nav-data inventory --conversion-root /data/ops/compaction-20261119 \
  --owner $(python3 -c 'import uuid;print(uuid.uuid4().hex)') \
  --output /data/releases/free-v5-20261119
```

`inventory` copies the conversion provenance and sqlite catalogs into `sources/`, checks them
against frozen entries, and audits every episode in parallel (`--workers`, default 64). It
writes `manifest.json` (with `files_sha256`) and `episodes.jsonl.gz`. The output directory
must not exist. Hand the directory to nav-train:

```bash
cd ../nav-train
.venv/bin/nav-train --policy configs/visual_history.free_subtask.v1.json history \
  --inventory /data/releases/free-v5-20261119
```

`nav-data extract --conversion-root R --output DIR` exports canonical rows directly. It is a
distribution/debugging view, not the nav-train interface.

### Planner-chunk annotation (offline)

`planner-chunks` and `planner-chunk-pool` annotate frozen planner-chunk cases against one or
more model endpoints without the collection service. `planner-chunks` takes a config with an
`endpoints` list and `--concurrency`; `planner-chunk-pool` takes a multi-endpoint `profiles`
config such as:

```bash
.venv/bin/nav-data planner-chunk-pool --cases annotations/planner_chunks/fixtures/cases.json \
  --config annotations/planner_chunks/pool64-fp4-default.v1.json \
  --protocol annotations/planner_chunks/protocol.v1.json --output /data/runs/chunks-1
```

## 7. Hard rules

Violating these is a bug, not a style choice:

- **Create-only records:** `nav_data.records.write` never overwrites, so a retry cannot replace
  evidence a receipt already references.
- **Never auto-replay:** timeouts, worker death and service restarts end as
  `failed_or_uncertain` with the evidence retained.
- **Structure is not semantics:** releases carry `training_admission=pending_review` and
  `training_ready=false`. Structural and media checks are not instruction review.
- **Splits belong to nav-train:** releases record `split_assignment=unassigned`.
- **Frozen schema identities:** `nav_train.compact_episode.v1`,
  `nav_train.free_collection_inventory.v1` and `nav_train.request_templates.v1` are frozen
  strings inside existing releases. Do not rename them.
- **Geometry only narrows:** visible-floor selection refines inside the model's box and never
  expands it.
- **No privileged leakage:** the model sees only the four public views (plus the assigned
  instruction for R2R). Goals, reference paths and acceptance metrics stay in server
  artifacts.
- Historical evidence stays in `nav-train/docs/evidence/`. It is linked, not moved or
  rewritten.

## 8. Development

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests ops        # line-length 105, rules E4,E9,F,I,PLC0415
```

All imports are at module top level (`PLC0415`); a cycle that blocks hoisting is a module
boundary to fix, not a reason for a local import.

```
src/nav_data/
  records.py              create-only JSON records, content hashes, atomic text writes
  cli.py                  single entry point for every stage
  collection/             client.py (sim-engine client + model calls), pool.py (endpoint pool),
                          admission.py (scene window), distribution.py (row export)
  tasks/                  region.py, instruction.py, short_instructions.py, short_run.py, pointnav.py,
                          recording.py, policy.py
  planner_chunks/         prepare.py, full_prepare.py, replenish.py, merging.py, collect.py,
                          annotation.py, pool.py
  release/                compact.py, evidence.py, inventory.py
ops/compaction_runner.py  batch compaction (see section 5)
annotations/planner_chunks/  frozen configs, protocols, prompts, fixtures
```

Unit tests cover prepare/verify, replenishment, chunk merging, client submission and
compaction. They do not establish that a real collection run succeeds: run a small collection
against a live service before production.

## Documentation (Chinese)

- [Release format](docs/release-format.md): the nav-train interface
- [Collection service (client side)](docs/collection-service.md)
- [Data mainlines](docs/navigation-data-mainlines.md)
- [Visible-region PointNav](docs/visible-region-pointnav-20261114.md)
- [GS data production](docs/gs-data-production.md), [first pilot](docs/gs-data-production-pilot-20261116.md)
- [Free-instruction v5 production](docs/free-instruction-v5-production-20261115.md)
