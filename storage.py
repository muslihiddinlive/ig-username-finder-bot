"""Ma'lumotlar bazasi: RAM + Telegram kanalidagi pinned snapshot (JSON fayl).

Eslatma: botlar kanal tarixini o'qiy olmaydi, shuning uchun qayta tiklash faqat
oxirgi pinned snapshot orqali. Log xabarlari faqat audit uchun.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {"price_per_found": 1, "packages": [10, 25, 50, 100], "vip_price": 100, "vip_days": 30}
TAKEN_TTL = 3 * 86400
CACHE_MAX = 30000


class Store:
    def __init__(self, superadmins: set[int] | None = None):
        self.d = {
            "users": {},
            "settings": dict(DEFAULT_SETTINGS),
            "admins": [],
            "taken": {},
            "payments": {},
            "apify": {"month": "", "used": 0},
        }
        self.superadmins = set(superadmins or ())
        self.dirty = False
        self.log_queue: list[str] = []

    # --- yordamchi
    def _touch(self):
        self.dirty = True

    def log(self, kind: str, **kw):
        self.log_queue.append(f"{time.strftime('%F %T', time.gmtime())} {kind} {json.dumps(kw, ensure_ascii=False)}")

    # --- foydalanuvchilar va balans
    def user(self, uid: int, name: str = "") -> dict:
        k = str(uid)
        u = self.d["users"].get(k)
        if u is None:
            u = {"balance": 0, "vip_until": 0, "name": name, "joined": int(time.time())}
            self.d["users"][k] = u
            self._touch()
        return u

    def balance(self, uid: int) -> int:
        return self.user(uid)["balance"]

    def credit(self, uid: int, amount: int, kind: str = "topup", ref: str = ""):
        u = self.user(uid)
        u["balance"] += amount
        self._touch()
        self.log(kind, uid=uid, amount=amount, ref=ref, bal=u["balance"])

    def debit(self, uid: int, amount: int, kind: str = "found", ref: str = "") -> bool:
        """Atomar: asyncio bitta oqimda ishlagani uchun await bo'lmagan joyda poyga yo'q."""
        u = self.user(uid)
        if amount < 0 or u["balance"] < amount:
            return False
        u["balance"] -= amount
        self._touch()
        self.log(kind, uid=uid, amount=-amount, ref=ref, bal=u["balance"])
        return True

    # --- VIP
    def is_vip(self, uid: int) -> bool:
        return self.is_superadmin(uid) or self.user(uid)["vip_until"] > time.time()

    def price_for(self, uid: int) -> int:
        """Superadmin uchun 0 (cheksiz), boshqalar uchun admin belgilagan narx."""
        return 0 if self.is_superadmin(uid) else int(self.settings["price_per_found"])

    def grant_vip(self, uid: int, days: int):
        u = self.user(uid)
        start = max(u["vip_until"], time.time())
        u["vip_until"] = int(start + days * 86400)
        self._touch()

    # --- sozlamalar / rollar
    @property
    def settings(self) -> dict:
        return self.d["settings"]

    def set_setting(self, k: str, v):
        self.d["settings"][k] = v
        self._touch()

    def is_superadmin(self, uid: int) -> bool:
        return uid in self.superadmins

    def is_admin(self, uid: int) -> bool:
        return self.is_superadmin(uid) or uid in self.d["admins"]

    def add_admin(self, uid: int):
        if uid not in self.d["admins"]:
            self.d["admins"].append(uid)
            self._touch()

    def del_admin(self, uid: int):
        if uid in self.d["admins"]:
            self.d["admins"].remove(uid)
            self._touch()

    # --- band username keshi (faqat 'taken')
    def is_taken(self, name: str) -> bool:
        ts = self.d["taken"].get(name)
        return bool(ts and time.time() - ts < TAKEN_TTL)

    def mark_taken(self, name: str):
        t = self.d["taken"]
        t[name] = int(time.time())
        if len(t) > CACHE_MAX:
            for k, _ in sorted(t.items(), key=lambda kv: kv[1])[: CACHE_MAX // 10]:
                t.pop(k, None)
        self._touch()

    # --- Apify oylik byudjet
    def apify_left(self, budget: int) -> int:
        a = self.d["apify"]
        month = time.strftime("%Y-%m")
        if a["month"] != month:
            a["month"], a["used"] = month, 0
        return max(0, budget - a["used"])

    def apify_spend(self, n: int):
        self.d["apify"]["used"] = max(0, self.d["apify"]["used"] + n)
        self._touch()

    # --- to'lovlar (dublikatdan himoya)
    def seen_payment(self, charge_id: str) -> bool:
        return charge_id in self.d["payments"]

    def record_payment(self, charge_id: str, uid: int, amount: int):
        self.d["payments"][charge_id] = {"uid": uid, "amount": amount, "ts": int(time.time())}
        self._touch()

    # --- serializatsiya
    def dump_json(self) -> str:
        return json.dumps(self.d, ensure_ascii=False, separators=(",", ":"))

    def load_json(self, raw: str):
        loaded = json.loads(raw)
        loaded["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}
        for k, v in self.d.items():
            loaded.setdefault(k, v)
        self.d = loaded


class ChannelPersistence:
    def __init__(self, bot, channel_id: int, store: Store, interval: int = 10, local_path: str = "data/snapshot.json"):
        self.bot, self.channel_id, self.store = bot, channel_id, store
        self.interval, self.local_path = interval, local_path
        self.snap_id: int | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> str:
        if self.channel_id:
            try:
                chat = await self.bot.get_chat(self.channel_id)
                pm = chat.pinned_message
                if pm and pm.document:
                    buf = await self.bot.download(pm.document.file_id)
                    self.store.load_json(buf.read().decode())
                    self.snap_id = pm.message_id
                    return "channel"
            except Exception:
                log.exception("kanaldan snapshot o'qilmadi")
        if os.path.exists(self.local_path):
            self.store.load_json(open(self.local_path, encoding="utf-8").read())
            return "local"
        return "fresh"

    async def flush(self):
        from aiogram.types import BufferedInputFile

        async with self._lock:
            payload = self.store.dump_json()
            self.store.dirty = False
            try:
                os.makedirs(os.path.dirname(self.local_path) or ".", exist_ok=True)
                with open(self.local_path, "w", encoding="utf-8") as f:
                    f.write(payload)
            except OSError:
                pass
            if not self.channel_id:
                return
            msg = await self.bot.send_document(
                self.channel_id,
                BufferedInputFile(payload.encode(), filename="snapshot.json"),
                caption="snapshot " + time.strftime("%F %T", time.gmtime()),
                disable_notification=True,
            )
            pinned = False
            try:
                await self.bot.pin_chat_message(self.channel_id, msg.message_id, disable_notification=True)
                pinned = True
            except Exception:
                log.exception("snapshot pin qilinmadi (botga 'Pin messages' huquqi bering)")
            if pinned and self.snap_id:
                try:
                    await self.bot.delete_message(self.channel_id, self.snap_id)
                except Exception:
                    pass
            if pinned:
                self.snap_id = msg.message_id

    async def _drain_log(self):
        if not (self.channel_id and self.store.log_queue):
            self.store.log_queue.clear() if not self.channel_id else None
            return
        lines, self.store.log_queue = self.store.log_queue, []
        chunk = ""
        for ln in lines:
            if len(chunk) + len(ln) + 1 > 3900:
                await self.bot.send_message(self.channel_id, chunk, disable_notification=True)
                chunk = ""
            chunk += ln + "\n"
        if chunk:
            await self.bot.send_message(self.channel_id, chunk, disable_notification=True)

    async def run(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                if self.store.dirty:
                    await self.flush()
                await self._drain_log()
            except Exception:
                log.exception("persistence xatosi")
