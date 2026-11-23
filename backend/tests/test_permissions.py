"""Unit coverage for tenant-role permission resolution without a database."""

from lib.permissions import has_permission


def test_inventory_manager_can_manage_stock_but_not_company_settings():
    user = {"role": "inventory_manager", "department_ids": ["Inventory"]}
    assert has_permission(user, "inventory.manage")
    assert not has_permission(user, "company.manage")


def test_department_employee_is_scoped_to_assigned_department():
    user = {"role": "department_employee", "department_ids": ["Production"]}
    assert has_permission(user, "production.department.write", "Production")
    assert not has_permission(user, "production.department.write", "Inventory")


def test_membership_deny_override_wins_over_role_grant():
    user = {"role": "owner", "permissions_override": ["deny:inventory.manage"]}
    assert not has_permission(user, "inventory.manage")


def test_explicit_cross_department_grant_is_required():
    user = {"role": "department_manager", "department_ids": ["Production"],
            "cross_department_permissions": ["production.department.write"]}
    assert has_permission(user, "production.department.write", "Inventory")


def test_legacy_owner_role_keeps_existing_role_mapping():
    assert has_permission({"role": "owner"}, "reports.company.read")


def test_department_manager_does_not_receive_company_manager_grants():
    user = {"role": "department_manager", "department_ids": ["Production"]}
    assert not has_permission(user, "reports.company.read")
    assert not has_permission(user, "production.company.read")
    assert not has_permission(user, "orders.read")
    assert has_permission(user, "orders.department.read", "Production")
    assert not has_permission(user, "orders.department.read", "Inventory")


def test_legacy_roles_use_their_explicit_catalogue_before_alias_fallback():
    sales = {"role": "sales_staff", "department_ids": ["Production"]}
    dispatch = {"role": "dispatch_clerk", "department_ids": ["Production"]}
    supervisor = {"role": "supervisor", "department_ids": ["Production"]}
    assert has_permission(sales, "orders.manage")
    assert not has_permission(sales, "production.department.write", "Production")
    assert has_permission(dispatch, "finished_goods.manage")
    assert not has_permission(dispatch, "production.department.write", "Production")
    assert has_permission(supervisor, "production.department.write", "Production")
    assert has_permission(supervisor, "orders.department.read", "Production")
    assert not has_permission(supervisor, "orders.department.read", "Inventory")


def test_quality_inspection_is_limited_to_qc_and_owner_roles():
    assert has_permission({"role": "qc_inspector", "department_ids": ["Quality"]}, "quality.inspect")
    assert has_permission({"role": "owner"}, "quality.inspect")
    assert not has_permission({"role": "supervisor", "department_ids": ["Packaging"]}, "quality.inspect")
