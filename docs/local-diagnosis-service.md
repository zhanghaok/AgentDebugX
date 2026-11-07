# Local DataAgent diagnosis service

Local replica of the intranet diagnosis service: OpenCode 2 SQLite store →
AgentDebugX `AgentTrajectory` → deterministic outcome signals → LLM detector
(`ServiceJudge`) → confidence-filtered attribution (`ServiceAttributor`) →
recovery proposals (`ZhReflexionSuggestion`) → `#299`-shaped response
envelope, served with FastAPI on `:7777`. The same process serves a native,
dependency-free HTML console for browsing sessions and running diagnoses.

## Pipeline

```
opencode.db (session_v2, session_message)
  └─ db_adapter/v2_adapter.py        -> SessionRecord (opencode export shape)
      └─ opencode_converter.py       -> AgentTrajectory (goal = first real user
                                        message; event_id = {trace}_step{n})
          └─ outcome.py              -> OutcomeSignals (rejection / unrecovered
                                        tool error / recovered tool error /
                                        empty SQL / no wrap-up / normal)
              └─ api/diagnose.py     -> ServiceJudge (detector=llm; data taxonomy,
                                        Chinese prompt, graded context)
                                    ->  ServiceAttributor (attributor=all_at_once;
                                        signals + eligible findings, failure check)
                                    ->  ZhReflexionSuggestion (recovery)
                  └─ report.py/brief.py -> #299 wire envelope + 诊断简报 markdown
                      └─ service.py  -> P4 root selection, background thread,
                                        in-memory state, diagnosis/ files
                          └─ app.py  -> POST/GET /api/v1/diagnose[...]
                                        GET /api/v1/sessions[...]
                                        GET / (static/index.html console)
```

Chinese localization (`name_zh`, `suggestion_templates_zh`, Chinese Reflexion
text) lives in `localization.py`, mirroring the intranet fork. The 11
data-scenario modes (`data.*`, `environment.*`) live in `data_taxonomy.py`
and are registered when `DiagnosisService` starts, so the OSS taxonomy is
unchanged for other callers.

## P2–P4 behaviour

* **P2 — data taxonomy.** The judge sees the extra failure modes, each with a
  positive/negative example. `root_cause_kind` maps `data.*` to `agent`,
  `environment.*` and `system.tool_execution_error`/`_llm_limit`/
  `_environment_error` to `environment`.
* **P3 — outcome before attribution.** `outcome.py` extracts deterministic
  signals before any model call: user rejection, an unrecovered failed tool
  call before the final answer, an empty SQL result, no wrap-up (tool
  result/error/aborted finish) and normal end. A failed tool call followed by
  a successful tool call before the final answer is recorded as
  `recovered_tool_error` and does not mark the run `failed`. Verdict is
  `failed`, `normal` or `ambiguous`; the attributor is told the verdict and
  must return all-null when it agrees the run did not fail. A `normal` verdict
  with no finding at or above the threshold skips attribution entirely
  (`attribution.method = "skipped"`, zero recovery proposals).
* **P4 — confidence threshold.** `AGENTDEBUG_MIN_CONFIDENCE` (default `0.5`)
  controls publication. Missing model confidence counts as `0.3`. Findings
  below the threshold stay in `report.findings` with
  `metadata.below_threshold = true`, but can never become the root cause. Root
  selection order: none when the attributor explicitly says `no_failure`,
  otherwise the attribution conclusion when it lands on an eligible finding,
  otherwise the most confident eligible finding, otherwise none.
  `report.metadata.root_cause_kind` records `agent` / `environment` / `none`.

## Endpoints

