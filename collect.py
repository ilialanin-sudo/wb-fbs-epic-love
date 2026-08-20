#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка FBS по одному бренду из большого мультибрендового кабинета WB.

Кабинет отдаёт десятки тысяч заказов в сутки, а фильтра по бренду в API нет,
поэтому сплошной перебор истории каждый час невозможен — упрёмся в лимиты.
Схема другая:

  1. /api/v3/orders/new           — одним запросом вся очередь сборки кабинета,
                                    фильтруем по префиксу артикула;
  2. /api/v3/orders (курсор)      — инкремент с сохранённой позиции, не более
                                    max_pages_per_run страниц за прогон; отсюда
                                    берётся supplyId и заказы, которые успели
                                    уехать в поставку между прогонами;
  3. /api/v3/orders/status        — статусы всех отслеживаемых заказов;
  4. /api/v3/supplies/{id}        — состояние поставок, в которых лежат заказы;
  5. /api/v3/stocks/{warehouse}   — остатки FBS бренда по складам продавца;
  6. content-api                  — карточки бренда: фото, предмет, штрихкоды.

Состояние живёт в state/ и коммитится в репозиторий, поэтому каждый следующий
прогон продолжает с того места, где остановился предыдущий.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

from wb_client import WBClient

HERE = os.path.dirname(os.path.abspath(__file__))
MP = "marketplace-api.wildberries.ru"
CONTENT = "content-api.wildberries.ru"

FINAL_SUPPLIER = {"cancel", "cancel_client", "declined_by_client"}
FINAL_WB = {"sold", "canceled", "canceled_by_client", "declined_by_client", "defect"}


def path(*p):
    return os.path.join(HERE, *p)


