# `claude-code-swarm` — Architecture Reference

## 1. Design Philosophy

Running multiple parallel **Claude Code** sessions on a single repository fails in three predictable ways:
1. **Silent File Overwrites**: Two sessions edit the same file simultaneously, clobbering each other's changes.
2. **Broken Main Branch**: Partial or syntactically invalid edits are merged without AST or conflict-marker verification.
3. **Unbounded Token/USD Spend**: Parallel subagents loop or retry without a shared global budget circuit breaker.

`claude-code-swarm` solves all three with a **zero-dependency Python 3.10+ stdlib kernel** (`sqlite3` WAL, `subprocess` git worktrees, `ast`, `hashlib`, JSON-RPC 2.0 stdio MCP).

---

## 2. Core Subsystems

### 2.1 Git Worktree Isolator (`core/worktree_manager.py`)
- Each worker receives a dedicated working tree under `.swarm/worktrees/<worker_id>` checked out on branch `swarm/<worker_id>`.
- `spawn_worker`, `list_workers`, `diff_worker`, `merge_worker`, and `cleanup_worker` manage the full lifecycle.
- `merge_worker` auto-commits dirty worker changes (optional), runs `MergeGate`, and performs `--no-ff` merges with automatic `--abort` on git conflict.

### 2.2 SQLite3 WAL Lease Arbiter (`core/lease_arbiter.py`)
- Uses `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;` and `BEGIN IMMEDIATE` transactions.
- Supports `exclusive` (pessimistic write) and `shared` (optimistic read) leases with TTL expiration.
- **Hierarchical Prefix Locking**: Locking `src/core/` blocks `src/core/engine.py` and vice versa.
- **Re-entrant**: The same `worker_id` can refresh its own lease without self-blocking.

### 2.3 Typed DAG Blackboard (`core/blackboard.py`)
- Persists `tasks` (with `dependencies_json`, `target_paths_json`, `priority`, and `status`) and `handoffs` in SQLite3 WAL.
- `claim_task()` atomically selects the highest-priority `pending` task whose DAG dependencies are all `resolved`.

### 2.4 Global Swarm Budget & Circuit Breaker (`core/swarm_budget.py`)
- Enforces `global_cap_usd`, `max_tokens`, and per-worker `cap_usd` quotas.
- `HookEngine` checks `SwarmBudget.check_budget(worker_id)` on every `PreToolUse` event and trips immediately when any cap is reached.

### 2.5 Pre-Merge AST Gate & SHA-256 Audit Ledger (`core/merge_gate.py`)
- Scans changed files for git conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).
- Parses all `.py` files with `ast.parse()` to guarantee zero syntax errors before merge.
- Optionally executes `pytest` inside the worker worktree.
- Appends every gate and hook block event to `.swarm/audit_ledger.jsonl` chained from `GENESIS_HASH` (`"0" * 64`).

### 2.6 Claude Code Hook Engine & Injector (`core/hook_engine.py`, `core/injector.py`)
- `SwarmInjector` idempotently configures `.claude/settings.json`, `.mcp.json`, `CLAUDE.md`, and `.claude/commands/swarm-*.md`.
- `HookEngine` handles `PreToolUse`, `PostToolUse`, `Stop`, and `SubagentStop` JSON stdin payloads and returns exit code `2` + `permissionDecision: "deny"` on lease or budget violations.

### 2.7 Native Stdio MCP Server (`serve/mcp_server.py`)
- Zero-dependency JSON-RPC 2.0 server exposing `swarm_claim_task`, `swarm_acquire_lock`, `swarm_release_lock`, `swarm_post_handoff`, `swarm_budget_check`, `swarm_merge_ready`, and `swarm_status`.
