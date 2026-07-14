# Tango Local HTTP Job Server

## Status

Proposed

## Summary

Add a small local HTTP server to Tango that allows callers to start, inspect, stop, and retry Tango workflows through HTTP requests.

The server starts when Tango is invoked without a workflow command:

```bash
python tango.py
```

Server-specific arguments may be supplied:

```bash
python tango.py --host 127.0.0.1 --port 8765
```

Existing workflow commands remain unchanged:

```bash
python tango.py plan ...
python tango.py implement ...
python tango.py phase ...
```

Each HTTP job runs Tango as a separate child process using the existing CLI interface. The server does not execute workflow logic directly inside server threads.

The initial implementation uses only the Python standard library and preserves Tango’s current no-install, single-command usage model.

---

# 1. Motivation

Tango currently runs as a synchronous command-line workflow. This works well for interactive terminal use, but external tools cannot easily:

- start a Tango workflow programmatically;
- inspect whether it is queued, running, completed, or failed;
- retrieve output incrementally;
- stop an active workflow;
- retry a failed or stopped workflow;
- manage multiple workflows without controlling terminal processes directly.

A local HTTP server provides a small automation boundary around the existing CLI without changing the core orchestration model.

The server is intended primarily for:

- local personal automation;
- integrations with local tools;
- Telegram or personal-assistant integrations;
- lightweight dashboards;
- scripts that need asynchronous job control;
- future orchestration layers.

It is not intended to be a public multi-user service.

---

# 2. Goals

The change must:

1. Start a local HTTP server when Tango is run without `plan`, `implement`, or `phase`.
2. Preserve all existing Tango CLI commands and behavior.
3. Accept complete Tango workflow arguments through JSON HTTP requests.
4. Run each workflow as an isolated Tango subprocess.
5. Persist job requests, status, and output to disk.
6. Allow job status to survive server restarts.
7. Allow callers to retrieve logs incrementally.
8. Allow callers to stop running or queued jobs.
9. Allow callers to retry an existing job using the original request.
10. Prevent two jobs from concurrently modifying the same repository.
11. Support limited concurrency across different repositories.
12. Bind to localhost by default.
13. Remain standard-library-only.
14. Shut down active child process groups cleanly when the server exits.

---

# 3. Non-goals

The first implementation will not include:

- a browser-based user interface;
- WebSocket support;
- Server-Sent Events;
- remote file upload;
- arbitrary shell command execution;
- arbitrary environment-variable injection;
- multi-user accounts;
- TLS termination;
- OAuth;
- job priorities;
- scheduled or recurring jobs;
- editing a queued job;
- deleting job history through the API;
- distributed workers;
- execution on remote machines;
- automatic Git worktree creation;
- automatic cloning of repositories;
- automatic agent authentication;
- an embedded database;
- Windows process-group support beyond best-effort compatibility.

---

# 4. Core design decision

## 4.1 Workflows run as subprocesses

Every HTTP job must invoke Tango through its normal CLI interface.

Example child command:

```bash
python /path/to/tango.py implement \
  --phase 15 \
  --writer codex \
  --reviewer claude \
  --repo-dir /Users/yaron/LLM/Rally \
  --spec docs/spec.md \
  --plan docs/plan.md \
  --max-iters 3
```

The server must not call Tango workflow functions directly inside the HTTP server process.

## 4.2 Rationale

Subprocess execution provides:

- isolation from Tango’s module-level mutable configuration;
- isolation from `sys.exit()` calls;
- independent exit codes;
- straightforward stdout/stderr capture;
- reliable stop behavior;
- clean separation between server state and workflow state;
- compatibility with the existing CLI;
- reduced risk of one job corrupting another job’s in-memory state.

This also allows the HTTP layer to remain thin.

---

# 5. CLI behavior

## 5.1 Server mode

Running Tango without a workflow command starts the server:

```bash
python tango.py
```

Equivalent explicit configuration:

```bash
python tango.py \
  --host 127.0.0.1 \
  --port 8765 \
  --jobs-dir ~/.tango/jobs \
  --max-concurrent 1
```

## 5.2 Workflow mode

Existing workflow commands remain valid and retain their existing semantics:

```bash
python tango.py plan ...
python tango.py implement ...
python tango.py phase ...
```

No existing workflow flag may change meaning.

## 5.3 Command detection

The positional workflow command becomes optional:

```python
parser.add_argument(
    "step",
    nargs="?",
    choices=["plan", "implement", "phase"],
)
```

After parsing:

```python
if args.step is None:
    run_server(server_options)
else:
    run_workflow(workflow_options)
```

Implementation may use separate parsers internally if that results in clearer validation.

## 5.4 Server options

The following options are supported only when no workflow command is supplied.

### `--host`

Interface on which to listen.

```text
Default: 127.0.0.1
```

Example:

```bash
python tango.py --host 127.0.0.1
```

### `--port`

TCP port on which to listen.

```text
Default: 8765
Range: 1–65535
```

Example:

```bash
python tango.py --port 9000
```

### `--jobs-dir`

Directory used for persistent server job state.

```text
Default: ~/.tango/jobs
```

The path must support `~` expansion and be resolved to an absolute path.

### `--max-concurrent`

Maximum number of child workflows that may run concurrently across distinct repositories.

```text
Default: 1
Minimum: 1
```

This limit does not override the per-repository lock. Only one job per repository may run at a time regardless of this value.

### `--allowed-repo`

Optional repository root that jobs are allowed to target.

The option may be repeated:

```bash
python tango.py \
  --allowed-repo ~/LLM/Rally \
  --allowed-repo ~/LLM/Tango
```

Rules:

- Paths are expanded, resolved, and normalized at startup.
- When no `--allowed-repo` is supplied, any local Git repository is allowed.
- When at least one is supplied, every submitted `repo_dir` must resolve to one of the explicitly allowed repository paths.
- Being a child directory of an allowed path does not implicitly make it allowed unless that child directory resolves to the same Git repository root.
- Symbolic links must not bypass the allowlist.

