# HN Filter: Enhanced Multi-Step Architecture & Implementation Plan

A lightweight, serverless Python pipeline that reads Hacker News every morning, scores stories against your personal interest profile using the Gemini API with structured outputs, and posts a curated digest to a Discord channel. Designed to run completely free via GitHub Actions cron with zero server maintenance.

---

## 1. System Architecture & Workflow

```mermaid
flowchart TD
    subgraph Trigger
        A["GitHub Actions Cron (06:00 UTC)"] --> B["CLI Entrypoint: main.py"]
        A_Manual["workflow_dispatch / Manual CLI"] --> B
    end

    subgraph "Phase 1: Fetch & Pre-filter"
        B --> C["hn.py: fetch_top_stories()"]
        C -->|HTTP GET /v0/topstories.json| D["HN Firebase API"]
        D -->|Top 150 IDs| C
        C -->|Concurrent fetch with Semaphore(10)| E["HN Item Details"]
        E --> F["Filter: type=='story', not dead/deleted, score >= MIN_HN_SCORE"]
    end

    subgraph "Phase 2: Deduplication"
        F --> G["store.py: filter_new(stories)"]
        H[("data/seen.json")] <-->|Read / Prune > 14d| G
        G -->|Zero new stories| I["notify.py: Post 'No new stories' status"]
        G -->|New stories| J["Batch into chunks of BATCH_SIZE (40)"]
    end

    subgraph "Phase 3: AI Scoring"
        J --> K["scorer.py: score_batches()"]
        L["interests.md"] -->|System Instruction| K
        K -->|Structured JSON / response_schema| M["Google Gemini 2.5 Flash / Flash-Lite"]
        M -->|List of Verdicts: id, score 0-10, reason| K
    end

    subgraph "Phase 4: Curation & Notification"
        K --> N["main.py: Filter score >= THRESHOLD (7), Sort Descending, Cap at MAX_ITEMS (10)"]
        N --> O["notify.py: format_digest() & chunk messages (<1800 chars)"]
        O -->|HTTP POST with 429 backoff| P["Discord Webhook"]
    end

    subgraph "Phase 5: State Persistence & CI Commit"
        P -->|On Success| Q["store.py: mark_seen() & atomic save()"]
        Q --> R["Git commit & push data/seen.json"]
    end

    subgraph "Failure Handling"
        K -.->|Error| S["Abort without saving seen.json -> Next run retries"]
        P -.->|Error| S
        S -.-> T["Discord Webhook: Post failure alert"]
    end
```

---

## 2. Technical Stack & Key Decisions

| Component | Choice | Rationale & Best Practice |
|---|---|---|
| **Runtime** | Python 3.12+ | Asyncio native support, modern typing, clean syntax |
| **HTTP Client** | `httpx` | Async client with HTTP/2 support, timeouts, connection pooling, and built-in mock transports |
| **Data Validation** | `pydantic` v2 & `pydantic-settings` | Fast schema validation, type coercion, environment variable management |
| **LLM Provider** | Google GenAI SDK (`google-genai`) | Official new SDK; supports `response_schema` for guaranteed JSON adhering to Pydantic models |
| **LLM Model** | Gemini 2.5 Flash / Flash-Lite | Free tier on Google AI Studio, high RPM/TPM quota, strong reasoning for technical curation |
| **State Storage** | `data/seen.json` (committed to git) | Serverless, no external DB needed. Daily git commit keeps GitHub Actions schedule active |
| **Delivery** | Discord Webhook | Zero-setup push notifications to mobile/desktop, supports markdown formatting |
| **Test Framework** | `pytest`, `pytest-asyncio`, `respx` | Declarative async testing, robust HTTP request mocking without live network calls |

---

## 3. Detailed Multi-Step Implementation Plan

### Step 1: Environment, Dependencies & Test Harness Setup

**Goal:** Establish a clean development environment, pin dependencies, configure test runners, and verify tooling before writing domain logic.

