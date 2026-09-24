"""Pure domain models and rules without infrastructure dependencies."""

from .events import EventEnvelope, build_event_id
from .intraday import (
    ACTIVE_PHASES,
    IRREVERSIBLE_STATES,
    PHASE_LABELS,
    SHANGHAI,
    Advice,
    DecisionState,
    advice,
    evaluate,
    phase_at,
    resolve_transition,
)

__all__ = [
    "ACTIVE_PHASES",
    "IRREVERSIBLE_STATES",
    "PHASE_LABELS",
    "SHANGHAI",
    "Advice",
    "DecisionState",
    "EventEnvelope",
    "advice",
    "build_event_id",
    "evaluate",
    "phase_at",
    "resolve_transition",
]
