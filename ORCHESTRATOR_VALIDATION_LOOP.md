# Orchestrator Validation Loop — Architecture & Workflow

## Overview

The orchestrator now acts as a **continuous quality gate** that validates all completed work and decides whether to accept, refine, or request additional work. This creates an iterative loop until the original task is fully satisfied.

## Architecture

The orchestrator runs three phases every 1 minute:

```
┌─────────────────────────────────────────────────────────┐
│ Orchestrator Agent (every 1 minute)                     │
├─────────────────────────────────────────────────────────┤
│                                                          │
│ PHASE 1: VALIDATION                                     │
│ ├─ Scan validation/ for completed subtasks              │
│ ├─ Group by parent_task_id                              │
│ ├─ Call LLM validation prompt for each parent           │
│ ├─ Decision: complete | refine | additional_work | redo │
│ └─ Create follow-ups if needed                          │
│                                                          │
│ PHASE 2: DEPENDENCY RESOLUTION                          │
│ ├─ Scan agent inboxes for blocked tasks                 │
│ ├─ For each depends_on, check if dependencies ready     │
│ ├─ Wire completed outputs to dependent tasks            │
│ └─ Remove blocking fields                               │
│                                                          │
│ PHASE 3: DISPATCH                                       │
│ ├─ Read inbox/ for new parent tasks                     │
│ ├─ Call LLM decomposition prompt                        │
│ ├─ Create subtasks in agent inboxes                     │
│ └─ Move parent to processing/                           │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## Task Flow

### Initial Decomposition
```
inbox/task_001.task.md
    ↓
[DISPATCH PHASE]
    ↓
Orchestrator decomposes into:
  - research_task_001a.task.md → agents/research/inbox/
  - coder_task_001b.task.md   → agents/coder/inbox/
  - (parent moved to processing/)
    ↓
Max 5 iterations
```

### Agent Execution
```
research_task_001a.task.md (agents/research/inbox/)
    ↓ [Research agent processes]
    ↓
validation/research_task_001a.task.md  ← writes result to outbox/research_task_001a_result.md

coder_task_001b.task.md (agents/coder/inbox/)
    ↓ [Coder agent processes with research output in context_files]
    ↓
validation/coder_task_001b.task.md  ← writes result to outbox/coder_task_001b_result.md
```

### Validation & Decision Loop
```
[VALIDATION PHASE]
Orchestrator LLM sees:
  - Original parent task
  - All completed subtask results (from outbox/)
  - Current iteration (1-5)

LLM decides ONE of:

1. "complete" → Mark parent as done, move to outbox/
   └─ Task is FINISHED ✓

2. "refine" → Create follow-up tasks for refinement
   ├─ Follow-ups in agent inboxes
   ├─ Parent iteration += 1
   ├─ Parent stays in processing/
   └─ Loop continues

3. "additional_work" → Create new subtasks (incomplete approach)
   ├─ Follow-ups in agent inboxes
   ├─ Parent iteration += 1
   ├─ Parent stays in processing/
   └─ Loop continues

4. "redo" → Reject and ask for full rework
   ├─ Follow-ups in agent inboxes with failure context
   ├─ Parent iteration += 1
   ├─ Parent stays in processing/
   └─ Loop continues

5. Iteration limit (5) reached → Force "complete" (loop prevention)
```

## Example: REST API Task

### Iteration 1: Decomposition & Execution

```
INPUT:
  id: task_001
  body: "Build a production-ready REST API with authentication, error handling, 
         and deployment documentation."

DISPATCH PHASE:
  Orchestrator decomposes into:
    - research_001a: "Research authentication patterns"
    - coder_001b: "Implement FastAPI with OAuth2" (depends_on: [research_001a])
    
EXECUTION:
  research_001a completes
    → outbox/research_001a_result.md contains OAuth2 findings
  
  Dependency resolver wires research output to coder task
    → coder task gets context_files: [outbox/research_001a_result.md]
  
  coder_001b completes
    → outbox/coder_001b_result.md contains generated API code
```

### Iteration 1: Validation

```
VALIDATION PHASE:
  Orchestrator LLM receives:
    - Original task: "Build production-ready REST API..."
    - research_001a result: "OAuth2 recommended, use PyJWT..."
    - coder_001b result: "Generated CRUD API with auth routes"
  
  LLM decision: "additional_work"
  Reasoning: "Code structure is good and auth works, but missing:
             - Error handling middleware
             - Deployment config (Docker, environment vars)
             - API documentation"
  
  Follow-ups created:
    - coder_001c: "Add comprehensive error handling and validation"
    - coder_001d: "Add Docker config and deployment docs"
