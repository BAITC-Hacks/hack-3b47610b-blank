"""Causal, conservative estimates from complete-day availability evidence."""

from collections import defaultdict
from datetime import date
from statistics import median

RULES_VERSION = "lost-demand-1"


def estimate_days(evidence, sales, as_of):
    """Estimate each absent day using only earlier, observed in-stock days.

    sales maps ISO dates to {quantity, state, document_keys}. Missing sales are
    unknown; neither a blank transaction nor missing row means zero.
    """
    cutoff = date.fromisoformat(as_of)
    prior_available = []
    absent_rank = defaultdict(int)
    result = []
    for item in sorted(evidence, key=lambda row: row["date"]):
        day = date.fromisoformat(item["date"])
        if day >= cutoff:
            continue
        observed = sales.get(item["date"], {"quantity": None, "state": "missing", "document_keys": []})
        quantity = observed["quantity"] if observed["state"] == "value" else None
        row = {"date": item["date"], "period": day.replace(day=1).isoformat(),
               "available": item["available"], "observed_hours": item["observed_hours"],
               "observed_regular_quantity": quantity, "sales_state": observed["state"],
               "document_keys": observed["document_keys"], "estimated_lost_quantity": None,
               "method": None, "reference_days": [], "reason": None}
        if item["observed_hours"] != 24 or item["available"] is None:
            row["state"] = "unknown_availability"
            row["reason"] = "Нет подтверждения наличия или отсутствия на протяжении полных 24 часов."
        elif item["available"]:
            row["state"] = "available" if quantity is not None and quantity >= 0 else "unknown_sales"
            row["reason"] = ("День наличия с наблюдаемой регулярной продажей." if row["state"] == "available"
                             else "Наличие подтверждено, но продажа за день не установлена.")
            if row["state"] == "available":
                prior_available.append((day, quantity))
            row["estimated_lost_quantity"] = 0 if row["state"] == "available" else None
        else:
            absent_rank[row["period"]] += 1
            if quantity is not None and quantity > 0:
                row["state"] = "needs_review"
                row["reason"] = "Положительная продажа конфликтует с полнодневным отсутствием в выбранном охвате."
            else:
                seasonal = [(source_day, amount) for source_day, amount in prior_available
                            if source_day.month == day.month and (day - source_day).days <= 740]
                seasonal_weeks = {(source_day.isocalendar().year, source_day.isocalendar().week)
                                  for source_day, _ in seasonal}
                if len(seasonal) >= 7 and len(seasonal_weeks) >= 2:
                    pool, method = seasonal, "same_calendar_month"
                else:
                    recent = [(source_day, amount) for source_day, amount in prior_available
                              if (day - source_day).days <= 56]
                    recent_weeks = {(source_day.isocalendar().year, source_day.isocalendar().week)
                                    for source_day, _ in recent}
                    if len(recent) < 14 or len(recent_weeks) < 3:
                        pool, method = [], None
                    else:
                        pool, method = recent, "recent_56_days_fallback"
                if not pool:
                    row["state"] = "insufficient_history"
                    row["reason"] = "Недостаточно предшествующих дней полного наличия с известными продажами."
                elif absent_rank[row["period"]] > 2 * len(pool):
                    row["state"] = "extrapolation_limit"
                    row["reason"] = "Период отсутствия превышает двойной объём сопоставимых дней; оценка остановлена."
                else:
                    weekdays = [(source_day, amount) for source_day, amount in pool
                                if source_day.weekday() == day.weekday()]
                    if len(weekdays) >= 3:
                        pool = weekdays
                        method += "_weekday"
                    row["state"] = "estimated"
                    row["estimated_lost_quantity"] = float(median(amount for _, amount in pool))
                    row["method"] = method + "_median"
                    row["reference_days"] = [source_day.isoformat() for source_day, _ in pool]
                    row["reason"] = "Медиана наблюдаемых продаж в предшествующие дни полного наличия."
        result.append(row)
    return result
