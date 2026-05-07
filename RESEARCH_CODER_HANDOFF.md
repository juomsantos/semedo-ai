# Research → Coder Task Handoff

## Problem Solved

When the orchestrator decomposed a complex task into both research and code subtasks:
- Both subtasks were created independently with `context_files: []`
- Research output was never passed to the coder
- Coder had no access to research findings needed to generate code

## Solution

**Three-tier system:**

### 1. Dependency Declaration (Orchestrator)

When decomposing a task into both research and code subtasks, the orchestrator now:
- Tracks which subtasks depend on which others
- Sets `depends_on: [research_task_id]` on the coder subtask
- Sets `parent_task_id` on all subtasks for tracking

```yaml
# Research subtask
id: task_20260507_120456_123456
type: research
depends_on: []           # No dependencies

# Coder subtask  
id: task_20260507_120456_234567
type: code
depends_on: [task_20260507_120456_123456]  # Blocked until research completes
```

### 2. Dependency Resolution (Orchestrator)

On each orchestrator run (every 1 minute):
- Scans all agent inboxes for tasks with `depends_on` fields
- Checks if dependencies are completed (exist in `outbox/`)
- Wires completed outputs to dependent tasks via `context_files`
- Removes `depends_on` field to unblock processing

```python
# If research output exists at outbox/task_20260507_120456_123456_result.md
# Then coder task gets updated with:
context_files: [
  "outbox/task_20260507_120456_123456_result.md"  # ← research output
]
```

### 3. Agent Processing

Agents now:
- Check for `depends_on` field before processing
- Skip blocked tasks (dependency not yet resolved)
- Process normally once unblocked

```python
# In agent_coder.py
if task["meta"].get("depends_on"):
    log.info("Skipping — unresolved dependencies")
    return

# Research output is now available in context_files
```

## Flow Example

```
1. Orchestrator receives: "Build a Python ML pipeline"
   ↓
2. Orchestrator decomposes into:
   - research_task_1: "Research best libraries and patterns"
   - coder_task_2: "Generate code" (depends_on: [research_task_1])
   ↓
3. Research agent picks up research_task_1
   → Completes, writes to outbox/task_xxx_result.md
   ↓
4. Orchestrator runs dependency resolver
   → Detects research_task_1 is complete
   → Updates coder_task_2.context_files = [outbox/task_xxx_result.md]
   → Removes depends_on field
   ↓
5. Coder agent picks up coder_task_2
   → Reads research output from context_files
   → Generates code based on research findings
   → Writes to outbox/ and chains to QA
```

## Task Fields

### parent_task_id
- Tracks which original task spawned this subtask
- Used for grouping and rollup completion
- Set by orchestrator on all subtasks

### depends_on  
- List of task IDs this task depends on
- If present, task is blocked from processing
- Removed by dependency resolver when all deps resolved

### context_files
- List of file paths to include in agent context
- Populated by dependency resolver
- Read by agents and prepended to user message

## Files Changed

- `scripts/shared/task_io.py` — Added `depends_on`, `parent_task_id` parameters; added `resolve_task_dependencies()` function
- `scripts/agent_orchestrator.py` — Wires dependencies between research and code subtasks; calls dependency resolver
- `scripts/agent_coder.py` — Guards against processing blocked tasks

## Testing

Create a task with both research and code requirements:

```markdown
---
id: test_handoff_001
type: code
priority: medium
created_by: test
---

## Task Description

Research the best approach for building a REST API with FastAPI.
Then generate a complete, production-ready API with authentication.

## Expected Output

A working FastAPI application with:
1. Authentication system
2. CRUD endpoints
3. Error handling
4. Deployment instructions
```

Expected behavior:
1. Orchestrator creates research task
2. Orchestrator creates code task with `depends_on: [research_task_id]`
3. Research task completes within 2 minutes
4. Orchestrator resolver runs, wires research output to code task
5. Code task becomes unblocked and starts within 4 minutes total
6. Coder sees research findings in context and generates informed code
