# Local DataAgent diagnosis service

Local replica of the intranet diagnosis service: OpenCode 2 SQLite store →
AgentDebugX `AgentTrajectory` → LLM detector (`LLMJudgeAnalyzer`) →
`all_at_once` attribution (`AllAtOnceAttributor`) → recovery proposals
(`ReflexionSuggestion`, Chinese text) → `#299`-shaped response envelope,
served with FastAPI on `:7777`.

## Pipeline

```
opencode.db (session_v2, session_message)
  └─ db_adapter/v2_adapter.py        -> SessionRecord (opencode export shape)
      └─ opencode_converter.py       -> AgentTrajectory (event_id = {trace}_step{n})
          └─ DiagnosePipeline        -> LLMJudgeAnalyzer (detector=llm)
                                    ->  AllAtOnceAttributor (attributor=all_at_once)
                                    ->  ZhReflexionSuggestion (recovery)
              └─ report.py/brief.py  -> #299 wire envelope + 诊断简报 markdown
                  └─ service.py      -> background thread, in-memory state, diagnosis/ files
                      └─ app.py      -> POST/GET /api/v1/diagnose[...]
```

Chinese localization (`name_zh`, `suggestion_templates_zh`, Chinese Reflexion
text) lives in `localization.py`, mirroring the intranet fork.

## Endpoints

| Method | Path | Behaviour |
| --- | --- | --- |
| POST | `/api/v1/diagnose` | Body `{session_id, detector, attributor}`; `llm`/`all_at_once` only; 400 on missing fields, 404 unknown session, 503 when no LLM key. Returns `{status:"processing", session_id, detector, attributor}` |
| GET | `/api/v1/diagnose/{sessionId}` | `processing` (with nulls) → `done` (with `stored_at`/`data.trajectory/report/brief`/`elapsed_seconds`) or `failed` |
| GET | `/healthz` | Liveness + resolved db path/model/LLM state |

The same two diagnose routes are also registered under the browser-visible
prefix `/api/agentdebug/api/v1/diagnose[...]` (issue #266 path), so either
direct wiring from the UI or the intranet Gateway arrangement works.

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
excludes localhost from any proxy.

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
| `DIAGNOSIS_HOST` / `DIAGNOSIS_PORT` | listen address | `127.0.0.1` / `7777` |

## Fidelity notes

* `event_id` is deterministic (`{trace_id}_step{n}`), `step_index` strictly
  increasing; a `tool` part becomes `TOOL_CALL` + `TOOL_RESULT`, both carrying
  `tool`/`tool_status`/`callID` plus `message_id`/`message_idx`/`part_idx`/`role`.
* DataAgent prompt envelopes (`<environment>`, `<server_tools>`, …) are
  stripped from visible text (`sanitize.py`); synthetic client-tool
  continuation messages are skipped.
* `attribution.elapsed_ms` is pinned to `0`, matching both intranet samples.
* The report `metadata.recovery_target` / `root_cause_*` grafting comes from
  upstream `DiagnoseContext.build()` — no extra code needed.
* Original sessions are never persisted: only trajectory/report/brief files.
