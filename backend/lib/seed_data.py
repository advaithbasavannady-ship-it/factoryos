"""Demonstration data for FactoryOS (general manufacturing).

All records are tagged with `company_id`. Data is generated relative to today's date and
`ensure_fresh()` tops up attendance, production and new orders for each day that passes,
so the demo always has a live-looking "today". Everything here is SIMULATED.
"""

import asyncio
import os
import random
import uuid
from datetime import date, datetime, timedelta, timezone

from lib.dates import today_iso
from lib.demo_safety import DemoContext, open_demo_context, validate_demo_context

# Optional at import time so ordinary backend startup does not require demo settings.
# Mutating entry points resolve and verify it immediately before any database operation.
CID: str | None = os.environ.get("DEMO_COMPANY_ID")
HISTORY_DAYS = 90
ATTENDANCE_DAYS = 45
COLLECTIONS = [
    "companies", "customers", "orders", "employees", "attendance", "production", "machines",
    "maintenance", "inventory", "suppliers", "purchases", "consumption", "events",
    "conversations", "meta", "users", "notifications",
]


def _bind_context(context: DemoContext) -> None:
    validate_demo_context(context)
    global CID
    CID = context.company_id
    COMPANY["id"] = CID


def uid() -> str:
    return str(uuid.uuid4())


def ds(d: date) -> str:
    return d.isoformat()


def parse(s: str) -> date:
    return date.fromisoformat(s)


COMPANY = {
    "id": CID,
    "name": "Apex Precision Industries",
    "industry": "General Manufacturing",
    "size": "Medium (50-250 employees)",
    "location": "Columbus, Ohio",
    "currency": "USD",
    "production_unit": "units",
    "departments": ["Machining", "Fabrication", "Assembly", "Finishing", "Quality Control", "Packaging", "Maintenance"],
    "production_departments": ["Machining", "Fabrication", "Assembly", "Finishing", "Packaging"],
    "shift_start": "08:00",
    "shift_end": "17:00",
    "owner_name": "Marcus Vance",
    "owner_role": "Managing Director",
}

DEPTS = [
    ("Machining", 10, ["CNC Machinist", "CNC Operator", "Setter", "Shift Lead"]),
    ("Fabrication", 8, ["Welder", "Fabricator", "Laser Operator", "Shift Lead"]),
    ("Assembly", 10, ["Assembler", "Line Technician", "Line Supervisor"]),
    ("Finishing", 6, ["Coating Technician", "Polisher", "Supervisor"]),
    ("Quality Control", 5, ["QC Inspector", "QC Engineer"]),
    ("Packaging", 5, ["Packer", "Dispatch Clerk"]),
    ("Maintenance", 4, ["Maintenance Technician", "Electrician", "Maintenance Lead"]),
]
FIRST = ["James", "Maria", "Robert", "Linda", "Michael", "Priya", "David", "Sofia", "Daniel", "Aisha",
         "Kevin", "Elena", "Brian", "Grace", "Carlos", "Hannah", "Jason", "Mei", "Ryan", "Olivia",
         "Tyler", "Fatima", "Eric", "Chloe", "Marcus", "Nina", "Derek", "Rosa", "Sean", "Leah"]
LAST = ["Carter", "Nguyen", "Patel", "Johnson", "Garcia", "Miller", "Okafor", "Kim", "Lopez", "Walsh",
        "Brooks", "Singh", "Turner", "Russo", "Hughes", "Diaz", "Foster", "Chen", "Murphy", "Reyes",
        "Price", "Novak", "Bennett", "Ali", "Ward", "Fischer", "Coleman", "Ortiz", "Hayes"]

# code, name, type, department, manufacturer, daily_capacity, status, health
MACHINES = [
    ("CNC-01", "Vertical Machining Center", "CNC Mill", "Machining", "Haas VF-2", 140, "running", 92),
    ("CNC-02", "CNC Turning Center", "CNC Lathe", "Machining", "DMG Mori NLX", 160, "running", 88),
    ("CNC-03", "5-Axis Machining Center", "CNC Mill", "Machining", "Mazak Variaxis", 110, "maintenance", 54),
    ("LSR-01", "Fiber Laser Cutter", "Laser Cutter", "Fabrication", "Trumpf TruLaser", 220, "running", 95),
    ("PRB-01", "Hydraulic Press Brake", "Press Brake", "Fabrication", "Amada HG", 180, "idle", 81),
    ("WLD-01", "Robotic Welding Cell", "Welding Robot", "Fabrication", "Lincoln Electric", 150, "running", 77),
    ("ASM-01", "Assembly Line A", "Assembly Line", "Assembly", "In-house", 260, "running", 90),
    ("ASM-02", "Assembly Line B", "Assembly Line", "Assembly", "In-house", 240, "idle", 85),
    ("PNT-01", "Powder Coating Booth", "Coating Line", "Finishing", "Nordson Encore", 300, "running", 73),
    ("CMM-01", "Coordinate Measuring Machine", "Inspection", "Quality Control", "Zeiss Contura", 0, "running", 97),
    ("PKG-01", "Automated Packaging Line", "Packaging", "Packaging", "Bosch Pack", 420, "maintenance", 58),
    ("CMP-01", "Rotary Screw Compressor", "Utility", "Maintenance", "Atlas Copco GA", 0, "idle", 83),
]

CAPACITY = {m[0]: m[5] for m in MACHINES}

