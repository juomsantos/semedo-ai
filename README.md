# AI Team

A self-contained, local multi-agent AI system. You describe what you want; a team of specialised AI agents researches, writes, tests, and validates the work — iterating autonomously until the result meets quality criteria. Everything runs on your machine using [Ollama](https://ollama.com).

---

## What it does

AI Team coordinates a pipeline of agents that collaborate through a shared filesystem:

- **Orchestrator** — decomposes your task into subtasks, routes them to the right workers, validates results, and iterates until the work is done.
- **Research agent** — answers questions and gathers information using web search and your local knowledge base.
- **Coder agent** — writes and implements code, drawing on research output and accumulated project knowledge.
- **QA agent** — executes the code, reviews it, and sends it back for revision if it fails — automatically.
- **Claude Code agent** — handles complex reasoning tasks via the Claude CLI; runs with your approval.

You interact through a **real-time web dashboard** — submit tasks, monitor progress, review results, manage the knowledge base, and chat with the pipeline assistant. No coding or command-line required for day-to-day use.

---

## Key features

- **Fully local** — all LLMs run via Ollama on your own hardware; no data leaves your machine (except optional web search calls)
- **Event-driven** — agents are triggered instantly by a file watcher; no constant polling
- **Validation loop** — the orchestrator reviews every result and can request refinements, rewrites, or additional work (up to 5 iterations per task)
- **QA gate** — all code tasks are automatically tested before being accepted
- **Local knowledge base** — a RAG API (FastAPI + ChromaDB) lets agents query your documents; ingest anything from the dashboard
- **Web-enabled agents** — research and QA agents can search the web and fetch pages in their reasoning loop
- **Real-time dashboard** — task status, agent stats, logs, approvals, and knowledge base management in one place
- **Persistent results** — all completed work is stored in `outbox/` and always accessible

---

## Architecture overview

```
Users (Dashboard · REST API · file drop)
        │
        ▼
     inbox/
        │
        ▼
   Orchestrator  ──────────────────────────────────────────┐
   (3-phase loop)                                          │
   Phase 1: Validate results                               │
   Phase 2: Resolve dependencies                           ▼
   Phase 3: Decompose & dispatch             RAG API (knowledge base)
        │                                   http://localhost:8000
   ┌────┼──────────────────┐
   ▼    ▼                  ▼
Coder  Research      Claude Code*
   │                  *requires approval
   └──► QA agent
        │
        ▼
   validation/  →  Orchestrator reviews  →  outbox/ (done) or retry
```

For the full technical deep-dive — agent internals, task file schema, concurrency model, RAG integration, QA loop, fault tolerance — see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | |
| **Ollama** | Running and reachable on your network; set the URL in `config.json` |
| **Ollama models** | Pull the models you want to use (default: `qwen3.5:9b`, `qwen3-embedding:8b`) |
| **Claude CLI** _(optional)_ | Only needed if you want to use the Claude Code agent |

---

## Installation

**1. Clone the repository**
```bash
git clone https://github.com/your-username/ai-team.git
cd ai-team
```

**2. Install main dependencies**
```bash
pip install -r requirements.txt
```

**3. Install RAG API dependencies**
```bash
cd rag_api
pip install -r requirements.txt
cd ..
```

**4. Pull the required Ollama models**
```bash
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:8b
```

**5. Configure the system**

Copy the example config and edit it:
```bash
cp config.example.json config.json
```

Open `config.json` and set:
- `ollama.base_url` — URL of your Ollama server (e.g. `http://localhost:11434`)
- `web_search.ollama_api_key` — your [Ollama API key](https://ollama.com) (required for web search; agents work without it but cannot browse the web)

```json
{
  "ollama": {
    "base_url": "http://localhost:11434",
    "timeout": 360
  },
  "web_search": {
    "ollama_api_key": "your-key-here"
  }
}
```

> ⚠️ **Do not commit `config.json`** — it contains your API key. It is listed in `.gitignore` by default.

---

## Running

**Start the agents and RAG API:**
```bash
# Windows
RUN_SCHEDULER.bat

# Linux / Mac
./RUN_SCHEDULER.sh

# Or directly:
python scripts/scheduler.py
```

**Start the dashboard** (separate terminal, optional but recommended):
```bash
python dashboard/run_dashboard.py
```

Open **http://localhost:5000** in your browser.

Press `Ctrl+C` in the scheduler terminal to stop all agents gracefully.

---

## Using AI Team

### Submitting a task

The easiest way is the **dashboard → Submit Task tab**:

1. Choose a task type: `research`, `code`, `summarize`, `review`, or `plan`
2. Set priority: `high`, `medium`, or `low`
3. Describe what you want in plain language
4. Optionally describe the expected output
5. Click **Submit**

The orchestrator picks it up immediately, breaks it down into subtasks, and dispatches them to the right agents. You can watch progress in the **Tasks** tab in real time.

### Monitoring progress

The dashboard **Tasks** tab shows every task in the pipeline with its current status:

| Status | Meaning |
|---|---|
| `pending` | Waiting to be picked up |
| `dispatched` | Orchestrator has decomposed it; subtasks are running |
| `processing` | An agent is actively working on it |
| `awaiting validation` | Worker finished; orchestrator is reviewing |
| `complete` | Done — result in `outbox/` |
| `failed` | Could not be completed after retries |

Click any task to see its full detail: metadata, task body, agent logs, and result.

### Viewing results

Completed results appear in the **Results** tab, organised by agent. Each result includes the full deliverable — research report, generated code, QA verdict, or the orchestrator's aggregated summary across all subtasks.

Result files are also available directly at `outbox/*_result.md`.

### Using the Chat assistant

The **Chat** tab gives you a conversational interface to the pipeline. The assistant has full context of what's in the pipeline (tasks, results, logs) and can answer questions about the work. It can also create tasks on your behalf — just describe what you want.

### Approving Claude Code tasks

When the orchestrator routes a task to the Claude Code agent, it requires your explicit approval before the Claude CLI runs. Pending tasks appear in the **Approvals** tab with **Approve** and **Reject** buttons. Approved tasks run immediately; rejected ones are moved to `failed/` with your reason logged.

### Managing the knowledge base

The **Knowledge Base** tab lets you add documents that all agents can query during their work:

1. Paste or type content into the text area
2. Give it a title and an optional source label
3. Click **Ingest**

Agents automatically query the knowledge base before processing tasks. This is useful for project documentation, coding guidelines, reference material, or any context you want the agents to consistently apply.

---

## Configuration reference

All settings are in `config.json` at the project root.

| Field | Default | Description |
|---|---|---|
| `ollama.base_url` | `http://localhost:11434` | Ollama server URL |
| `ollama.timeout` | `360` | Per-call LLM timeout in seconds |
| `web_search.ollama_api_key` | — | Ollama API key for web search |
| `agents.<name>.model` | `qwen3.5:9b` | Model used by each agent |
| `agents.<name>.process_timeout` | varies | Max runtime for the agent subprocess (seconds) |
| `scheduler.enable_timer_polling` | `false` | Enable timer-based polling alongside the file watcher |
| `dashboard.port` | `5000` | Dashboard port |
| `rag_api.url` | `http://localhost:8000` | RAG API address |
| `chat.model` | `qwen3.5:9b` | Model used by the dashboard chat assistant |

To use different models, change the `model` field for any agent and pull the model with `ollama pull <model-name>`.

---

## Project structure (top level)

```
ai-team/
  scripts/          ← agent scripts and shared utilities
  agents/           ← per-agent inboxes and system prompts
  dashboard/        ← Flask web UI
  rag_api/          ← local knowledge base service (FastAPI + ChromaDB)
  tests/            ← pytest unit-test suite
  inbox/            ← drop task files here to submit work
  outbox/           ← completed results
  processing/       ← tasks currently being worked on
  validation/       ← results awaiting orchestrator review
  failed/           ← tasks that could not be completed
  logs/             ← per-agent execution logs
  config.json       ← runtime configuration (not committed)
  pytest.ini        ← pytest configuration
  requirements-dev.txt ← test/dev dependencies (pytest, pytest-cov)
  ARCHITECTURE.md   ← full technical reference
  DASHBOARD.md      ← dashboard API reference
```

---

## Testing

The project has a pytest test suite covering the shared helpers, the
orchestrator's pure helpers, the RAG tool, and the Ollama client wrapper.
Tests run in under 2 seconds and never touch the real `inbox/`, `outbox/`,
Ollama server, or RAG API — a `fake_project` fixture builds a temp project
tree and the network is mocked.

**Install test dependencies (one-time):**
```bash
pip install -r requirements-dev.txt
```

**Run the suite:**
```bash
pytest                                    # all tests
pytest tests/test_task_io.py              # one file
pytest -k safe_read_context               # filter by name
pytest --cov=shared --cov-report=term     # with coverage
```

What's covered today:

| Area | Tests | Coverage |
|---|---|---|
| `shared/config.py` | accessors + JSON loader | 100% |
| `shared/rag_tool.py` | every failure mode → still returns a string | 100% |
| `shared/rag_injection.py` | pre-prompt RAG injection (filter + truncate) | 100% |
| `shared/token_logger.py` | JSONL output, task-ID filter | 100% |
| `shared/logger.py` | `AgentLogger` level routing, UTF-8 | 97% |
| `shared/ollama_client.py` | `chat`, `chat_with_tools`, error mapping (network mocked) | 94% |
| `shared/task_io.py` | frontmatter round-trip, `mark_processing`, `safe_read_context` traversal defense, dependency wiring | 91% |
| `agent_orchestrator.py` | pure helpers: `_find_qa_for_output`, `_find_retry_coder_output`, `_find_qa_for_coder_subtask`, `_extract_qa_verdict` | partial (LLM-driven logic deferred) |

What's **not** covered yet: `file_watcher.py` (needs watchdog mocking),
`web_search.py` (network wrapper), the orchestrator's decomposition and
validation loops (need full LLM mocking), and the Flask dashboard endpoints.

See [ARCHITECTURE.md](ARCHITECTURE.md#testing) for how the test fixtures
work and conventions for adding new tests.

---

## Troubleshooting

**Agents don't start / "FATAL: task_io import failed"**
A syntax error exists in `scripts/shared/task_io.py` or a dependency is missing. Check the scheduler log at `logs/scheduler/general.log`.

**"Workspace still starting" or Ollama connection errors**
Verify that Ollama is running and that `ollama.base_url` in `config.json` points to the correct address. Test with `curl http://<your-ollama-host>:11434/api/tags`.

**RAG API not starting**
Run `pip install -r rag_api/requirements.txt` and confirm `uvicorn` is available. Check `logs/scheduler/general.log` for the startup error.

**Tasks stuck in `processing/`**
Restart the scheduler — orphan recovery runs automatically at startup and returns any stuck tasks to their worker queues.

**Web search not working**
Confirm `web_search.ollama_api_key` is set in `config.json`. Agents continue to function without it but cannot make web search or fetch calls.

---

## Licence

GNU General Public License v3.0
