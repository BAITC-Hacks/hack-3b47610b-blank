"""Conservative document classification for a versioned preparation run.

This module receives only observed documents. It does not read the hidden
synthetic truth, supplier workbooks, forecasts or order quantities.
"""

from collections import defaultdict
from math import isfinite
from statistics import median

RULES_VERSION = "regular-demand-1"
POLICIES = ("review_only", "exclude_high_confidence")
ACTIONS = ("keep_regular", "exclude_regular", "treat_as_return")


def validate_rules(policy, decisions):
    if policy not in POLICIES:
        raise ValueError("Политика: review_only или exclude_high_confidence.")
    if not isinstance(decisions, list):
        raise ValueError("Ручные решения должны быть списком JSON.")
    result = {}
    for entry in decisions:
        if not isinstance(entry, dict) or set(entry) != {"document_key", "action", "author", "reason", "project_commitment_quantity"}:
            raise ValueError("Решение требует ключ документа, действие, автора, основание и отдельное обязательство.")
        if entry["action"] not in ACTIONS or not isinstance(entry["document_key"], str) or not entry["document_key"].strip():
            raise ValueError("Неверный ключ документа или действие решения.")
        if not all(isinstance(entry[key], str) and entry[key].strip() for key in ("author", "reason")):
            raise ValueError("Решению нужны непустые автор и основание.")
        quantity = entry["project_commitment_quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, (float, int)) or not isfinite(quantity) or quantity < 0:
            raise ValueError("Проектное обязательство должно быть неотрицательным числом.")
        if quantity and entry["action"] != "exclude_regular":
            raise ValueError("Отдельное проектное обязательство задаётся только при исключении из регулярного спроса.")
        if entry["document_key"] in result:
            raise ValueError("Два решения для одного документа в одной версии недопустимы.")
        result[entry["document_key"]] = dict(entry)
    return result


def classify_documents(documents, *, policy="review_only", decisions=None):
    """Return new document records; every source row remains untouched."""
    chosen = validate_rules(policy, [] if decisions is None else decisions)
    by_key = {item["document_key"]: item for item in documents}
    unknown = chosen.keys() - by_key.keys()
    if unknown:
        raise ValueError("Решение ссылается на отсутствующий документ: " + sorted(unknown)[0])
    sizes = defaultdict(list)
    seasonal = defaultdict(list)
    for item in documents:
        quantity = item["raw_quantity"]
        if item["source_state"] == "value" and quantity is not None and quantity > 0 and (item["document_type"] or "").strip().casefold() == "расходная накладная":
            sizes[item["sku"]].append(quantity)
            if item["period"]:
                seasonal[item["sku"], int(item["period"][5:7])].append(quantity)
    baselines = {sku: median(values) for sku, values in sizes.items()}
    monthly = {key: median(values) for key, values in seasonal.items() if len(values) >= 8}
    thresholds = {}
    candidates = {}
    for item in documents:
        sku, quantity = item["sku"], item["raw_quantity"]
        comparison = max(baselines.get(sku, 0), monthly.get((sku, int(item["period"][5:7])), 0)) if item["period"] else baselines.get(sku, 0)
        threshold = max(25, comparison * 8)
        thresholds[item["document_key"]] = threshold
        # With only one observed purchase the median is that purchase itself.
        # There is no defensible baseline, so the document goes to review.
        no_baseline = len(sizes[sku]) == 1
        if item["source_state"] == "value" and quantity is not None and quantity > 0 and (item["document_type"] or "").strip().casefold() == "расходная накладная" and (quantity >= threshold or no_baseline):
            candidates[item["document_key"]] = True
    customer_months = defaultdict(set)
    for item in documents:
        if item["document_key"] in candidates and item.get("customer_id") and item["period"]:
            customer_months[item["sku"], item["customer_id"]].add(item["period"])
    result = []
    for item in documents:
        row = dict(item)
        key, quantity = item["document_key"], item["raw_quantity"]
        manual = chosen.get(key)
        doc_type = (item["document_type"] or "").strip().casefold()
        customer = item.get("customer_id")
        repeated = customer and len(customer_months[item["sku"], customer]) >= 3
        support = len(sizes[item["sku"]])
        reason = "Обычная наблюдаемая операция."
        status = "regular"
        regular = quantity
        return_quantity = 0
        removed = 0
        if item["source_state"] != "value" or quantity is None or item["period"] is None:
            status, regular, reason = "needs_review", None, "Пустое, ошибочное или неоднозначное количество; частичная сумма не принята за полную."
        elif quantity < 0:
            if doc_type in ("возврат от покупателя", "customer return"):
                status, regular, return_quantity, removed = "return", 0, quantity, quantity
                reason = "Отдельный документ возврата покупателя; знак сохранён, в обычный спрос не превращён."
            else:
                status, regular, reason = "needs_review", None, "Отрицательный документ требует решения; знак и тип сохранены."
        elif doc_type != "расходная накладная":
            status, regular, reason = "needs_review", None, "Тип документа не подтверждает обычную продажу."
        elif key in candidates and repeated:
            reason = "Крупные покупки того же обезличенного клиента повторяются минимум в трёх месяцах; регулярный компонент сохранён."
        elif key in candidates:
            status = "candidate"
            if support < 12:
                reason = "Крупная операция при короткой/нулевой истории; нужна проверка, исключение не предложено уверенно."
            elif customer is None:
                reason = "Крупная операция по SKU и документу; клиентский анализ недоступен, номер документа не принят за клиента."
            else:
                reason = "Крупная операция относительно обычного и сезонного размера документа; повторяемость клиента не подтверждена."
                if quantity >= max(100, baselines[item["sku"]] * 20):
                    status = "high_confidence_candidate"
                    if policy == "exclude_high_confidence":
                        status, regular, removed = "excluded_by_policy", 0, quantity
                        reason += " Явно выбрана политика исключения уверенных одиночных случаев."
        if manual:
            if item["source_state"] != "value" or quantity is None:
                raise ValueError("Нельзя принять ручное решение о количестве, которое не установлено.")
            if manual["action"] == "treat_as_return":
                if quantity >= 0:
                    raise ValueError("Возврат должен сохранять отрицательный знак.")
                status, regular, return_quantity, removed = "return_manual", 0, quantity, quantity
            elif manual["action"] == "exclude_regular":
                if quantity <= 0:
                    raise ValueError("Исключать разовую продажу можно только из положительного количества.")
                status, regular, return_quantity, removed = "excluded_manual", 0, 0, quantity
            else:
                if quantity < 0:
                    raise ValueError("Отрицательный документ нельзя считать обычным положительным спросом.")
                status, regular, return_quantity, removed = "regular_manual", quantity, 0, 0
            reason = "Ручное решение: " + manual["reason"]
        row.update(status=status, reason=reason, regular_quantity=regular,
                   removed_component=removed, return_quantity=return_quantity,
                   project_commitment_quantity=manual["project_commitment_quantity"] if manual else 0,
                   manual_decision=manual, typical_document_quantity=baselines.get(item["sku"]),
                   threshold=thresholds[key], prior_document_count=support,
                   client_analysis="available" if customer is not None else "unavailable")
        result.append(row)
    return result


def aggregate_months(documents):
    by_month = defaultdict(list)
    for item in documents:
        if item["period"]:
            by_month[item["sku"], item["period"]].append(item)
    result = []
    for (sku, period), items in sorted(by_month.items()):
        unresolved = [item["document_key"] for item in items if item["status"] in ("candidate", "high_confidence_candidate", "needs_review")]
        known_raw = sum(item["raw_quantity"] for item in items if item["raw_quantity"] is not None)
        regular_known = sum(item["regular_quantity"] for item in items if item["regular_quantity"] is not None)
        removed = sum(item["removed_component"] for item in items)
        returned = sum(item["return_quantity"] for item in items)
        commitment = sum(item["project_commitment_quantity"] for item in items)
        result.append({"sku": sku, "period": period, "state": "needs_review" if unresolved else "value",
                       "raw_signed_quantity": known_raw, "regular_quantity": None if unresolved else regular_known,
                       "known_regular_subtotal": regular_known, "removed_component": removed,
                       "return_quantity": returned, "project_commitment_quantity": commitment,
                       "document_count": len(items), "unresolved_document_keys": unresolved,
                       "source_document_keys": [item["document_key"] for item in items]})
    return result