CUSTOMERS = [
    ("Northwind Hydraulics", "Ellen Park", "Detroit"), ("Summit Agri Equipment", "Tom Becker", "Des Moines"),
    ("Brightline Rail Systems", "Anita Rao", "Chicago"), ("Keystone HVAC Solutions", "Paul Hart", "Pittsburgh"),
    ("Vertex Medical Devices", "Dr. Lisa Moore", "Minneapolis"), ("Ironclad Construction Co.", "Mike Duran", "Indianapolis"),
    ("BluePeak Energy", "Sara Lin", "Houston"), ("Orion Robotics", "Jake Wilson", "Boston"),
    ("Granite State Pumps", "Karen Doyle", "Manchester"), ("Pacific Marine Supply", "Leo Tran", "Seattle"),
    ("Midwest Conveyor Corp.", "Dana Fox", "Milwaukee"), ("Atlas Truck Bodies", "Ray Gomez", "Louisville"),
]
PRODUCTS = [
    ("Hydraulic Valve Body HV-200", 48.0), ("Steel Mounting Bracket MB-14", 6.5), ("Gearbox Housing GH-310", 112.0),
    ("Precision Drive Shaft PS-50", 34.0), ("Control Panel Enclosure CP-8", 85.0), ("Conveyor Roller CR-75", 19.5),
    ("Pump Impeller PI-120", 41.0), ("Fabricated Base Frame FF-900", 240.0),
]

SUPPLIERS = [
    ("Great Lakes Steel Supply", "Hank Morris", "Cleveland", ["Mild Steel Sheet", "Stainless Rod"], 4.6, 5),
    ("AlumTech Metals", "Irene Cho", "Cincinnati", ["Aluminium Bar"], 4.3, 7),
    ("SealPro Components", "Victor Hale", "Dayton", ["Hydraulic Seals", "Bearings"], 4.1, 10),
    ("ColorCoat Industrial", "Megan Shaw", "Toledo", ["Powder Coat Paint", "Cutting Coolant"], 4.4, 6),
    ("WeldMax Consumables", "Omar Haddad", "Akron", ["Welding Wire"], 4.7, 4),
    ("FastenRight Distribution", "Julia Brandt", "Columbus", ["Fasteners", "Packaging Cartons"], 4.0, 3),
]
# sku, name, unit, qty, reorder, unit_cost, supplier_idx, location, daily_use
RAW = [
    ("RM-1001", "Mild Steel Sheet 2mm", "sheets", 420, 300, 38.0, 0, "Bay A-1", 28),
    ("RM-1002", "Aluminium Bar 6061", "kg", 680, 500, 4.2, 1, "Bay A-2", 45),
    ("RM-1003", "Stainless Steel Rod 304", "kg", 210, 350, 6.8, 0, "Bay A-3", 30),
    ("RM-1004", "Hydraulic Seal Kit", "kits", 95, 120, 12.5, 2, "Rack C-2", 14),
    ("RM-1005", "Powder Coat Paint RAL 7016", "kg", 140, 100, 9.6, 3, "Paint Store", 11),
    ("RM-1006", "Welding Wire ER70S-6", "kg", 380, 150, 3.1, 4, "Rack B-4", 18),
    ("RM-1007", "Fasteners M8 Assorted", "boxes", 64, 80, 22.0, 5, "Rack C-1", 7),
    ("RM-1008", "Deep Groove Bearing 6205", "pcs", 900, 400, 2.4, 2, "Rack C-3", 60),
    ("RM-1009", "Cutting Coolant Concentrate", "litres", 260, 120, 5.5, 3, "Fluid Store", 12),
    ("RM-1010", "Corrugated Shipping Cartons", "pcs", 1500, 800, 0.9, 5, "Packaging Bay", 110),
]
FINISHED = [("FG-2001", 0, 340, 150), ("FG-2002", 1, 2600, 1200), ("FG-2003", 2, 85, 120), ("FG-2004", 3, 520, 300),
            ("FG-2005", 4, 140, 80), ("FG-2006", 5, 1100, 600), ("FG-2007", 6, 260, 200), ("FG-2008", 7, 22, 30)]


def _employees() -> list[dict]:
    out, n = [], 0
    rng = random.Random(11)
    for dept, count, roles in DEPTS:
        for i in range(count):
            n += 1
            first, last = FIRST[(n * 7) % len(FIRST)], LAST[(n * 11) % len(LAST)]
            out.append({
                "id": uid(), "company_id": CID, "emp_code": f"EMP-{1000 + n}", "name": f"{first} {last}",
                "department": dept, "role": roles[-1] if i == 0 else roles[i % (len(roles) - 1 or 1)],
                "shift": "General (08:00-17:00)" if n % 4 else "Early (06:00-15:00)",
                "phone": f"+1 614-555-{1000 + n * 37 % 9000:04d}",
                "email": f"{first.lower()}.{last.lower()}@apexprecision.demo",
                "joined_on": ds(date(2016 + rng.randint(0, 8), rng.randint(1, 12), rng.randint(1, 28))),
            })
    return out


def _attendance_for(day: date, employees: list[dict], live: bool) -> list[dict]:
    rng = random.Random(f"att-{day.isoformat()}")
    out = []
    for e in employees:
        early = e["shift"].startswith("Early")
        base_h = 6 if early else 8
        r = rng.random()
        status = "absent" if r < 0.06 else "leave" if r < 0.10 else "late" if r < 0.17 else "present"
        rec = {"id": uid(), "company_id": CID, "employee_id": e["id"], "date": ds(day), "status": status,
               "check_in": None, "check_out": None}
        if status in ("present", "late"):
            mins = rng.randint(-20, 5) if status == "present" else rng.randint(16, 70)
            t = datetime(2000, 1, 1, base_h, 0) + timedelta(minutes=mins)
            rec["check_in"] = t.strftime("%H:%M")
            if not live:
                out_t = datetime(2000, 1, 1, base_h + 9, 0) + timedelta(minutes=rng.randint(-15, 45))
                rec["check_out"] = out_t.strftime("%H:%M")
        out.append(rec)
    return out


