"""
Shared subtask-file creation + research→coder dependency wiring.

Previously inlined three times: in ``process_task`` (initial decomposition),
``redecompose_with_research`` (research-first redecomposition), and
``handle_validation_decision`` (validation follow-ups). All three loops do the
same thing — for each subtask: handle ``pending_approval`` routing, look up
the worker inbox, call ``create_task_file`` with the right kwargs, track the
created path by worker, then wire a ``depends_on`` edge if both research and
coder subtasks were created.
"""

from pathlib import Path

import shared.task_io as _task_io
from shared.task_io import create_task_file, read_task, write_result


def dispatch_subtasks(
    subtasks: list[dict],
    parent_task_id: str,
    worker_inboxes: dict,
    agent_name: str,
    log,
    *,
    validation_context: dict | None = None,
    prev_outputs_by_worker: dict | None = None,
    subtask_label: str = "subtask",
    log_prefix: str = "",
) -> dict:
    """
    Create .task.md files for ``subtasks`` and wire research→coder ``depends_on``.

    Parameters
    ----------
    subtasks : list of dicts with keys ``worker``, ``type``, ``description``,
        ``expected_output``. (For validation follow-ups the same keys apply.)
    parent_task_id : id stamped onto each new subtask file via ``parent_task_id``.
    worker_inboxes : dict mapping worker name to inbox ``Path``.
    agent_name : string written to ``created_by`` on each new subtask.
    log : ``AgentLogger`` instance (must support ``.info`` / ``.error``).
    validation_context : optional dict to forward to ``create_task_file`` for
        follow-up tasks issued in response to a validation decision.
    prev_outputs_by_worker : optional ``{worker_name: list[str]}`` of prior result
        paths to wire into ``context_files`` per worker (used by
        refine/additional_work follow-ups so the agent sees its prior output).
    subtask_label : noun used in info logs (``"subtask"`` for initial dispatch,
        ``"follow-up task"`` for validation follow-ups).
    log_prefix : optional prefix prepended to every log line emitted here
        (e.g. ``"Re-decompose: "``).

    Returns
    -------
    dict ``{worker_name: created_path}`` for downstream callers (today only used
    to check whether both research and coder were created).
    """
    created: dict = {}

    for subtask in subtasks:
        worker = subtask.get("worker")

        # pending_approval routes to the claude-code/pending/ folder for manual
        # approval, not to a worker inbox.
        if worker == "pending_approval":
            pending_inbox = _task_io.PROJECT_ROOT / "agents" / "claude-code" / "pending"
            pending_inbox.mkdir(parents=True, exist_ok=True)
            new_task_path = create_task_file(
                inbox_path=pending_inbox,
                task_type=subtask.get("type"),
                description=subtask.get("description"),
                expected_output=subtask.get("expected_output"),
                assigned_to="pending_approval",
                created_by=agent_name,
                parent_task_id=parent_task_id,
            )
            log.info(f"{log_prefix}Created pending task {new_task_path.name} → pending_approval")
            continue

        inbox = worker_inboxes.get(worker)
        if not inbox:
            log.error(f"{log_prefix}Unknown worker '{worker}' — skipping {subtask_label}")
            continue

        chain_to = "qa" if subtask.get("type") == "code" else None
        # For code subtasks/follow-ups, preserve the clean description as
        # ``original_description`` so downstream QA tasks aren't built from a
        # body that already contains validation context.
        original_description = subtask.get("description") if subtask.get("type") == "code" else None

        ctx_files = None
        if prev_outputs_by_worker:
            ctx_files = prev_outputs_by_worker.get(worker)

        new_task_path = create_task_file(
            inbox_path=inbox,
            task_type=subtask.get("type"),
            description=subtask.get("description"),
            expected_output=subtask.get("expected_output"),
            assigned_to=worker,
            created_by=agent_name,
            chain_to=chain_to,
            original_description=original_description,
            parent_task_id=parent_task_id,
            context_files=ctx_files,
            validation_context=validation_context,
        )

        created[worker] = new_task_path
        log.info(f"{log_prefix}Created {subtask_label} {new_task_path.name} → {worker}")

    # Wire research→coder dependency if both present.
    if "research" in created and "coder" in created:
        research_path = created["research"]
        coder_path = created["coder"]
        research_task = read_task(research_path)
        coder_task = read_task(coder_path)
        coder_task["meta"]["depends_on"] = [research_task["meta"]["id"]]
        write_result(str(coder_path), coder_task["body"], meta=coder_task["meta"])
        log.info(
            f"{log_prefix}Wired dependency: coder {coder_path.name} depends on "
            f"research {research_path.name}"
        )

    return created
