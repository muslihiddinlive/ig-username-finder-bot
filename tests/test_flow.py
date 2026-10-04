"""Handlerlarning to'liq oqimi: soxta Telegram sessiyasi bilan haqiqiy aiogram Dispatcher orqali."""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message

import ai as ai_mod
from bot import Ctx, build_router
from checkers import FREE, TAKEN, Pipeline
from search import Searcher
from storage import Store
from tests.test_core import Fake, mkcfg

NOW = 1_700_000_000
BOT_USER = {"id": 99, "is_bot": True, "first_name": "B"}


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendInvoice", "EditMessageText", "SendDocument"):
            return Message(message_id=len(self.calls) + 100, date=datetime.now(), chat=Chat(id=5, type="private")).as_(bot)
        return True

    def texts(self):
        return [getattr(c, "text", "") or "" for c in self.calls if type(c).__name__ == "SendMessage"]

    def alerts(self):
        return [c for c in self.calls if type(c).__name__ == "AnswerCallbackQuery" and getattr(c, "show_alert", False)]


class Env:
    def __init__(self, uid=5, superadmins=(), balance=10, price=1, free=(), cfg=None, delay=0, fail=False):
        self.uid = uid
        self.cfg = cfg or mkcfg()
        self.store = Store(set(superadmins))
        self.store.set_setting("price_per_found", price)
        if balance:
            self.store.credit(uid, balance)
        fr = set(free)

        async def cb(names):
            if fail:
                raise RuntimeError('apify HTTP 402: kredit tugagan')
            if delay:
                await asyncio.sleep(delay)
            return {n: (FREE if n in fr else TAKEN) for n in names}

        api = Fake("api", {}, True)
        api.check_batch = cb
        self.pipeline = Pipeline([api])
        self.searcher = Searcher(self.cfg, self.store, self.pipeline)
        self.session = FakeSession()
        self.bot = Bot("123456:ABCDEF", session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(build_router(Ctx(self.cfg, self.store, None, self.pipeline, self.searcher)))
        self.n = 0

    def _user(self):
        return {"id": self.uid, "is_bot": False, "first_name": "T"}

    async def say(self, text):
        self.n += 1
        msg = {"message_id": self.n, "date": NOW, "chat": {"id": self.uid, "type": "private"}, "from": self._user(), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.dp.feed_raw_update(self.bot, {"update_id": self.n, "message": msg})

    async def press(self, data):
        self.n += 1
        cq = {"id": str(self.n), "from": self._user(), "chat_instance": "c", "data": data,
              "message": {"message_id": 500, "date": NOW, "chat": {"id": self.uid, "type": "private"}, "from": BOT_USER, "text": "x"}}
        await self.dp.feed_raw_update(self.bot, {"update_id": self.n, "callback_query": cq})

    async def wait_search(self, timeout=10):
        await asyncio.sleep(0.05)
        for _ in range(int(timeout / 0.05)):
            if not self.searcher.active:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("qidiruv tugamadi")


def run(coro):
    return asyncio.run(coro)


def test_full_search_flow_custom_limit_and_charge():
    async def go():
        e = Env(free={"uz111", "uz222"}, balance=10, price=1)
        await e.say("/start")
        await e.press("menu:search")
        await e.press("m:starts")
        await e.say("uz")
        await e.press("lim:custom")
        await e.say("5")
        await e.press("cs:digit")      # AttributeError bo'lgan joy
        await e.press("cs:go")
        await e.wait_search()
        found = [t for t in e.session.texts() if t.startswith("✅")]
        assert len(found) == 2 and e.store.balance(5) == 8
        assert any("Qidiruv yakunlandi" in t for t in e.session.texts())
    run(go())


def test_limit_exceeded_alert_and_preset_6():
    async def go():
        e = Env(free={"uzbaaa"})
        await e.say("/start")
        await e.press("menu:search")
        await e.press("m:starts")
        await e.say("uzb")
        await e.press("lim:preset")    # 6 -> bo'sh joy = 3
        for t in ("cs:letter", "cs:digit", "cs:us"):
            await e.press(t)
        assert not e.session.alerts()
        await e.press("cs:dot")        # 4-tur 3 ta joyga sig'maydi
        assert any("limitingizdan oshib ketdingiz" in (a.text or "") for a in e.session.alerts())
    run(go())


def test_mode_both_and_any_limit():
    async def go():
        e = Env(free={"uz12dev"}, balance=3, price=1)
        await e.say("/start")
        await e.press("menu:search")
        await e.press("m:both")
        await e.say("uz")
        await e.say("dev")
        await e.press("lim:any")
        await e.press("cs:digit")
        await e.press("cs:go")
        await e.wait_search()
        assert any(t.startswith("✅") and "uz12dev" in t for t in e.session.texts())
    run(go())


def test_superadmin_free_and_admin_panel_inputs():
    async def go():
        e = Env(uid=5, superadmins={5}, balance=0, free={"uz111"})
        await e.say("/start")
        assert any("cheksiz" in t for t in e.session.texts())
        await e.say("/admin")
        await e.press("adm:price")
        await e.say("7")
        assert e.store.settings["price_per_found"] == 7
        await e.press("adm:packages")
        await e.say("5, 20 50")
        assert e.store.settings["packages"] == [5, 20, 50]
        await e.press("adm:addadmin")
        await e.say("424242")
        assert e.store.is_admin(424242)
        await e.press("adm:grant")
        await e.say("777 15")
        assert e.store.balance(777) == 15
        await e.press("adm:stats")
        await e.press("menu:search")
        await e.press("m:starts")
        await e.say("uz")
        await e.press("lim:custom")
        await e.say("5")
        await e.press("cs:digit")
        await e.press("cs:go")
        await e.wait_search()
        assert any(t.startswith("✅") and "(−" not in t for t in e.session.texts()) and e.store.balance(5) == 0
    run(go())


def test_non_admin_cannot_use_panel():
    async def go():
        e = Env(uid=6, balance=5)
        await e.say("/admin")
        await e.press("adm:price")
        assert e.store.settings["price_per_found"] == 1
        assert any(a for a in e.session.alerts())
    run(go())


def test_bai_flow_with_mocked_ai(monkeypatch):
    async def fake_generate(cfg, request, avoid):
        return ["uzbdev", "uzbcode"]

    monkeypatch.setattr(ai_mod, "generate", fake_generate)

    async def go():
        e = Env(uid=5, superadmins={5}, balance=0, free={"uzbdev"})
        await e.press("menu:ai")
        await e.say("6 xonali uzb developer")
        await e.wait_search()
        assert any(t.startswith("✅") and "uzbdev" in t for t in e.session.texts())
    run(go())


def test_stop_button_cancels_running_search_in_realtime():
    import time
    from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove
    from search import STOP_TEXT

    async def go():
        e = Env(free={"uz111"}, delay=30)   # tekshiruv so'rovi 30 soniya "qotib" qoladi
        await e.say("/start")
        await e.press("menu:search")
        await e.press("m:starts")
        await e.say("uz")
        await e.press("lim:custom")
        await e.say("5")
        await e.press("cs:digit")
        await e.press("cs:go")
        await asyncio.sleep(0.2)
        assert 5 in e.searcher.active
        t0 = time.time()
        await e.say(STOP_TEXT)
        await e.wait_search(timeout=3)
        assert time.time() - t0 < 2                      # 30 soniya kutmadi
        sends = [c for c in e.session.calls if type(c).__name__ == "SendMessage"]
        assert any(isinstance(c.reply_markup, ReplyKeyboardMarkup) and c.text.startswith("🔎") for c in sends)
        assert isinstance(sends[-1].reply_markup, ReplyKeyboardRemove) and "to'xtatildi" in sends[-1].text
        assert e.store.balance(5) == 10                  # pul yechilmadi
    run(go())


def test_stop_button_without_active_search_and_budget_message():
    from aiogram.types import ReplyKeyboardRemove
    from search import STOP_TEXT

    async def go():
        e = Env()
        await e.say(STOP_TEXT)
        last = [c for c in e.session.calls if type(c).__name__ == "SendMessage"][-1]
        assert "Faol qidiruv yo'q" in last.text and isinstance(last.reply_markup, ReplyKeyboardRemove)
    run(go())


def test_checker_failure_is_reported_to_admin_not_user_and_not_counted_as_checked():
    async def go(uid, sup):
        e = Env(uid=uid, superadmins=sup, balance=5, fail=True)
        await e.say("/start")
        await e.press("menu:search")
        await e.press("m:starts")
        await e.say("uz")
        await e.press("lim:custom")
        await e.say("5")
        await e.press("cs:digit")
        await e.press("cs:go")
        await e.wait_search()
        return e.session.texts()[-1], e.store.balance(uid)
    admin_text, _ = run(go(5, {5}))
    assert "Aniq tekshirildi: 0" in admin_text and "402" in admin_text and "Sabab" in admin_text
    user_text, bal = run(go(6, set()))
    assert "Aniq tekshirildi: 0" in user_text and "402" not in user_text and bal == 5