## 5.5 Invalid CLI combinations

Server-only flags must not be accepted alongside a workflow command.

Invalid:

```bash
python tango.py plan \
  --phase 3 \
  --writer claude \
  --reviewer codex \
  --port 8765
```

Workflow-only arguments must not be accepted in server mode.

Invalid:

```bash
python tango.py \
  --phase 3 \
  --writer claude \
  --reviewer codex
```

The command must exit non-zero and print a clear argument error.

---

# 6. Server implementation

## 6.1 Standard library

The server must use Python’s standard library.

Recommended components:

```python
http.server.ThreadingHTTPServer
http.server.BaseHTTPRequestHandler
threading
queue
subprocess
signal
pathlib
json
datetime
urllib.parse
secrets
os
sys
time
```

No FastAPI, Flask, database package, or external HTTP server dependency is introduced.

## 6.2 Suggested source organization

The initial implementation may remain in `tango.py`, but server responsibilities should be separated logically.

Preferred structure:

```text
tango.py
tango_server.py
```

Responsibilities:

### `tango.py`

- existing workflow behavior;
- workflow CLI argument parsing;
- top-level mode selection;
- invocation of `run_server()` in server mode.

### `tango_server.py`

- server argument definitions;
- request validation;
- HTTP routing;
- job persistence;
- scheduler;
- process management;
- stop and retry behavior.

The server module must not import or invoke mutable workflow internals to execute a job. It may import shared constants or pure validation helpers.

If keeping one file is materially simpler, the implementation may remain self-contained, provided the responsibilities are clearly separated.

---

# 7. API overview

Default base URL:

```text
http://127.0.0.1:8765
```

Required endpoints:

```text
GET  /help
POST /jobs
GET  /jobs/{id}
GET  /jobs/{id}/status
GET  /jobs/{id}/logs
POST /jobs/{id}/stop
POST /jobs/{id}/retry
```

`GET /jobs/{id}` and `GET /jobs/{id}/status` return the same status representation.

`POST /jobs` is the canonical start endpoint.

For convenience and compatibility with the initially proposed route, the implementation may also support:

```text
POST /jobs/start
```

If supported, it must behave exactly like `POST /jobs`.

Optional health endpoint:

```text
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

The health endpoint does not execute tools.

## 7.1 Agent help endpoint

```http
GET /help
```

Purpose: let an agent that only knows the base URL discover how to call this API, without out-of-band documentation.

The endpoint requires no request body and returns a static, self-contained JSON description of the API.

Response:

```http
200 OK
Content-Type: application/json; charset=utf-8
```

```json
{
  "name": "tango-server",
  "description": "Tango runs an adversarial coding workflow against a local Git repository: a writer agent (Claude or Codex) drafts a plan or writes code for one phase, and a reviewer agent (the other model) checks it in a read-only sandbox and returns a machine-parseable verdict; writer and reviewer repeat until the reviewer approves or max_iters is reached. This server runs that workflow as background jobs over HTTP instead of a blocking terminal command, so a calling agent can start a job, poll its status and logs, stop it, or retry it without holding a process open.",
  "workflow_steps": {
    "plan": "Writer drafts an implementation plan for the phase into plans/phase-N.md; reviewer approves or requests changes. Does not write code.",
    "implement": "Writer codes the already-approved plan for the phase and commits; reviewer inspects the diff and approves or requests changes. Requires an approved plan to already exist.",
    "phase": "Runs plan then implement for the phase back to back."
  },
  "base_url": "http://127.0.0.1:8765",
  "endpoints": [
    {
      "method": "POST",
      "path": "/jobs",
      "description": "Start a new Tango workflow job.",
      "request_fields": {
        "step": {"type": "enum", "values": ["plan", "implement", "phase"], "required": true},
        "phase": {"type": "string", "required": true},
        "writer": {"type": "enum", "values": ["claude", "codex"], "required": true},
        "reviewer": {"type": "enum", "values": ["claude", "codex"], "required": true},
        "repo_dir": {"type": "string", "required": true, "description": "Path to target Git repository."},
        "spec": {"type": "string", "required": false},
        "plan": {"type": "string", "required": false},
        "config": {"type": "string", "required": false},
        "base_sha": {"type": "string", "required": false},
        "max_iters": {"type": "integer", "required": false, "range": [1, 100]},
        "claude_model": {"type": "string", "required": false, "nullable": true},
        "codex_model": {"type": "string", "required": false, "nullable": true},
        "claude_effort": {"type": "string", "required": false, "nullable": true},
        "codex_effort": {"type": "string", "required": false, "nullable": true},
        "no_stream": {"type": "boolean", "required": false, "default": false},
        "dry_run": {"type": "boolean", "required": false, "default": false},
        "reset": {"type": "boolean", "required": false, "default": false},
        "resume_claude": {"type": "string", "required": false, "nullable": true},
        "resume_codex": {"type": "string", "required": false, "nullable": true}
      },
      "response": "202 Accepted with job id, status, status_url, logs_url",
      "example_request": {
        "step": "phase",
        "phase": "3",
        "writer": "claude",
        "reviewer": "codex",
        "repo_dir": "/Users/me/code/project"
      }
    },
    {"method": "GET", "path": "/jobs/{id}", "description": "Get full job status."},
    {"method": "GET", "path": "/jobs/{id}/status", "description": "Alias of GET /jobs/{id}."},
    {"method": "GET", "path": "/jobs/{id}/logs", "description": "Read combined stdout/stderr incrementally.", "query_params": {"offset": "byte offset, default 0", "limit": "max bytes, default 65536, max 1048576"}},
    {"method": "POST", "path": "/jobs/{id}/stop", "description": "Stop a queued or running job."},
    {"method": "POST", "path": "/jobs/{id}/retry", "description": "Retry a failed, stopped, or succeeded job as a new job.", "request_fields": {"reset": {"type": "boolean", "required": false}}},
    {"method": "GET", "path": "/health", "description": "Liveness check."},
    {"method": "GET", "path": "/help", "description": "This document."}
  ],
  "job_statuses": ["queued", "running", "succeeded", "failed", "stopping", "stopped", "interrupted"],
  "error_shape": {
    "error": {"code": "string", "message": "string", "details": {}}
  },
  "notes": [
    "One active job per canonical Git repository root at a time.",
    "Jobs run as detached subprocess groups; stdin is closed, so interactive prompts fail the job rather than blocking.",
    "Poll /jobs/{id}/logs with next_offset for incremental output."
  ]
}
```

Implementation must build this document from the same field/enum definitions used for request validation (§10.3–§10.5), not a hand-maintained duplicate, so it cannot drift out of sync with actual behavior.

The `/help` response is static per server build/config and does not depend on request contents.

---

# 8. HTTP conventions

## 8.1 Content type

Requests with JSON bodies must use:

```http
Content-Type: application/json
```

Responses must use:

```http
Content-Type: application/json; charset=utf-8
```

Logs returned as JSON also use the JSON content type.

## 8.2 Request size

The server must enforce a maximum JSON request-body size.

Recommended initial limit:

```text
1 MiB
```

Requests exceeding the limit return:

```http
413 Payload Too Large
```

## 8.3 Unknown paths

Unknown paths return:

```http
404 Not Found
```

## 8.4 Unsupported methods

Unsupported methods return:

```http
405 Method Not Allowed
```

## 8.5 JSON error shape

All API errors use:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Human-readable explanation.",
    "details": {}
  }
}
```

