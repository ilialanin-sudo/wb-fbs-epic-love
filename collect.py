#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка данных WB по двум кабинетам одного бренда.

Два режима:
  * ежечасный  — заказы, продажи, реклама  (быстро, 6 запросов)
  * суточный   — плюс финотчёт, из него пересчитываются ставки юнит-экономики
                 (медленно, WB жёстко лимитирует финотчёт)

Ставки складываются в state/rates.json — он коммитится в репозиторий и живёт
между запусками, поэтому ежечасный прогон финотчёт не трогает.

ENV:
  WB_TOKEN_<КЛЮЧ>               — персональный токен на каждый кабинет из config.json
  WB_FIN_MAX_AGE_H              — через сколько часов обновлять финотчёт (24)
  WB_FORCE_FIN=1                — обновить финотчёт принудительно
  WINDOW_DAYS                   — глубина окна заказов (28)
"""
import os, sys, json, gzip, time, datetime, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_client import call, log, WBError

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATE = os.path.join(BASE, "state")
os.makedirs(DATA, exist_ok=True)
os.makedirs(STATE, exist_ok=True)

MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime.now(MSK)
TODAY = NOW.date()
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "28"))
START = TODAY - datetime.timedelta(days=WINDOW_DAYS - 1)
FIN_WEEKS_BACK = int(os.environ.get("FIN_WEEKS_BACK", "6"))


def _cfg():
    p = os.path.join(BASE, "config.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


CFG = _cfg()
# Кабинет может торговать десятком брендов, а дашборд нужен по одному.
# brand_filter — список брендов (как они написаны в карточках), по которым
# оставляем данные. Пусто — берём кабинет целиком, как раньше.
_bf = CFG.get("brand_filter")
if isinstance(_bf, str):
    _bf = [_bf]
BRANDS = {str(b).strip().lower() for b in (_bf or []) if str(b).strip()}
H_CONTENT = "content-api.wildberries.ru"

# start_date — день, раньше которого смотреть нечего (бренд ещё не продавался).
# Обрезает и окно выгрузки, и глубину сборочных заданий с возвратами.
_sd = os.environ.get("START_DATE") or CFG.get("start_date")
START_DATE = datetime.date.fromisoformat(str(_sd)[:10]) if _sd else None
if START_DATE and START_DATE > START:
    START = START_DATE



def load_cabinets():
    """Кабинеты описываются в config.json — код ни к каким названиям не привязан.

    "cabinets": [{"key": "main", "title": "Основной", "env": "WB_TOKEN_MAIN"}, ...]
    Кабинетов может быть один, два или сколько угодно.
    """
    cfg = {}
    p = os.path.join(BASE, "config.json")
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    out = []
    if not cfg.get("cabinets"):
        # запасной путь: конфиг старый или без блока cabinets — находим кабинеты
        # по переменным окружения WB_TOKEN_*, чтобы сборка не падала на пустом месте
        found = sorted(k for k, v in os.environ.items()
                       if k.startswith("WB_TOKEN_") and v.strip())
        for env in found:
            key = env[len("WB_TOKEN_"):].lower()
            out.append((key, key.upper(), os.environ[env].strip()))
        if out:
            log("  в config.json нет блока cabinets — беру кабинеты из переменных: "
                + ", ".join(k for k, _, _ in out))
        return out
    for c in cfg.get("cabinets") or []:
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        env = c.get("env") or ("WB_TOKEN_" + key.upper())
        tok = os.environ.get(env, "").strip()
        if tok:
            out.append((key, c.get("title") or key.upper(), tok))
        else:
            log(f"  кабинет «{c.get('title') or key}»: нет токена в {env} — пропускаю")
    return out


CABS = load_cabinets()

H_STAT = "statistics-api.wildberries.ru"
H_ADV = "advert-api.wildberries.ru"
H_FIN = "finance-api.wildberries.ru"
H_MP = "marketplace-api.wildberries.ru"
H_ANL = "seller-analytics-api.wildberries.ru"
FBS = "Склад продавца"


def save(name, obj):
    with gzip.open(os.path.join(DATA, name + ".json.gz"), "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def load(name, default=None):
    p = os.path.join(DATA, name + ".json.gz")
    if not os.path.exists(p):
        return default
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- статистика
def pull_stat(token, path, name, date_from, key="srid"):
    """flag=0 отдаёт всё, что менялось с date_from. Заказ всегда меняется не
    раньше даты создания, поэтому окно по дате заказа покрывается полностью.

    WB отдаёт не больше 80 000 строк за запрос и молча обрезает остальное.
    В большом кабинете 28 дней в один запрос не влезают, поэтому идём
    страницами: следующий запрос начинается с максимального lastChangeDate.
    Ключ склейки у заказов — srid, у продаж — пара (saleID, srid): у продажи
    и возврата один и тот же srid, и по одному srid возврат затирает продажу.
    """
    rows, cur = {}, date_from.isoformat()
    for page in range(12):
        batch = call(token, H_STAT, path, query={"dateFrom": cur, "flag": 0}) or []
        for r in batch:
            rows[tuple(r.get(k) for k in key.split("+"))] = r
        mx = max((r.get("lastChangeDate") or "" for r in batch), default=cur)
        log(f"    {name}: стр.{page} +{len(batch)}, всего {len(rows)} (по {mx})")
        if len(batch) < 80000 or not mx or mx == cur:
            break
        cur = mx
        time.sleep(65)
    out = list(rows.values())
    fbs = sum(1 for r in out if r.get("warehouseType") == FBS)
    log(f"    {name}: итого {len(out)} строк, FBS {fbs}")
    return out


# ------------------------------------------------------- номенклатуры бренда
def brand_nmids(token):
    """nmId всех карточек нужных брендов. Без этого бренд не отделить:
    в сборочных заданиях, возвратах и финотчёте поля brand нет вообще."""
    if not BRANDS:
        return None, {}
    nms, art, cur = set(), {}, {"limit": 100}
    for _ in range(60):
        r = call(token, H_CONTENT, "/content/v2/get/cards/list", method="POST",
                 body={"settings": {"cursor": cur, "filter": {"withPhoto": -1}}}) or {}
        cards = r.get("cards") or []
        for c in cards:
            if str(c.get("brand") or "").strip().lower() in BRANDS:
                nms.add(int(c["nmID"]))
                art[int(c["nmID"])] = c.get("vendorCode")
        c2 = r.get("cursor") or {}
        if len(cards) < 100:
            break
        cur = {"limit": 100, "updatedAt": c2.get("updatedAt"), "nmID": c2.get("nmID")}
        time.sleep(0.3)
    log(f"    карточки бренда: {len(nms)} nmId")
    return nms, art


def by_brand(rows, nms):
    """Строки статистики несут поле brand — по нему и режем, оно точнее списка
    карточек: карточку могли удалить, а заказ по ней в окне ещё есть."""
    if not BRANDS:
        return rows
    out = [r for r in rows
           if str(r.get("brand") or "").strip().lower() in BRANDS
           or (nms and r.get("nmId") in nms)]
    return out


def by_nm(rows, nms):
    if not BRANDS or nms is None:
        return rows
    return [r for r in rows if r.get("nmId") in nms]


# ------------------------------------------------------------------ реклама
def pull_adv(token, nms):
    """Списания по кампаниям и, если задан бренд, только по его кампаниям.

    Кампания привязана к карточкам, а не к бренду, поэтому:
      1) /adv/v1/upd — какие кампании вообще тратили деньги в окне;
      2) /api/advert/v2/adverts — какие nmId в этих кампаниях;
      3) оставляем те, где есть карточки бренда.
    Кампания на одну карточку (а таких почти все) раскладывается точно.
    Смешанные — через /adv/v3/fullstats, там расход разложен по nmId и дням.
    """
    try:
        upd = call(token, H_ADV, "/adv/v1/upd",
                   query={"from": START.isoformat(), "to": TODAY.isoformat()}) or []
    except WBError as e:
        log("    реклама недоступна:", e)
        return [], []
    log(f"    реклама: {len(upd)} списаний по {len({u.get('advertId') for u in upd})} кампаниям")
    if not BRANDS or nms is None:
        return upd, []

    ids = sorted({u.get("advertId") for u in upd if u.get("advertId")})
    camp = {}
    for i in range(0, len(ids), 50):
        try:
            r = call(token, H_ADV, "/api/advert/v2/adverts",
                     query={"ids": ",".join(map(str, ids[i:i + 50]))}) or {}
        except WBError as e:
            log(f"    состав кампаний недоступен: {e}")
            return [], []
        for a in (r.get("adverts") or []):
            camp[a.get("id")] = {x.get("nm_id") for x in (a.get("nm_settings") or [])
                                 if x.get("nm_id")}
        time.sleep(0.4)

    mine = {k: v for k, v in camp.items() if v & nms}
    mixed = [k for k, v in mine.items() if v - nms]
    upd_b = [u for u in upd if u.get("advertId") in mine]
    spend = sum(float(u.get("updSum") or 0) for u in upd_b)
    log(f"    кампании бренда: {len(mine)} из {len(camp)}, расход {spend:,.0f} ₽"
        .replace(",", " ") + (f", смешанных {len(mixed)}" if mixed else ""))

    # расход по nmId и дням: у кампании на одну карточку — прямо из upd,
    # у смешанной — из fullstats
    advnm = []
    for u in upd_b:
        c = mine.get(u.get("advertId")) or set()
        if len(c) == 1:
            advnm.append(dict(nmId=next(iter(c)), date=str(u.get("updTime", ""))[:10],
                              sum=float(u.get("updSum") or 0)))
    if mixed:
        for i in range(0, len(mixed), 50):
            try:
                st = call(token, H_ADV, "/adv/v3/fullstats",
                          query={"ids": ",".join(map(str, mixed[i:i + 50])),
                                 "beginDate": START.isoformat(),
                                 "endDate": (TODAY - datetime.timedelta(days=1)).isoformat()}) or []
            except WBError as e:
                log(f"    fullstats недоступен: {e}")
                break
            for c in st:
                for d in (c.get("days") or []):
                    day = str(d.get("date", ""))[:10]
                    for app in (d.get("apps") or []):
                        for nm in (app.get("nm") or []):
                            if nm.get("nmId") in nms:
                                advnm.append(dict(nmId=nm["nmId"], date=day,
                                                  sum=float(nm.get("sum") or 0)))
            if i + 50 < len(mixed):
                time.sleep(62)
    return upd_b, advnm



# ------------------------------------------- возвраты продавцу (в том числе на ПВЗ)
def pull_returns(token, days=None, nms=None):
    """Отчёт «Возвраты и перемещения товаров»: что едет обратно к продавцу.

    Это единственное место в API, где видно физический путь возврата:
    `readyToReturnDt` — момент, когда товар доехал до ПВЗ и готов к выдаче,
    `completedDt` — когда его забрали. В финотчёте такого события нет вообще,
    там обратная логистика списывается в день отказа.

    Тонкости, проверенные на живых данных:
      * окно запроса не больше 31 дня — иначе 400;
      * `orderDt` — день оформления возврата, то есть день отказа покупателя,
        а НЕ день исходного заказа (сверено с cancelDate: совпадает по дням,
        и приходит раньше, чем WB проставит isCancel);
      * свой srid вида `mp.<hex>.r` — с srid заказа не сшивается, связь только
        через nmId, размер и дату;
      * список товаров нигде не задаётся: какие артикулы подключены к возврату
        на ПВЗ, видно из самого отчёта — включили новый, он появится сам.
    """
    days = days or int(os.environ.get("RETURNS_DAYS", "60"))
    rows, seen = [], set()
    d2 = TODAY
    while d2 > TODAY - datetime.timedelta(days=days):
        d1 = max(d2 - datetime.timedelta(days=31), TODAY - datetime.timedelta(days=days))
        r = call(token, H_ANL, "/api/v1/analytics/goods-return",
                 query={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat()}) or {}
        for x in (r.get("report") or []):
            k = (x.get("srid"), x.get("shkId"), x.get("nmId"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(x)
        if d1 <= TODAY - datetime.timedelta(days=days):
            break
        d2 = d1
        time.sleep(3)
    if nms is not None:
        before = len(rows)
        rows = [x for x in rows if x.get("nmId") in nms]
        log(f"    бренд: {len(rows)} строк из {before}")
    kinds = collections.Counter(x.get("returnType") for x in rows)
    log(f"    возвраты: {len(rows)} строк; " +
        ", ".join(f"{k} — {v}" for k, v in kinds.most_common(3)))
    return rows


# ------------------------------------------------------- сборка и отгрузка
def pull_marketplace(token, days, nms=None):
    """Операционная картина FBS: сборочные задания, их статусы и поставки.

    Это другой раздел API, не статистика. Здесь видно, что происходит с заказом
    физически: висит ли он на сборке, уехал ли в поставке, отменён ли.
    Дата отгрузки берётся из поставки — по closedAt, когда поставка закрыта.
    """
    frm = int(time.mktime((TODAY - datetime.timedelta(days=days)).timetuple()))
    orders, nxt = [], 0
    for _ in range(30):
        r = call(token, H_MP, "/api/v3/orders",
                 query={"limit": 1000, "next": nxt, "dateFrom": frm}) or {}
        batch = r.get("orders") or []
        orders += batch
        nxt = r.get("next") or 0
        if len(batch) < 1000 or not nxt:
            break
        time.sleep(0.4)

    statuses = []
    ids = [o["id"] for o in orders]
    for i in range(0, len(ids), 1000):
        r = call(token, H_MP, "/api/v3/orders/status", method="POST",
                 body={"orders": ids[i:i + 1000]}) or {}
        statuses += r.get("orders") or []
        time.sleep(0.4)

    supplies, nxt = [], 0
    for _ in range(20):
        r = call(token, H_MP, "/api/v3/supplies", query={"limit": 1000, "next": nxt}) or {}
        batch = r.get("supplies") or []
        supplies += batch
        nxt = r.get("next") or 0
        if len(batch) < 1000 or not nxt:
            break
        time.sleep(0.4)

    st = {s["id"]: s for s in statuses}
    if nms is not None:
        before = len(orders)
        orders = [o for o in orders if o.get("nmId") in nms]
        log(f"    бренд: {len(orders)} заданий из {before}")
    slim = []
    for o in orders:
        s = st.get(o["id"]) or {}
        slim.append(dict(id=o["id"], createdAt=o.get("createdAt"), supplyId=o.get("supplyId"),
                         nmId=o.get("nmId"), article=o.get("article"),
                         warehouseId=o.get("warehouseId"),
                         price=(o.get("convertedPrice") or o.get("price") or 0) / 100,
                         supplierStatus=s.get("supplierStatus"), wbStatus=s.get("wbStatus")))
    # склады продавца: их может быть несколько, и отгружают они по-разному
    try:
        whs = call(token, H_MP, "/api/v3/warehouses") or []
    except Exception as e:
        log(f"    список складов недоступен: {e}")
        whs = []
    log(f"    сборка: {len(slim)} заданий, {len(supplies)} поставок, {len(whs)} складов")
    return dict(orders=slim,
                warehouses=[dict(id=w.get("id"), name=w.get("name")) for w in whs],
                supplies=[dict(id=s["id"], done=bool(s.get("done")),
                               createdAt=s.get("createdAt"), closedAt=s.get("closedAt"),
                               # scanDt — момент, когда поставку приняли на стороне WB;
                               # closedAt — когда её закрыл продавец. Разница важна для
                               # коэффициента скорости, поэтому храним оба
                               scanDt=s.get("scanDt"))
                          for s in supplies])


# ----------------------------------------------------------------- финотчёт
FIN_KEEP = ("rrdId", "srid", "nmId", "vendorCode", "subjectName", "sellerOperName",
            "quantity", "retailPrice", "retailPriceWithDisc", "retailAmount",
            "forPay", "acquiringFee", "commissionPercent", "deliveryAmount", "returnAmount",
            "deliveryService", "rebillLogisticCost", "penalty", "deduction",
            "paidStorage", "paidAcceptance", "deliveryMethod",
            "saleDt", "orderDt", "rrDate", "dateFrom", "dateTo")


def pull_finance(token, keep_srids=None, keep_nms=None):
    """Недельными окнами: одним куском WB отдаёт сотни мегабайт.

    В большом кабинете за шесть недель это миллионы строк, и все они в памяти
    не нужны: экономику считаем по FBS-заказам окна и по карточкам бренда.
    Фильтруем сразу на приёме."""
    out, d0 = [], TODAY - datetime.timedelta(days=7 * FIN_WEEKS_BACK)
    while d0 <= TODAY:
        d1 = min(d0 + datetime.timedelta(days=6), TODAY)
        rrdid, pages = 0, 0
        while pages < 8:
            batch = call(token, H_FIN, "/api/finance/v1/sales-reports/detailed",
                         method="POST",
                         body={"dateFrom": d0.isoformat(), "dateTo": d1.isoformat(),
                               "rrdid": rrdid, "limit": 100000})
            pages += 1
            if not batch:
                break
            for r in batch:
                if keep_srids is not None and r.get("srid") not in keep_srids \
                        and not (keep_nms and r.get("nmId") in keep_nms):
                    continue
                out.append({k: r.get(k) for k in FIN_KEEP})
            rrdid = max(r["rrdId"] for r in batch)
            log(f"    финотчёт {d0}—{d1}: пришло {len(batch)}, оставлено всего {len(out)}")
            if len(batch) < 100000:
                break
            time.sleep(20)
        d0 = d1 + datetime.timedelta(days=1)
        time.sleep(10)
    return out


# ------------------------------------------------------- ставки из финотчёта
def fnum(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def cohort_rates(orders_raw, sold_srids, fin_by_srid, lo, hi, tag):
    """Ставки по когорте СЫРЫХ заказов (со всеми, кто потом отменится и не выкупится).

    Ключевой момент: у WB isCancel=true ставится и на невыкуп, поэтому у свежего
    дня отмен почти нет, а у зрелой когорты их 55–60 %. Считать экономику можно
    только от сырого заказа — иначе свежий день завышен вдвое.
    """
    cohort = [r for r in orders_raw if lo <= r["date"][:10] <= hi]
    n = len(cohort)
    if not n:
        return None
    a = collections.defaultdict(float)
    covered = 0
    for r in cohort:
        f = fin_by_srid.get(r["srid"])
        if not f:
            continue
        covered += 1
        for k, v in f.items():
            a[k] += v
    bought = sum(1 for r in cohort if r["srid"] in sold_srids)
    retail = a["retail"]
    return dict(
        tag=tag, window=f"{lo}—{hi}", orders_raw=n,
        covered=covered, coverage=round(covered / n, 3),
        bought=bought, buyout_of_raw=round(bought / n, 4),
        retail=round(retail, 2), customer=round(a["customer"], 2), forpay=round(a["forpay"], 2),
        payout_share=round(a["forpay"] / retail, 4) if retail else None,
        spp_share=round(1 - a["customer"] / retail, 4) if retail else None,
        logistics=round(a["log"], 2),
        logistics_per_order=round(a["log"] / n, 2),
        logistics_fwd_per_order=round((a["log_fwd"] + a["log_mix"] / 2) / n, 2),
        logistics_back_per_order=round((a["log_back"] + a["log_mix"] / 2) / n, 2),
        logistics_back_share=round((a["log_back"] + a["log_mix"] / 2) / a["log"], 4) if a["log"] else 0,
        events_per_order=round((a["dev"] + a["ret"]) / n, 3),
        deliveries=int(a["dev"]), returns=int(a["ret"]),
        handling_per_order=round(a["acc"] / n, 2),
        penalty_per_order=round(a["pen"] / n, 2),
        storage_per_order=round(a["sto"] / n, 2),
        avg_order_price=round(sum(fnum(r.get("priceWithDisc")) for r in cohort) / n, 2),
    )


def payout_block(fin, srids, tag):
    """Сколько из цены продавца реально доходит до продавца — по продажам,
    без привязки к когорте: это отношение, а не ставка на заказ.

    Считаем отдельно для FBS и FBW, потому что СПП WB компенсирует по-разному:
    на FBW доплачивает сверх того, что заплатил покупатель, на FBS почти нет.
    """
    rows = fin if srids is None else [r for r in fin if r.get("srid") in srids]
    sale = [r for r in rows if r["sellerOperName"] == "Продажа"]
    ret = [r for r in rows if r["sellerOperName"] == "Возврат"]
    if not sale:
        return None
    retail = sum(fnum(r["retailPriceWithDisc"]) for r in sale) \
        - sum(fnum(r["retailPriceWithDisc"]) for r in ret)
    customer = sum(fnum(r["retailAmount"]) for r in sale) \
        - sum(fnum(r["retailAmount"]) for r in ret)
    forpay = sum(fnum(r["forPay"]) for r in sale) - sum(fnum(r["forPay"]) for r in ret)
    qty = sum(fnum(r["quantity"]) for r in sale) - sum(fnum(r["quantity"]) for r in ret)
    base = [fnum(r.get("commissionPercent")) for r in sale if fnum(r.get("commissionPercent"))]
    if not retail:
        return None
    return dict(
        tag=tag, qty=int(qty),
        retail=round(retail, 2), customer=round(customer, 2), forpay=round(forpay, 2),
        payout_share=round(forpay / retail, 4),
        kept_share=round(1 - forpay / retail, 4),
        spp_share=round(1 - customer / retail, 4),
        spp_compensated=round(forpay / customer, 4) if customer else None,
        base_commission_pct=round(sum(base) / len(base), 2) if base else None,
        weeks=sorted({(r.get("dateFrom") or "")[:10] for r in rows if r.get("dateFrom")}),
    )


def index_finance(fin):
    """Финотчёт → срез по srid: сколько денег прошло по каждому заказу."""
    idx = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in fin:
        srid = r.get("srid")
        if not srid:
            continue
        a = idx[srid]
        cost = fnum(r.get("deliveryService")) + fnum(r.get("rebillLogisticCost"))
        dev, ret = fnum(r.get("deliveryAmount")), fnum(r.get("returnAmount"))
        a["log"] += cost
        a["dev"] += dev
        a["ret"] += ret
        # строка логистики относится либо к доставке покупателю, либо к обратному
        # плечу при невыкупе или возврате — раскладываем, чтобы их было видно врозь
        if ret > 0 and dev == 0:
            a["log_back"] += cost
        elif dev > 0 and ret == 0:
            a["log_fwd"] += cost
        else:
            a["log_mix"] += cost
        a["acc"] += fnum(r.get("paidAcceptance"))
        a["pen"] += fnum(r.get("penalty"))
        a["sto"] += fnum(r.get("paidStorage"))
        op = r.get("sellerOperName")
        if op == "Продажа":
            a["retail"] += fnum(r.get("retailPriceWithDisc"))
            a["customer"] += fnum(r.get("retailAmount"))
            a["forpay"] += fnum(r.get("forPay"))
            a["qty"] += fnum(r.get("quantity"))
        elif op == "Возврат":
            # в отчёте WB возвраты записаны положительными числами и вычитаются
            a["retail"] -= fnum(r.get("retailPriceWithDisc"))
            a["customer"] -= fnum(r.get("retailAmount"))
            a["forpay"] -= fnum(r.get("forPay"))
            a["qty"] -= fnum(r.get("quantity"))
    return idx


def rates_from_finance(fin, orders_all, sales_all, nms=None):
    """Когортные ставки. Порядок источников: сначала собственная выборка бренда,
    потом FBS всего кабинета, потом кабинет целиком.

    Разделение неслучайно. Выкупу нужна дозревшая когорта — у молодого бренда её
    просто нет, и приходится одалживать у кабинета. А логистика и приёмка — это
    физика конкретного товара: у куртки литраж втрое больше, чем у футболки,
    поэтому их берём по бренду, как только наберётся хоть какая-то выборка,
    даже когда выкуп ещё чужой. Источник каждой ставки виден в дашборде.
    """
    if not fin:
        return dict(payout_share=None, error="финотчёт пуст",
                    updated_at=NOW.isoformat(timespec="seconds"))
    idx = index_finance(fin)
    covered_to = max((r.get("dateTo") or "")[:10] for r in fin)
    cov_d = datetime.date.fromisoformat(covered_to)
    lo = (cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_LO", "21")))).isoformat()
    hi = (cov_d - datetime.timedelta(days=int(os.environ.get("COHORT_HI", "8")))).isoformat()

    sold = {r["srid"] for r in sales_all if str(r.get("saleID", "")).startswith("S")}
    fbs_orders = [r for r in orders_all if r.get("warehouseType") == FBS]
    brand_orders = by_brand(fbs_orders, nms) if BRANDS else []
    label = ", ".join(sorted(BRANDS)).upper() if BRANDS else ""

    whole = cohort_rates(orders_all, sold, idx, lo, hi, "кабинет целиком")
    fbs = cohort_rates(fbs_orders, sold, idx, lo, hi, "FBS кабинета")
    brand = cohort_rates(brand_orders, sold, idx, lo, hi, f"FBS бренда {label}") \
        if brand_orders else None

    MIN_ORD, MIN_COV = int(os.environ.get("MIN_COHORT", "150")), 0.6
    MIN_BRAND = int(os.environ.get("MIN_COHORT_BRAND", "60"))
    use_brand = bool(brand and brand["orders_raw"] >= MIN_BRAND
                     and brand["coverage"] >= MIN_COV and brand["payout_share"])
    use_fbs = bool(fbs and fbs["orders_raw"] >= MIN_ORD and fbs["coverage"] >= MIN_COV
                   and fbs["payout_share"])
    src = brand if use_brand else (fbs if use_fbs else whole)
    if use_brand:
        source = f"FBS бренда {label}"
    elif use_fbs:
        source = f"FBS кабинета — у бренда {label} когорта ещё не дозрела" if BRANDS else "FBS"
    else:
        source = "кабинет целиком (FBS ещё не дозрел)"

    # логистика и приёмка — по бренду, как только наберётся выборка:
    # это габариты товара, а не общая ставка кабинета
    logi_src = src["tag"] if src else None
    logi = src
    if not use_brand and brand and brand["covered"] >= int(os.environ.get("MIN_LOGI_BRAND", "25")):
        logi = brand
        logi_src = f"FBS бренда {label}, {brand['covered']} заказов в финотчёте"

    # предварительный, ещё не дозревший срез — просто чтобы видеть тренд
    prev_src = brand_orders if BRANDS else fbs_orders
    preview = None
    if prev_src:
        f_lo = min(r["date"][:10] for r in prev_src)
        preview = cohort_rates(prev_src, sold, idx, f_lo, hi,
                               (f"бренд {label}" if BRANDS else "FBS") + ", предварительно")

    fbs_srids = {r["srid"] for r in fbs_orders} | \
        {r["srid"] for r in sales_all if r.get("warehouseType") == FBS}
    brand_srids = ({r["srid"] for r in brand_orders} |
                   {r["srid"] for r in by_brand(sales_all, nms)
                    if r.get("warehouseType") == FBS}) if BRANDS else set()
    pay_brand = payout_block(fin, brand_srids, f"FBS бренда {label}") if brand_srids else None
    pay_fbs_cab = payout_block(fin, fbs_srids, "FBS кабинета")
    pay_whole = payout_block(fin, None, "кабинет целиком")
    MIN_PAY = int(os.environ.get("MIN_PAYOUT_QTY", "30"))
    # aggregate.py читает payout_fbs — кладём туда лучшее, что есть по бренду
    pay_fbs = pay_brand if (pay_brand and pay_brand["qty"] >= MIN_PAY) else pay_fbs_cab
    use_pay_fbs = bool(pay_fbs and pay_fbs["qty"] >= MIN_PAY)

    lag = []
    ord_date = {r["srid"]: r["date"][:10] for r in (brand_orders or fbs_orders)}
    for r in fin:
        if fnum(r.get("returnAmount")) > 0 and r.get("srid") in ord_date and r.get("rrDate"):
            try:
                dd = (datetime.date.fromisoformat(str(r["rrDate"])[:10])
                      - datetime.date.fromisoformat(ord_date[r["srid"]])).days
            except Exception:
                continue
            if 0 < dd < 60:
                lag.append(dd)
    lag.sort()
    return_lag = dict(qty=len(lag),
                      median=lag[len(lag) // 2] if lag else None,
                      p25=lag[int(len(lag) * .25)] if lag else None,
                      p75=lag[int(len(lag) * .75)] if lag else None) if lag else None

    return dict(
        return_lag=return_lag,
        payout_share=(pay_fbs if use_pay_fbs else src)["payout_share"] if (src or pay_fbs) else None,
        payout_source=(f"{pay_fbs['tag']}, {pay_fbs['qty']} шт" if use_pay_fbs
                       else "кабинет целиком (FBS-продаж мало)"),
        payout_fbs=pay_fbs, payout_whole=pay_whole, payout_brand=pay_brand,
        payout_fbs_cabinet=pay_fbs_cab,
        payout_fbs_qty=(pay_fbs or {}).get("qty", 0),
        spp_share=src["spp_share"] if src else None,
        logistics_per_order=logi["logistics_per_order"] if logi else None,
        logistics_fwd_per_order=logi["logistics_fwd_per_order"] if logi else None,
        logistics_back_per_order=logi["logistics_back_per_order"] if logi else None,
        logistics_source=logi_src,
        handling_per_order=logi["handling_per_order"] if logi else None,
        penalty_per_order=logi["penalty_per_order"] if logi else None,
        buyout_of_raw=src["buyout_of_raw"] if src else None,
        source=source,
        cohort_whole=whole, cohort_fbs=fbs, cohort_brand=brand,
        cohort_fbs_preview=preview,
        covered_to=covered_to,
        updated_at=NOW.isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------- main
# суммы и обороты в state/rates.json не храним: файл лежит в публичном
# репозитории, а для расчёта нужны только коэффициенты и размеры выборок
MONEY_KEYS = ("retail", "customer", "forpay", "logistics", "avg_order_price")


def strip_money(obj):
    if isinstance(obj, dict):
        return {k: strip_money(v) for k, v in obj.items() if k not in MONEY_KEYS}
    if isinstance(obj, list):
        return [strip_money(v) for v in obj]
    return obj


def main():
    if not CABS:
        sys.exit("не задан ни один токен: нужна переменная WB_TOKEN_<КЛЮЧ> на каждый кабинет из config.json")
    state_path = os.path.join(STATE, "rates.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            state = {}

    max_age = float(os.environ.get("WB_FIN_MAX_AGE_H", "24"))
    force = os.environ.get("WB_FORCE_FIN") == "1"

    def stale(key):
        if force or key not in state:
            return True
        try:
            prev = datetime.datetime.fromisoformat(state[key]["updated_at"])
        except Exception:
            return True
        return (NOW - prev).total_seconds() > max_age * 3600

    log(f"окно заказов: {START} — {TODAY} (МСК {NOW:%H:%M})")

    if BRANDS:
        log("  бренд: " + ", ".join(sorted(BRANDS)).upper())

    NMS = {}
    for key, title, tok in CABS:
        if not BRANDS:
            NMS[key] = None
            continue
        log(f"  [{title}] карточки бренда")
        try:
            nms, art = brand_nmids(tok)
        except Exception as e:
            log(f"    список карточек недоступен: {e}")
            nms, art = set(), {}
        NMS[key] = nms
        save(f"nm_{key}", dict(nms=sorted(nms), articles={str(k): v for k, v in art.items()}))

    # Выгружаем кабинет целиком, а на диск кладём только бренд: ставки
    # экономики считаются по кабинету, а весь дашборд — по бренду.
    orders, sales = {}, {}
    for key, title, tok in CABS:
        log(f"  [{title}] заказы")
        orders[key] = pull_stat(tok, "/api/v1/supplier/orders", "заказы", START)
        b = by_brand(orders[key], NMS[key])
        log(f"    бренд: {len(b)} заказов из {len(orders[key])}")
        save(f"orders_{key}", b)

    time.sleep(62)
    for key, title, tok in CABS:
        log(f"  [{title}] продажи")
        sales[key] = pull_stat(tok, "/api/v1/supplier/sales", "продажи", START, key="saleID+srid")
        b = by_brand(sales[key], NMS[key])
        log(f"    бренд: {len(b)} строк из {len(sales[key])}")
        save(f"sales_{key}", b)

    for key, title, tok in CABS:
        log(f"  [{title}] реклама")
        try:
            upd, advnm = pull_adv(tok, NMS[key])
        except Exception as e:
            log(f"    реклама недоступна: {e}")
            upd, advnm = [], []
        save(f"adv_{key}", upd)
        if advnm:
            save(f"advnm_{key}", advnm)

    asm_days = int(os.environ.get("ASSEMBLY_DAYS", "30"))
    if START_DATE:
        asm_days = min(asm_days, (TODAY - START_DATE).days + 1)
    for key, title, tok in CABS:
        log(f"  [{title}] сборка и отгрузка")
        try:
            save(f"mp_{key}", pull_marketplace(tok, asm_days, NMS[key]))
        except Exception as e:
            log(f"    раздел сборки недоступен: {e}")

    for key, title, tok in CABS:
        log(f"  [{title}] возвраты продавцу")
        try:
            rd = int(os.environ.get("RETURNS_DAYS", "60"))
            if START_DATE:
                rd = min(rd, (TODAY - START_DATE).days + 1)
            save(f"ret_{key}", pull_returns(tok, days=rd, nms=NMS[key]))
        except Exception as e:
            log(f"    отчёт по возвратам недоступен: {e}")

    for key, title, tok in CABS:
        if not stale(key):
            log(f"  [{title}] финотчёт свежий ({state[key]['updated_at']}) — пропускаю")
            continue
        log(f"  [{title}] финотчёт (долго)")
        try:
            keep = {r["srid"] for r in orders[key] if r.get("warehouseType") == FBS} | \
                   {r["srid"] for r in sales[key] if r.get("warehouseType") == FBS}
            fin = pull_finance(tok, keep_srids=keep, keep_nms=NMS[key])
            state[key] = rates_from_finance(fin, orders[key], sales[key], NMS[key])
            log(f"    ставки: до продавца доходит {state[key]['payout_share']}, "
                f"логистика/заказ {state[key]['logistics_per_order']} "
                f"({state[key].get('logistics_source')}), "
                f"выкуп {state[key]['buyout_of_raw']}, источник — {state[key]['source']}")
        except Exception as e:
            log(f"    финотчёт не собран: {e}")
            if key not in state:
                state[key] = dict(payout_share=None, error=str(e),
                                  updated_at=NOW.isoformat(timespec="seconds"))

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(strip_money(state), f, ensure_ascii=False, indent=1)
    log("готово")


if __name__ == "__main__":
    main()
