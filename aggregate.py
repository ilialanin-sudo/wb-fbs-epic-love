#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свод состояния FBS по бренду в один JSON для страницы.

Ничего не запрашивает у WB — работает только с тем, что собрал collect.py.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
MSK = timezone(timedelta(hours=3))

# supplierStatus — что происходит на стороне продавца
SUPPLIER_RU = {
    "new": "новое",
    "confirm": "на сборке",
    "complete": "собрано",
    "cancel": "отменено продавцом",
    "cancel_client": "отменено покупателем",
    "declined_by_client": "отказ до сборки",
}
# wbStatus — что происходит на стороне Wildberries
WB_RU = {
    "waiting": "у продавца",
    "sorted": "отсортирован",
    "sold": "получен покупателем",
    "canceled": "отменён",
    "canceled_by_client": "отменён покупателем",
    "declined_by_client": "отказ покупателя",
    "defect": "брак",
    "ready_for_pickup": "в ПВЗ, ждёт покупателя",
    "canceled_by_bank": "отменён банком",
}
CANCELLED = {"canceled", "canceled_by_client", "declined_by_client", "defect", "canceled_by_bank"}


def path(*p):
    return os.path.join(HERE, *p)


def load(name, default=None):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default if default is not None else {}


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def supply_state(s):
    """Черновик → закрыта и едет → принята. Отклонённая поставка отдельно."""
    if not s:
        return "нет поставки", "none"
    if s.get("rejectDt"):
        return "отклонена", "bad"
    if s.get("scanDt"):
        return "принята WB", "done"
    if s.get("closedAt") or s.get("done"):
        return "отгружается", "way"
    return "черновик", "draft"


def stage_of(rec, sup):
    """Одна понятная стадия заказа вместо двух технических статусов."""
    ss = rec.get("supplierStatus")
    ws = rec.get("wbStatus")
    if ws in CANCELLED or ss in ("cancel", "cancel_client", "declined_by_client"):
        return "отменён", "cancel"
    if ws == "sold":
        return "выкуплен", "sold"
    if ws == "ready_for_pickup":
        return "ждёт в ПВЗ", "pickup"
    if sup and sup.get("scanDt"):
        return "принят WB", "accepted"
    if sup and (sup.get("closedAt") or sup.get("done")):
        return "в пути на склад WB", "way"
    if rec.get("supplyId"):
        return "в поставке", "supply"
    if ss == "confirm":
        return "на сборке", "assembly"
    return "в очереди сборки", "queue"