`details` may be omitted when unnecessary.

The response must not expose:

- stack traces;
- unrelated environment variables;
- server filesystem content outside relevant validated paths.

---

# 9. Starting a job

## 10.1 Endpoint

```http
POST /jobs
```

Optional alias:

```http
POST /jobs/start
```

## 10.2 Request body

Example:

```json
{
  "step": "implement",
  "phase": "15",
  "writer": "codex",
  "reviewer": "claude",
  "repo_dir": "/Users/yaron/LLM/Rally",
  "spec": "docs/superpowers/specs/2026-06-28-milestone7-orchestration-design.md",
  "plan": "docs/superpowers/plans/2026-07-08-m7-phase15-basic-recovery.md",
  "config": "impl-prompts.toml",
  "base_sha": "7d7facf",
  "max_iters": 3,
  "claude_model": null,
  "codex_model": null,
  "claude_effort": null,
  "codex_effort": null,
  "no_stream": false,
  "dry_run": false,
  "reset": false,
  "resume_claude": null,
  "resume_codex": null
}
```

## 10.3 Supported fields

### Required fields

#### `step`

String enum:

```text
plan
implement
phase
```

#### `phase`

Non-empty string.

The phase identifier is passed to the existing CLI unchanged after validation.

#### `writer`

String enum:

```text
claude
codex
```

#### `reviewer`

String enum:

```text
claude
codex
```

#### `repo_dir`

String path to the target Git repository.

The server resolves the path before storing or scheduling the job.

### Optional fields

#### `spec`

String path.

Equivalent to the CLI `--spec`.

Relative paths retain normal Tango semantics and are interpreted relative to the target repository.

#### `plan`

String path.

Equivalent to `--plan`.

#### `config`

String path.

Equivalent to `--config`.

The server must preserve the existing CLI path-resolution behavior.

#### `base_sha`

String Git revision.

Equivalent to `--base-sha`, when supported by the current workflow command.

#### `max_iters`

Positive integer.

Equivalent to `--max-iters`.

Recommended validation range:

```text
1–100
```

Default:

```text
Use Tango’s existing CLI default.
```

#### `claude_model`

String or null.

Equivalent to `--claude-model`.

#### `codex_model`

String or null.

Equivalent to `--codex-model`.

#### `claude_effort`

String or null.

Must match the effort values currently accepted by Tango’s CLI.

#### `codex_effort`

String or null.

Must match the effort values currently accepted by Tango’s CLI.

#### `no_stream`

Boolean.

Equivalent to `--no-stream`.

This affects Tango’s own child behavior. The server still captures all child output.

Default:

```json
false
```

#### `dry_run`

Boolean.

Equivalent to `--dry-run`.

Default:

```json
false
```

#### `reset`

Boolean.

Equivalent to `--reset`.

Default:

```json
false
```

#### `resume_claude`

String or null.

Equivalent to `--resume-claude`.

#### `resume_codex`

String or null.

Equivalent to `--resume-codex`.

## 10.4 Unknown fields

Unknown properties must be rejected.

Example response:

```http
400 Bad Request
```

```json
{
  "error": {
    "code": "unknown_field",
    "message": "Unknown request field: max_iterations.",
    "details": {
      "field": "max_iterations"
    }
  }
}
```

This prevents silent mistakes and limits the server’s execution surface.

## 10.5 Argument compatibility

The server must validate that submitted fields are valid for the selected workflow step.

For example:

- fields unsupported by `plan` must be rejected if the CLI would reject them;
- required plan inputs for `implement` must follow the existing CLI behavior;
- `phase` must support the same set of relevant options as the CLI;
- the server must not invent HTTP-only workflow semantics.

Where practical, HTTP request validation and CLI validation should share pure helper functions.

## 10.6 Path validation

Before accepting a job:

1. Expand `~` in `repo_dir`.
2. Resolve it to an absolute canonical path.
3. Confirm that it exists.
4. Confirm that it is a directory.
5. Confirm that it belongs to a Git repository.
6. Resolve the Git repository root.
7. Apply the allowed-repository policy.
8. Store the canonical repository root in the request record.

The server should validate Git repository membership with a command equivalent to:

```bash
git -C <repo_dir> rev-parse --show-toplevel
```

No shell invocation may be used.