def _production_for(day: date, machines: list[dict], live: bool, today: date) -> list[dict]:
    out = []
    for m in machines:
        cap = CAPACITY.get(m["code"], 0)
        if not cap:
            continue
        rng = random.Random(f"prod-{day.isoformat()}-{m['code']}")
        planned = cap
        if live:
            # planned downtime lowers today's target; idle machines run a reduced plan
            if m["status"] in ("maintenance", "offline"):
                planned = 0
            elif m["status"] == "idle":
                planned = int(cap * 0.5)
            factor = rng.uniform(0.82, 0.95)
        else:
            dip = 0.55 if rng.random() < 0.05 else 1.0  # occasional breakdown day
            factor = rng.uniform(0.86, 1.05) * dip
        actual = int(planned * factor)
        rejected = int(actual * rng.uniform(0.008, 0.04))
        out.append({"id": uid(), "company_id": CID, "date": ds(day), "machine_id": m["id"],
                    "department": m["department"], "planned": planned, "actual": actual, "rejected": rejected})
    return out


PRODUCT_DEPT = {
    "Hydraulic Valve Body HV-200": "Machining", "Steel Mounting Bracket MB-14": "Fabrication",
    "Gearbox Housing GH-310": "Machining", "Precision Drive Shaft PS-50": "Machining",
    "Control Panel Enclosure CP-8": "Fabrication", "Conveyor Roller CR-75": "Assembly",
    "Pump Impeller PI-120": "Machining", "Fabricated Base Frame FF-900": "Fabrication",
}
CUSTOMER_INDUSTRY = ["Hydraulics", "Agriculture", "Rail", "HVAC", "Medical Devices", "Construction",
                     "Energy", "Robotics", "Pumps", "Marine", "Material Handling", "Automotive"]


def _customers(today: date) -> list[dict]:
    out = []
    for i, (name, contact, city) in enumerate(CUSTOMERS):
        slug = name.split()[0].lower()
        out.append({"id": uid(), "company_id": CID, "name": name, "contact_person": contact,
                    "email": f"purchasing@{slug}.demo", "phone": f"+1 555-{310 + i * 41}-{2000 + i * 377}",
                    "city": city, "industry": CUSTOMER_INDUSTRY[i], "created_on": ds(today - timedelta(days=400 - i * 20))})
    return out


def _snapshot(c: dict) -> dict:
    return {k: c[k] for k in ("name", "contact_person", "email", "phone", "city")}


def _orders_for(day: date, today: date, seq_start: int, count: int, customers: list[dict]) -> list[dict]:
    rng = random.Random(f"ord-{day.isoformat()}")
    out = []
    age = (today - day).days
    for i in range(count):
        cust = customers[rng.randrange(len(customers))]
        prod, price = PRODUCTS[rng.randrange(len(PRODUCTS))]
        lead = rng.randint(10, 24)
        expected = day + timedelta(days=lead)
        qty = rng.choice([50, 100, 150, 200, 250, 400, 500, 750, 1000])
        r = rng.random()
        if age == 0:
            status = "new"
        elif age <= 3:
            status = "new" if r < 0.5 else "in_production"
        elif age <= 12:
            status = "in_production" if r < 0.5 else "quality_check" if r < 0.65 else "completed" if r < 0.85 else "dispatched"
        else:
            status = "dispatched" if r < 0.85 else "completed" if r < 0.93 else "in_production"
        dept = PRODUCT_DEPT[prod]
        o = {
            "id": uid(), "company_id": CID, "order_no": f"SO-{day.year}-{seq_start + i:04d}",
            "customer_id": cust["id"], "customer": _snapshot(cust),
            "product": prod, "quantity": qty, "unit_price": price, "status": status,
            "priority": rng.choice(["low", "medium", "medium", "high"]),
            "order_date": ds(day), "expected_delivery": ds(expected), "dispatched_date": None,
            "notes": rng.choice(["", "Customer requests certificate of conformance.", "Ship via LTL freight.",
                                 "Repeat order - same spec as last quarter.", "Partial delivery acceptable."]),
            "department": None, "production_target_date": None, "produced": 0, "qc_passed": 0, "qc_rejected": 0,
            "qc_inspector": None, "dispatch_carrier": None, "dispatch_ref": None,
            "timeline": [{"stage": "Order received", "date": ds(day), "note": f"{qty} x {prod}"}],
        }
        if status != "new":
            start = day + timedelta(days=1)
            o.update(department=dept, production_target_date=ds(expected - timedelta(days=2)))
            o["timeline"].append({"stage": "Production started", "date": ds(min(start, today)), "note": f"Assigned to {dept}"})
            o["produced"] = int(qty * rng.uniform(0.2, 0.85)) if status == "in_production" else qty
        if status in ("completed", "dispatched"):
            rej = int(qty * rng.uniform(0, 0.02))
            o.update(qc_passed=qty, qc_rejected=rej, qc_inspector=rng.choice(["Hannah Diaz", "Eric Chen", "Grace Price"]))
            qd = min(day + timedelta(days=max(3, lead - 5)), today - timedelta(days=1))
            o["timeline"].append({"stage": "Quality inspection passed", "date": ds(qd), "note": f"{qty} passed, {rej} reworked"})
        if status == "quality_check":
            o["timeline"].append({"stage": "Sent to quality inspection", "date": ds(today - timedelta(days=1)), "note": f"{qty} produced"})
        if status == "dispatched":
            dd = min(day + timedelta(days=max(4, lead - rng.randint(-2, 4))), today - timedelta(days=1))
            carrier = rng.choice(["FedEx Freight", "XPO Logistics", "Own fleet", "Old Dominion"])
            o.update(dispatched_date=ds(dd), dispatch_carrier=carrier, dispatch_ref=f"BOL-{rng.randint(100000, 999999)}")
            o["timeline"].append({"stage": "Dispatched", "date": ds(dd), "note": carrier})
        out.append(o)
    return out


