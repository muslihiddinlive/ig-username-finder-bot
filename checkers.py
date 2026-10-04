"""Username tekshiruvchilar: Apify aktori (ishonchli) va oddiy HTTP probe (taxminiy)."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
import logging
import os
import random

import aiohttp

log = logging.getLogger(__name__)
FREE, TAKEN, UNKNOWN = "free", "taken", "unknown"


class CheckerError(Exception):
    pass


class ApifyChecker:
    name = "apify"
    authoritative = True  # 'free' natijasi pul yechish uchun yetarli

    def __init__(self, token: str, actor: str, budget: int, store, concurrency: int = 3, delay: float = 1.5, proxy: bool = False):
        self.token, self.actor, self.budget, self.store = token, actor.replace("/", "~"), budget, store
        self.concurrency, self.delay, self.proxy = concurrency, delay, proxy
        self.last_reason = ""
        self._sem = asyncio.Semaphore(2)
        self.last_debug = ""

    def available(self, n: int) -> bool:
        return bool(self.token) and self.store.apify_left(self.budget) >= n

    async def check_batch(self, names: list[str]) -> dict[str, str]:
        if not self.available(len(names)):
            raise CheckerError("apify byudjet/token yo'q")
        url = f"https://api.apify.com/v2/acts/{self.actor}/run-sync-get-dataset-items"
        body = {"usernames": ",".join(names), "platforms": "instagram",
                "concurrency": self.concurrency, "delayBetweenRequests": self.delay}
        if self.proxy:
            body["proxyConfiguration"] = {"useApifyProxy": True}
        async with self._sem:
            self.store.apify_spend(len(names))  # oldindan band qilamiz, xato bo'lsa qaytaramiz
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
                    async with s.post(url, json=body, headers={"Authorization": f"Bearer {self.token}"}) as r:
                        if r.status not in (200, 201):
                            body_txt = (await r.text())[:200]
                            self.last_debug = f"HTTP {r.status}: {body_txt}"
                            raise CheckerError(f"apify HTTP {r.status}: {body_txt[:120]}")
                        items = await r.json()
                        sample = items[:3] if isinstance(items, list) else items
                        self.last_debug = (f"HTTP {r.status}; elementlar={len(items) if isinstance(items, list) else type(items).__name__}"
                                           f"; namuna={json.dumps(sample, ensure_ascii=False)[:500]}")
                        self.last_reason = _explain(items)
            except BaseException:
                self.store.apify_spend(-len(names))
                raise
        return parse_apify_items(items, names)


def _explain(items) -> str:
    """Nega natijalar 'noma'lum' ekanini qisqa matnda tushuntiradi (aktorning `error` maydonidan)."""
    if not isinstance(items, list):
        return f"kutilmagan javob turi: {type(items).__name__}"
    ig = [it for it in items if "instagram" in str(it.get("platform", "")).lower()]
    if not ig:
        plats = sorted({str(it.get("platform", "?")) for it in items})[:5]
        return f"Instagram natijalari qaytmadi (kelgan platformalar: {plats or 'yo`q'})"
    nulls = [it for it in ig if it.get("available") is None]
    if not nulls:
        return ""
    errs = Counter(str(it.get("error") or "error maydoni bo'sh")[:110] for it in nulls)
    return f"{len(nulls)}/{len(ig)} natija null: " + "; ".join(f"{n}× {e}" for e, n in errs.most_common(2))


def parse_apify_items(items: list, names: list[str]) -> dict[str, str]:
    out = {n: UNKNOWN for n in names}
    for it in items or []:
        if "instagram" not in str(it.get("platform", "")).lower():
            continue
        u = str(it.get("username", "")).lower().lstrip("@")
        if u in out:
            a = it.get("available")
            out[u] = FREE if a is True else TAKEN if a is False else UNKNOWN
    return out


class ProbeChecker:
    """Ommaviy profil sahifasiga oddiy so'rov. Instagram bunga ishonchli javob bermaydi,
    shuning uchun natijasi faqat 'taxminiy' (pul yechilmaydi)."""

    name = "probe"
    authoritative = False

    def __init__(self, proxies_file: str = "", concurrency: int = 3):
        self.proxies: list[str] = []
        if proxies_file and os.path.exists(proxies_file):
            self.proxies = [x.strip() for x in open(proxies_file) if x.strip() and not x.startswith("#")]
        self._sem = asyncio.Semaphore(concurrency)

    async def _one(self, s: aiohttp.ClientSession, u: str) -> str:
        async with self._sem:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            proxy = random.choice(self.proxies) if self.proxies else None
            if proxy and "://" not in proxy:
                proxy = "http://" + proxy
            try:
                async with s.get(
                    f"https://www.instagram.com/{u}/", proxy=proxy, allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
                ) as r:
                    if r.status == 404:
                        return FREE
                    if r.status == 200 and f"@{u}" in (await r.text()).lower():
                        return TAKEN
            except Exception:
                pass
            return UNKNOWN

    async def check_batch(self, names: list[str]) -> dict[str, str]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            res = await asyncio.gather(*(self._one(s, n) for n in names))
        return dict(zip(names, res))


class Pipeline:
    """Tartib bilan tekshiradi: ishonchsiz filtr -> ishonchli tasdiq."""

    def __init__(self, checkers: list):
        self.checkers = checkers
        self.last_error = ""

    def has_verifier(self, n: int) -> bool:
        """Kamida bitta ishonchli (tasdiqlovchi) tekshiruvchi hozir ishlay oladimi."""
        return any(ck.authoritative and ck.available(n) for ck in self.checkers)

    async def check(self, names: list[str]) -> dict[str, tuple[str, bool]]:
        """name -> (status, verified). verified=True faqat ishonchli manba tasdiqlaganda."""
        result: dict[str, tuple[str, bool]] = {}
        soft_free: set[str] = set()
        pending = list(names)
        for ck in self.checkers:
            if not pending:
                break
            if ck.authoritative and not ck.available(len(pending)):
                self.last_error = f"{ck.name}: byudjet tugagan yoki token yo'q"
                continue
            try:
                res = await ck.check_batch(pending)
            except Exception as e:
                log.warning("%s xato: %s", ck.name, e)
                self.last_error = f"{ck.name}: {str(e)[:150]}"
                continue
            if ck.authoritative and all(v == UNKNOWN for v in res.values()):
                why = getattr(ck, "last_reason", "")
                self.last_error = f"{ck.name}: barcha natijalar noma'lum" + (f" — {why}" if why else "")
            nxt = []
            for n in pending:
                st = res.get(n, UNKNOWN)
                if st == TAKEN:
                    result[n] = (TAKEN, True)
                elif st == FREE and ck.authoritative:
                    result[n] = (FREE, True)
                else:
                    if st == FREE:
                        soft_free.add(n)
                    nxt.append(n)
            pending = nxt
        for n in pending:
            result[n] = (FREE, False) if n in soft_free else (UNKNOWN, False)
        return result
