"""Финансовая отчётность по прогону — на любой момент, а не только за месяц.

Прогон хранит деньги и начисления по шагам хранения (по умолчанию — сутки,
``economics.report_bucket_hours``) с разбивкой по статьям и разделам. Из них
собираются отчёты любой периодичности — час, сутки, неделя, месяц, квартал —
и срез на любую дату внутри прогона:

* :func:`cash_flow_statement` — отчёт о движении денежных средств (ОДДС):
  текущая, инвестиционная и финансовая деятельность, остаток на начало и
  конец каждого периода;
* :func:`income_statement` — отчёт о прибылях и убытках (ОПиУ) по
  начислениям; капитальные вложения (стройка) в него не входят и показаны
  отдельной строкой;
* :func:`cash_gap_report` — кассовые разрывы: когда остаток уходит в минус,
  на сколько и на какой срок, какой резерв нужен;
* :func:`payment_calendar` — платёжный календарь: поступления и платежи по
  дням и статьям;
* :func:`variance_report` — план-факт: два прогона (план и прогноз) по
  статьям и периодам;
* :func:`dashboard` — онлайн-панель: остаток, скорость расходования денег,
  запас в днях, выручка, маржа, показатели проектов и активные сигналы.

Всё считается из результата прогона (``result.finance``), повторный прогон
не нужен. Точность среза по статьям — шаг хранения; остаток на конец прогона
— точный.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

CATEGORIES = ("operating", "investing", "financing")
CATEGORY_NAMES = {
    "operating": "Текущая деятельность",
    "investing": "Инвестиционная деятельность",
    "financing": "Финансовая деятельность",
}
PERIOD_HOURS = {"hour": 1.0, "day": 24.0, "week": 168.0, "month": 730.0, "quarter": 2190.0, "year": 8760.0}

_EPS = 1e-9


# ----------------------------------------------------------------- периоды --


def _origin(finance: dict[str, Any]) -> datetime | None:
    raw = finance.get("start_date")
    if not raw:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(raw)[:10]), datetime.min.time())
    except ValueError:
        return None


def _period_key(
    t: float, period: str | float, origin: datetime | None
) -> tuple[Any, str, float]:
    """(ключ периода, подпись, начало периода в часах прогона) для момента t."""
    if isinstance(period, (int, float)):
        step = float(period)
        index = int(math.floor(t / step + _EPS))
        return index, _hours_label(index * step, origin), index * step
    if period == "total":
        return 0, "весь прогон", 0.0
    if origin is None or period == "hour":
        step = PERIOD_HOURS[period]
        index = int(math.floor(t / step + _EPS))
        if origin is None:
            names = {"hour": "час", "day": "сутки", "week": "неделя",
                     "month": "месяц", "quarter": "квартал", "year": "год"}
            return index, f"{names[period]} {index + 1}", index * step
        return index, _hours_label(index * step, origin), index * step
    moment = origin + timedelta(hours=t + _EPS)
    if period == "day":
        start = datetime(moment.year, moment.month, moment.day)
        label = start.strftime("%d.%m.%Y")
    elif period == "week":
        day0 = datetime(moment.year, moment.month, moment.day) - timedelta(days=moment.weekday())
        start = day0
        iso = day0.isocalendar()
        label = f"{iso[0]}-W{iso[1]:02d} (с {day0.strftime('%d.%m')})"
    elif period == "month":
        start = datetime(moment.year, moment.month, 1)
        label = start.strftime("%m.%Y")
    elif period == "quarter":
        q = (moment.month - 1) // 3
        start = datetime(moment.year, 3 * q + 1, 1)
        label = f"{q + 1} кв. {moment.year}"
    elif period == "year":
        start = datetime(moment.year, 1, 1)
        label = str(moment.year)
    else:
        raise ValueError(f"неизвестный период {period!r}")
    hours = (start - origin).total_seconds() / 3600.0
    return start, label, max(0.0, hours)


def _hours_label(hours: float, origin: datetime | None) -> str:
    if origin is None:
        return f"t = {hours:g} ч"
    return (origin + timedelta(hours=hours)).strftime("%d.%m.%Y %H:%M")


def _date_of(t: float, origin: datetime | None) -> str | None:
    if origin is None:
        return None
    return (origin + timedelta(hours=t)).strftime("%Y-%m-%d %H:%M")


def _day_of(t: float, origin: datetime | None) -> str | None:
    """Календарный день, в котором лежит момент t."""
    if origin is None:
        return None
    return (origin + timedelta(hours=t + _EPS)).strftime("%Y-%m-%d")


def _bucket_range(finance: dict[str, Any], until: float | None) -> float:
    end = float(finance.get("end_time") or 0.0)
    return end if until is None else min(float(until), end)


# --------------------------------------------------------------------- ОДДС --


def cash_flow_statement(
    finance: dict[str, Any],
    period: str | float = "month",
    until: float | None = None,
) -> dict[str, Any]:
    """Отчёт о движении денежных средств по периодам.

    ``period`` — ``hour | day | week | month | quarter | year | total`` или
    число часов. ``until`` — срез: отчёт с начала прогона до этого момента
    (часы прогона; точность — шаг хранения). Движения после конца прогона
    (отсрочки платежей) показаны отдельно как ожидаемые поступления и
    платежи.
    """
    bucket = float(finance.get("bucket_hours") or 24.0)
    origin = _origin(finance)
    end = _bucket_range(finance, until)
    opening = float(finance.get("opening_cash") or 0.0)
    periods: dict[Any, dict[str, Any]] = {}
    pending = {"inflow": 0.0, "outflow": 0.0, "articles": {}}
    for index, article, income, category, amount in finance.get("cash", []):
        t = index * bucket
        if t >= end - _EPS:
            if t >= float(finance.get("end_time") or 0.0) - _EPS:
                key = "inflow" if income else "outflow"
                pending[key] += amount
                pending["articles"][article] = pending["articles"].get(article, 0.0) + (
                    amount if income else -amount
                )
            continue
        key, label, start = _period_key(t, period, origin)
        slot = periods.setdefault(
            key,
            {
                "label": label, "start": start,
                "sections": {
                    c: {"inflow": {}, "outflow": {}} for c in CATEGORIES
                },
            },
        )
        section = slot["sections"].setdefault(category, {"inflow": {}, "outflow": {}})
        side = section["inflow" if income else "outflow"]
        side[article] = side.get(article, 0.0) + amount
    for article, income, _category, amount in finance.get("cash_after_horizon", []):
        pending["inflow" if income else "outflow"] += amount
        pending["articles"][article] = pending["articles"].get(article, 0.0) + (
            amount if income else -amount
        )
    rows = []
    balance = opening
    totals = {c: 0.0 for c in CATEGORIES}
    for key in sorted(periods, key=lambda k: periods[k]["start"]):
        slot = periods[key]
        row: dict[str, Any] = {
            "period": slot["label"],
            "start_hours": round(slot["start"], 4),
            "start_date": _date_of(slot["start"], origin),
            "opening_balance": round(balance, 2),
            "sections": {},
        }
        net_total = 0.0
        for category, section in slot["sections"].items():
            inflow = sum(section["inflow"].values())
            outflow = sum(section["outflow"].values())
            net = inflow - outflow
            net_total += net
            totals[category] = totals.get(category, 0.0) + net
            row["sections"][category] = {
                "name": CATEGORY_NAMES.get(category, category),
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "net": round(net, 2),
                "inflow_by_article": _rounded(section["inflow"]),
                "outflow_by_article": _rounded(section["outflow"]),
            }
        balance += net_total
        row["net_cash_flow"] = round(net_total, 2)
        row["closing_balance"] = round(balance, 2)
        rows.append(row)
    return {
        "period": period,
        "currency": finance.get("currency", ""),
        "start_date": finance.get("start_date"),
        "until_hours": round(end, 4),
        "until_date": _date_of(end, origin),
        "opening_balance": round(opening, 2),
        "closing_balance": round(balance, 2),
        "net_by_section": {c: round(v, 2) for c, v in totals.items()},
        "rows": rows,
        "after_horizon": {
            "inflow": round(pending["inflow"], 2),
            "outflow": round(pending["outflow"], 2),
            "by_article": _rounded(pending["articles"]),
            "note": "поступления и платежи с отсрочкой, срок которых — после конца прогона "
            "(дебиторская и кредиторская задолженность на конец)",
        },
        "precision_hours": bucket,
    }


def _rounded(values: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 2) for k, v in sorted(values.items()) if abs(v) > 0.005}


def cash_balance_series(finance: dict[str, Any], until: float | None = None) -> list[dict[str, Any]]:
    """Остаток денег на конец каждого шага хранения (по умолчанию — суток)."""
    bucket = float(finance.get("bucket_hours") or 24.0)
    origin = _origin(finance)
    end = _bucket_range(finance, until)
    n = int(math.ceil(end / bucket - _EPS)) if end > 0 else 0
    inflow = [0.0] * n
    outflow = [0.0] * n
    for index, _article, income, _category, amount in finance.get("cash", []):
        if 0 <= index < n:
            if income:
                inflow[index] += amount
            else:
                outflow[index] += amount
    balance = float(finance.get("opening_cash") or 0.0)
    out = []
    for i in range(n):
        balance += inflow[i] - outflow[i]
        t_end = min(end, (i + 1) * bucket)
        out.append(
            {
                "t": round(t_end, 4),
                "t_start": round(i * bucket, 4),
                # шаг хранения подписан днём, в котором он начался: «остаток
                # на конец 30.09», а не «на 00:00 01.10»
                "date": _day_of(i * bucket, origin),
                "inflow": round(inflow[i], 2),
                "outflow": round(outflow[i], 2),
                "balance": round(balance, 2),
            }
        )
    return out


def cash_gap_report(finance: dict[str, Any], threshold: float = 0.0) -> dict[str, Any]:
    """Кассовые разрывы: остаток ниже порога (по умолчанию — ниже нуля)."""
    series = cash_balance_series(finance)
    gaps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    minimum = None
    for point in series:
        if minimum is None or point["balance"] < minimum["balance"]:
            minimum = point
        below = point["balance"] < threshold - _EPS
        if below and current is None:
            current = {
                "from": point["t_start"], "from_date": point["date"], "depth": point["balance"],
            }
        if below and current is not None:
            current["depth"] = min(current["depth"], point["balance"])
            current["to"] = point["t"]
            current["to_date"] = point["date"]
        if not below and current is not None:
            gaps.append(current)
            current = None
    if current is not None:
        current["open_at_end"] = True
        gaps.append(current)
    bucket = float(finance.get("bucket_hours") or 24.0)
    for gap in gaps:
        gap["duration_hours"] = round(gap["to"] - gap["from"], 4)
        gap["depth"] = round(gap["depth"], 2)
    worst = min((g["depth"] for g in gaps), default=None)
    return {
        "threshold": threshold,
        "has_gap": bool(gaps),
        "gaps": gaps,
        "min_balance": None if minimum is None else minimum["balance"],
        "min_balance_at": None if minimum is None else minimum["t"],
        "min_balance_date": None if minimum is None else minimum["date"],
        "days_below": sum(1 for p in series if p["balance"] < threshold - _EPS) * bucket / 24.0,
        "required_reserve": round(max(0.0, threshold - worst), 2) if worst is not None else 0.0,
        "first_gap_at": gaps[0]["from"] if gaps else None,
        "first_gap_date": gaps[0]["from_date"] if gaps else None,
    }


def payment_calendar(
    finance: dict[str, Any], from_hours: float = 0.0, days: int = 30
) -> dict[str, Any]:
    """Платёжный календарь: поступления и платежи по дням и статьям."""
    bucket = float(finance.get("bucket_hours") or 24.0)
    origin = _origin(finance)
    to_hours = from_hours + 24.0 * days
    days_map: dict[int, dict[str, Any]] = {}
    for index, article, income, category, amount in finance.get("cash", []):
        t = index * bucket
        if t < from_hours - _EPS or t >= to_hours - _EPS:
            continue
        day = int((t + _EPS) // 24.0)
        slot = days_map.setdefault(day, {"inflow": {}, "outflow": {}})
        side = slot["inflow" if income else "outflow"]
        side[article] = side.get(article, 0.0) + amount
    opening = _balance_at(finance, from_hours)
    balance = opening
    rows = []
    for day in sorted(days_map):
        slot = days_map[day]
        inflow = sum(slot["inflow"].values())
        outflow = sum(slot["outflow"].values())
        balance += inflow - outflow
        rows.append(
            {
                "day_start_hours": day * 24.0,
                "date": _day_of(day * 24.0, origin),
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "inflow_by_article": _rounded(slot["inflow"]),
                "outflow_by_article": _rounded(slot["outflow"]),
                "balance": round(balance, 2),
            }
        )
    return {
        "from_hours": from_hours,
        "days": days,
        "opening_balance": round(opening, 2),
        "rows": rows,
        "closing_balance": round(balance, 2),
    }


def _balance_at(finance: dict[str, Any], t: float) -> float:
    bucket = float(finance.get("bucket_hours") or 24.0)
    balance = float(finance.get("opening_cash") or 0.0)
    for index, _article, income, _category, amount in finance.get("cash", []):
        if index * bucket < t - _EPS:
            balance += amount if income else -amount
    return balance


# --------------------------------------------------------------------- ОПиУ --


def income_statement(
    finance: dict[str, Any], period: str | float = "month", until: float | None = None
) -> dict[str, Any]:
    """Отчёт о прибылях и убытках по начислениям.

    Капитальные вложения (стройка, оборудование) не расходы периода: они
    показаны отдельно в ``capital_expenditure``.
    """
    bucket = float(finance.get("bucket_hours") or 24.0)
    origin = _origin(finance)
    end = _bucket_range(finance, until)
    periods: dict[Any, dict[str, Any]] = {}
    for index, article, income, capex, amount in finance.get("accrual", []):
        t = index * bucket
        if t >= end - _EPS:
            continue
        key, label, start = _period_key(t, period, origin)
        slot = periods.setdefault(
            key, {"label": label, "start": start, "revenue": {}, "expense": {}, "capex": {}}
        )
        target = slot["capex"] if capex else slot["revenue" if income else "expense"]
        target[article] = target.get(article, 0.0) + amount
    rows = []
    totals = {"revenue": 0.0, "expense": 0.0, "capex": 0.0}
    for key in sorted(periods, key=lambda k: periods[k]["start"]):
        slot = periods[key]
        revenue = sum(slot["revenue"].values())
        expense = sum(slot["expense"].values())
        capex_total = sum(slot["capex"].values())
        totals["revenue"] += revenue
        totals["expense"] += expense
        totals["capex"] += capex_total
        rows.append(
            {
                "period": slot["label"],
                "start_hours": round(slot["start"], 4),
                "revenue": round(revenue, 2),
                "expenses": round(expense, 2),
                "profit": round(revenue - expense, 2),
                "margin": round((revenue - expense) / revenue, 4) if revenue > _EPS else None,
                "revenue_by_article": _rounded(slot["revenue"]),
                "expenses_by_article": _rounded(slot["expense"]),
                "capital_expenditure": round(capex_total, 2),
                "capital_by_article": _rounded(slot["capex"]),
            }
        )
    profit = totals["revenue"] - totals["expense"]
    return {
        "period": period,
        "currency": finance.get("currency", ""),
        "until_hours": round(end, 4),
        "rows": rows,
        "totals": {
            "revenue": round(totals["revenue"], 2),
            "expenses": round(totals["expense"], 2),
            "profit": round(profit, 2),
            "margin": round(profit / totals["revenue"], 4) if totals["revenue"] > _EPS else None,
            "capital_expenditure": round(totals["capex"], 2),
        },
    }


# ---------------------------------------------------------------- план-факт --


def variance_report(
    plan: dict[str, Any], fact: dict[str, Any], period: str | float = "month"
) -> dict[str, Any]:
    """План-факт по деньгам: два прогона (план и прогноз/факт) по статьям.

    Периоды сопоставляются по календарным датам, если у обоих прогонов есть
    дата начала, иначе — по часам прогона.
    """
    def by_period(finance: dict[str, Any]) -> dict[str, dict[str, float]]:
        statement = cash_flow_statement(finance, period)
        out: dict[str, dict[str, float]] = {}
        for row in statement["rows"]:
            slot = out.setdefault(row["period"], {})
            for section in row["sections"].values():
                for article, amount in section["inflow_by_article"].items():
                    slot[article] = slot.get(article, 0.0) + amount
                for article, amount in section["outflow_by_article"].items():
                    slot[article] = slot.get(article, 0.0) - amount
        return out

    plan_rows = by_period(plan)
    fact_rows = by_period(fact)
    labels = list(dict.fromkeys([*plan_rows, *fact_rows]))
    rows = []
    for label in labels:
        p = plan_rows.get(label, {})
        f = fact_rows.get(label, {})
        articles = sorted(set(p) | set(f))
        rows.append(
            {
                "period": label,
                "articles": [
                    {
                        "article": a,
                        "plan": round(p.get(a, 0.0), 2),
                        "fact": round(f.get(a, 0.0), 2),
                        "variance": round(f.get(a, 0.0) - p.get(a, 0.0), 2),
                    }
                    for a in articles
                ],
                "plan_net": round(sum(p.values()), 2),
                "fact_net": round(sum(f.values()), 2),
                "variance_net": round(sum(f.values()) - sum(p.values()), 2),
            }
        )
    return {"period": period, "rows": rows}


# ------------------------------------------------------------ онлайн-панель --


def dashboard(result: dict[str, Any], at: float | None = None, window_days: float = 30.0) -> dict[str, Any]:
    """Онлайн-панель руководителя на момент ``at`` (по умолчанию — конец прогона).

    Показатели, которые обычно видны раз в месяц, здесь доступны на любой
    час прогона: остаток денег, скорость их расходования (burn rate), запас
    в днях (runway), выручка и маржа за скользящее окно, индексы проектов и
    активные сигналы мониторинга.
    """
    finance = result.get("finance") or {}
    end = float(finance.get("end_time") or 0.0)
    t = end if at is None else min(float(at), end)
    window = 24.0 * window_days
    t0 = max(0.0, t - window)
    bucket = float(finance.get("bucket_hours") or 24.0)
    balance = (
        float(finance.get("closing_cash"))
        if at is None and finance.get("closing_cash") is not None
        else _balance_at(finance, t)
    )
    inflow = outflow = 0.0
    for index, _article, income, _category, amount in finance.get("cash", []):
        if t0 - _EPS <= index * bucket < t - _EPS:
            if income:
                inflow += amount
            else:
                outflow += amount
    revenue = expense = capex = 0.0
    for index, _article, income, is_capex, amount in finance.get("accrual", []):
        if t0 - _EPS <= index * bucket < t - _EPS:
            if is_capex:
                capex += amount
            elif income:
                revenue += amount
            else:
                expense += amount
    span_days = max((t - t0) / 24.0, _EPS)
    net_per_day = (inflow - outflow) / span_days
    burn = -net_per_day if net_per_day < 0 else 0.0
    gap = cash_gap_report(finance)
    projects = {}
    for pid, data in (result.get("projects") or {}).items():
        indicators = _project_at(data, t)
        projects[pid] = {
            "name": data.get("name"),
            **indicators,
            "finished_at": data.get("finished_at"),
            "planned_finish": data.get("planned_finish"),
        }
    monitoring = result.get("monitoring") or {}
    active = _active_alerts(monitoring.get("alerts") or [], t)
    origin = _origin(finance)
    return {
        "at_hours": round(t, 4),
        "at_date": _date_of(t, origin),
        "window_days": window_days,
        "cash": {
            "balance": round(balance, 2),
            "inflow_window": round(inflow, 2),
            "outflow_window": round(outflow, 2),
            "net_per_day": round(net_per_day, 2),
            "burn_rate_per_day": round(burn, 2),
            "runway_days": round(balance / burn, 1) if burn > _EPS and balance > 0 else None,
            "min_balance": gap["min_balance"],
            "min_balance_date": gap["min_balance_date"],
            "has_gap": gap["has_gap"],
        },
        "profit_and_loss_window": {
            "revenue": round(revenue, 2),
            "expenses": round(expense, 2),
            "profit": round(revenue - expense, 2),
            "margin": round((revenue - expense) / revenue, 4) if revenue > _EPS else None,
            "revenue_run_rate_month": round(revenue / span_days * 30.4, 2),
            "capital_expenditure": round(capex, 2),
        },
        "projects": projects,
        "alerts_active": active,
        "alerts_total": sum(1 for a in monitoring.get("alerts") or [] if a.get("state") == "raised"),
    }


def _project_at(data: dict[str, Any], t: float) -> dict[str, Any]:
    """PV/EV/AC проекта в момент t прогона по сохранённой серии."""
    series = data.get("series") or {}
    times = series.get("t") or []
    offset = float(data.get("time_offset") or 0.0)
    target = t + offset
    if not times:
        return {}
    i = 0
    while i + 1 < len(times) and times[i + 1] <= target + _EPS:
        i += 1
    pv, ev, ac = series["pv"][i], series["ev"][i], series["ac"][i]
    final = data.get("final") or {}
    bac = final.get("bac") or 0.0
    return {
        "t_project": times[i],
        "pv": pv, "ev": ev, "ac": ac, "bac": bac,
        "spi": round(ev / pv, 4) if pv > _EPS else None,
        "cpi": round(ev / ac, 4) if ac > _EPS else None,
        "percent_complete": round(ev / bac, 4) if bac > _EPS else None,
        "eac": round(bac * ac / ev, 2) if ev > _EPS else None,
    }


def _active_alerts(alerts: list[dict[str, Any]], t: float) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        if alert["t"] > t + _EPS:
            break
        if alert["state"] == "raised":
            active[alert["rule"]] = alert
        else:
            active.pop(alert["rule"], None)
    return sorted(active.values(), key=lambda a: a["t"])


def reporting_lag(
    result: dict[str, Any], period: str = "month", delay_days: float = 10.0
) -> dict[str, Any]:
    """На сколько позже о сигналах узнали бы из периодической отчётности.

    Для каждого сигнала мониторинга: когда он возник (ежедневная проверка) и
    когда то же отклонение попало бы в отчёт за период (конец периода плюс
    срок подготовки отчёта). Разница — время, потерянное для решения.
    """
    finance = result.get("finance") or {}
    origin = _origin(finance)
    end = float(finance.get("end_time") or 0.0)
    alerts = [
        a for a in ((result.get("monitoring") or {}).get("alerts") or [])
        if a.get("state") == "raised"
    ]
    rows = []
    for alert in alerts:
        t = float(alert["t"])
        _key, label, start = _period_key(t, period, origin)
        period_end = _period_end(start, period, origin)
        available = period_end + 24.0 * delay_days
        lag_days = (available - t) / 24.0
        rows.append(
            {
                "rule": alert["rule"],
                "name": alert.get("name"),
                "severity": alert.get("severity"),
                "raised_at": t,
                "raised_date": _day_of(t, origin),
                "report_period": label,
                "report_available_at": round(available, 4),
                "report_available_date": _day_of(available, origin),
                "lag_days": round(lag_days, 1),
                "report_after_horizon": available > end + _EPS,
            }
        )
    lags = [r["lag_days"] for r in rows]
    return {
        "period": period,
        "delay_days": delay_days,
        "alerts": rows,
        "mean_lag_days": round(sum(lags) / len(lags), 1) if lags else None,
        "max_lag_days": max(lags) if lags else None,
    }


def _period_end(start: float, period: str | float, origin: datetime | None) -> float:
    """Конец периода, начавшегося в момент start (часы прогона)."""
    if isinstance(period, (int, float)):
        return start + float(period)
    if origin is None or period in ("hour", "day", "week") or period not in PERIOD_HOURS:
        return start + PERIOD_HOURS.get(str(period), 730.0)
    begin = origin + timedelta(hours=start + _EPS)
    if period == "month":
        nxt = datetime(begin.year + (begin.month == 12), begin.month % 12 + 1, 1)
    elif period == "quarter":
        month = ((begin.month - 1) // 3) * 3 + 4
        nxt = datetime(begin.year + (month > 12), (month - 1) % 12 + 1, 1)
    else:  # year
        nxt = datetime(begin.year + 1, 1, 1)
    return (nxt - origin).total_seconds() / 3600.0


#: Какие ещё отчёты стоит выводить и отслеживать — справка для интерфейса и
#: документации (что показывает и как часто обновлять).
RECOMMENDED_REPORTS = [
    {
        "id": "cash_flow", "name": "Отчёт о движении денежных средств (ОДДС)",
        "frequency": "ежедневно", "purpose": "откуда пришли и куда ушли деньги по видам деятельности",
    },
    {
        "id": "payment_calendar", "name": "Платёжный календарь",
        "frequency": "ежедневно, горизонт 30–90 дней",
        "purpose": "какие поступления и платежи ожидаются по дням, хватит ли денег",
    },
    {
        "id": "cash_gap", "name": "Кассовые разрывы",
        "frequency": "при каждом пересчёте прогноза",
        "purpose": "когда остаток уйдёт в минус, насколько и какой нужен резерв",
    },
    {
        "id": "income", "name": "Отчёт о прибылях и убытках (ОПиУ)",
        "frequency": "еженедельно", "purpose": "прибыльность по начислениям без капвложений",
    },
    {
        "id": "evm", "name": "Освоенный объём (EVM)",
        "frequency": "ежедневно для стройки",
        "purpose": "CPI, SPI, прогноз стоимости и срока завершения проекта",
    },
    {
        "id": "variance", "name": "План-факт", "frequency": "еженедельно",
        "purpose": "отклонения прогноза от бюджета по статьям и периодам",
    },
    {
        "id": "dashboard", "name": "Панель показателей онлайн",
        "frequency": "непрерывно", "purpose": "остаток, burn rate, запас в днях, выручка, маржа, сигналы",
    },
]