```

### Iteration 2: Execution & Validation

```
EXECUTION:
  coder_001c completes: "Added error handling, input validation"
  coder_001d completes: "Added Dockerfile, .env.example, deployment guide"

VALIDATION:
  LLM receives all results from iteration 1 + new completions
  
  Decision: "refine"
  Reasoning: "API is feature-complete, but QA review recommended"
  
  Follow-ups:
    - qa_001e: "Review API code, run tests, verify deployment config"
    (Note: iteration becomes 3)
```

### Iteration 3: QA & Final Validation

```
EXECUTION:
  qa_001e runs tests, deployment checks
    → outbox/qa_001e_result.md: "PASS - All tests pass, ready for production"

VALIDATION:
  LLM receives all previous results + QA verdict
  
  Decision: "complete"
  Reasoning: "API meets all requirements, passes QA, deployment ready"

RESULT:
  Parent task moved from processing/ → outbox/task_001_complete.md ✓
  Task is DONE
```

## Key Features

### 1. Iteration Tracking
- Parent task metadata includes `iteration` (1-5)
- Incremented each time validation chooses "refine"/"additional_work"/"redo"
- Max iterations = 5 to prevent infinite loops
- At iteration 5, forced to "complete" to break loop

### 2. Context Preservation
- Parent task ID tracked via `parent_task_id` metadata
- All follow-up tasks linked to original parent
- Original task requirements always visible to validation LLM
- Decision reasoning logged for audit trail

### 3. Smart Follow-ups
- Validation LLM can request specific follow-up work
- Follow-ups become new subtasks in appropriate agent inboxes
- Coder tasks auto-chain to QA for code review
- Research output wired to dependent tasks via `context_files`

### 4. Loop Prevention
- Max 5 validation rounds per task
- Forced completion at limit to prevent infinite loops
- Iteration counter logged and monitored
- Clear reasoning recorded for why task completed

## File Structure

```
inbox/
  task_001.task.md                ← New parent task

processing/
  task_001.task.md                ← Parent in validation loop
  (iteration, parent_task_id fields added to metadata)

validation/
  research_001a.task.md           ← Completed, awaiting validation
  coder_001b.task.md              ← Completed, awaiting validation
  coder_001c.task.md              ← Completed, awaiting validation
  qa_001e.task.md                 ← Completed, awaiting validation

agents/*/inbox/
  research_001a.task.md
  coder_001b.task.md  (depends_on: [research_001a])
  coder_001c.task.md  (depends_on: [coder_001b])
  qa_001e.task.md

outbox/
  task_001_complete.md            ← Final approved result
  research_001a_result.md
  coder_001b_result.md
  coder_001c_result.md
  qa_001e_result.md
```

## System Prompts

### Dispatch Prompt
File: `agents/orchestrator/system_prompt.md`
- Decides how to decompose parent task
- Routes to appropriate agents
- Outputs: JSON array of subtasks

### Validation Prompt  
File: `agents/orchestrator/validation_system_prompt.md`
- Reviews completed subtask results
- Decides: complete, refine, additional_work, redo
- Outputs: JSON with decision + follow-ups

## Configuration

Tuning parameters in orchestrator:
- `MAX_ITERATIONS = 5` — max validation rounds
- `VALIDATION_PHASE` runs first every cycle — detects completed work early
- Follow-up task delay: ~2 minutes (next cron tick)

## Monitoring

Check orchestrator logs to see validation decisions:
```bash
tail -f logs/orchestrator/general.log | grep -E "decision|iteration|APPROVED"
```

Expected log output:
```
[2026-05-07T12:34:00Z] [INFO] [orchestrator] Validation decision for task_001: additional_work
[2026-05-07T12:34:00Z] [INFO] [orchestrator] Reasoning: Code structure good, needs error handling
[2026-05-07T12:34:01Z] [INFO] [orchestrator] Created follow-up task ... → coder
[2026-05-07T12:34:01Z] [INFO] [orchestrator] Task task_001 iteration incremented to 2
```

## Troubleshooting

### Task stuck in "validation/" folder
- Check logs for validation LLM errors
- Ensure validation system prompt is properly formatted
- Verify Ollama is responding

### Iteration count too high
- LLM is being indecisive; follow-ups may be too vague
- Check validation_system_prompt.md clarity
- Manually mark parent as complete if genuinely done

### Follow-ups not being created
- Check "follow_ups" field in validation decision JSON
- Ensure worker names are valid (coder, research, claude-code)
- Verify agent inboxes exist and are writable