**Implementation Tasks:**
1. Note: Repository is already cloned and on branch `main`.
2. Create and activate a Python 3.12 virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Create `requirements.txt` (runtime) and `requirements-dev.txt` (testing & linting):
   - **`requirements.txt`**:
     ```text
     httpx>=0.27.0
     pydantic>=2.7.0
     pydantic-settings>=2.2.0
     google-genai>=0.1.1
     python-dotenv>=1.0.1
     ```
   - **`requirements-dev.txt`**:
     ```text
     -r requirements.txt
     pytest>=8.0.0
     pytest-asyncio>=0.23.0
     pytest-cov>=5.0.0
     respx>=0.21.0
     ruff>=0.3.0
     mypy>=1.9.0
     ```
4. Configure `.gitignore`:
   ```text
   .venv/
   __pycache__/
   *.pyc
   .pytest_cache/
   .coverage
   htmlcov/
   .env
   .mypy_cache/
   .ruff_cache/
   ```
5. Create `pytest.ini` for seamless async test execution:
   ```ini
   [pytest]
   asyncio_mode = auto
   testpaths = tests
   python_files = test_*.py
   ```
6. Scaffold the package directories:
   ```bash
   mkdir -p hnfilter tests data .github/workflows
   touch hnfilter/__init__.py tests/__init__.py
   ```
7. Create `.env.example` and a default `interests.md` profile.

**Testing Plan for Step 1:**
- **Test Objective:** Validate that the test runner executes and the project package is importable.
- **Test File:** `tests/test_smoke.py`
  - `test_package_import()`: Import `hnfilter` and assert `hnfilter.__file__` is defined.
  - `test_environment_sanity()`: Assert Python version is >= 3.12.
- **Run Command:** `pytest -v`
- **Done When:** `pytest` runs cleanly, and `python -c "import hnfilter"` succeeds with code 0.

---

### Step 2: Configuration & Typed Data Models

**Goal:** Define strongly typed, immutable models for HN stories and LLM verdicts, along with environment-based configuration management.

**Implementation Tasks:**
1. **`hnfilter/models.py`**:
   - `Story`:
     - Fields: `id: int`, `title: str`, `url: str`, `domain: str`, `score: int`, `comments: int = 0`, `time: int`
     - Computed properties: `hn_url` (`https://news.ycombinator.com/item?id={id}`)
     - Helper `@classmethod from_hn_item(cls, item: dict[str, Any]) -> Story | None`:
       - Handles stories without a `url` (e.g. Ask HN, Show HN text posts) by defaulting `url` to `hn_url` and `domain` to `news.ycombinator.com`.
       - Extracts clean domain via `urllib.parse.urlparse`.
       - Returns `None` if item is missing critical fields or not of type `"story"`.
   - `Verdict`:
     - Fields: `id: int`, `score: int = Field(ge=0, le=10)`, `reason: str = Field(max_length=150)`
   - `VerdictBatch`:
     - Fields: `verdicts: list[Verdict]` (used as `response_schema` for Gemini structured outputs)
   - `DigestItem`:
     - Combined `Story` and `Verdict` representation ready for rendering.
2. **`hnfilter/config.py`**:
   - `Settings(BaseSettings)` using `pydantic-settings`:
     - `gemini_api_key: str = ""` (optional when running in dry-run mode)
     - `gemini_model: str = "gemini-2.5-flash"`
     - `discord_webhook_url: str = ""` (optional when running in dry-run mode)
     - `fetch_limit: int = 150`
     - `min_hn_score: int = 10`
     - `batch_size: int = 40`
     - `threshold: int = 7`
     - `max_items: int = 10`
     - `interests_file: Path = Path("interests.md")`
     - `seen_file: Path = Path("data/seen.json")`
     - `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`