def _events(today: date) -> list[dict]:
    E = [
        (0, "08:30", "09:00", "Daily production stand-up", "Review yesterday's output, today's targets and blockers.",
         ["Marcus Vance", "Shift Leads", "QC Engineer"], "medium", "completed", "meeting", "Shop floor huddle zone"),
        (0, "11:00", "12:00", "Northwind Hydraulics quarterly review", "Delivery performance and Q3 volume forecast.",
         ["Marcus Vance", "Ellen Park", "Sales Manager"], "high", "scheduled", "meeting", "Conference Room A"),
        (0, "14:00", "15:30", "CNC-03 spindle repair sign-off", "Confirm repair, calibration and return-to-service.",
         ["Maintenance Lead", "Machining Supervisor"], "high", "scheduled", "maintenance", "Machining bay"),
        (0, "16:30", "17:00", "Dispatch planning", "Confirm tomorrow's truck slots.", ["Dispatch Clerk", "Marcus Vance"],
         "low", "scheduled", "delivery", "Dispatch office"),
        (1, "10:00", "11:00", "Supplier negotiation - Great Lakes Steel", "Annual steel pricing contract.",
         ["Marcus Vance", "Purchase Manager", "Hank Morris"], "high", "scheduled", "meeting", "Video call"),
        (1, "15:00", "16:00", "Safety committee", "Monthly safety review and near-miss reports.",
         ["HSE Officer", "Department Heads"], "medium", "scheduled", "meeting", "Training room"),
        (2, "09:00", "12:00", "ISO 9001 surveillance audit", "External auditor on site.", ["QC Engineer", "Marcus Vance"],
         "critical", "scheduled", "audit", "Quality office"),
        (3, "13:00", "14:00", "Board financial review", "Monthly P&L and capex approvals.", ["Marcus Vance", "CFO"],
         "high", "scheduled", "meeting", "Board room"),
        (4, "07:00", "11:00", "PKG-01 preventive maintenance", "Belt replacement and sensor check.",
         ["Maintenance Team"], "medium", "scheduled", "maintenance", "Packaging bay"),
        (6, "10:00", "11:30", "New customer visit - Orion Robotics", "Plant tour and capability presentation.",
         ["Marcus Vance", "Sales Manager", "Jake Wilson"], "high", "scheduled", "meeting", "Reception"),
        (8, "14:00", "15:00", "Brightline Rail order dispatch", "Large frame order loading.", ["Dispatch Clerk"],
         "medium", "scheduled", "delivery", "Loading dock 2"),
        (12, "09:30", "10:30", "Quarterly town hall", "Company update for all employees.", ["All Staff"],
         "medium", "scheduled", "meeting", "Canteen"),
        (-1, "10:00", "11:00", "Weekly operations review", "KPIs across all departments.", ["Department Heads"],
         "medium", "completed", "meeting", "Conference Room A"),
        (-2, "15:00", "16:00", "Interview - Maintenance Technician", "Second round.", ["Maintenance Lead", "HR"],
         "low", "completed", "other", "HR office"),
        (-5, "09:00", "10:00", "Vertex Medical quality call", "Corrective action follow-up.", ["QC Engineer", "Dr. Lisa Moore"],
         "high", "completed", "meeting", "Video call"),
    ]
    return [{"id": uid(), "company_id": CID, "title": t, "description": d, "date": ds(today + timedelta(days=o)),
             "start_time": s, "end_time": e, "participants": p, "location": loc,
             "reminder_minutes": 30 if pr in ("high", "critical") else 15, "priority": pr, "status": st,
             "category": cat, "created_by": "Owner" if i % 3 else "Personal Assistant"}
            for i, (o, s, e, t, d, p, pr, st, cat, loc) in enumerate(E)]


