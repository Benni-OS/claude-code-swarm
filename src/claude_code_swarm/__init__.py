"""claude-code-swarm: Zero-dependency multi-agent orchestration kernel for parallel Claude Code sessions."""

__version__ = "0.1.0"
__author__ = "Benni Alencar"

from claude_code_swarm.core.blackboard import Blackboard, HandoffRecord, TaskRecord
from claude_code_swarm.core.hook_engine import HookDecision, HookEngine
from claude_code_swarm.core.injector import SwarmInjector
from claude_code_swarm.core.lease_arbiter import (
    AccessCheckResult,
    LeaseArbiter,
    LeaseRecord,
    LeaseResult,
)
from claude_code_swarm.core.merge_gate import (
    GENESIS_HASH,
    GateReport,
    MergeGate,
    SwarmAuditLedger,
)
from claude_code_swarm.core.swarm_budget import BudgetStatus, SwarmBudget
from claude_code_swarm.core.worktree_manager import (
    MergeResult,
    WorkerWorktree,
    WorktreeManager,
)

__all__ = [
    "__version__",
    "__author__",
    "AccessCheckResult",
    "Blackboard",
    "BudgetStatus",
    "GENESIS_HASH",
    "GateReport",
    "HandoffRecord",
    "HookDecision",
    "HookEngine",
    "LeaseArbiter",
    "LeaseRecord",
    "LeaseResult",
    "MergeGate",
    "MergeResult",
    "SwarmAuditLedger",
    "SwarmBudget",
    "SwarmInjector",
    "TaskRecord",
    "WorkerWorktree",
    "WorktreeManager",
]