def load(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save(name, obj):
    os.makedirs(os.path.dirname(path(name)), exist_ok=True)
    with open(path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class Collector:
    def __init__(self):
        self.cfg = load("config.json", {})
        token = (os.environ.get("WB_TOKEN") or "").strip()
        if not token:
            env = path(".env")
            if os.path.exists(env):
                for line in open(env, encoding="utf-8"):
                    if line.strip().startswith("WB_TOKEN="):
                        token = line.split("=", 1)[1].strip()
        if not token:
            raise SystemExit("нет токена: положите WB_TOKEN в окружение или в .env рядом")
        self.wb = WBClient(token, min_interval=1.7)
        self.prefixes = [p.lower() for p in self.cfg.get("article_prefixes", [])]
        self.orders = load("state/orders.json", {})
        self.supplies = load("state/supplies.json", {})
        self.cards = load("state/cards.json", {})
        self.cursor = load("state/cursor.json", {})
        self.warehouses = load("state/warehouses.json", {})
        self.stocks = load("state/stocks.json", {})
        self.stats = {"queue_total": 0, "pages": 0, "scanned": 0, "new_hits": 0}

    # ------------------------------------------------------------- фильтр
    def mine(self, order):
        art = (order.get("article") or "").lower()
        if any(art.startswith(p) or p in art for p in self.prefixes):
            return True
        return str(order.get("nmId")) in self.cards

    def upsert(self, order, in_queue=False):
        key = str(order["id"])
        rec = self.orders.get(key, {})
        keep = {k: order.get(k) for k in (
            "id", "article", "nmId", "chrtId", "skus", "createdAt", "warehouseId",
            "officeId", "offices", "price", "salePrice", "convertedPrice",
            "rid", "orderUid", "cargoType", "isZeroOrder", "supplyId")}
        # supplyId приходит только из /api/v3/orders — не затираем его пустотой
        if keep.get("supplyId") is None and rec.get("supplyId"):
            keep["supplyId"] = rec["supplyId"]
        rec.update({k: v for k, v in keep.items() if v is not None})
        rec.setdefault("firstSeen", now_iso())
        rec["inQueue"] = in_queue if in_queue else rec.get("inQueue", False)
        if in_queue:
            rec["queueSeen"] = now_iso()
        self.orders[key] = rec
        return rec

    # ------------------------------------------------- 1. очередь сборки
    def collect_queue(self):
        d = self.wb.get(MP, "/api/v3/orders/new") or {}
        allq = d.get("orders", [])
        self.stats["queue_total"] = len(allq)
        live = set()
        for o in allq:
            if self.mine(o):
                self.upsert(o, in_queue=True)
                live.add(str(o["id"]))
                self.stats["new_hits"] += 1
        # заказ, пропавший из очереди, уехал в поставку
        for key, rec in self.orders.items():
            if rec.get("inQueue") and key not in live:
                rec["inQueue"] = False
                rec.setdefault("leftQueueAt", now_iso())
        print(f"очередь кабинета: {len(allq)}, из них бренда: {len(live)}", flush=True)

    # ------------------------------- 2. инкремент по всем заказам кабинета
    def collect_incremental(self):
        cur = self.cursor.get("orders_next")
        date_from = self.cursor.get("orders_dateFrom")
        if cur is None or date_from is None:
            days = int(self.cfg.get("backfill_days", 3))
            date_from = int(time.time()) - days * 86400
            cur = 0
            print(f"первый прогон: догоняю историю за {days} дн.", flush=True)
        limit = int(os.environ.get("WB_MAX_PAGES") or self.cfg.get("max_pages_per_run", 40))
        hits = 0
        done = False
        for _ in range(limit):
            try:
                d = self.wb.get(MP, "/api/v3/orders",
                                {"limit": 1000, "next": cur, "dateFrom": date_from})
            except RuntimeError as e:
                # лимит не отпускает — останавливаемся на последней удачной странице,
                # курсор уже сохранён, следующий прогон продолжит отсюда
                print(f"инкремент прерван: {e}", flush=True)
                break
            page = (d or {}).get("orders", [])
            cur = (d or {}).get("next", cur)
            self.stats["pages"] += 1
            self.stats["scanned"] += len(page)
            for o in page:
                if self.mine(o):
                    self.upsert(o)
                    hits += 1
            if len(page) < 1000:
                done = True
                break
        self.cursor["orders_next"] = cur
        self.cursor["orders_dateFrom"] = date_from
        self.cursor["orders_caught_up"] = done
        state = "догнал текущий момент" if done else "хвост остался, доберу в следующий час"
        print(f"инкремент: {self.stats['pages']} стр., {self.stats['scanned']} заказов, "
              f"бренда {hits} — {state}", flush=True)

    # ------------------------------------------------------- 3. статусы
    def collect_statuses(self):
        keep = int(self.cfg.get("keep_days", 45))
        edge = datetime.now(timezone.utc) - timedelta(days=keep)
        ids = []
        for key, rec in self.orders.items():
            created = parse(rec.get("createdAt"))
            if created and created < edge:
                continue
            if rec.get("wbStatus") in FINAL_WB and rec.get("supplierStatus") in ("complete",) \
               and rec.get("statusSettled"):
                continue
            ids.append(int(key))
        if not ids:
            print("статусы: отслеживать нечего", flush=True)
            return
        got = 0
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            d = self.wb.post(MP, "/api/v3/orders/status", {"orders": chunk}) or {}
            for s in d.get("orders", []):
                rec = self.orders.get(str(s["id"]))
                if not rec:
                    continue
                if rec.get("supplierStatus") != s.get("supplierStatus") or \
                   rec.get("wbStatus") != s.get("wbStatus"):
                    rec["statusChangedAt"] = now_iso()
                rec["supplierStatus"] = s.get("supplierStatus")
                rec["wbStatus"] = s.get("wbStatus")
                rec["isCancellable"] = s.get("isCancellable")
                rec["statusAt"] = now_iso()
                if s.get("wbStatus") in FINAL_WB:
                    rec["statusSettled"] = True
                got += 1
        print(f"статусы обновлены: {got}", flush=True)

    # ------------------------------------------------------ 4. поставки
    def collect_supplies(self):
        need = set()
        for rec in self.orders.values():
            sid = rec.get("supplyId")
            if not sid:
                continue
            known = self.supplies.get(sid)
            if not known or not known.get("done") or not known.get("scanDt"):
                need.add(sid)
        for sid in sorted(need):
            try:
                d = self.wb.get(MP, f"/api/v3/supplies/{sid}")
            except RuntimeError as e:
                print(f"поставка {sid}: {e}", flush=True)
                continue
            if d:
                d["seenAt"] = now_iso()
                self.supplies[sid] = d
        print(f"поставок обновлено: {len(need)}, всего в базе: {len(self.supplies)}", flush=True)

    # -------------------------------------------------------- 5. карточки
    def refresh_cards(self):
        last = parse(self.cursor.get("cards_refreshed_at"))
        hours = float(self.cfg.get("cards_refresh_hours", 12))
        if last and datetime.now(timezone.utc) - last < timedelta(hours=hours):
            return
        brand = self.cfg.get("brand", "")
        pages = int(self.cfg.get("cards_refresh_pages", 6))
        cursor = {"limit": 100}
        found = 0
        for _ in range(pages):
            d = self.wb.post(CONTENT, "/content/v2/get/cards/list",
                             {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}) or {}
            cards = d.get("cards", [])
            for c in cards:
                if (c.get("brand") or "").strip().lower() != brand.strip().lower():
                    continue
                ph = c.get("photos") or []
                self.cards[str(c["nmID"])] = {
                    "vendorCode": c.get("vendorCode"), "brand": c.get("brand"),
                    "subject": c.get("subjectName"), "title": c.get("title"),
                    "photo": ph[0].get("tm") if ph else None,
                    "sizes": [{"techSize": s.get("techSize"), "skus": s.get("skus"),
                               "chrtID": s.get("chrtID")} for s in (c.get("sizes") or [])],
                }
                found += 1
            cur = d.get("cursor", {})
            if len(cards) < 100:
                break
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        # у части карточек может не быть размеров и штрихкодов — добираем точечно
        # по артикулу: поиск по полному vendorCode работает надёжно
        for nm, c in list(self.cards.items()):
            if c.get("sizes") or not c.get("vendorCode"):
                continue
            d = self.wb.post(CONTENT, "/content/v2/get/cards/list",
                             {"settings": {"cursor": {"limit": 10},
                                           "filter": {"withPhoto": -1,
                                                      "textSearch": c["vendorCode"]}}}) or {}
            for got in d.get("cards", []):
                if str(got.get("nmID")) != str(nm):
                    continue
                ph = got.get("photos") or []
                c["sizes"] = [{"techSize": z.get("techSize"), "skus": z.get("skus"),
                               "chrtID": z.get("chrtID")} for z in (got.get("sizes") or [])]
                c["photo"] = c.get("photo") or (ph[0].get("tm") if ph else None)
                c["title"] = c.get("title") or got.get("title")
        self.cursor["cards_refreshed_at"] = now_iso()
        print(f"карточки бренда: в свежих страницах {found}, всего известно {len(self.cards)}",
              flush=True)

    # ------------------------------------------------- остатки FBS по складам
    def brand_skus(self):
        out = {}
        for nm, c in self.cards.items():
            for z in c.get("sizes") or []:
                for sku in z.get("skus") or []:
                    out[sku] = {"nmId": int(nm), "article": c.get("vendorCode"),
                                "techSize": z.get("techSize")}
        return out

    def collect_stocks(self):
        skus = self.brand_skus()
        if not skus:
            print("остатки: штрихкоды бренда неизвестны — пропускаю", flush=True)
            return
        last = parse(self.cursor.get("stocks_full_at"))
        hours = float(self.cfg.get("stocks_full_refresh_hours", 4))
        full = not last or datetime.now(timezone.utc) - last > timedelta(hours=hours)
        if full:
            targets = list(self.warehouses.keys())
        else:
            # между полными обходами трогаем только склады, где остаток уже есть
            targets = [w for w, v in self.stocks.items() if v.get("total")]
        if not targets:
            return
        keys = list(skus.keys())
        fresh = {}
        for wh in targets:
            rows = []
            try:
                for i in range(0, len(keys), 1000):
                    d = self.wb.post(MP, f"/api/v3/stocks/{wh}", {"skus": keys[i:i + 1000]}) or {}
                    rows += [r for r in d.get("stocks", []) if (r.get("amount") or 0) > 0]
            except RuntimeError as e:
                print(f"остатки, склад {wh}: {e}", flush=True)
                continue
            total = sum(r["amount"] for r in rows)
            if not total:
                continue                      # пустые склады в состоянии не держим
            fresh[wh] = {"total": total,
                         "rows": [{"sku": r["sku"], "amount": r["amount"], **skus[r["sku"]]}
                                  for r in rows if r["sku"] in skus],
                         "at": now_iso()}
        if full:
            self.stocks = fresh
            self.cursor["stocks_full_at"] = now_iso()
        else:
            self.stocks.update(fresh)
        total = sum(v.get("total", 0) for v in self.stocks.values())
        live = sum(1 for v in self.stocks.values() if v.get("total"))
        print(f"остатки FBS: {total} шт на {live} складах "
              f"({'полный обход' if full else 'быстрая проверка'})", flush=True)

    # ------------------------------------------------------- склады продавца
    def refresh_warehouses(self):
        last = parse(self.cursor.get("wh_refreshed_at"))
        if last and datetime.now(timezone.utc) - last < timedelta(hours=24):
            return
        try:
            d = self.wb.get(MP, "/api/v3/warehouses") or []
        except RuntimeError as e:
            print(f"склады: {e}", flush=True)
            return
        for w in d:
            self.warehouses[str(w["id"])] = {"name": w.get("name"), "officeId": w.get("officeId"),
                                             "cargoType": w.get("cargoType"),
                                             "deliveryType": w.get("deliveryType")}
        self.cursor["wh_refreshed_at"] = now_iso()
        print(f"складов продавца: {len(self.warehouses)}", flush=True)

    # ------------------------------------------------------------ уборка
    def prune(self):
        keep = int(self.cfg.get("keep_days", 45))
        edge = datetime.now(timezone.utc) - timedelta(days=keep + 15)
        drop = [k for k, r in self.orders.items()
                if (parse(r.get("createdAt")) or datetime.now(timezone.utc)) < edge]
        for k in drop:
            del self.orders[k]
        used = {r.get("supplyId") for r in self.orders.values() if r.get("supplyId")}
        for sid in [s for s in self.supplies if s not in used]:
            del self.supplies[sid]
        if drop:
            print(f"вычищено старых заказов: {len(drop)}", flush=True)

    def run(self):
        t0 = time.time()
        steps = [("карточки", self.refresh_cards), ("склады", self.refresh_warehouses),
                 ("очередь", self.collect_queue), ("инкремент", self.collect_incremental),
                 ("статусы", self.collect_statuses), ("поставки", self.collect_supplies),
                 ("остатки", self.collect_stocks)]
        failed = []
        for name, fn in steps:
            try:
                fn()
            except Exception as e:                       # noqa: BLE001
                failed.append(name)
                print(f"шаг «{name}» не отработал: {e}", flush=True)
        try:
            self.prune()
        except Exception as e:                           # noqa: BLE001
            print(f"уборка не отработала: {e}", flush=True)
        # состояние сохраняем в любом случае: половина данных лучше, чем потерянный прогон
        self.cursor["last_run"] = now_iso()
        self.cursor["queue_total_cabinet"] = self.stats["queue_total"]
        self.cursor["last_failed_steps"] = failed
        save("state/orders.json", self.orders)
        save("state/supplies.json", self.supplies)
        save("state/cards.json", self.cards)
        save("state/warehouses.json", self.warehouses)
        save("state/stocks.json", self.stocks)
        save("state/cursor.json", self.cursor)
        print(f"готово за {time.time() - t0:.0f} с; заказов бренда в базе: {len(self.orders)}"
              + (f"; сбойные шаги: {', '.join(failed)}" if failed else ""), flush=True)


if __name__ == "__main__":
    Collector().run()