async def _seed_all(context: DemoContext) -> None:
    validate_demo_context(context)
    _bind_context(context)
    today = parse(today_iso())
    for c in COLLECTIONS:
        await context.database[c].delete_many({"company_id": CID})
    await context.database.companies.insert_one(dict(COMPANY, company_id=CID))

    employees = _employees()
    await context.database.employees.insert_many([dict(e) for e in employees])
    att = []
    for k in range(ATTENDANCE_DAYS, -1, -1):
        att += _attendance_for(today - timedelta(days=k), employees, k == 0)
    await context.database.attendance.insert_many(att)

    machines = []
    for code, name, mtype, dept, mfr, cap, status, health in MACHINES:
        rng = random.Random(code)
        last_m = today - timedelta(days=rng.randint(8, 70))
        next_m = today + timedelta(days=rng.randint(1, 4) if health < 80 else rng.randint(9, 45))
        if status == "maintenance":
            last_m, next_m = today, today + timedelta(days=60)
        machines.append({
            "id": uid(), "company_id": CID, "code": code, "name": name, "type": mtype, "department": dept,
            "manufacturer": mfr, "status": status, "health": health,
            "utilization": 0 if status == "maintenance" else rng.randint(35, 55) if status == "idle" else rng.randint(72, 94),
            "runtime_hours_today": 0.0 if status == "maintenance" else round(rng.uniform(1.5, 3.5) if status == "idle" else rng.uniform(5.5, 7.8), 1),
            "downtime_hours_week": round(rng.uniform(14, 26) if status == "maintenance" else rng.uniform(0.5, 6.5), 1),
            "last_maintenance": ds(last_m), "next_maintenance": ds(next_m),
            "installed_on": ds(date(2012 + rng.randint(0, 11), rng.randint(1, 12), 1)), "simulated": True,
        })
    await context.database.machines.insert_many([dict(m) for m in machines])

    prod = []
    for k in range(HISTORY_DAYS, -1, -1):
        prod += _production_for(today - timedelta(days=k), machines, k == 0, today)
    await context.database.production.insert_many(prod)

    by_code = {m["code"]: m for m in machines}
    techs = ["Leah Hayes", "Derek Ortiz", "Sean Coleman", "External - OEM Service"]
    mrng = random.Random(99)
    maint = []
    for m in machines:
        for j in range(mrng.randint(2, 3)):
            d = today - timedelta(days=mrng.randint(5, 85))
            mt = mrng.choice(["preventive", "preventive", "calibration", "breakdown", "inspection"])
            maint.append({"id": uid(), "company_id": CID, "machine_id": m["id"], "date": ds(d), "type": mt,
                          "description": {"preventive": "Scheduled preventive service: lubrication, filters, belts",
                                          "calibration": "Axis and sensor calibration",
                                          "breakdown": "Unplanned stoppage - component replaced",
                                          "inspection": "Monthly safety and condition inspection"}[mt],
                          "technician": mrng.choice(techs), "duration_hours": round(mrng.uniform(1, 9), 1),
                          "cost": round(mrng.uniform(150, 4200), 0), "status": "completed"})
    maint += [
        {"id": uid(), "company_id": CID, "machine_id": by_code["CNC-03"]["id"], "date": ds(today), "type": "breakdown",
         "description": "Spindle bearing failure - bearing replacement and re-alignment", "technician": "External - OEM Service",
         "duration_hours": 14, "cost": 6800, "status": "in_progress"},
        {"id": uid(), "company_id": CID, "machine_id": by_code["PKG-01"]["id"], "date": ds(today), "type": "preventive",
         "description": "Conveyor belt replacement and photo-eye sensor check", "technician": "Derek Ortiz",
         "duration_hours": 6, "cost": 1250, "status": "in_progress"},
        {"id": uid(), "company_id": CID, "machine_id": by_code["WLD-01"]["id"], "date": ds(today + timedelta(days=2)),
         "type": "calibration", "description": "Torch TCP calibration and wire feeder service", "technician": "Sean Coleman",
         "duration_hours": 3, "cost": 480, "status": "scheduled"},
        {"id": uid(), "company_id": CID, "machine_id": by_code["PNT-01"]["id"], "date": ds(today + timedelta(days=3)),
         "type": "preventive", "description": "Booth filter change and gun nozzle replacement", "technician": "Leah Hayes",
         "duration_hours": 4, "cost": 720, "status": "scheduled"},
    ]
    await context.database.maintenance.insert_many(maint)

    suppliers = [{"id": uid(), "company_id": CID, "name": n, "contact_person": c, "email": f"sales@{n.split()[0].lower()}.demo",
                  "phone": f"+1 800-555-{1200 + i * 111}", "city": city, "materials": mats, "rating": r, "lead_time_days": lt}
                 for i, (n, c, city, mats, r, lt) in enumerate(SUPPLIERS)]
    await context.database.suppliers.insert_many([dict(s) for s in suppliers])

    items = [{"id": uid(), "company_id": CID, "sku": sku, "name": name, "category": "raw", "unit": unit, "quantity": float(q),
              "reorder_level": float(ro), "unit_cost": cost, "supplier_id": suppliers[si]["id"], "location": loc}
             for sku, name, unit, q, ro, cost, si, loc, _u in RAW]
    items += [{"id": uid(), "company_id": CID, "sku": sku, "name": PRODUCTS[pi][0], "category": "finished", "unit": "pcs",
               "quantity": float(q), "reorder_level": float(ro), "unit_cost": round(PRODUCTS[pi][1] * 0.62, 2),
               "supplier_id": None, "location": "Finished Goods Warehouse"} for sku, pi, q, ro in FINISHED]
    await context.database.inventory.insert_many([dict(i) for i in items])

    cons = []
    for k in range(30, -1, -1):
        d = today - timedelta(days=k)
        rng = random.Random(f"cons-{d}")
        for (sku, *_r, use), it in zip(RAW, items):
            cons.append({"id": uid(), "company_id": CID, "date": ds(d), "item_id": it["id"],
                         "quantity": round(use * rng.uniform(0.7, 1.25) * (0.6 if k == 0 else 1), 1)})
    await context.database.consumption.insert_many(cons)

    prng = random.Random(5)
    purchases = []
    for i in range(14):
        it = items[prng.randrange(len(RAW))]
        sup = next(s for s in suppliers if s["id"] == it["supplier_id"])
        od = today - timedelta(days=prng.randint(0, 40))
        ed = od + timedelta(days=sup["lead_time_days"])
        status = "received" if ed < today else "ordered"
        purchases.append({"id": uid(), "company_id": CID, "po_no": f"PO-{today.year}-{301 + i:04d}", "supplier_id": sup["id"],
                          "item_id": it["id"], "quantity": float(prng.choice([100, 200, 250, 500, 1000])),
                          "unit_cost": it["unit_cost"], "order_date": ds(od), "expected_date": ds(ed), "status": status})
    await context.database.purchases.insert_many(purchases)

    customers = _customers(today)
    await context.database.customers.insert_many([dict(c) for c in customers])
    orders, seq = [], 1
    for k in range(60, -1, -1):
        d = today - timedelta(days=k)
        n = 5 if k == 0 else random.Random(f"n-{d}").choice([1, 1, 2, 2, 3])
        orders += _orders_for(d, today, seq, n, customers)
        seq += n
    # a few dispatched today
    cands = [o for o in orders if o["status"] == "completed" and 5 <= (today - parse(o["order_date"])).days <= 20][:3]
    for o in cands:
        o.update(status="dispatched", dispatched_date=ds(today), dispatch_carrier="Own fleet", dispatch_ref="BOL-TODAY")
        o["timeline"].append({"stage": "Dispatched", "date": ds(today), "note": "Own fleet"})
    await context.database.orders.insert_many(orders)

    await context.database.events.insert_many(_events(today))
    await context.database.meta.insert_one({"company_id": CID, "last_date": ds(today), "order_seq": seq})


