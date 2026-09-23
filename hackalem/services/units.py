"""Version-bound unit evidence and explicit conversion confirmations.

No archive MOQ or price is promoted to current supplier terms by this module.
Confirmation applies only to the selected SKU's units and conversion factor.
"""

import json
import math
import re
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime


def _unit(value):
    return (value or "").strip().lower().rstrip(".")


def _suggest_factor(accounting, purchase, name):
    if not accounting or not purchase:
        return None
    if _unit(accounting) == _unit(purchase):
        return 1.0
    found = set()
    for number, base, pack in re.findall(r"(\d+(?:[.,]\d+)?)\s*(шт|м)\s*/\s*(компл|упак)", (name or "").lower()):
        if _unit(accounting) == base and _unit(purchase) == pack:
            found.add(float(number.replace(",", ".")))
    return next(iter(found)) if len(found) == 1 and next(iter(found)) > 0 else None


def build_unit_assessments(connection, snapshot_id):
    """Materialize suggestions once with snapshot code/rules; never revise history."""
    products = defaultdict(list)
    for row in connection.execute(
        """SELECT p.*, s.source_kind FROM products p JOIN snapshot_files s ON s.file_id=p.file_id
        WHERE s.snapshot_id=? ORDER BY s.source_kind, p.row""", (snapshot_id,),
    ):
        products[row["sku"]].append(dict(row))
    catalog = defaultdict(list)
    for row in connection.execute(
        """SELECT c.* FROM catalog_items c JOIN snapshot_files s ON s.file_id=c.file_id
        WHERE s.snapshot_id=?""", (snapshot_id,),
    ):
        catalog[row["article"]].append(dict(row))
    minimums = defaultdict(list)
    for row in connection.execute(
        """SELECT m.* FROM measures m JOIN snapshot_files s ON s.file_id=m.file_id
        WHERE s.snapshot_id=? AND m.metric='minimum_order'""", (snapshot_id,),
    ):
        minimums[row["sku"]].append(dict(row))
    for sku, records in products.items():
        units = sorted({row["unit"] for row in records if row["unit"]})
        articles = sorted({row["article"] for row in records if row["article"]})
        matches = [row for article in articles for row in catalog.get(article, [])]
        accounting = units[0] if len(units) == 1 else None
        chosen = matches[0] if len(matches) == 1 and len(articles) == 1 else None
        purchase = chosen["purchase_unit"] if chosen else None
        factor = _suggest_factor(accounting, purchase, chosen["name"] if chosen else None)
        if len(units) > 1:
            status = "accounting_unit_conflict"
        elif accounting is None:
            status = "accounting_unit_missing"
        elif not chosen:
            status = "catalog_match_missing_or_ambiguous"
        elif not purchase:
            status = "purchase_unit_missing"
        elif _unit(accounting) != _unit(purchase):
            status = "conversion_confirmation_required"
        else:
            status = "archive_units_unconfirmed"
        details = {
            "accounting_units": units, "articles": articles,
            "accounting_evidence": [row for row in records if row["unit"]],
            "minimum_order_observations": minimums.get(sku, []),
            "archive_candidates": matches,
            "archive_status": "archived_external_cache", "archive_terms_active": False,
            "factor_basis": "matching_unit_labels" if factor == 1 else "supplier_name_hint" if factor else None,
            "factor_confirmed": False,
        }
        connection.execute(
            """INSERT INTO unit_assessments (snapshot_id, sku, accounting_unit, purchase_unit,
            proposed_factor, status, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, sku, accounting, purchase, factor, status, json.dumps(details, ensure_ascii=False)),
        )


def get_unit_assessment(database_path, snapshot_id, sku):
    from hackalem.services.systeme import _connect, _snapshot
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        row = connection.execute(
            "SELECT * FROM unit_assessments WHERE snapshot_id=? AND sku=?", (snapshot_id, sku),
        ).fetchone()
        if row is None:
            raise ValueError(f"Для SKU {sku} в снимке №{snapshot_id} нет сведений о единицах.")
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        result["confirmations"] = [dict(item) for item in connection.execute(
            "SELECT * FROM unit_confirmations WHERE snapshot_id=? AND sku=? ORDER BY id", (snapshot_id, sku),
        )]
        return result


def confirm_unit_conversion(database_path, snapshot_id, sku, factor, *,
                            archive_units_confirmed=False, reason="", confirmed_by=""):
    """Persist a user's explicit unit decision, without activating archive prices."""
    from hackalem.services.systeme import _connect
    assessment = get_unit_assessment(database_path, snapshot_id, sku)
    if archive_units_confirmed is not True:
        raise ValueError("Подтвердите применимость учётной и закупочной единиц архивного источника.")
    if not assessment["accounting_unit"] or not assessment["purchase_unit"]:
        raise ValueError("Единицы отсутствуют или неоднозначны; сначала уточните источник.")
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
        raise ValueError("Коэффициент должен быть положительным конечным числом.")
    if not reason.strip() or not confirmed_by.strip():
        raise ValueError("Укажите основание и автора подтверждения.")
    with closing(_connect(database_path)) as connection, connection:
        cursor = connection.execute(
            """INSERT INTO unit_confirmations (snapshot_id, sku, factor, accounting_unit,
            purchase_unit, archive_units_confirmed, reason, confirmed_by, created_at_utc)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (snapshot_id, sku, factor, assessment["accounting_unit"], assessment["purchase_unit"],
             reason.strip(), confirmed_by.strip(), datetime.now(UTC).isoformat()),
        )
        return dict(connection.execute("SELECT * FROM unit_confirmations WHERE id=?", (cursor.lastrowid,)).fetchone())


def convert_quantity(database_path, snapshot_id, sku, quantity, *, confirmation_id=None):
    """A proposal alone never permits conversion; bind consent to snapshot + SKU."""
    from hackalem.services.systeme import _connect
    if confirmation_id is None:
        raise ValueError("Для перевода количества требуется явное подтверждение коэффициента.")
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity):
        raise ValueError("Количество должно быть конечным числом.")
    with closing(_connect(database_path)) as connection:
        confirmation = connection.execute(
            "SELECT * FROM unit_confirmations WHERE id=? AND snapshot_id=? AND sku=?",
            (confirmation_id, snapshot_id, sku),
        ).fetchone()
        if confirmation is None:
            raise ValueError("Подтверждение не относится к выбранному снимку и SKU.")
        return {
            "snapshot_id": snapshot_id, "sku": sku, "confirmation_id": confirmation_id,
            "purchase_quantity": quantity, "purchase_unit": confirmation["purchase_unit"],
            "factor": confirmation["factor"], "accounting_quantity": quantity * confirmation["factor"],
            "accounting_unit": confirmation["accounting_unit"],
            "archive_prices_and_moq_active": False,
        }


def list_unit_issues(database_path, snapshot_id, *, sku=None, limit=100):
    """Per-SKU import diagnostics, separate from later business reconciliation."""
    from hackalem.services.systeme import _connect, _snapshot
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        query = "SELECT sku, accounting_unit, purchase_unit, proposed_factor, status FROM unit_assessments WHERE snapshot_id=? AND status != 'archive_units_unconfirmed'"
        args = [snapshot_id]
        if sku is not None:
            query += " AND sku=?"
            args.append(sku)
        query += " ORDER BY sku LIMIT ?"
        args.append(limit)
        return [dict(row) for row in connection.execute(query, args)]