**Testing Plan for Step 2:**
- **Test Objective:** Verify data model validation, normalization, and configuration parsing.
- **Test File:** `tests/test_models.py`
  - `test_story_from_standard_hn_item()`: Pass a full HN item JSON dict; verify fields and `hn_url`.
  - `test_story_without_url_fallback()`: Pass an Ask HN item without `url`; assert `url` defaults to `hn_url` and `domain` to `"news.ycombinator.com"`.
  - `test_story_from_invalid_item()`: Pass a job, comment, or dead story; assert returns `None`.
  - `test_verdict_validation()`: Verify `score` validation rejects values < 0 or > 10.
  - `test_verdict_batch_deserialization()`: Verify JSON list parses directly into `VerdictBatch`.
- **Test File:** `tests/test_config.py`
  - `test_settings_defaults()`: Assert expected defaults are populated without an `.env` file.
  - `test_settings_env_override(monkeypatch)`: Set environment variables and verify proper casting and override.
- **Done When:** All tests in `tests/test_models.py` and `tests/test_config.py` pass.

---

### Step 3: Resilient Hacker News Fetcher (`hn.py`)

**Goal:** Concurrently fetch the top stories from the official Firebase HN API with polite rate limiting, connection pooling, and error resilience.

**Implementation Tasks:**
1. **`hnfilter/hn.py`**:
   - `fetch_top_story_ids(client: httpx.AsyncClient, limit: int) -> list[int]`:
     - GET `https://hacker-news.firebaseio.com/v0/topstories.json`
     - Return slice `ids[:limit]`
   - `fetch_item(client: httpx.AsyncClient, item_id: int, semaphore: asyncio.Semaphore) -> dict[str, Any] | None`:
     - Concurrency guarded by `asyncio.Semaphore(10)`.
     - GET `https://hacker-news.firebaseio.com/v0/item/{item_id}.json`.
     - Timeout: 10 seconds.
     - Retry logic: Up to 2 retries with exponential backoff on network errors or 5xx.
     - Graceful error catch: Return `None` on persistent error or if item is `None`/`null`.
   - `fetch_stories(limit: int = 150, min_score: int = 10) -> list[Story]`:
     - Orchestrates fetching top IDs and concurrently resolving items via `asyncio.gather`.
     - Converts items via `Story.from_hn_item()`.
     - Filters out stories with `story.score < min_score`.

**Testing Plan for Step 3:**
- **Test Objective:** Validate async HTTP fetching, concurrency throttling, and edge-case payload handling without live internet access.
- **Test File:** `tests/test_hn.py` (using `respx`)
  - `test_fetch_top_story_ids()`: Mock `topstories.json` returning 500 IDs; verify only `limit` are requested.
  - `test_fetch_stories_success()`: Mock IDs and item endpoints; verify list of valid `Story` objects returned.
  - `test_fetch_stories_filters_low_score()`: Item with score 5 filtered out when `min_score=10`.
  - `test_fetch_stories_handles_null_item()`: Item endpoint returns `null` (deleted); assert handled gracefully without throwing.
  - `test_fetch_stories_handles_http_errors()`: One item returns 500; assert other items still succeed and failing item is skipped.
  - `test_fetch_stories_filters_non_stories()`: Item with `type: "job"` is ignored.
- **Done When:** `pytest tests/test_hn.py` passes 100% with all network calls mocked.

---

### Step 4: Atomic State Storage & Deduplication (`store.py`)

**Goal:** Ensure stories are never sent twice, storage does not grow unbounded, writes are atomic (immune to partial writes on failure), and missing files are handled gracefully.

**File Structure (`data/seen.json`):**
```json
{
  "version": 1,
  "last_run": "2026-10-14T06:00:00Z",
  "seen": {
    "45012345": "2026-10-14",
    "45012389": "2026-10-14"
  }
}
```