async def _top_up(context: DemoContext, last: date, today: date) -> None:
    """Generate simulated activity for each missing day since the last generated date."""
    validate_demo_context(context)
    await context.database.attendance.update_many(
        {"company_id": CID, "date": {"$lt": ds(today)}, "status": {"$in": ["present", "late"]}, "check_out": None},
        {"$set": {"check_out": "17:10"}},
    )
    employees = await context.database.employees.find({"company_id": CID}, {"_id": 0}).to_list(1000)
    machines = await context.database.machines.find({"company_id": CID}, {"_id": 0}).to_list(1000)
    customers = await context.database.customers.find({"company_id": CID}, {"_id": 0}).to_list(1000)
    meta = await context.database.meta.find_one({"company_id": CID})
    seq = meta.get("order_seq", 1000)
    d = max(last + timedelta(days=1), today - timedelta(days=30))
    while d <= today:
        live = d == today
        att = _attendance_for(d, employees, live)
        if att:
            await context.database.attendance.insert_many(att)
        prod = _production_for(d, machines, live, today)
        if prod:
            await context.database.production.insert_many(prod)
        n = 4 if live else 2
        if customers:
            await context.database.orders.insert_many(_orders_for(d, d, seq, n, customers))
        seq += n
        d += timedelta(days=1)
    await context.database.meta.update_one({"company_id": CID}, {"$set": {"last_date": ds(today), "order_seq": seq}})


_lock = asyncio.Lock()
_checked: dict[str, str] = {}

# ---------- v2: demo users, order lifecycle migration, workflow showcase ----------
WEB = {"user_id": "web", "name": "Online order form", "role": "customer"}


async def _ensure_users(context: DemoContext) -> dict[str, dict]:
    validate_demo_context(context)
    if not await context.database.users.count_documents({"company_id": CID}):
        emps = await context.database.employees.find({"company_id": CID}, {"_id": 0}).sort("emp_code", 1).to_list(1000)
        company = await context.database.companies.find_one({"id": CID}) or COMPANY
        lead = lambda d, i=0: [e for e in emps if e["department"] == d][i]["name"]  # noqa: E731
        users = [
            ("u-owner", company["owner_name"], "owner", company["owner_role"], None),
            ("u-ops", "Sarah Mitchell", "manager", "Operations Manager", None),
            ("u-pm", "Daniel Brooks", "production_manager", "Production Manager", None),
            *[(f"u-sup-{d.lower()}", lead(d), "supervisor", f"{d} Supervisor", d) for d in COMPANY["production_departments"]],
            ("u-qc", lead("Quality Control"), "qc_inspector", "QC Inspector", "Quality Control"),
            ("u-dispatch", lead("Packaging", 1), "dispatch_clerk", "Dispatch Clerk", "Packaging"),
            ("u-sales", "Emily Ross", "sales_staff", "Sales Coordinator", None),
        ]
        await context.database.users.insert_many([{"id": i, "company_id": CID, "name": n, "role": r, "title": t, "department": d} for i, n, r, t, d in users])
    return {u["id"]: u for u in await context.database.users.find({"company_id": CID}, {"_id": 0}).to_list(100)}


def _act(u: dict) -> dict:
    return {"user_id": u["id"], "name": u["name"], "role": u["role"]}


def _e(stage: str, date_s: str, note: str, who: dict, hm: str = "10:00") -> dict:
    return {"stage": stage, "date": date_s, "note": note, "at": f"{date_s}T{hm}:00+00:00", "user": who["name"], "role": who["role"]}


async def _migrate_orders(context: DemoContext) -> None:
    """Upgrade legacy orders in place (idempotent: only orders without `source`)."""
    validate_demo_context(context)
    U = await _ensure_users(context)
    sup = {u["department"]: u for u in U.values() if u["role"] == "supervisor"}
    today = parse(today_iso())
    docs = await context.database.orders.find({"company_id": CID, "source": {"$exists": False}}, {"_id": 0}).to_list(10000)
    for o in docs:
        rng = random.Random(o["order_no"])
        st = {"new": "confirmed", "completed": "ready_for_dispatch"}.get(o["status"], o["status"])
        online = rng.random() < 0.4
        value = o["quantity"] * o["unit_price"]
        od = o["order_date"]
        tl = [_e("Order received", od, f"{o['quantity']} x {o['product']} via {'online order form' if online else 'offline order request'}",
                 WEB if online else U["u-sales"], "09:10")]
        if online:
            tl.append(_e("Confirmed", od, "Auto-confirmed: all rules satisfied", {"name": "Auto-confirmation rules", "role": "system"}, "09:11"))
        else:
            tl += [_e("Submitted for approval", od, "Submitted by Emily Ross", U["u-sales"], "09:12"),
                   _e("Approved", od, "Order confirmed for production", U["u-ops"], "11:30")]
        for t in o.get("timeline", [])[1:]:
            who = {"Production started": U["u-pm"], "Sent to quality inspection": sup.get(o.get("department") or "", U["u-pm"]),
                   "Quality inspection passed": U["u-qc"], "Dispatched": U["u-dispatch"]}.get(t["stage"], U["u-pm"])
            stage = {"Production started": "Assigned to production", "Sent to quality inspection": "Sent to quality control"}.get(t["stage"], t["stage"])
            if stage == "Dispatched":
                tl.append(_e("Ready for dispatch", t["date"], f"{o['quantity']} units moved to finished goods", U["u-qc"], "08:30"))
            tl.append(_e(stage, t["date"], t.get("note", ""), who, "14:00"))
        if st == "ready_for_dispatch":
            tl.append(_e("Ready for dispatch", tl[-1]["date"], f"{o['quantity']} units moved to finished goods", U["u-qc"], "16:00"))
        stages = []
        if st in ("in_production", "quality_check", "ready_for_dispatch", "dispatched") and o.get("department"):
            d = o["department"]
            stages = [{"department": d, "supervisor": sup.get(d, {}).get("name"), "assigned_employees": [], "target_date": o.get("production_target_date"),
                       "received": o["quantity"], "produced": o.get("produced", 0), "rejected": 0, "transferred": 0,
                       "status": "done" if o.get("produced", 0) >= o["quantity"] else "active"}]
        age = (today - parse(od)).days
        paid = value if st == "dispatched" and age > 25 else value * 0.5 if st == "dispatched" else value * 0.3 if online or st in ("ready_for_dispatch", "quality_check") else 0
        o.update(
            status=st, source="online" if online else "offline", submitted_by=WEB if online else _act(U["u-sales"]),
            submitted_at=f"{od}T09:10:00+00:00", decision=None if online else "approved",
            decided_by=None if online else _act(U["u-ops"]), decided_at=None if online else f"{od}T11:30:00+00:00",
            decision_note="", holds=[], customization="", payment_terms="advance" if online else "credit",
            amount_paid=round(paid, 2), payments=[{"id": uid(), "amount": round(paid, 2), "date": od, "method": "Bank transfer",
                                                   "reference": f"TRX-{rng.randint(10000, 99999)}", "recorded_by": "Sarah Mitchell"}] if paid else [],
            route_name=o.get("department"), stages=stages, current_stage=0,
            qc_status={"quality_check": "pending", "ready_for_dispatch": "passed", "dispatched": "passed"}.get(st), timeline=tl,
        )
        await context.database.orders.replace_one({"company_id": CID, "id": o["id"]}, o)


