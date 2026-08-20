#!/usr/bin/env python3
"""HTTP-клиент WB.

Правила: Authorization без Bearer; на 429 ждать ровно X-Ratelimit-Retry;
троттлинг по (host, path); повторов не больше max_429.
"""
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode


class WBClient:
    def __init__(self, token, verbose=True, min_interval=0.0):
        self.token = token.strip()
        self.verbose = verbose
        self.min_interval = min_interval
        self._next_allowed = {}

    def _log(self, *a):
        if self.verbose:
            print("[wb]", *a, flush=True)

    def _wait(self, key):
        d = self._next_allowed.get(key, 0) - time.time()
        if d > 0:
            time.sleep(d + 0.05)

    def request(self, method, host, path, body=None, query=None, max_429=14, timeout=180):
        key = (host, path.split("?")[0])
        url = f"https://{host}{path}" + (("?" + urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            self._wait(key)
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    payload = r.read().decode()
                    if self.min_interval:
                        self._next_allowed[key] = time.time() + self.min_interval
                    return json.loads(payload) if payload.strip() else None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    attempt += 1
                    retry = (e.headers.get("X-Ratelimit-Retry")
                             or e.headers.get("Retry-After")
                             or e.headers.get("X-Ratelimit-Reset") or "2")
                    retry = int(retry) if str(retry).isdigit() else 2
                    # WB часто отдаёт retry=1 при плотном глобальном лимите: если
                    # долбиться ровно через секунду, окно не откроется никогда —
                    # с каждой попыткой пауза растёт
                    wait = retry + min(2 * attempt, 45)
                    self._next_allowed[key] = time.time() + wait + 0.3
                    if attempt >= max_429:
                        raise RuntimeError(f"429 не отпускает: {path}")
                    self._log(f"429 {path}: пауза {wait}s ({attempt}/{max_429})")
                    continue
                if e.code in (500, 502, 503, 504):
                    # у WB бывают короткие «can't dial» — это не наша ошибка
                    attempt += 1
                    if attempt < 5:
                        wait = 5 * attempt
                        self._log(f"{e.code} {path}: пауза {wait}s ({attempt}/5)")
                        self._next_allowed[key] = time.time() + wait
                        continue
                raise RuntimeError(f"HTTP {e.code} {path}: {e.read().decode()[:300]}")
            except (urllib.error.URLError, TimeoutError) as e:
                attempt += 1
                if attempt >= 4:
                    raise
                self._log(f"сеть {path}: {e} — повтор через 5s")
                time.sleep(5)

    def get(self, host, path, query=None, **kw):
        return self.request("GET", host, path, query=query, **kw)

    def post(self, host, path, body, **kw):
        return self.request("POST", host, path, body=body, **kw)
