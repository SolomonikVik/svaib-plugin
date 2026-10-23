#!/usr/bin/env python3
"""calculator.py — производные из прочитанных значений. Считает код, не модель.

Канон вертикали (architecture.md, «Инструменты»): любое число проходит либо через
extractor, либо через calculator. Чтение отдаёт ряды, этот скрипт — производные от них:

    python3 read_metrics.py --book <книга>.xlsx --card <карта>.json --json > values.json
    python3 calculator.py --values values.json [--period 2026-07] [--json]

Что умеет: выполнение плана, отклонение факта от плана, рост к прошлому году,
изменение к предыдущему периоду. Больше ничего — интерпретация не его работа.

Чего не делает никогда:
  * не складывает и не усредняет проценты — процент считается только от абсолютных
    величин, иначе получается число, похожее на правду (на книге клиента такой
    агрегат разошёлся с абсолютным финрезультатом на 15%);
  * не достраивает отсутствующий период и не берёт соседний вместо пропущенного;
  * не сравнивает величины в разных единицах.
"""
from __future__ import annotations

import argparse, copy, json, sys
from pathlib import Path

PERCENT_UNITS = {"%", "percent", "процент", "проценты"}


def is_percent(row):
    return str(row.get("unit") or "").strip().lower() in PERCENT_UNITS


def calculable(row):
    return row.get("status") != "refused" and bool(row.get("values")) and not row.get("companion")


def same_unit(left, right):
    if is_percent(left) and is_percent(right):
        return True
    return str(left.get("unit") or "").strip().lower() == str(right.get("unit") or "").strip().lower()


def prev_period(period):
    """Предыдущий период той же гранулярности: 2026-07 → 2026-06."""
    if "-W" in period or "-Q" in period:
        return None
    try:
        y, m = period.split("-")
        y, m = int(y), int(m)
    except ValueError:
        return None
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def year_ago(period):
    try:
        y, m = period.split("-")
        return f"{int(y) - 1}-{m}"
    except ValueError:
        return None


def derive(rows, period=None):
    """Производные по каждой метрике за выбранный период (по умолчанию — последний с фактом)."""
    out = []
    for row in rows:
        if not calculable(row):
            continue
        percent = is_percent(row)
        vals = row["values"]
        p = period or max(vals)
        res = {"metric": row["metric"], "unit": row.get("unit"), "period": p, "derived": {}, "notes": []}
        if p not in vals:
            res["notes"].append(f"за {p} значения нет — производные не считаются")
            out.append(res); continue
        fact = vals[p]
        res["fact"] = fact

        plan = (row.get("plan") or {}).get(p)
        if percent and plan is not None:
            res["plan"] = plan
            res["derived"]["отклонение от плана, п.п."] = round(fact - plan, 2)
        elif plan == 0:
            res["notes"].append("план равен нулю — выполнение не считается")
        elif plan is not None:
            res["derived"]["выполнение плана, %"] = round(fact / plan * 100, 1)
            res["derived"]["отклонение от плана"] = round(fact - plan, 2)
            res["plan"] = plan

        prev = prev_period(p)
        if prev and prev in vals:
            key = "изменение к прошлому периоду, п.п." if percent else "изменение к прошлому периоду"
            res["derived"][key] = round(fact - vals[prev], 2)
            if not percent and vals[prev]:
                res["derived"]["изменение к прошлому периоду, %"] = round(
                    (fact - vals[prev]) / abs(vals[prev]) * 100, 1)

        # Прошлый год: либо в этом же ряду, либо отдельной метрикой-компаньоном.
        # Полное служебное имя связывает ряд с одной метрикой. Подстрока может
        # связать MRR с Новым MRR. Дубли не сворачиваем: это неоднозначность.
        # Не нашли — говорим об этом вслух: молчаливо
        # пропущенная производная выглядит как «роста нет», а не «не с чем сравнить».
        ya = year_ago(p)
        base = vals.get(ya)
        if base is None:
            candidates = [other for other in rows
                          if other is not row and other.get("status") != "refused"
                          and other.get("companion") and same_unit(row, other)
                          and other["metric"] == row["metric"] + " год назад"
                          and ya in (other.get("values") or {})]
            if len(candidates) == 1:
                base = candidates[0]["values"][ya]
            elif len(candidates) > 1:
                res["notes"].append(f"неоднозначный ряд прошлого года: {len(candidates)} записей "
                                    f"«{row['metric']} год назад» за {ya}")
        if percent and base is not None:
            res["year_ago"] = base
            res["derived"]["изменение к прошлому году, п.п."] = round(fact - base, 2)
        elif base in (None, 0):
            res["notes"].append(
                f"рост к прошлому году не посчитан: нет значения за {ya}. "
                f"Нужен ряд прошлого года — в той же метрике или отдельной записью карты "
                f"с year_offset: -1 и точным именем «{row['metric']} год назад»")
        else:
            res["derived"]["рост к прошлому году, %"] = round((fact - base) / abs(base) * 100, 1)
            res["year_ago"] = base

        if percent:
            res["notes"].append("метрика в процентах: разница показана в пунктах, "
                                "агрегаты и средние по ней не считаются")
        for n in row.get("notes", []):
            res["notes"].append(n)
        out.append(res)
    return out


def build_report(rows, period=None):
    """Исходные строки плюс расчёты: отказ, план без факта и пометки не исчезают."""
    calculations = iter(derive(rows, period))
    report = copy.deepcopy(rows)
    for source, result in zip(rows, report):
        if calculable(source):
            result["calculation"] = next(calculations)
    return report


def render(items):
    lines = []
    for it in items:
        head = f"**{it['metric']}**" + (f" ({it['unit']})" if it.get("unit") else "") + f" — {it['period']}"
        lines.append(head)
        if "fact" in it:
            row = [f"факт {it['fact']}"]
            if "plan" in it: row.append(f"план {it['plan']}")
            if "year_ago" in it: row.append(f"год назад {it['year_ago']}")
            lines.append("  " + " · ".join(row))
        for k, v in it["derived"].items():
            lines.append(f"  {k}: {v}")
        for n in it["notes"]:
            lines.append(f"  — {n}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--values", required=True, help="выход read_metrics.py --json")
    ap.add_argument("--period", help="период, за который считать (YYYY-MM); по умолчанию последний с фактом")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--complete", action="store_true", help="полный результат чтения с расчётами, JSON")
    a = ap.parse_args()
    rows = json.loads(Path(a.values).read_text(encoding="utf-8"))
    if a.complete:
        print(json.dumps(build_report(rows, a.period), ensure_ascii=False, indent=2))
        return 0
    items = derive(rows, a.period)
    print(json.dumps(items, ensure_ascii=False, indent=2) if a.json else render(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
