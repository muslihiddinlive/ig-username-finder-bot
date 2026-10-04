"""Qidiruv ishchisi: nomzodlarni tekshiradi, FAQAT tasdiqlangan bo'shi uchun Stars yechadi.

To'xtatish real-time: klaviatura tugmasi bosilishi bilan davom etayotgan tekshiruv so'rovi ham bekor qilinadi.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from typing import AsyncIterator

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

import ai as ai_mod
from checkers import FREE, TAKEN, UNKNOWN
from generator import iter_candidates

log = logging.getLogger(__name__)

STOP_TEXT = "⏹ To'xtatish"
STOP_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=STOP_TEXT)]], resize_keyboard=True)
_STOPPED = object()
_DONE = object()


class Job:
    def __init__(self):
        self.stop = False
        self.event = asyncio.Event()

    def request_stop(self):
        self.stop = True
        self.event.set()


async def spec_batches(cfg, store, specs) -> AsyncIterator[list[str]]:
    per = max(1, cfg.max_checks // max(1, len(specs)))
    gens = [iter_candidates(s, per) for s in specs]
    buf: list[str] = []
    while gens:
        for g in list(gens):
            try:
                n = next(g)
            except StopIteration:
                gens.remove(g)
                continue
            if store.is_taken(n):
                continue
            buf.append(n)
            if len(buf) >= cfg.chunk_size:
                yield buf
                buf = []
                await asyncio.sleep(0)
    if buf:
        yield buf


async def ai_batches(cfg, store, request: str) -> AsyncIterator[list[str]]:
    avoid: list[str] = []
    for _ in range(cfg.ai_rounds):
        cands = await ai_mod.generate(cfg, request, avoid)
        avoid += cands
        cands = [c for c in cands if not store.is_taken(c)]
        if cands:
            yield cands


async def _next(it):
    try:
        return await it.__anext__()
    except StopAsyncIteration:
        return _DONE


class Searcher:
    def __init__(self, cfg, store, pipeline, max_parallel: int = 3):
        self.cfg, self.store, self.pipeline = cfg, store, pipeline
        self.active: dict[int, Job] = {}
        self.sem = asyncio.Semaphore(max_parallel)

    def _bal(self, uid: int) -> str:
        return "∞" if self.store.is_superadmin(uid) else f"{self.store.balance(uid)}⭐"

    def stop(self, uid: int) -> bool:
        job = self.active.get(uid)
        if job:
            job.request_stop()
        return bool(job)

    async def _race(self, job: Job, aw):
        """aw ni bajaradi, lekin to'xtatish so'ralsa darrov bekor qilib _STOPPED qaytaradi."""
        task = asyncio.ensure_future(aw)
        waiter = asyncio.ensure_future(job.event.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if task.done() and not (job.stop and task.cancelled()):
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return _STOPPED

    async def run(self, bot, chat_id: int, uid: int, batches: AsyncIterator[list[str]], title: str, max_found: int | None = None):
        if uid in self.active:
            await bot.send_message(chat_id, "Sizda faol qidiruv bor. Klaviaturadagi «⏹ To'xtatish» tugmasini bosing.")
            return
        job = Job()
        self.active[uid] = job
        price = self.store.price_for(uid)
        msg = await bot.send_message(chat_id, f"🔎 {title}\nBoshlanmoqda… To'xtatish uchun pastdagi tugmani bosing.", reply_markup=STOP_KB)
        checked = unknown = processed = found = unknown_streak = charged = 0
        reason = "nomzodlar tugadi"
        detail = ""
        it = batches.__aiter__()
        try:
            async with self.sem:
                done = False
                while not done:
                    batch = await self._race(job, _next(it))
                    if batch is _STOPPED:
                        reason = "to'xtatildi"
                        break
                    if batch is _DONE:
                        break
                    if not self.pipeline.has_verifier(len(batch)):
                        reason = "ishonchli tekshiruvchi mavjud emas (Apify byudjeti tugagan yoki token yo'q)"
                        break
                    res = await self._race(job, self.pipeline.check(batch))
                    if res is _STOPPED:
                        reason = "to'xtatildi"
                        break
                    processed += len(batch)
                    conclusive = sum(1 for st, v in res.values() if st == TAKEN or (st == FREE and v))
                    checked += conclusive
                    n_unknown = len(batch) - conclusive
                    unknown += n_unknown
                    for name, (st, verified) in res.items():
                        if job.stop:
                            reason, done = "to'xtatildi", True
                            break
                        if st == TAKEN:
                            self.store.mark_taken(name)
                        elif st == FREE and verified:
                            if price and not self.store.debit(uid, price, "found", ref=name):
                                reason, done = "balans tugadi", True
                                break
                            found += 1
                            charged += price
                            await bot.send_message(chat_id, f"✅ <code>{html.escape(name)}</code>" + (f"  (−{price}⭐)" if price else ""))
                            if max_found and found >= max_found:
                                reason, done = "so'ralgan miqdor topildi", True
                                break
                    if done:
                        break
                    unknown_streak = unknown_streak + 1 if n_unknown == len(batch) else 0
                    if unknown_streak >= 3:
                        reason = "tekshiruv xizmati javob bermayapti"
                        detail = self.pipeline.last_error
                        break
                    if price and self.store.balance(uid) < price:
                        reason = "balans tugadi"
                        break
                    if processed >= self.cfg.max_checks:
                        reason = "tekshiruv limiti tugadi"
                        break
                    with contextlib.suppress(TelegramBadRequest):
                        await bot.edit_message_text(
                            f"🔎 {title}\nTekshirildi: {checked} | Noma'lum: {unknown} | Topildi: {found}\nBalans: {self._bal(uid)}",
                            chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:  # noqa: BLE001
            log.exception("qidiruv xatosi")
            reason = f"xato: {e}"
        finally:
            self.active.pop(uid, None)
            aclose = getattr(it, "aclose", None)
            if aclose:
                with contextlib.suppress(Exception):
                    await aclose()
        with contextlib.suppress(TelegramBadRequest):
            await bot.edit_message_text(f"🏁 {title}\nTekshirildi: {checked} | Noma'lum: {unknown} | Topildi: {found}", chat_id=chat_id, message_id=msg.message_id)
        await bot.send_message(
            chat_id,
            f"Qidiruv yakunlandi: {reason}.\nAniq tekshirildi: {checked}, noma'lum: {unknown}\nTopildi: {found} ta, yechildi: {charged}⭐\nBalans: {self._bal(uid)}"
            + (f"\n🔧 Sabab (adminlar uchun): {html.escape(detail)}" if detail and self.store.is_admin(uid) else ""),
            reply_markup=ReplyKeyboardRemove())
