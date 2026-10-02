"""Qidiruv ishchisi: nomzodlarni tekshiradi, FAQAT tasdiqlangan bo'shi uchun Stars yechadi."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import AsyncIterator

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import ai as ai_mod
from checkers import FREE, TAKEN, UNKNOWN
from generator import iter_candidates

log = logging.getLogger(__name__)


class Job:
    def __init__(self):
        self.stop = False


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


class Searcher:
    def __init__(self, cfg, store, pipeline, max_parallel: int = 3):
        self.cfg, self.store, self.pipeline = cfg, store, pipeline
        self.active: dict[int, Job] = {}
        self.sem = asyncio.Semaphore(max_parallel)

    def stop(self, uid: int) -> bool:
        job = self.active.get(uid)
        if job:
            job.stop = True
        return bool(job)

    async def run(self, bot, chat_id: int, uid: int, batches: AsyncIterator[list[str]], title: str, max_found: int | None = None):
        if uid in self.active:
            await bot.send_message(chat_id, "Sizda faol qidiruv bor. Avval uni to'xtating.")
            return
        job = Job()
        self.active[uid] = job
        price = int(self.store.settings["price_per_found"])
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ To'xtatish", callback_data="stop")]])
        msg = await bot.send_message(chat_id, f"🔎 {title}\nBoshlanmoqda…", reply_markup=kb)
        checked = found = unknown_streak = 0
        charged = 0
        reason = "nomzodlar tugadi"
        try:
            async with self.sem:
                done = False
                async for batch in batches:
                    if job.stop:
                        reason = "to'xtatildi"
                        break
                    res = await self.pipeline.check(batch)
                    checked += len(batch)
                    n_unknown = 0
                    for name, (st, verified) in res.items():
                        if st == TAKEN:
                            self.store.mark_taken(name)
                        elif st == FREE and verified:
                            if not self.store.debit(uid, price, "found", ref=name):
                                reason, done = "balans tugadi", True
                                break
                            found += 1
                            charged += price
                            await bot.send_message(chat_id, f"✅ <code>{html.escape(name)}</code>  (−{price}⭐)")
                            if max_found and found >= max_found:
                                reason, done = "so'ralgan miqdor topildi", True
                                break
                        elif st == UNKNOWN or not verified:
                            n_unknown += 1
                    if done:
                        break
                    unknown_streak = unknown_streak + 1 if n_unknown == len(batch) else 0
                    if unknown_streak >= 3:
                        reason = "tekshiruv xizmati javob bermayapti"
                        break
                    if self.store.balance(uid) < price:
                        reason = "balans tugadi"
                        break
                    if checked >= self.cfg.max_checks:
                        reason = "tekshiruv limiti tugadi"
                        break
                    try:
                        await bot.edit_message_text(
                            f"🔎 {title}\nTekshirildi: {checked} | Topildi: {found}\nBalans: {self.store.balance(uid)}⭐",
                            chat_id=chat_id, message_id=msg.message_id, reply_markup=kb)
                    except TelegramBadRequest:
                        pass
        except Exception as e:  # noqa: BLE001
            log.exception("qidiruv xatosi")
            reason = f"xato: {e}"
        finally:
            self.active.pop(uid, None)
        try:
            await bot.edit_message_text(f"🏁 {title}\nTekshirildi: {checked} | Topildi: {found}", chat_id=chat_id, message_id=msg.message_id)
        except TelegramBadRequest:
            pass
        await bot.send_message(
            chat_id,
            f"Qidiruv yakunlandi: {reason}.\nTopildi: {found} ta, yechildi: {charged}⭐\nBalans: {self.store.balance(uid)}⭐")
