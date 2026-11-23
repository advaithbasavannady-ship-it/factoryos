"""Central permission catalogue and tenant-membership permission evaluation."""

from __future__ import annotations

from fastapi import HTTPException


ROLE_ALIASES = {
    "ceo": "owner",
    "administrator": "admin",
    "department_employee": "employee",
    "supervisor": "department_manager",
    "sales_staff": "department_employee",
    "dispatch_clerk": "department_employee",
    "qc_inspector": "department_employee",
}

GLOBAL = {
    "company.read", "company.manage", "users.read", "users.manage", "roles.manage",
    "dashboard.read", "reports.company.read", "reports.export",
    "orders.read", "orders.create", "orders.manage", "orders.approve",
    "orders.department.read",
    "job_cards.read", "job_cards.manage", "production.company.read",
    "job_cards.department.manage", "quality.inspect",
    "inventory.read", "inventory.manage", "inventory.approve", "inventory.issue",
    "purchases.read", "purchases.manage", "scrap.read", "scrap.manage",
    "finished_goods.read", "finished_goods.manage", "audit.read", "messages.company.read",
    "machines.manage", "schedule.manage", "attendance.company.read", "attendance.company.write",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": set(GLOBAL),
    "admin": {"company.read", "company.manage", "users.read", "users.manage", "roles.manage", "audit.read"},
    "manager": {
        "company.read", "dashboard.read", "reports.company.read", "reports.export",
        "orders.read", "orders.create", "orders.manage", "orders.approve",
        "job_cards.read", "job_cards.manage", "production.company.read", "inventory.read",
        "inventory.approve", "purchases.read", "purchases.manage", "scrap.read",
        "scrap.manage", "finished_goods.read", "finished_goods.manage", "messages.company.read",
        "machines.manage", "schedule.manage", "attendance.company.read", "attendance.company.write",
    },
    "department_manager": {
        "dashboard.read", "reports.department.read", "orders.department.read", "job_cards.read",
        "job_cards.department.manage", "production.department.read", "production.department.write",
        "inventory.read", "material_requests.create", "material_requests.review_department",
        "scrap.read", "messages.department.read", "messages.department.write",
        "imports.department.create", "attendance.department.read", "attendance.department.write",
    },
    "department_employee": {
        "orders.department.read", "job_cards.read", "production.department.read", "production.department.write",
        "material_requests.create", "messages.department.read", "messages.department.write",
        "imports.department.create", "attendance.department.read",
    },
    "inventory_manager": {
        "inventory.read", "inventory.manage", "inventory.approve", "inventory.issue",
        "material_requests.read", "material_requests.approve", "purchases.read", "purchases.manage",
        "imports.department.create", "reports.department.read", "reports.export",
        "messages.department.read", "messages.department.write",
    },
    "accountant": {
        "reports.department.read", "reports.company.read", "reports.export", "purchases.read", "inventory.read",
        "orders.read", "audit.read",
    },
    # Existing legacy roles remain mapped so established workflows can migrate safely.
    "production_manager": {"company.read", "dashboard.read", "reports.department.read", "orders.read",
        "orders.approve", "job_cards.read", "job_cards.manage", "production.company.read",
        "production.department.read", "production.department.write", "inventory.read",
        "material_requests.create", "material_requests.review_department"},
    "supervisor": {"dashboard.read", "reports.department.read", "orders.department.read", "job_cards.read",
        "production.department.read", "production.department.write", "inventory.read",
        "material_requests.create", "messages.department.read", "messages.department.write"},
    "qc_inspector": {"job_cards.read", "production.department.read", "production.department.write", "quality.inspect",
        "scrap.read", "messages.department.read"},
    "dispatch_clerk": {"orders.read", "job_cards.read", "finished_goods.read", "finished_goods.manage"},
    "sales_staff": {"orders.read", "orders.create", "orders.manage", "job_cards.read"},
}


def _membership_departments(user: dict) -> set[str]:
    departments = user.get("department_ids")
    if departments is None:
        department = user.get("department")
        departments = [department] if department else []
    return {str(value) for value in departments if value}


def has_permission(user: dict, permission: str, department_id: str | None = None) -> bool:
    role = str(user.get("role", ""))
    canonical = ROLE_ALIASES.get(role, role)
    # Prefer a role's explicit catalogue. Aliases are only fallbacks for legacy
    # role names without a dedicated grant set.
    grants = set(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS.get(canonical, set())))
    overrides = set(user.get("permissions_override") or [])
    cross_department = set(user.get("cross_department_permissions") or [])
    denies = {value[5:] for value in overrides if value.startswith("deny:")}
    grants.update(value for value in overrides if not value.startswith("deny:"))
    # A cross-department grant is an explicit membership grant for this permission.
    grants.update(cross_department)
    if permission in denies or permission not in grants:
        return False
    if department_id and permission.startswith(("orders.department.", "job_cards.department.", "production.department.", "attendance.department.", "messages.department.", "imports.department.")):
        if canonical == "owner" or permission in cross_department:
            return True
        return department_id in _membership_departments(user)
    return True


def require_permission(user: dict, permission: str, *, department_id: str | None = None) -> None:
    if not has_permission(user, permission, department_id):
        raise HTTPException(status_code=403, detail="permission denied")


def require_any_permission(user: dict, *permissions: str) -> None:
    if not any(has_permission(user, permission) for permission in permissions):
        raise HTTPException(status_code=403, detail="permission denied")