**Implementation Tasks:**
1. **`hnfilter/store.py`**:
   - `SeenStore` class:
     - `path: Path`
     - `load() -> SeenStore`: Reads `seen.json`. If missing or corrupt, returns empty store and logs warning.
     - `filter_new(stories: list[Story]) -> list[Story]`: Returns stories whose `id` (as string or int) is not in `self.seen`.
     - `mark_seen(stories: list[Story], date_str: str | None = None) -> None`: Adds story IDs with current date `YYYY-MM-DD`.
     - `prune(max_age_days: int = 14) -> int`: Drops entries older than `max_age_days` to prevent infinite file growth.
     - `save() -> None`: Updates `last_run` timestamp. Writes JSON to a temporary file in the same directory (`data/seen.json.tmp`) and executes `os.replace` for atomic POSIX commit. Automatically creates parent directory if missing.

**Testing Plan for Step 4:**
- **Test Objective:** Test storage persistence, deduplication, atomic writing, and automated pruning.
- **Test File:** `tests/test_store.py`
  - `test_store_load_missing_file(tmp_path)`: Loading from non-existent path returns valid empty `SeenStore`.
  - `test_store_load_corrupt_file(tmp_path)`: Corrupt JSON string returns empty store without crashing.
  - `test_filter_new(tmp_path)`: Given 3 stories where 1 is in `seen`, verify only 2 are returned.
  - `test_mark_seen_and_save(tmp_path)`: Verify stories are saved with today's date and file matches schema.
  - `test_prune_old_entries(tmp_path)`: Add entries with dates 20 days ago and today; verify old entries are dropped and recent kept.
  - `test_atomic_write(tmp_path)`: Verify temp file is cleaned up and replaced atomically.
- **Done When:** All test cases in `tests/test_store.py` pass.

---

### Step 5: Gemini Structured Output Scorer (`scorer.py`)

**Goal:** Batch stories, pass your interest profile as system instructions, and get guaranteed structured ratings (`VerdictBatch`) from Gemini with exponential backoff on rate limits.

**Implementation Tasks:**
1. **`hnfilter/scorer.py`**:
   - Use the official Google GenAI SDK:
     ```python
     from google import genai
     from google.genai import types
     ```
   - Load `interests.md`: Read interests file; provide sensible fallback if file is empty or missing.
   - Batching: Chunk `list[Story]` into batches of `batch_size` (e.g., 40).
   - System Instruction:
     ```text
     You are a technical news curator filtering Hacker News stories for a software engineer.
     Your task is to score each story from 0 to 10 based strictly on how well it matches the user's interests.
     Be selective and strict: most stories (unrelated, hype, superficial) should score 4 or lower.
     Only stories with high technical substance, practical engineering value, or deep dives matching interests should score 7 or above.
     Return a verdict for every story provided.
     ```
   - Request Configuration:
     - `response_mime_type="application/json"`
     - `response_schema=VerdictBatch` (Pydantic model passed directly to SDK)
     - `temperature=0.0`
   - Prompt Construction per batch:
     ```json
     [{"id": 45012345, "title": "...", "domain": "...", "points": 120, "comments": 45}]
     ```
   - Retry & Resilience:
     - Handle `429 Too Many Requests` or `503 Service Unavailable` with 3 retries and exponential backoff (2s, 4s, 8s).
     - Missing ID recovery: If model omits an ID, synthesize a `Verdict(id=id, score=0, reason="Scorer omitted")`.

**Testing Plan for Step 5:**
- **Test Objective:** Test batching, prompt generation, structured output parsing, error recovery, and rate limit retries with a mocked Gemini client.
- **Test File:** `tests/test_scorer.py`
  - `test_batching_logic()`: 95 stories with batch size 40 splits into batches of 40, 40, and 15.
  - `test_score_batch_success()`: Mock `client.models.generate_content` returning valid `VerdictBatch`; assert verdicts correctly mapped.
  - `test_score_batch_handles_omitted_id()`: Mock model returning verdict for 1 of 2 stories; verify omitted story receives score 0.
  - `test_scorer_rate_limit_retry()`: Mock first call raising 429, second call succeeding; assert retry succeeds.
  - `test_scorer_exhausted_retries_raises()`: Repeated 429 errors raise a descriptive exception.