If the submitted path is within a Git repository, the canonical repository identity is the top-level Git root.

## 10.7 Git revision validation

When `base_sha` is supplied, validate it before accepting the job using an argv-based Git command such as:

```bash
git -C <repo_dir> rev-parse --verify <base_sha>^{commit}
```

Invalid revisions return `400`.

This validation confirms that the revision exists at submission time. The repository may still change before a queued job begins.

## 10.8 Response

Successful job creation returns:

```http
202 Accepted
```

Example:

```json
{
  "id": "job-20260713-143012-a81f",
  "status": "queued",
  "created_at": "2026-07-13T14:30:12.483+03:00",
  "status_url": "/jobs/job-20260713-143012-a81f/status",
  "logs_url": "/jobs/job-20260713-143012-a81f/logs"
}
```

The job is persisted before the response is returned.

---

# 10. Job identifiers

Job IDs must:

- be unique;
- be safe as directory names;
- be difficult to collide accidentally;
- not expose a sequential database identifier.

Recommended format:

```text
job-YYYYMMDD-HHMMSS-<random>
```

Example:

```text
job-20260713-143012-a81f
```

The random component should contain sufficient entropy to avoid collisions.

If a collision occurs, generate another ID.

Job IDs must be validated before using them in filesystem paths.

Recommended allowed pattern:

```text
^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$
```

The server must never concatenate an unvalidated ID directly into a path.

---

# 11. Job status

## 12.1 Endpoint

Canonical endpoint:

```http
GET /jobs/{id}
```

Alias:

```http
GET /jobs/{id}/status
```

Both return the same body.

## 12.2 Status values

Required states:

```text
queued
running
succeeded
failed
stopping
stopped
```

Optional restart-recovery state:

```text
interrupted
```

If `interrupted` is not implemented, jobs left in `running` or `stopping` after a server restart become `failed` with a clear error reason.

## 12.3 Response

```json
{
  "id": "job-20260713-143012-a81f",
  "status": "running",
  "step": "implement",
  "phase": "15",
  "writer": "codex",
  "reviewer": "claude",
  "repo_dir": "/Users/yaron/LLM/Rally",
  "created_at": "2026-07-13T14:30:12.483+03:00",
  "started_at": "2026-07-13T14:30:13.105+03:00",
  "finished_at": null,
  "exit_code": null,
  "pid": 48217,
  "retry_of": null,
  "error": null
}
```

## 12.4 Field definitions

### `id`

Job identifier.

### `status`

Current lifecycle state.

### `step`

Submitted Tango workflow step.

### `phase`

Submitted phase identifier.

### `writer`

Writer agent.

### `reviewer`

Reviewer agent.

### `repo_dir`

Canonical Git repository root.

### `created_at`

ISO 8601 timestamp including timezone.

### `started_at`

ISO 8601 timestamp or null.

### `finished_at`

ISO 8601 timestamp or null.

### `exit_code`

Child exit code or null.

### `pid`

Current child PID or null.

PID is informational only and must not be accepted from clients for control operations.

### `retry_of`

Original job ID when this job is a retry, otherwise null.

### `error`

Structured server-side failure information or null.

Example:

```json
{
  "code": "server_restart",
  "message": "The server restarted while this job was running."
}
```

Normal Tango failures should primarily be represented by the child exit code and captured logs.

## 12.5 Missing job

```http
404 Not Found
```

```json
{
  "error": {
    "code": "job_not_found",
    "message": "Job 'job-...' was not found."
  }
}
```

---

# 12. Job logs

## 13.1 Endpoint

```http
GET /jobs/{id}/logs
```

## 13.2 Basic behavior

The endpoint returns combined stdout and stderr from the child Tango process.

The child is launched with:

```python
stderr=subprocess.STDOUT
```

so output ordering is preserved as closely as practical.

## 13.3 Incremental reads

Supported query parameters:

### `offset`

Byte offset from which to start reading.

```text
Default: 0
Minimum: 0
```

### `limit`

Maximum bytes to return.

Recommended defaults:

```text
Default: 65536
Maximum: 1048576
```

Example:

```http
GET /jobs/job-123/logs?offset=18422&limit=65536
```

## 13.4 Response

```json
{
  "id": "job-20260713-143012-a81f",
  "offset": 18422,
  "next_offset": 25104,
  "complete": false,
  "content": "[tango] Running implementation review...\n"
}
```

### `offset`

Actual starting offset.

### `next_offset`

Offset to use for the next request.

### `complete`

True when the job is terminal and `next_offset` is at or beyond the current end of the log.

### `content`

UTF-8-decoded output.

Invalid byte sequences should be replaced rather than causing the request to fail.

## 13.5 Offset beyond end of file

If `offset` is beyond the current file size:

- return an empty `content`;
- return the current file size as `next_offset`;
- do not return an error.

## 13.6 Missing log file

Every accepted job should have a log file created immediately.

If it is unexpectedly missing, return:

```http
500 Internal Server Error
```

with a sanitized error.

## 13.7 Streaming

SSE and WebSocket log streaming are explicitly deferred.

Clients should poll using `next_offset`.

---

# 13. Stopping a job

## 14.1 Endpoint

```http
POST /jobs/{id}/stop
```

The request body is optional and ignored if empty.

Unknown non-empty body fields should be rejected.

## 14.2 Queued jobs

Stopping a queued job:

1. removes it from effective scheduling;
2. marks it `stopped`;
3. sets `finished_at`;
4. leaves `started_at` null;
5. leaves `exit_code` null.

Response:

```http
200 OK
```

```json
{
  "id": "job-...",
  "status": "stopped"
}
```

It is acceptable for an internal queue entry to remain physically present as long as the scheduler skips it based on persisted state.

## 14.3 Running jobs

Stopping a running job:

