"""Per-request diagnostics for outfit generation.

One record per AI attempt and one summary per request, all sharing a request id, so a
short answer ("only two outfits") can be traced to its cause afterwards. Records hold
counts and stable reason codes only: never prompts, model replies, event notes, item
descriptions or credentials.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

logger = logging.getLogger(__name__)

# Stable codes; quality-check codes come from outfit_quality.QualityProblem.code.
AI_CALL_FAILED = "ai_call_failed"
AI_RESPONSE_TRUNCATED = "ai_response_truncated"
PARSE_ERROR = "parse_error"
MODEL_SHORT_RESPONSE = "model_short_response"

OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"


@dataclass
class AttemptRecord:
    phase: str
    requested: int
    ai_status: str = "pending"
    model: str | None = None
    returned: int = 0
    approved: int = 0
    rejected: Counter = field(default_factory=Counter)

    @property
    def short_by(self) -> int:
        """Options the model left out; an unparsable answer is a format error instead."""
        if self.ai_status != "ok" or self.rejected.get(PARSE_ERROR):
            return 0
        return max(self.requested - self.returned, 0)

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "requested": self.requested,
            "ai_status": self.ai_status,
            "model": self.model,
            "returned": self.returned,
            "approved": self.approved,
            "short_by": self.short_by,
            "rejected": dict(sorted(self.rejected.items())),
        }


@dataclass
class RecommendationDiagnostics:
    request_id: UUID
    kind: str
    target: int
    kept: int = 0
    attempts: list[AttemptRecord] = field(default_factory=list)

    def start_attempt(self, phase: str, requested: int) -> AttemptRecord:
        record = AttemptRecord(phase=phase, requested=requested)
        self.attempts.append(record)
        return record

    def ai_failed(self, record: AttemptRecord, code: str) -> None:
        record.ai_status = code
        self._log("recommendation_attempt", self._attempt_payload(record))

    def ai_answered(self, record: AttemptRecord, model: str | None) -> None:
        record.ai_status = "ok"
        record.model = model

    def finish_attempt(self, record: AttemptRecord) -> None:
        self._log("recommendation_attempt", self._attempt_payload(record))

    @property
    def ai_calls(self) -> int:
        return len(self.attempts)

    def reasons(self) -> list[str]:
        codes: set[str] = set()
        for record in self.attempts:
            codes.update(record.rejected)
            if record.ai_status not in ("ok", "pending"):
                codes.add(record.ai_status)
            if record.short_by:
                codes.add(MODEL_SHORT_RESPONSE)
        return sorted(codes)

    def summary(self, saved: int) -> dict:
        if saved >= self.target:
            outcome = OUTCOME_COMPLETE
        elif saved > 0:
            outcome = OUTCOME_PARTIAL
        else:
            outcome = OUTCOME_FAILED
        return {
            "request_id": str(self.request_id),
            "kind": self.kind,
            "target": self.target,
            "kept": self.kept,
            "saved": saved,
            "outcome": outcome,
            "ai_calls": self.ai_calls,
            "reasons": self.reasons(),
            "attempts": [record.as_dict() for record in self.attempts],
        }

    def finish(self, saved: int) -> dict:
        summary = self.summary(saved)
        self._log("recommendation_result", summary)
        return summary

    def _attempt_payload(self, record: AttemptRecord) -> dict:
        return {
            "request_id": str(self.request_id),
            "kind": self.kind,
            "target": self.target,
            "kept": self.kept,
            **record.as_dict(),
        }

    @staticmethod
    def _log(event: str, payload: dict) -> None:
        logger.info("%s %s", event, json.dumps(payload, sort_keys=True))
