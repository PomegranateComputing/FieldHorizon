from __future__ import annotations

from dataclasses import dataclass, field

# Optional cheap seam (Implementation Brief III): a parameter seam only --
# no roles, policies, permissions, compartments, security labels, or
# authorization enforcement live here or anywhere else in this codebase.
# In embedded single-user mode every operation defaults to
# LOCAL_ADMIN_PRINCIPAL, so nothing changes behaviorally; this exists so a
# future authorization layer could be added without rewriting the domain
# to thread a new parameter through every call site from scratch.


@dataclass(frozen=True)
class PrincipalContext:
    principal_id: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    is_local_admin: bool = False


LOCAL_ADMIN_PRINCIPAL = PrincipalContext(principal_id="local-admin", roles=("admin",), is_local_admin=True)