- **Done When:** All test cases in `tests/test_scorer.py` pass.

---

### Step 6: Discord Notifier & Message Chunker (`notify.py`)

**Goal:** Format curated stories into clean, readable Markdown and deliver them via Discord Webhook, splitting messages cleanly under Discord's 2000-character limit and respecting rate limits.

**Formatting Standard:**
```markdown
**[9] SQLite in the Browser with WASM/OPFS** (342 pts · 88 cmts)
*High-performance local DB persistence in web apps with practical benchmarks.*
<https://example.com/article> · <https://news.ycombinator.com/item?id=45012345>
```
*(Angle brackets `<...>` suppress Discord's automatic embed previews to keep channel feed clean.)*

**Implementation Tasks:**
1. **`hnfilter/notify.py`**:
   - `format_story_block(item: DigestItem) -> str`
   - `chunk_messages(blocks: list[str], header: str, max_chars: int = 1800) -> list[str]`:
     - Ensures header is attached to the first message.
     - Never splits an individual story block across two messages.
     - Guarantees each chunk is under `max_chars`.
   - `send_discord_message(webhook_url: str, content: str, client: httpx.Client | None = None) -> None`:
     - POST `{"content": content}`.
     - On HTTP 429 (Rate Limit): Read `Retry-After` header or JSON `retry_after`, `time.sleep()`, and retry up to 3 times.
     - On non-2xx status code: Raise `DiscordDeliveryError`.
   - `send_digest(...)`:
     - Handles zero picks gracefully: `"**HN Digest** · 0 picks from 135 new stories today. Nothing met your threshold."`
     - Delivers chunked messages with a polite 1-second pause between requests.

**Testing Plan for Step 6:**
- **Test Objective:** Validate message formatting, character limit enforcement, chunking boundaries, and webhook error handling.
- **Test File:** `tests/test_notify.py`
  - `test_format_story_block()`: Verify output matches formatting specification and suppresses link preview.
  - `test_chunk_messages_under_limit()`: 3 stories fitting into 1 message produces exactly 1 chunk.
  - `test_chunk_messages_splits_cleanly()`: 15 long stories split into multiple chunks; assert no chunk exceeds 1800 characters and no story block is cut in half.
  - `test_send_discord_message_success()`: Mock 204 No Content response from Discord; assert successful POST.
  - `test_send_discord_message_rate_limit()`: Mock 429 response with `Retry-After: 0.1`; verify retry succeeds.
  - `test_send_digest_zero_picks()`: Verify "no picks" notification is composed and sent.
- **Done When:** All test cases in `tests/test_notify.py` pass.

---

### Step 7: Pipeline Orchestrator & CLI Runner (`main.py`)

**Goal:** Tie together fetching, deduplication, scoring, delivery, and state persistence into a unified CLI with `--dry-run` and debugging capabilities.

**Execution Lifecycle:**
```text
1. Parse CLI arguments (--dry-run, --limit, --threshold)
2. Load Settings and interests.md
3. Load SeenStore from data/seen.json
4. Fetch top stories from HN (hn.py)
5. Filter against SeenStore (store.py)
6. If 0 new stories:
   - Send short status message if not dry-run
   - Exit 0
7. Score new stories with Gemini (scorer.py)
8. Filter: score >= threshold, sort descending, slice [:max_items]
9. Deliver digest to Discord (notify.py)
10. Atomic commit: mark_seen(all_scored_stories), prune(), save()
    CRITICAL: Step 10 MUST only run if Step 9 succeeds, or in dry-run mode be skipped!
```

**Implementation Tasks:**
1. **`hnfilter/main.py`**:
   - Use `argparse` for flags:
     - `--dry-run`: Fetch and score stories, print digest and ranking table to stdout, skip Discord POST and do NOT mutate `seen.json`.
     - `--limit`: Override fetch limit.
     - `--threshold`: Override score threshold.
     - `--verbose`: Enable DEBUG logging.
   - Setup structured Python logging (`logging.getLogger("hnfilter")`).
   - Clean exit codes: `0` for success, `1` for configuration/runtime error.

**Testing Plan for Step 7:**
- **Test Objective:** End-to-end integration test with mocked external APIs validating the full pipeline lifecycle.
- **Test File:** `tests/test_main.py`
  - `test_pipeline_dry_run_does_not_mutate_store(tmp_path, capsys)`: Run `main()` with `--dry-run`; assert stories are printed, Discord is not called, and `seen.json` is not modified.
  - `test_pipeline_live_run_updates_store(tmp_path)`: Run `main()` without dry-run; assert Discord is called, and `seen.json` is updated with scored stories.
  - `test_pipeline_discord_failure_aborts_store_save(tmp_path)`: Simulate Discord webhook error; assert pipeline exits with code 1 and `seen.json` is NOT updated (allowing retry on next run).
  - `test_pipeline_zero_stories_flow(tmp_path)`: When all stories are already in `seen.json`, assert scoring is skipped and status notification is sent.
- **Done When:** All tests in `tests/test_main.py` pass.

---

### Step 8: Multi-Stage GitHub Actions CI/CD Pipeline

**Goal:** Automate test execution on code changes, and execute the daily digest reliably every morning with git conflict prevention and failure alerts.

**Implementation Tasks:**
1. **`.github/workflows/ci.yml` (Pull Request & Push Validation)**:
   - Triggers on `push` and `pull_request` to `main`.
   - Steps:
     - Checkout code.
     - Set up Python 3.12 with pip cache.
     - Install `requirements-dev.txt`.
     - Run `ruff check .` (linting).
     - Run `pytest --cov=hnfilter --cov-report=term-missing` (tests + coverage assertion).
2. **`.github/workflows/daily.yml` (Scheduled Digest Execution)**:
   - Triggers on cron schedule (`0 6 * * *` UTC) and `workflow_dispatch`.
   - Concurrency group `daily-digest` with `cancel-in-progress: false`.
   - Steps:
     1. Checkout repository with full history (`fetch-depth: 0`).
     2. Set up Python 3.12.
     3. Install `requirements.txt`.
     4. Pre-run test check: `pytest tests/test_models.py tests/test_store.py` (fast sanity check).
     5. Run digest: `python -m hnfilter.main`.
     6. Git commit & push state:
        ```bash
        git config user.name "github-actions[bot]"
        git config user.email "github-actions[bot]@users.noreply.github.com"
        git add data/seen.json
        if ! git diff --staged --quiet; then
          git commit -m "chore(state): update seen.json [skip ci]"
          git pull --rebase -X theirs origin main
          git push origin main
        fi
        ```
     7. Discord alert on failure: Send message to webhook containing link to the failed GitHub Actions run log.

**Testing Plan for Step 8:**
- **Test Objective:** Validate workflow syntax and simulate CI runs locally using `actionlint` or syntax check.
- **Validation Checklist:**
  - Verify YAML formatting with `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/daily.yml'))"`.
  - Validate secrets and environment variables are referenced correctly (`${{ secrets.GEMINI_API_KEY }}`, `${{ secrets.DISCORD_WEBHOOK_URL }}`).
- **Done When:** Workflows pass YAML linting and validation.

---

### Step 9: Live Smoke Testing & Go-Live Verification

**Goal:** Run live end-to-end tests against real APIs locally, verify Discord notifications, and execute the first GitHub Actions manual run.

**Execution Steps:**
1. **Local Dry-Run with Real APIs**:
   - Populate local `.env` with real `GEMINI_API_KEY`.
   - Run:
     ```bash
     python -m hnfilter.main --dry-run --limit 30
     ```
   - Verify terminal displays the top filtered stories with plausible scores and one-line reasons matching `interests.md`.
2. **Local Live Discord Test**:
   - Add real `DISCORD_WEBHOOK_URL` to `.env`.
   - Run live with small limit:
     ```bash
     python -m hnfilter.main --limit 20 --threshold 5
     ```
   - Check Discord channel:
     - Message formatting is crisp and readable.
     - URLs wrapped in `<...>` without annoying preview cards.
     - Points and comment counts match.
   - Inspect `data/seen.json`:
     - IDs are recorded with today's date.
3. **Second Run Deduplication Check**:
   - Immediately re-run `python -m hnfilter.main --limit 20`.
   - Verify: Stories from the first run are ignored, and a status line or only new stories are processed.
4. **GitHub Secrets Configuration**:
   - Go to GitHub Repository > **Settings** > **Secrets and variables** > **Actions**.
   - Add Secret: `GEMINI_API_KEY`
   - Add Secret: `DISCORD_WEBHOOK_URL`
   - Add Variable: `GEMINI_MODEL` (e.g., `gemini-2.5-flash`)
5. **Actions Manual Trigger**:
   - Go to **Actions** tab > **Daily HN digest** > **Run workflow**.
   - Confirm:
     - Job status is green.
     - Discord receives the morning digest.
     - A new commit updating `data/seen.json` appears on branch `main`.

---

## 4. Testing Strategy Matrix

| Layer | Target | Tool / Fixture | Scope & Edge Cases Covered |
|---|---|---|---|
| **Unit** | `models.py` | `pytest` | Ask HN missing URLs, unicode titles, invalid scores (<0, >10), non-story filtering |
| **Unit** | `config.py` | `pytest`, `monkeypatch` | Environment variable overrides, missing secrets handling, default fallback values |
| **Integration** | `hn.py` | `respx`, `pytest-asyncio` | Concurrency semaphore throttle, deleted `null` items, 500 error retries, score thresholding |
| **Unit** | `store.py` | `tmp_path` fixture | Atomic file replace, missing/corrupted file recovery, 14-day TTL pruning, deduplication |
| **Unit** | `scorer.py` | `unittest.mock` | Gemini structured output parsing, batching (0, <batch, >batch), 429 rate limit backoff |
| **Unit** | `notify.py` | `respx` | 2000-char message chunking without splitting stories, 429 Retry-After, zero-picks message |
| **E2E Mocked** | `main.py` | `tmp_path`, `respx` | Complete pipeline run, dry-run state isolation, Discord failure aborting state save |
| **Live Smoke** | CLI | Terminal / Discord | Real API keys, real prompt evaluation against `interests.md`, live webhook post |
| **CI** | GitHub Actions | `ci.yml` | Automated Ruff linting, test suite execution, coverage verification on push |

---

## 5. Edge Cases & Resilience Playbook

1. **Gemini Free Tier Quotas & 429s:**
   - 150 top stories / 40 per batch = only 4 requests per day.
   - Built-in 3-step exponential backoff (`2s`, `4s`, `8s`) ensures transient spikes or quota boundaries don't fail the job.
2. **Discord 2000-Character Message Limit:**
   - `chunk_messages()` hard-caps blocks at 1800 characters per message, ensuring safety headers and markdown delimiters never breach Discord limits.
3. **HN API Inconsistencies:**
   - Ask HN / Show HN text items lack `url`. Fallback cleanly routes to `https://news.ycombinator.com/item?id={id}`.
   - Dead or deleted items return `null` or `deleted: true`. Ignored safely without raising exceptions.
4. **GitHub Actions 60-Day Inactivity Rule:**
   - Scheduled workflows automatically pause if no commits occur in 60 days. Because `seen.json` is committed back to the repository on each run, the repo stays perpetually active.
5. **Git Merge Conflicts in Actions:**
   - Workflow runs `git pull --rebase origin main` before `git push`, eliminating rejection when manual commits occur on `main`.
6. **State Preservation on Delivery Failure:**
   - If scoring or Discord notification fails, the pipeline aborts immediately without modifying `data/seen.json`. The next execution will re-evaluate those stories rather than silently losing them.