1. atomically changes status to `stopping`;
2. sends `SIGTERM` to the child process group;
3. waits for a fixed grace period;
4. sends `SIGKILL` to the process group if it remains alive;
5. waits for the process to be reaped;
6. marks the job `stopped`;
7. records its exit code when available;
8. sets `finished_at`;
9. releases repository and concurrency locks.

Recommended grace period:

```text
10 seconds
```

## 14.4 Process groups

On POSIX systems, jobs must start in a new session:

```python
subprocess.Popen(
    argv,
    start_new_session=True,
    ...
)
```

Stop signals must target the process group:

```python
os.killpg(process.pid, signal.SIGTERM)
```

This is required because Tango may launch Claude, Codex, Git, and descendant processes.

Killing only the immediate Python process is insufficient.

## 14.5 Terminal jobs

Stopping a job already in a terminal state is idempotent.

For:

```text
succeeded
failed
stopped
```

return:

```http
200 OK
```

with the existing status.

## 14.6 Stopping state

If another stop request arrives while a job is `stopping`, return its current state without initiating another independent stop sequence.

---

# 14. Retrying a job

## 15.1 Endpoint

```http
POST /jobs/{id}/retry
```

## 15.2 Behavior

Retry creates a new job.

It must not:

- reset the old job’s status;
- reuse the old job ID;
- truncate the old log;
- modify the old request record.

The new job copies the original validated workflow request.

## 15.3 Request body

Optional body:

```json
{
  "reset": false
}
```

The only supported override in the initial implementation is `reset`.

Rules:

- omitted `reset` preserves the original request’s value;
- `reset: true` forces the new request to use Tango’s `--reset`;
- `reset: false` forces the retry not to use `--reset`.

Unknown fields are rejected.

## 15.4 Retry and Tango checkpoints

Default retry behavior should allow Tango’s normal checkpoint/resume behavior to operate.

The retry endpoint must not delete:

```text
.agent-workflow/
```

unless the copied request or retry override explicitly enables Tango’s existing reset behavior.

## 15.5 Eligible source states

Retry is permitted for jobs in these states:

```text
failed
stopped
succeeded
```

Allowing retries of succeeded jobs provides reproducible reruns, though callers should use `reset` deliberately.

Retry of:

```text
queued
running
stopping
```

returns:

```http
409 Conflict
```

## 15.6 Response

```http
202 Accepted
```

```json
{
  "id": "job-20260713-144102-b92c",
  "status": "queued",
  "retry_of": "job-20260713-143012-a81f",
  "status_url": "/jobs/job-20260713-144102-b92c/status",
  "logs_url": "/jobs/job-20260713-144102-b92c/logs"
}
```

---

# 15. Job persistence

## 16.1 Storage layout

Default:

```text
~/.tango/jobs/
```

Each job gets a dedicated directory:

```text
~/.tango/jobs/
  job-20260713-143012-a81f/
    request.json
    status.json
    output.log
```

Optional internal files may be added later, but these three are the stable core.

## 16.2 `request.json`

Contains the validated and normalized workflow request.

Example:

```json
{
  "step": "implement",
  "phase": "15",
  "writer": "codex",
  "reviewer": "claude",
  "repo_dir": "/Users/yaron/LLM/Rally",
  "spec": "docs/spec.md",
  "plan": "docs/plan.md",
  "config": "impl-prompts.toml",
  "base_sha": "7d7facf",
  "max_iters": 3,
  "claude_model": null,
  "codex_model": null,
  "claude_effort": null,
  "codex_effort": null,
  "no_stream": false,
  "dry_run": false,
  "reset": false,
  "resume_claude": null,
  "resume_codex": null
}
```

Secrets must not be stored here.

## 16.3 `status.json`

Contains lifecycle state.

Example:

```json
{
  "id": "job-20260713-143012-a81f",
  "status": "failed",
  "created_at": "2026-07-13T14:30:12.483+03:00",
  "started_at": "2026-07-13T14:30:13.105+03:00",
  "finished_at": "2026-07-13T14:42:44.901+03:00",
  "exit_code": 1,
  "pid": null,
  "retry_of": null,
  "error": null
}
```

## 16.4 `output.log`

Combined child stdout and stderr.

The server may prepend a small server-generated header containing non-secret job metadata, for example:

```text
[tango-server] Job: job-...
[tango-server] Started: ...
[tango-server] Command: python tango.py implement ...
```

If logging the command, sensitive values must be redacted. The current workflow request contains no token or arbitrary environment fields, but redaction should still be centralized.

## 16.5 Atomic writes

`request.json` and `status.json` must be written atomically.

Required pattern:

1. write complete JSON to a temporary file in the same directory;
2. flush it;
3. optionally call `fsync`;
4. rename it over the target using an atomic replace operation.

Example conceptual flow:

```text
status.json.tmp.<random>
    ↓
os.replace(...)
    ↓
status.json
```

The server must not expose partially written JSON.

## 16.6 Directory creation

The jobs root is created automatically if missing.

Permissions should be restricted where supported.

Recommended POSIX mode:

```text
0700 for directories
0600 for files
```

The implementation should not fail solely because exact permission modes are unavailable on a platform.

---

# 16. Job lifecycle

## 17.1 Normal successful flow

```text
queued
  ↓
running
  ↓
succeeded
```

A zero child exit code produces `succeeded`.

## 17.2 Workflow failure

```text
queued
  ↓
running
  ↓
failed
```

A non-zero child exit code produces `failed`, unless the server initiated a stop.

## 17.3 Stop flow

```text
queued → stopped
```

or:

```text
running
  ↓
stopping
  ↓
stopped
```

## 17.4 Server-side launch failure

If the server cannot start the subprocess:

```text
queued
  ↓
running or failed directly
  ↓
failed
```

The final record must include a sanitized `error`.

Example:

```json
{
  "code": "process_start_failed",
  "message": "Unable to start the Tango child process."
}
```

Detailed diagnostics may be appended to `output.log`, provided they do not expose secrets.

## 17.5 State synchronization