| Method | Path | Behaviour |
| --- | --- | --- |
| POST | `/api/v1/diagnose` | Body `{session_id, detector, attributor}`; `llm`/`all_at_once` only; 400 on missing fields, 404 unknown session, 503 when no LLM key. Returns `{status:"processing", session_id, detector, attributor}` |
| GET | `/api/v1/diagnose/{sessionId}` | `processing` (with nulls) → `done` (with `stored_at`/`data.trajectory/report/brief`/`elapsed_seconds`) or `failed` |
| GET | `/api/v1/sessions` | Session picker rows, newest `time_updated` first; optional `limit` (1–500, default 100) |
| GET | `/api/v1/sessions/{sessionId}` | Session metadata plus converted trajectory wire (`goal` + events); 404 unknown |
| GET | `/healthz` | Liveness + resolved db path/model/LLM state (`/api/agentdebug/healthz` for the gateway-served page) |
| GET | `/`, `/ui` | Self-contained native HTML console (`api/static/index.html`) |

The diagnose and session routes are also registered under the browser-visible
prefix `/api/agentdebug/api/v1/...` (issue #266 path), and the page is served at
`/api/agentdebug` too, so either direct wiring or the intranet Gateway
arrangement works.

## Web console

Open `http://127.0.0.1:7777/`. The page is a single HTML file with inline
CSS/JS — no build step and no CDN assets:

* the sidebar lists `session_v2` sessions (title, agent, message count, update
  time) and filters by title/session id/slug/agent;
* a sticky two-tab workspace separates **诊断报告** from **会话轨迹**. A
  session with a completed diagnosis opens directly on the report tab, so the
  result is visible without scrolling past the trajectory; a session without
  one opens on the trajectory and the report tab shows a placeholder with a
  **开始诊断** action. The report tab badge mirrors the verdict (`正常` /
  `失败` / `待确认` / `未诊断` / `诊断中`);
* selecting a session loads `GET /api/v1/sessions/{id}` and renders the
  trajectory as a user / reasoning / tool-call / tool-result timeline, with
  `step-start` and `step-finish` hidden; long reasoning text and tool payloads
  collapse to a one-line preview behind **展开全文 / 展开**, and the timeline
  header's **全部展开 / 全部收起** buttons toggle every block at once;
* the **轨迹地图** panel above the timeline draws the same events as a
  horizontal multi-lane map: failed tool calls are red, findings get an amber
  ring/flag, attribution a blue ring/flag and the published root cause a
  pulsing red node. Hover or focus a node for an event snippet; click it to
  scroll to and flash the matching timeline entry. After a diagnosis the
  report's findings, attribution primary span and root cause are also marked
  inline on the timeline (`发现 xx%` / `归因` / `根因` badges);
* **开始诊断** posts `{detector:"llm", attributor:"all_at_once"}`, polls
  `GET /api/v1/diagnose/{id}`, and renders the returned report: outcome
  signals, root-cause kind, findings (with below-threshold badges), all-at-once
  attribution, recovery proposals, the Markdown brief and artifact paths;
* `?session=<id>` deep-links to a session; if that session already has a
  completed in-memory diagnosis, the report is restored on selection.

All dynamic strings are escaped with `textContent`/an HTML escaper before
insertion, and the page makes no external requests beyond same-origin API
calls.

Artifacts are written to `diagnosis/` (git-ignored):
`session_{id}_trajectory.json`, `session_{id}_report.json`,
`session_{id}_brief.md`; `stored_at` mirrors those relative paths.

## Run

```bash
scripts/local-diagnosis-service.sh start
scripts/local-diagnosis-service.sh status
scripts/local-diagnosis-service.sh logs
scripts/local-diagnosis-service.sh stop
```

The script loads `dataagent/.local-dev/config/local.env` and maps
`GTS_LLM_BASE_URL`/`GTS_LLM_API_KEY` → `AGENTDEBUG_LLM_*` (default model
`deepseek-flash`), points `OPENCODE_DB` at the local runtime store, and
excludes localhost from any proxy. Then open `http://127.0.0.1:7777/` for the
console, or use the curl workflow below.

### Local credentials file (`local.env`)

The lifecycle script reads the detector LLM credentials from a shell-style
env file. **It is not part of this repository; create it locally and never
commit it.**

```
$DATAAGENT_HOME/.local-dev/config/local.env
# default: ~/develop/dataagent/.local-dev/config/local.env
```

Override the location with `DATAAGENT_ENV_FILE=/path/to/local.env`. The script
sources the file with `set -a`, so plain `KEY=value` lines are exported to the
service process; shell quoting and `export` are allowed but not required.
Minimum content for a working diagnosis:

```bash
# OpenAI-compatible endpoint root. If your gateway URL already ends in
# /chat/completions, drop that suffix here: the client appends it.
GTS_LLM_BASE_URL=https://your-llm-gateway.example.com/v1
GTS_LLM_API_KEY=sk-replace-with-your-key

# Optional. The script defaults to deepseek-flash; set this directly to pick
# another model. The other GTS_*/AGUI_*/KNOWLEDGE_* entries in the dataagent
# file are read but ignored by this service.
AGENTDEBUG_LLM_MODEL=deepseek-flash
```

The service-native names also work directly, without a dataagent checkout:
`AGENTDEBUG_LLM_BASE_URL`, `AGENTDEBUG_LLM_API_KEY`,
`AGENTDEBUG_LLM_MODEL`. `OPENCODE_DB` defaults to
`$DATAAGENT_HOME/.local-dev/runtime/opencode.db`. Without a usable base URL
and key, `/healthz` reports `llm_configured: false` and diagnosis submissions
return `503`; session browsing still works.

Keep `local.env` outside the checkout (`.local-dev/` and `local.env` are
git-ignored here only as a safety net) and rotate the key if it was ever
copied into a tracked file.

Manual smoke test:

```bash
curl -s localhost:7777/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"ses_xxx","detector":"llm","attributor":"all_at_once"}'
curl -s localhost:7777/api/v1/diagnose/ses_xxx
```

## Environment

| Variable | Meaning | Default |
| --- | --- | --- |
| `OPENCODE_DB` | OpenCode SQLite store | `~/.dataagent/opencode.db` |
| `DIAGNOSIS_OUT_DIR` | artifact directory | `<repo>/diagnosis` |
| `AGENTDEBUG_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | detector LLM | none / none / `deepseek-flash` |
| `AGENTDEBUG_LLM_TIMEOUT` | per-request timeout (s) | `180` |
| `AGENTDEBUG_MIN_CONFIDENCE` | minimum confidence for root-cause selection | `0.5` |
| `AGENTDEBUG_LANGUAGE` | `zh` service prompts or `en` upstream prompts | `zh` |
| `AGENTDEBUG_OUTCOME_REJECTION_TERMS` | comma-separated user-rejection terms | Chinese defaults |
| `AGENTDEBUG_OUTCOME_EMPTY_MARKERS` | comma-separated empty-result markers | `0 rows`, `无数据`, … |
| `DIAGNOSIS_HOST` / `DIAGNOSIS_PORT` | listen address | `127.0.0.1` / `7777` |

## Fidelity notes

* `event_id` is deterministic (`{trace_id}_step{n}`), `step_index` strictly
  increasing; a `tool` part becomes `TOOL_CALL` + `TOOL_RESULT`, both carrying
  `tool`/`tool_status`/`callID` plus `message_id`/`message_idx`/`part_idx`/`role`.
* DataAgent prompt envelopes (`<environment>`, `<server_tools>`, …) are
  stripped from visible text (`sanitize.py`); synthetic client-tool
  continuation messages are skipped.
* `attribution.elapsed_ms` is pinned to `0`, matching both intranet samples.
* `metadata.recovery_target` comes from upstream `DiagnoseContext.build()`;
  `root_cause_*` is then overwritten by the P4 selector so a below-threshold
  attribution can never be published as the root cause.
* The judge renders the judged chunk with the `th1` budget (1024 chars) and
  the rest of the run with `th3` (256 chars) via `GradedContextBuilder`. The
  pool is built deterministically from `clip_middle` — no extra model calls.
* Original sessions are never persisted: only trajectory/report/brief files.