def main():
    cfg = load("config.json")
    orders = load("state/orders.json")
    supplies = load("state/supplies.json")
    cards = load("state/cards.json")
    whs = load("state/warehouses.json")
    stocks = load("state/stocks.json")
    cursor = load("state/cursor.json")

    now = datetime.now(timezone.utc)
    deadline_h = float(cfg.get("assembly_deadline_h", 48))
    warn_h = float(cfg.get("warn_hours", 12))
    wh_over = {str(k): float(v) for k, v in (cfg.get("warehouse_deadline_h") or {}).items()}

    rows = []
    for key, rec in orders.items():
        created = parse(rec.get("createdAt"))
        sup = supplies.get(rec.get("supplyId") or "")
        stage, stage_key = stage_of(rec, sup)
        wh = whs.get(str(rec.get("warehouseId")), {})
        dl_h = wh_over.get(str(rec.get("warehouseId")), deadline_h)
        deadline = created + timedelta(hours=dl_h) if created else None
        pending = stage_key in ("queue", "assembly")
        left_h = (deadline - now).total_seconds() / 3600 if deadline else None
        # сколько заказ реально пролежал до попадания в поставку
        done_at = parse(rec.get("leftQueueAt")) or parse((sup or {}).get("closedAt"))
        spent_h = None
        if created and not pending:
            end = done_at if (done_at and done_at > created) else None
            if end:
                spent_h = (end - created).total_seconds() / 3600
        st, st_key = supply_state(sup)
        card = cards.get(str(rec.get("nmId")), {})
        rows.append({
            "id": rec.get("id"),
            "article": rec.get("article"),
            "nmId": rec.get("nmId"),
            "sku": (rec.get("skus") or [None])[0],
            "createdAt": rec.get("createdAt"),
            "deadlineAt": iso(deadline),
            "deadlineH": dl_h,
            "leftH": round(left_h, 2) if left_h is not None else None,
            "spentH": round(spent_h, 2) if spent_h is not None else None,
            "pending": pending,
            "overdue": bool(pending and left_h is not None and left_h < 0),
            "soon": bool(pending and left_h is not None and 0 <= left_h <= warn_h),
            "price": (rec.get("price") or 0) / 100 if rec.get("price") else None,
            "salePrice": (rec.get("salePrice") or 0) / 100 if rec.get("salePrice") else None,
            "warehouseId": rec.get("warehouseId"),
            "warehouse": wh.get("name") or f"склад {rec.get('warehouseId')}",
            "office": ", ".join(rec.get("offices") or []),
            "supplyId": rec.get("supplyId"),
            "supplyName": (sup or {}).get("name"),
            "supplyState": st,
            "supplyStateKey": st_key,
            "supplyClosedAt": (sup or {}).get("closedAt"),
            "supplyScanDt": (sup or {}).get("scanDt"),
            "supplierStatus": rec.get("supplierStatus"),
            "supplierStatusRu": SUPPLIER_RU.get(rec.get("supplierStatus"), rec.get("supplierStatus") or "—"),
            "wbStatus": rec.get("wbStatus"),
            "wbStatusRu": WB_RU.get(rec.get("wbStatus"), rec.get("wbStatus") or "—"),
            "stage": stage,
            "stageKey": stage_key,
            "title": card.get("title"),
            "subject": card.get("subject"),
            "photo": card.get("photo"),
            "statusAt": rec.get("statusAt"),
        })
    rows.sort(key=lambda r: (not r["pending"], r["leftH"] if r["leftH"] is not None else 1e9))

    # ---------------------------------------------------------- по артикулам
    agg = defaultdict(lambda: defaultdict(int))
    meta_by_art = {}
    for r in rows:
        a = r["article"] or "—"
        g = agg[a]
        meta_by_art.setdefault(a, {"nmId": r["nmId"], "title": r["title"],
                                   "subject": r["subject"], "photo": r["photo"]})
        g["всего"] += 1
        g[r["stageKey"]] += 1
        if r["overdue"]:
            g["overdue"] += 1
        if r["soon"]:
            g["soon"] += 1
        created = parse(r["createdAt"])
        if created:
            age = (now - created).total_seconds() / 86400
            if age <= 1:
                g["d1"] += 1
            if age <= 7:
                g["d7"] += 1
            if age <= 30:
                g["d30"] += 1
        if r["stageKey"] == "sold" and r["price"]:
            g["выручка"] += r["price"]

    articles = []
    for a, g in agg.items():
        m = meta_by_art[a]
        articles.append({
            "article": a, "nmId": m["nmId"], "title": m["title"],
            "subject": m["subject"], "photo": m["photo"],
            "total": g["всего"], "queue": g["queue"] + g["assembly"],
            "overdue": g["overdue"], "soon": g["soon"],
            "supply": g["supply"] + g["way"], "accepted": g["accepted"],
            "sold": g["sold"], "pickup": g["pickup"], "cancel": g["cancel"],
            "d1": g["d1"], "d7": g["d7"], "d30": g["d30"], "revenue": round(g["выручка"], 2),
        })
    articles.sort(key=lambda x: (-x["overdue"], -x["queue"], -x["d7"], x["article"]))

    # ------------------------------------------------------------- поставки
    sup_rows = []
    per_sup = defaultdict(list)
    for r in rows:
        if r["supplyId"]:
            per_sup[r["supplyId"]].append(r)
    for sid, items in per_sup.items():
        s = supplies.get(sid, {})
        st, st_key = supply_state(s)
        sup_rows.append({
            "id": sid, "name": s.get("name"), "createdAt": s.get("createdAt"),
            "closedAt": s.get("closedAt"), "scanDt": s.get("scanDt"),
            "rejectDt": s.get("rejectDt"), "rejectReason": s.get("rejectReason"),
            "state": st, "stateKey": st_key, "officeId": s.get("destinationOfficeId"),
            "orders": len(items),
            "articles": sorted({i["article"] for i in items if i["article"]}),
            "sum": round(sum(i["price"] or 0 for i in items), 2),
        })
    sup_rows.sort(key=lambda x: x["createdAt"] or "", reverse=True)

    # ---------------------------------------------------------- дни и итоги
    daily = defaultdict(lambda: {"orders": 0, "sold": 0, "cancel": 0})
    for r in rows:
        c = parse(r["createdAt"])
        if not c:
            continue
        d = c.astimezone(MSK).date().isoformat()
        daily[d]["orders"] += 1
        if r["stageKey"] == "sold":
            daily[d]["sold"] += 1
        if r["stageKey"] == "cancel":
            daily[d]["cancel"] += 1
    days = [{"date": d, **v} for d, v in sorted(daily.items())][-30:]

    pending = [r for r in rows if r["pending"]]
    overdue = [r for r in pending if r["overdue"]]
    soon = [r for r in pending if r["soon"]]
    in_supply = [r for r in rows if r["stageKey"] in ("supply", "way")]
    accepted = [r for r in rows if r["stageKey"] == "accepted"]

    oldest = min((r["leftH"] for r in pending if r["leftH"] is not None), default=None)
    spent = [r["spentH"] for r in rows if r["spentH"] is not None and not r["pending"]]
    spent.sort()

    # -------------------------------------------------------------- остатки
    CARGO = {1: "МГТ", 2: "СГТ", 3: "КГТ"}
    stock_rows, stock_by_art = [], defaultdict(int)
    for wh, v in stocks.items():
        if not v.get("total"):
            continue
        w = whs.get(str(wh), {})
        sizes = defaultdict(int)
        for r in v.get("rows", []):
            sizes[(r.get("article"), r.get("techSize"))] += r.get("amount", 0)
            stock_by_art[r.get("article")] += r.get("amount", 0)
        stock_rows.append({
            "warehouseId": wh, "warehouse": w.get("name") or f"склад {wh}",
            "cargo": CARGO.get(w.get("cargoType"), ""), "total": v.get("total"),
            "at": v.get("at"),
            "items": [{"article": a, "techSize": t, "amount": n}
                      for (a, t), n in sorted(sizes.items(), key=lambda x: -x[1])],
        })
    stock_rows.sort(key=lambda x: -x["total"])
    for a in articles:
        a["stock"] = stock_by_art.get(a["article"], 0)
    # артикулы бренда без единого заказа тоже должны быть видны — по остатку и карточке
    known = {a["article"] for a in articles}
    for nm, c in cards.items():
        vc = c.get("vendorCode")
        if not vc or vc in known:
            continue
        articles.append({"article": vc, "nmId": int(nm), "title": c.get("title"),
                         "subject": c.get("subject"), "photo": c.get("photo"),
                         "total": 0, "queue": 0, "overdue": 0, "soon": 0, "supply": 0,
                         "accepted": 0, "sold": 0, "pickup": 0, "cancel": 0,
                         "d1": 0, "d7": 0, "d30": 0, "revenue": 0,
                         "stock": stock_by_art.get(vc, 0)})
    articles.sort(key=lambda x: (-x["overdue"], -x["queue"], -x["d7"], -x["stock"], x["article"]))

    data = {
        "meta": {
            "brand": cfg.get("brand"),
            "generatedAt": iso(now),
            "deadlineH": deadline_h,
            "warnH": warn_h,
            "cabinetQueue": cursor.get("queue_total_cabinet"),
            "caughtUp": cursor.get("orders_caught_up"),
            "lastRun": cursor.get("last_run"),
            "cards": len(cards),
            "tracked": len(rows),
            "articles": len(articles),
            "stockTotal": sum(r["total"] for r in stock_rows),
            "stockWarehouses": len(stock_rows),
            "stockAt": max([r["at"] for r in stock_rows], default=None),
        },
        "kpi": {
            "queue": len(pending),
            "overdue": len(overdue),
            "soon": len(soon),
            "inSupply": len(in_supply),
            "accepted": len(accepted),
            "sold": sum(1 for r in rows if r["stageKey"] == "sold"),
            "cancel": sum(1 for r in rows if r["stageKey"] == "cancel"),
            "d1": sum(1 for r in rows if parse(r["createdAt"]) and (now - parse(r["createdAt"])).days < 1),
            "d7": sum(1 for r in rows if parse(r["createdAt"]) and (now - parse(r["createdAt"])).days < 7),
            "tightestH": round(oldest, 2) if oldest is not None else None,
            "medianAssemblyH": round(spent[len(spent) // 2], 2) if spent else None,
        },
        "orders": rows,
        "articles": articles,
        "supplies": sup_rows,
        "stocks": stock_rows,
        "days": days,
    }
    os.makedirs(path("data"), exist_ok=True)
    with open(path("data", "dashboard_data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    k = data["kpi"]
    print(f"свод: заказов {len(rows)}, артикулов {len(articles)}, поставок {len(sup_rows)}; "
          f"в сборке {k['queue']} (просрочено {k['overdue']}, горит {k['soon']}); "
          f"остаток {data['meta']['stockTotal']} шт на {data['meta']['stockWarehouses']} складах",
          flush=True)


if __name__ == "__main__":
    main()