async def _showcase(context: DemoContext) -> None:
    """Workflow examples for the demo: approvals, review holds, a multi-department order."""
    validate_demo_context(context)
    U = await _ensure_users(context)
    today = parse(today_iso())
    t = ds(today)
    custs = await context.database.customers.find({"company_id": CID}, {"_id": 0}).sort("name", 1).to_list(100)
    meta = await context.database.meta.find_one({"company_id": CID}) or {}
    seq = meta.get("order_seq", 500)
    snap = lambda c: {k: c[k] for k in ("name", "contact_person", "email", "phone", "city")}  # noqa: E731
    sys_rules = {"name": "Auto-confirmation rules", "role": "system"}

    def base(c, product, price, qty, days_ago, status, source, submitter, **kw):
        nonlocal seq
        seq += 1
        od = ds(today - timedelta(days=days_ago))
        o = {"id": uid(), "company_id": CID, "order_no": f"SO-{today.year}-{seq:04d}", "customer_id": c["id"], "customer": snap(c),
             "product": product, "quantity": qty, "unit_price": price, "status": status, "priority": kw.pop("priority", "medium"),
             "order_date": od, "expected_delivery": ds(today + timedelta(days=kw.pop("due_in", 18))), "dispatched_date": None,
             "notes": kw.pop("notes", ""), "department": None, "production_target_date": None, "produced": 0, "qc_passed": 0,
             "qc_rejected": 0, "qc_inspector": None, "dispatch_carrier": None, "dispatch_ref": None, "source": source,
             "submitted_by": submitter, "submitted_at": f"{od}T09:40:00+00:00", "decision": None, "decided_by": None, "decided_at": None,
             "decision_note": "", "holds": [], "customization": "", "payment_terms": "advance" if source == "online" else "credit",
             "amount_paid": 0, "payments": [], "route_name": None, "stages": [], "current_stage": 0, "qc_status": None,
             "timeline": [_e("Order received", od, f"{qty} x {product} via {'online order form' if source == 'online' else 'offline order request'}",
                             {"name": submitter["name"], "role": submitter["role"]}, "09:40")]}
        o.update(kw)
        return o

    sales, supm = _act(U["u-sales"]), _act(U["u-sup-machining"])
    out = []
    for c, prod, price, qty, ago, who in [(custs[0], "Pump Impeller PI-120", 41.0, 300, 0, sales),
                                          (custs[3], "Gearbox Housing GH-310", 112.0, 80, 0, supm),
                                          (custs[6], "Steel Mounting Bracket MB-14", 6.5, 1500, 1, sales)]:
        o = base(c, prod, price, qty, ago, "awaiting_approval", "offline", who)
        o["timeline"].append(_e("Submitted for approval", o["order_date"], f"Submitted by {who['name']}", who, "09:41"))
        out.append(o)
        await context.database.notifications.insert_one({"id": uid(), "company_id": CID, "recipient_roles": ["owner", "manager"],
                                           "title": f"Order {o['order_no']} awaiting approval",
                                           "body": f"{who['name']} submitted {qty} x {prod} for {c['name']}",
                                           "link": f"/orders?order={o['id']}", "created_at": f"{o['order_date']}T09:41:00+00:00", "read_by": []})
    o = base(custs[8], "Precision Drive Shaft PS-50", 34.0, 250, 2, "correction_requested", "offline", sales,
             decision="correction_requested", decided_by=_act(U["u-ops"]), decided_at=f"{ds(today - timedelta(days=1))}T15:20:00+00:00",
             decision_note="Confirm shaft diameter tolerance and the revised delivery date with the customer")
    o["timeline"] += [_e("Submitted for approval", o["order_date"], "Submitted by Emily Ross", sales, "09:41"),
                      _e("Correction requested", ds(today - timedelta(days=1)), o["decision_note"], U["u-ops"], "15:20")]
    out.append(o)
    o = base(custs[10], "Fabricated Base Frame FF-900", 240.0, 120, 3, "rejected", "offline", sales,
             decision="rejected", decided_by=_act(U["u-owner"]), decided_at=f"{ds(today - timedelta(days=2))}T12:05:00+00:00",
             decision_note="Customer credit limit exceeded; request advance payment before re-submitting")
    o["timeline"] += [_e("Submitted for approval", o["order_date"], "Submitted by Emily Ross", sales, "09:41"),
                      _e("Rejected", ds(today - timedelta(days=2)), o["decision_note"], U["u-owner"], "12:05")]
    out.append(o)
    hp = {"key": "payment", "label": "Advance payment of 3,060.00 required before confirmation", "cleared": False, "cleared_by": None, "cleared_at": None}
    o = base(custs[1], "Control Panel Enclosure CP-8", 85.0, 120, 0, "pending_review", "online", WEB, holds=[hp])
    o["timeline"].append(_e("Pending review", t, hp["label"], sys_rules, "09:41"))
    out.append(o)
    hc = {"key": "customization", "label": "Custom specification needs engineering review", "cleared": False, "cleared_by": None, "cleared_at": None}
    o = base(custs[7], "Conveyor Roller CR-75", 19.5, 400, 1, "pending_review", "online", WEB, holds=[hc],
             customization="Stainless steel shaft, food-grade coating, 600 mm face width", amount_paid=2340.0,
             payments=[{"id": uid(), "amount": 2340.0, "date": ds(today - timedelta(days=1)), "method": "Card (online)", "reference": "WEB-88412", "recorded_by": "Online order form"}])
    o["timeline"] += [_e("Payment recorded", o["order_date"], "2,340.00 via Card (online)", WEB, "09:40"), _e("Pending review", o["order_date"], hc["label"], sys_rules, "09:41")]
    out.append(o)
    # multi-department order mid-production
    route = ["Fabrication", "Assembly", "Finishing", "Packaging"]
    sups = {d: U[f"u-sup-{d.lower()}"]["name"] for d in route}
    stg = [{"department": "Fabrication", "received": 200, "produced": 200, "rejected": 4, "transferred": 200},
           {"department": "Assembly", "received": 200, "produced": 150, "rejected": 2, "transferred": 120},
           {"department": "Finishing", "received": 120, "produced": 90, "rejected": 0, "transferred": 0},
           {"department": "Packaging", "received": 0, "produced": 0, "rejected": 0, "transferred": 0}]
    for i, s in enumerate(stg):
        s.update(supervisor=sups[s["department"]], assigned_employees=[], target_date=ds(today + timedelta(days=2 + 2 * i)),
                 status="done" if i == 0 else "active" if s["received"] else "waiting")
    stg[0]["status"] = "active"  # 2 downstream rejects need replacing
    o = base(custs[2], "Fabricated Base Frame FF-900", 240.0, 200, 8, "in_production", "offline", sales, priority="high", due_in=12,
             decision="approved", decided_by=_act(U["u-ops"]), decided_at=f"{ds(today - timedelta(days=8))}T11:00:00+00:00",
             stages=stg, current_stage=0, department="Fabrication", route_name="Fabricated assemblies",
             production_target_date=ds(today + timedelta(days=8)), amount_paid=14400.0,
             payments=[{"id": uid(), "amount": 14400.0, "date": ds(today - timedelta(days=7)), "method": "Bank transfer", "reference": "TRX-55120", "recorded_by": "Sarah Mitchell"}])
    d = lambda n: ds(today - timedelta(days=n))  # noqa: E731
    o["timeline"] += [
        _e("Submitted for approval", d(8), "Submitted by Emily Ross", sales, "09:41"), _e("Approved", d(8), "Order confirmed for production", U["u-ops"], "11:00"),
        _e("Payment recorded", d(7), "14,400.00 via Bank transfer · TRX-55120", U["u-ops"], "10:00"),
        _e("Assigned to production", d(7), "Route: Fabrication → Assembly → Finishing → Packaging; first stage Fabrication", U["u-pm"], "13:00"),
        _e("Production update", d(5), "Fabrication: +200 good, +4 rejected", U["u-sup-fabrication"], "16:30"),
        _e("Units transferred", d(5), "200 units Fabrication → Assembly (received there: 200)", U["u-sup-fabrication"], "16:45"),
        _e("Production update", d(2), "Assembly: +150 good, +2 rejected", U["u-sup-assembly"], "16:10"),
        _e("Units transferred", d(2), "120 units Assembly → Finishing (received there: 120)", U["u-sup-assembly"], "16:20"),
        _e("Production update", d(0), "Finishing: +90 good, +0 rejected", U["u-sup-finishing"], "11:15"),
    ]
    out.append(o)
    await context.database.orders.insert_many(out)
    await context.database.meta.update_one({"company_id": CID}, {"$set": {"order_seq": seq}})


