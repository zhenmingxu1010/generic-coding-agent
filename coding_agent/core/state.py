from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    task: str
    workspace: str
    thread_id: str
    max_rounds: int
    max_repair_llm_calls: int
    mode: str
    supervisor: dict[str, Any]
    invariants: list[str]
    read_only: bool
    write_locked: bool
    read_only_policy: dict[str, Any]
    scope_grounding: dict[str, Any]
    scope_contract: dict[str, Any]
    scope_expansions: list[dict[str, Any]]
    task_contract: dict[str, Any]
    needs_verification: bool
    verification_reason: str
    implementation_batch_open: bool
    implementation_batch_started_round: int
    implementation_batch_remaining: list[str]

    run_dir: str
    trace_path: str
    messages_path: str
    context_pack_path: str
    context_summary_path: str
    state_snapshot_path: str
    patches_dir: str
    final_path: str
    project_memory_dir: str
    project_profile_path: str
    long_term_memory_path: str
    short_term_memory_path: str
    repository_map_path: str

    round_idx: int
    task_spec: dict[str, Any]
    task_intent: dict[str, Any]
    task_completeness: dict[str, Any]
    assumptions: list[dict[str, Any]]
    implementation_contract: dict[str, Any]
    clarification_questions: list[dict[str, Any]]
    clarification_history: list[dict[str, Any]]
    clarification_answer: str
    original_task: str
    repo_map: dict[str, Any]
    project_memory: dict[str, Any]
    memory_context: dict[str, Any]
    evidence_index: dict[str, Any]
    analysis_contract: dict[str, Any]
    structured_memory: dict[str, Any]
    repo_analysis_context: dict[str, Any]
    relevant_context: dict[str, Any]
    context_pack: dict[str, Any]
    context_summary: str
    plan: dict[str, Any]
    plan_step_idx: int
    completed_steps: list[int]
    blocked_steps: list[int]
    decision: dict[str, Any]
    last_tool_result: dict[str, Any]
    action_history: list[dict[str, Any]]
    repeated_action_count: int
    observations: list[dict[str, Any]]
    analysis_report: str
    analysis_quality: dict[str, Any]
    deliverable_review: dict[str, Any]
    deliverable_review_fingerprint: str
    deliverable_review_count: int
    deliverable_review_prompt_chars: int
    deliverable_review_errors: list[str]
    runtime_ok: bool
    analysis_quality_ok: bool
    quality_warnings: list[str]
    contract_check: dict[str, Any]
    contract_ok: bool
    semantic_contract_check: dict[str, Any]
    requirement_atoms: list[dict[str, Any]]
    requirement_atom_summary: dict[str, Any]
    requirement_atom_check: dict[str, Any]
    sample_data_review: dict[str, Any]
    semantic_checks: list[dict[str, Any]]
    interface_check: dict[str, Any]
    traceback_issues: list[dict[str, Any]]
    verification: dict[str, Any]
    verification_claims: dict[str, Any]
    verification_artifacts: list[dict[str, Any]]
    verification_plan_update: dict[str, Any]
    verification_plan_attempts: int
    verification_plan_errors: list[str]
    verification_grounding: dict[str, Any]
    verification_oracle_review: dict[str, Any]
    verification_oracle_prompt_chars: int
    verification_review_errors: list[str]
    verification_review_mode: str
    verification_review_prompt_chars: int
    verification_artifacts_dir: str
    verification_step_claims: dict[str, list[str]]
    verification_step_timeouts: dict[str, int]
    verification_step_stdin: dict[str, str]
    verification_step_sandboxes: dict[str, dict[str, Any]]
    verification_step_workspaces: dict[str, str]
    verification_infrastructure_step_names: list[str]
    executed_verification_steps: list[dict[str, Any]]
    skipped_file_plan_verify_steps: list[dict[str, Any]]
    normalized_file_plan_verify_steps: list[dict[str, Any]]
    inferred_sandbox_copy_paths: list[dict[str, Any]]
    verification_failure_fingerprint: str
    verification_failure_repeat_count: int
    verification_stalled: bool
    test_results: dict[str, Any]
    test_baseline: dict[str, Any]
    test_baseline_comparison: dict[str, Any]
    failure: dict[str, Any] | None
    failure_issues: list[dict[str, Any]]
    repair_history: list[dict[str, Any]]
    failure_history: list[dict[str, Any]]
    repair_read_budget: dict[str, Any]
    repair_read_cache: dict[str, Any]
    repair_action_budget: dict[str, Any]
    repair_llm_call_count: int
    repair_llm_call_budget: dict[str, Any]
    repair_prompt_stats: dict[str, Any]
    repair_controller: dict[str, Any]
    repair_controller_reused: bool
    force_repair_action: dict[str, Any] | None
    failed_writes: list[dict[str, Any]]
    import_error_context: dict[str, Any]

    file_plan: dict[str, Any]
    verification_test_registry: dict[str, Any]
    generated_files: list[dict[str, Any]]
    changed_files: list[str]
    output_layout: dict[str, Any]
    progress_guard: dict[str, Any]
    banned_actions: list[str]

    artifact_registry: dict[str, Any]
    write_scope_policy: dict[str, Any]
    write_scope_audit: dict[str, Any]
    write_intents: dict[str, Any]
    workspace_baseline: dict[str, Any]
    file_plan_review: dict[str, Any]
    test_oracle_review: dict[str, Any]
    failure_owner: str
    strategy_decision: dict[str, Any]
    final_gate_status: dict[str, Any]
    final_ok: bool
    outcome: str
    controlled_failure: bool
    stopped_reason: str
    route_next: str
