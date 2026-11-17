"""Approval queue, approver authentication, executor and audit log.

Safety model: agents can only call :meth:`ApprovalService.propose`, which writes a
``pending`` row. The only code path that reaches a :class:`ClusterWriter` is
:meth:`ApprovalService.decide` with ``approve=True`` *and* a valid approver token
*and* a non-expired pending action. Every step is written to the audit log.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update

from triage import db
from triage.hitl.models import (
    ActionStatus,
    ActionType,
    PendingAction,
    assess_risk,
    build_preview,
    validate_params,
)
from triage.k8s.base import ClusterReader, ClusterWriter

WriterFactory = Callable[[], ClusterWriter]
DecisionHook = Callable[[PendingAction, float], None]


class ApprovalError(Exception):
    """Base class for approval failures."""


class Unauthorized(ApprovalError):
    """Approver token missing or wrong."""


class NotFound(ApprovalError):
    """No such pending action."""


class InvalidState(ApprovalError):
    """Action is not pending (already decided, executed or expired)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _execute(writer: ClusterWriter, action: PendingAction) -> dict[str, Any]:
    """Dispatch an *approved* action to the cluster writer. Private on purpose."""
    p = action.params
    match action.action:
        case ActionType.ROLLOUT_RESTART:
            return writer.rollout_restart(action.namespace, action.deployment)
        case ActionType.SCALE_DEPLOYMENT:
            return writer.scale_deployment(action.namespace, action.deployment, int(p["replicas"]))
        case ActionType.ROLLBACK_DEPLOYMENT:
            return writer.rollback_deployment(action.namespace, action.deployment)
        case ActionType.PATCH_RESOURCES:
            return writer.patch_resources(
                action.namespace,
                action.deployment,
                p["container"],
                p.get("requests"),
                p.get("limits"),
            )
    raise InvalidState(f"unsupported action {action.action}")  # pragma: no cover


class ApprovalService:
    """Persistent HITL queue backed by the application database."""

    def __init__(
        self,
        engine: Engine,
        approver_token: str,
        writer_factory: WriterFactory,
        ttl: timedelta = timedelta(minutes=60),
        on_decision: DecisionHook | None = None,
    ) -> None:
        """Create the service; ``writer_factory`` is only called after an approval."""
        if not approver_token:
            raise ValueError("approver token must be non-empty")
        self._engine = engine
        self._token = approver_token.encode()
        self._writer_factory = writer_factory
        self._ttl = ttl
        self._on_decision = on_decision

    # ----------------------------------------------------------------- audit
    def _audit(self, actor: str, event: str, action_id: str | None, detail: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(db.audit_log).values(
                    at=_now(), actor=actor, event=event, action_id=action_id, detail=detail
                )
            )

    def audit_trail(self, action_id: str | None = None) -> list[dict[str, Any]]:
        """Return audit entries (optionally for one action), oldest first."""
        q = select(db.audit_log).order_by(db.audit_log.c.id)
        if action_id:
            q = q.where(db.audit_log.c.action_id == action_id)
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    # --------------------------------------------------------------- propose
    def propose(
        self,
        *,
        action: str,
        namespace: str,
        deployment: str,
        params: dict[str, Any] | None,
        requested_by: str,
        rationale: str = "",
        investigation_id: str | None = None,
        reader: ClusterReader | None = None,
    ) -> PendingAction:
        """File a remediation request. Never touches the cluster."""
        kind = ActionType(action)
        clean = validate_params(kind, params or {})
        preview, replicas = build_preview(reader, kind, namespace, deployment, clean)
        now = _now()
        pa = PendingAction(
            id=f"act_{uuid.uuid4().hex[:12]}",
            investigation_id=investigation_id,
            action=kind,
            namespace=namespace,
            deployment=deployment,
            params=clean,
            preview=preview,
            risk=assess_risk(kind, clean, replicas),
            rationale=rationale,
            requested_by=requested_by,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._engine.begin() as conn:
            conn.execute(insert(db.pending_actions).values(**pa.model_dump()))
        self._audit(requested_by, "proposed", pa.id, {"action": kind, "preview": preview})
        return pa

    # ------------------------------------------------------------------ read
    def get(self, action_id: str) -> PendingAction:
        """Load one action, marking it expired if its TTL passed."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(db.pending_actions).where(db.pending_actions.c.id == action_id)
            ).first()
        if row is None:
            raise NotFound(action_id)
        pa = PendingAction.model_validate(dict(row._mapping))
        pa.created_at, pa.expires_at = _aware(pa.created_at), _aware(pa.expires_at)
        if pa.status == ActionStatus.PENDING and pa.expires_at <= _now():
            self._set_status(pa.id, ActionStatus.EXPIRED)
            self._audit("system", "expired", pa.id, {})
            pa.status = ActionStatus.EXPIRED
        return pa

    def list(
        self, status: ActionStatus | None = None, investigation_id: str | None = None
    ) -> list[PendingAction]:
        """List actions, newest first."""
        q = select(db.pending_actions.c.id).order_by(db.pending_actions.c.created_at.desc())
        if investigation_id:
            q = q.where(db.pending_actions.c.investigation_id == investigation_id)
        with self._engine.connect() as conn:
            ids = [r.id for r in conn.execute(q)]
        actions = [self.get(i) for i in ids]
        return [a for a in actions if status is None or a.status == status]

    def _set_status(self, action_id: str, status: ActionStatus, **extra: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(db.pending_actions)
                .where(db.pending_actions.c.id == action_id)
                .values(status=status, **extra)
            )

    # ---------------------------------------------------------------- decide
    def check_token(self, token: str | None) -> None:
        """Constant-time comparison of the approver token."""
        if not token or not hmac.compare_digest(token.encode(), self._token):
            raise Unauthorized("invalid approver token")

    def decide(
        self, action_id: str, *, approve: bool, token: str | None, approver: str = "approver"
    ) -> PendingAction:
        """Approve (and execute) or reject a pending action."""
        try:
            self.check_token(token)
        except Unauthorized:
            self._audit(approver, "denied_bad_token", action_id, {})
            raise
        pa = self.get(action_id)
        if pa.status != ActionStatus.PENDING:
            raise InvalidState(f"action {action_id} is {pa.status}, not pending")
        now = _now()
        wait_s = (now - pa.created_at).total_seconds()
        if not approve:
            self._set_status(pa.id, ActionStatus.REJECTED, decided_at=now, decided_by=approver)
            self._audit(approver, "rejected", pa.id, {})
            pa = self.get(pa.id)
            if self._on_decision:
                self._on_decision(pa, wait_s)
            return pa

        self._set_status(pa.id, ActionStatus.APPROVED, decided_at=now, decided_by=approver)
        self._audit(approver, "approved", pa.id, {})
        try:
            result = _execute(self._writer_factory(), pa)
        except Exception as exc:
            self._set_status(pa.id, ActionStatus.FAILED, result={"error": str(exc)})
            self._audit("executor", "failed", pa.id, {"error": str(exc)})
        else:
            self._set_status(pa.id, ActionStatus.EXECUTED, result=result)
            self._audit("executor", "executed", pa.id, {"result": result})
        pa = self.get(pa.id)
        if self._on_decision:
            self._on_decision(pa, wait_s)
        return pa