All in-memory and on-disk state transitions must be protected against races.

The implementation must prevent:

- a completion handler changing `stopped` back to `failed`;
- two scheduler threads starting the same job;
- retry reading a partially written request;
- a stop operation racing with process launch;
- repository locks being released twice.

A per-job lock or a central job-manager lock may be used.

---

# 17. Scheduler and concurrency

## 18.1 Global concurrency

The number of running child workflows must not exceed:

```text
--max-concurrent
```

## 18.2 Per-repository concurrency

Only one job may be active for a canonical Git repository at a time.

Active means:

```text
running
stopping
```

Queued jobs do not hold the repository lock.

## 18.3 Queue behavior

When a job cannot start because:

- the global concurrency limit is reached; or
- another job is active for the same repository;

it remains `queued`.

The scheduler starts it when both conditions become available.

## 18.4 Queue ordering

Use first-in, first-out ordering based on `created_at`.

A queued job blocked by a busy repository must not necessarily block unrelated jobs behind it.

Example:

```text
Job A: repo-1, running
Job B: repo-1, queued
Job C: repo-2, queued
```

With available global capacity, Job C may start while Job B remains queued.

This avoids head-of-line blocking.

## 18.5 Repository identity

Repository equality is based on the resolved Git top-level path, not the raw submitted string.

These must be treated as the same repository:

```text
/Users/yaron/project
/Users/yaron/project/.
/Users/yaron/project/subdir
/path-through-symlink/project
```

when Git resolution identifies the same canonical root.

Different Git worktrees should be treated as different repositories when they have different working-tree root paths.

This allows safe parallel execution in separate worktrees.

## 18.6 Fairness

The scheduler should evaluate queued jobs in creation order and start every job that currently fits the available capacity and repository constraints.

No advanced priority policy is required.

---

# 18. Building the Tango child command

## 19.1 No shell

The server must construct an argv list.

Example:

```python
[
    sys.executable,
    "/absolute/path/to/tango.py",
    "implement",
    "--phase",
    "15",
    "--writer",
    "codex",
    "--reviewer",
    "claude",
    "--repo-dir",
    "/Users/yaron/LLM/Rally",
    "--max-iters",
    "3",
]
```

The child must be started without:

```python
shell=True
```

## 19.2 Script path

The server must identify the absolute path of the Tango entry script at startup.

It must not depend on the server process’s current working directory.

## 19.3 Optional values

Only explicitly present or semantically enabled options are added.

Examples:

```python
if request["spec"] is not None:
    argv.extend(["--spec", request["spec"]])

if request["dry_run"]:
    argv.append("--dry-run")
```

Boolean false values must not generate flags.

## 19.4 Working directory

The child process may use the repository root as its working directory:

```python
cwd=repo_dir
```

The command must still include:

```text
--repo-dir <canonical-repo-root>
```

to preserve explicit behavior.

## 19.5 Standard input

The child process must use:

```python
stdin=subprocess.DEVNULL
```

HTTP jobs are non-interactive.

If Claude, Codex, or another command asks for terminal input, the job should fail rather than block indefinitely waiting for a user.

This behavior must be documented.

## 19.6 Standard output

Output is written directly to the job’s `output.log`.

Recommended launch:

```python
with open(log_path, "ab", buffering=0) as log_file:
    process = subprocess.Popen(
        argv,
        cwd=repo_dir,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
```

Exact buffering implementation may vary, but logs must become visible while the job is running.

## 19.7 Environment

The child inherits the server’s environment by default.

The API must not allow clients to add or overwrite environment variables in the initial implementation.

The server may add an informational variable:

```text
TANGO_SERVER_JOB_ID=<job-id>
```

This is optional.

## 19.8 Authentication inheritance

Claude and Codex authentication comes from the environment and user account under which the Tango server runs.

The server does not:

- perform login;
- accept provider API keys through the job API;
- store agent credentials;
- repair credentials missing in non-interactive environments.

---

# 19. Server startup and recovery

## 20.1 Loading persisted jobs

At startup, scan immediate child directories under the configured jobs directory.

For each directory containing readable `request.json` and `status.json`:

- validate the job ID;
- parse the files;
- load the job into memory.

Malformed job directories must not prevent the server from starting.

They should be skipped with a warning to server stderr.

## 20.2 Queued jobs

Persisted jobs in `queued` state are eligible to run after restart.

The scheduler may resume them automatically.

## 20.3 Previously running jobs

The new server process cannot safely assume ownership of PIDs stored by the previous server.

Therefore, jobs persisted as:

```text
running
stopping
```

must not attempt to signal or reattach to the stored PID.

They become terminal during recovery.

Preferred status:

```text
interrupted
```

If avoiding a new status value, use:

```text
failed
```

with:

```json
{
  "code": "server_restart",
  "message": "The server restarted while the job was active."
}
```

Set:

- `finished_at` to the recovery time;
- `pid` to null;
- `exit_code` to null.

The preferred implementation includes the `interrupted` status because it distinguishes workflow failure from server interruption.

## 20.4 Existing terminal jobs

Jobs already marked:

```text
succeeded
failed
stopped
interrupted
```

remain unchanged and accessible.

## 20.5 Orphan child processes

This milestone does not attempt to discover or terminate child process groups left behind by an unclean server crash.

Graceful server shutdown must minimize this occurrence.

---

# 20. Graceful server shutdown

The server must handle at least:

```text
SIGINT
SIGTERM
```

On shutdown:

1. stop accepting new HTTP requests;
2. stop scheduling queued jobs;
3. mark active jobs `stopping`;
4. send `SIGTERM` to each active process group;
5. wait for the configured grace period;
6. send `SIGKILL` to remaining process groups;
7. reap child processes;
8. mark those jobs `stopped` or `interrupted`;
9. persist final states;
10. exit.

Queued jobs remain `queued` and may run on the next server startup.

A second interrupt may force faster termination, but state should still be persisted where practical.