async def _upgrade_v2(context: DemoContext) -> None:
    validate_demo_context(context)
    await _ensure_users(context)
    await _migrate_orders(context)
    await _showcase(context)
    await context.database.meta.update_one({"company_id": CID}, {"$set": {"schema": 2}})


async def _ensure_fresh(context: DemoContext) -> None:
    validate_demo_context(context)
    _bind_context(context)
    today = today_iso()
    if _checked.get(CID) == today:
        return
    async with _lock:
        if _checked.get(CID) == today:
            return
        meta = await context.database.meta.find_one({"company_id": CID})
        if not meta or not await context.database.companies.find_one({"id": CID}):
            await _seed_all(context)
        elif meta["last_date"] < today:
            await _top_up(context, parse(meta["last_date"]), parse(today))
            await _migrate_orders(context)
        meta = await context.database.meta.find_one({"company_id": CID}) or {}
        if meta.get("schema", 1) < 2:
            await _upgrade_v2(context)
        _checked[CID] = today


async def _reset_with_context(context: DemoContext) -> None:
    validate_demo_context(context)
    _bind_context(context)
    async with _lock:
        await _seed_all(context)
        await _upgrade_v2(context)
        _checked[CID] = today_iso()


async def _run_with_demo_context(operation):
    context = await open_demo_context()
    try:
        validate_demo_context(context)
        _bind_context(context)
        return await operation(context)
    finally:
        await context.close()


async def seed_all() -> None:
    await _run_with_demo_context(_seed_all)


async def ensure_users() -> dict[str, dict]:
    return await _run_with_demo_context(_ensure_users)


async def migrate_orders() -> None:
    await _run_with_demo_context(_migrate_orders)


async def upgrade_v2() -> None:
    await _run_with_demo_context(_upgrade_v2)


async def ensure_fresh() -> None:
    await _run_with_demo_context(_ensure_fresh)


async def reset_demo() -> None:
    await _run_with_demo_context(_reset_with_context)


async def seed_from_cli() -> None:
    """Seed and upgrade within one client/authentication boundary."""
    async def seed_and_upgrade(context: DemoContext) -> None:
        await _seed_all(context)
        await _upgrade_v2(context)

    await _run_with_demo_context(seed_and_upgrade)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