---

# 21. Validation and error codes

Recommended error codes:

```text
invalid_json
invalid_request
unknown_field
missing_field
invalid_field
invalid_path
repo_not_found
not_a_git_repo
repo_not_allowed
invalid_git_revision
job_not_found
job_not_retryable
payload_too_large
method_not_allowed
internal_error
```

Recommended status mappings:

| Condition | HTTP status |
|---|---:|
| Malformed JSON | 400 |
| Missing required field | 400 |
| Unknown field | 400 |
| Invalid enum or type | 400 |
| Invalid repository | 400 |
| Repository outside allowlist | 403 |
| Job not found | 404 |
| Retry conflicts with active state | 409 |
| Request too large | 413 |
| Unsupported method | 405 |
| Unexpected server failure | 500 |

---

# 22. Security requirements

## 23.1 Localhost default

Default binding:

```text
127.0.0.1
```

The default must never expose the server to the LAN.

## 23.2 Repository allowlist

When configured, repository allowlisting is applied after canonical Git-root resolution.

String-prefix checks are insufficient.

For example, allowing:

```text
/Users/yaron/LLM/Rally
```

must not accidentally allow:

```text
/Users/yaron/LLM/Rally-malicious
```

## 23.3 Command injection

All commands use argv lists.

No user field may be interpolated into a shell command.

## 23.4 Path traversal

Job IDs are validated before path construction.

HTTP paths must be URL-decoded once and validated.

Values such as these must be rejected:

```text
../other-job
job/../../etc
%2e%2e
```

## 23.5 Request schema

The strict request schema prevents clients from passing:

- arbitrary executable names;
- arbitrary flags;
- shell fragments;
- environment maps;
- stdin content;
- output paths;
- job directory overrides.

## 23.6 Logging

Do not log:

- unrelated environment variables;
- provider credentials.

## 23.7 Symlinks

Canonical path resolution must occur before:

- repository equality checks;
- allowed-repository checks;
- persistence of `repo_dir`.

## 23.8 Exposure warning

When binding to a non-loopback interface, print a prominent startup warning that the API can launch coding agents with repository write access.

---

# 23. Compatibility requirements

## 24.1 Existing CLI

All documented existing commands must continue to work.

Examples:

```bash
python tango.py plan --phase 3 --writer claude --reviewer codex
```

```bash
python tango.py implement --phase 3 --writer codex --reviewer claude
```

```bash
python tango.py phase --phase 3 --writer claude --reviewer codex
```

## 24.2 Exit codes

Existing workflow-mode exit codes remain unchanged.

Server startup returns non-zero when:

- arguments are invalid;
- the port cannot be bound;
- the jobs directory cannot be created or accessed;
- startup configuration is invalid.

## 24.3 Python support

The server must not increase Tango’s minimum Python version beyond the version already documented by the project.

## 24.4 Zero mandatory dependencies

Running:

```bash
python tango.py
```

must not require installing an HTTP framework.

---

# 24. Documentation changes

Update the README with a new section titled:

```text
Local HTTP server
```

Document:

- how server mode is selected;
- default host and port;
- all server CLI options;
- localhost security;
- the `/help` discovery endpoint for agents;
- request examples;
- status polling;
- incremental logs;
- stopping;
- retrying;
- concurrency behavior;
- one-active-job-per-repository rule;
- non-interactive authentication limitations;
- shutdown behavior;
- job storage location.

Example start:

```bash
python tango.py --port 8765
```

Example job:

```bash
curl -X POST http://127.0.0.1:8765/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "step": "phase",
    "phase": "3",
    "writer": "claude",
    "reviewer": "codex",
    "repo_dir": "/Users/me/code/project"
  }'
```

Example status:

```bash
curl http://127.0.0.1:8765/jobs/job-.../status
```

Example incremental logs:

```bash
curl 'http://127.0.0.1:8765/jobs/job-.../logs?offset=0'
```

Example stop:

```bash
curl -X POST http://127.0.0.1:8765/jobs/job-.../stop
```

Example retry:

```bash
curl -X POST http://127.0.0.1:8765/jobs/job-.../retry \
  -H 'Content-Type: application/json' \
  -d '{"reset": false}'
```

---

# 25. Testing strategy

Tests should use temporary directories and fake child commands wherever possible.

Tests must not require authenticated Claude or Codex accounts.

## 26.1 Argument parsing tests

Verify:

- no workflow command enters server mode;
- `plan`, `implement`, and `phase` still enter workflow mode;
- server flags are accepted in server mode;
- workflow flags are rejected in server mode;
- server flags are rejected in workflow mode;
- defaults are correct;
- invalid ports are rejected;
- invalid concurrency values are rejected.

## 26.2 Request validation tests

Verify rejection of:

- malformed JSON;
- oversized body;
- non-object JSON;
- missing required fields;
- unknown fields;
- invalid step;
- invalid writer;
- invalid reviewer;
- empty phase;
- non-string path values;
- nonexistent repository;
- non-Git directory;
- disallowed repository;
- invalid `base_sha`;
- invalid `max_iters`;
- invalid boolean types.

Verify valid requests are normalized correctly.

## 26.3 Job persistence tests

Verify:

- job directory is created;
- request is stored;
- initial status is stored as queued;
- log file is created immediately;
- status updates are atomic;
- a server restart can reload jobs;
- malformed job directories are skipped safely.

## 26.4 Execution tests

Use a fake Tango entry script that:

- prints output;
- sleeps;
- exits with configurable status;
- spawns a child process;
- handles or ignores `SIGTERM`.

Verify:

- argv is built correctly;
- no shell is used;
- cwd is the repository;
- stdin is closed;
- stdout and stderr appear in the combined log;
- exit code zero becomes `succeeded`;
- non-zero becomes `failed`;
- launch failure becomes `failed`;
- timestamps and PID fields are updated.

## 26.5 Log tests

Verify:

- full log reads;
- incremental reads;
- correct `next_offset`;
- `complete` behavior;
- empty reads at end of file;
- offsets beyond file size;
- limit enforcement;
- invalid query parameters;
- invalid UTF-8 replacement.

## 26.6 Stop tests

Verify:

- queued job becomes stopped;
- running job first becomes stopping;
- process group receives `SIGTERM`;
- descendants are terminated;
- `SIGKILL` occurs after grace timeout when required;
- terminal stop is idempotent;
- repository locks are released;
- a completion race does not overwrite stopped status.

## 26.7 Retry tests

Verify:

- retry creates a new ID;
- old job remains unchanged;
- request fields are copied;
- `retry_of` is populated;
- logs are not shared;
- reset override works;
- unknown override fields are rejected;
- active jobs cannot be retried;
- failed, stopped, and succeeded jobs can be retried.

## 26.8 Concurrency tests

Verify:

- global concurrency is enforced;
- same-repository jobs do not overlap;
- different repositories may overlap;
- blocked jobs remain queued;
- a blocked same-repo job does not block an unrelated repository;
- canonical and symlinked paths resolve to the same repository identity;
- separate Git worktrees may run concurrently;
- repository locks release after success, failure, stop, and launch error.

## 26.9 Recovery tests

Verify:

- queued jobs are loaded and scheduled;
- running jobs become interrupted or failed;
- stopping jobs become interrupted or failed;
- terminal jobs remain unchanged;
- stored PIDs are not signalled after restart.

## 26.10 HTTP routing tests

Verify:

- all required routes;
- `/help` returns the documented fields and matches the current validation schema;
- `/jobs/start` alias if implemented;
- `/jobs/{id}` and `/status` equivalence;
- 404 for unknown jobs;
- 404 for unknown paths;
- 405 for unsupported methods;
- JSON response content types;
- stable error shape.

---

# 26. Acceptance criteria

The change is complete when all the following are true.

## CLI

- Running `python tango.py` starts the local server.
- Running existing workflow commands behaves as before.
- Server-only and workflow-only arguments cannot be mixed.
- Server defaults to `127.0.0.1:8765`.
- The jobs directory defaults to `~/.tango/jobs`.
- The default maximum concurrency is one.

## Agent help

- `GET /help` returns a JSON document listing all endpoints, request fields, enums, job statuses, and the error shape.
- The `/help` content is derived from the same schema used to validate requests, not hand-duplicated.

## Starting jobs

- A valid `POST /jobs` request returns `202`.
- A persistent job directory is created before responding.
- The job initially reports `queued`.
- The scheduler starts the child when capacity and the repository lock are available.
- The child executes the existing Tango CLI with equivalent arguments.

## Status

- Status can be retrieved through both `/jobs/{id}` and `/jobs/{id}/status`.
- Status accurately reflects queued, running, terminal, and stopping states.
- Exit codes and timestamps are persisted.

## Logs

- Logs can be read while a job is running.
- `offset` and `next_offset` support efficient polling.
- Combined stdout and stderr are preserved.
- Logs remain available after server restart.

## Stop

- Queued jobs can be stopped.
- Running workflows and descendants are terminated as a process group.
- Stop is idempotent.
- Stopped jobs release scheduler locks.

## Retry

- Retry creates a new job.
- The original job is unchanged.
- Retry reuses the validated original request.
- Reset may be overridden.
- Active jobs cannot be retried.

## Concurrency

- No two active jobs operate on the same canonical repository root.
- Jobs against different repositories may run concurrently when configured.
- Queue processing avoids unnecessary head-of-line blocking.

## Persistence and recovery

- Terminal jobs survive restart.
- Queued jobs resume scheduling after restart.
- Previously active jobs are marked interrupted or failed.
- The new server does not signal stale stored PIDs.

## Security

- The server binds to loopback by default.
- Commands are never executed through a shell.
- Unknown workflow fields are rejected.
- Repository allowlisting cannot be bypassed through symlinks or string-prefix tricks.

## Dependencies

- No mandatory third-party dependency is added.
- The implementation works with Tango’s existing supported Python version.

---

# 27. Suggested implementation phases

## Phase 1: CLI mode selection and server skeleton

- Make workflow command optional.
- Add server-only arguments.
- Start `ThreadingHTTPServer`.
- Add `/health`.
- Add `/help`.
- Preserve existing workflow behavior.

## Phase 2: Job model and persistence

- Add job IDs.
- Add `request.json`, `status.json`, and `output.log`.
- Add atomic JSON writes.
- Add persisted job loading.
- Implement status endpoints.

## Phase 3: Submission and subprocess execution

- Add strict request validation.
- Add Git-root and allowlist validation.
- Build child argv.
- Run child in its own session.
- Capture output.
- Record lifecycle and exit code.

## Phase 4: Scheduling and repository locking

- Add global concurrency.
- Add canonical repository locks.
- Add fair queue scanning.
- Avoid head-of-line blocking.

## Phase 5: Logs, stop, and retry

- Add incremental logs.
- Add queued-job stop.
- Add process-group termination.
- Add retry with a new job ID.
- Add reset override.

## Phase 6: Recovery, shutdown, tests, and documentation

- Recover persisted jobs.
- Handle previously active states.
- Add graceful server shutdown.
- Add complete tests.
- Update README and examples.

---

# 28. Future extensions

The design should not require these features now, but should leave room for them:

- `GET /jobs` for listing and filtering jobs;
- SSE log streaming;
- job deletion and retention limits;
- per-job timeouts;
- per-job environment profiles defined by server configuration;
- named repository aliases;
- automatic Git worktrees;
- job cancellation reasons;
- scheduled jobs;
- callback webhooks;
- Unix-domain-socket binding;
- an optional FastAPI package;
- a browser dashboard;
- metrics and structured event logs;
- provider quota tracking;
- server-generated API schemas.

These future features must not complicate the first implementation.
