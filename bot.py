from __future__ import annotations

import asyncio
import html
import re
import secrets
import time
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message,
                           PreCheckoutQuery, ReplyKeyboardRemove)

from generator import SearchSpec, normalize_word, is_valid_username
from search import STOP_TEXT, Searcher, ai_batches, spec_batches

ANY_FREE_SLOTS = (2, 3, 4)
TYPE_LABELS = {"letter": "Harf (a-z)", "digit": "Raqam", "us": "_ pastki chiziq", "dot": ". nuqta"}


class A(StatesGroup):
    input = State()


class S(StatesGroup):
    word = State()
    word2 = State()
    custom = State()
    ai = State()


@dataclass
class Ctx:
    cfg: object
    store: object
    persist: object
    pipeline: object
    searcher: Searcher
    checkers: list = field(default_factory=list)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


def menu_kb(vip: bool):
    return kb([[("🔎 Qidirish", "menu:search")], [("💰 Balansni to'ldirish", "menu:topup")],
               [("🤖 bAI (VIP)" + (" ✅" if vip else ""), "menu:ai")]])


def valid_word(w: str) -> bool:
    return 0 < len(w) <= 28 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789._" for c in w)


def build_router(ctx: Ctx) -> Router:
    r = Router()
    st, cfg = ctx.store, ctx.cfg

    def price() -> int:
        return int(st.settings["price_per_found"])

    async def show_menu(m: Message, uid: int):
        u = st.user(uid, m.from_user.full_name if m.from_user else "")
        if st.is_superadmin(uid):
            return await m.answer("Salom, superadmin! Qidiruv va bAI siz uchun <b>cheksiz va bepul</b>. Boshqaruv: /admin",
                                  reply_markup=menu_kb(True))
        await m.answer(f"Salom! Balans: <b>{u['balance']}⭐</b>\nBitta topilgan bo'sh username: <b>{price()}⭐</b> "
                       f"(topilmasa pul yechilmaydi).", reply_markup=menu_kb(st.is_vip(uid)))

    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await show_menu(m, m.from_user.id)

    @r.message(F.text == STOP_TEXT)
    async def stop_button(m: Message):
        """Klaviatura tugmasi: faol qidiruvni darrov to'xtatadi (admin ham, oddiy user ham)."""
        if ctx.searcher.stop(m.from_user.id):
            await m.answer("⏹ To'xtatilmoqda…")
        else:
            await m.answer("Faol qidiruv yo'q.", reply_markup=ReplyKeyboardRemove())

    # ---------- to'ldirish ----------
    async def send_topup(m: Message):
        rows = [[(f"{p}⭐", f"pay:{p}")] for p in st.settings["packages"]]
        await m.answer("Hisobni to'ldirish — paketni tanlang (Telegram Stars):", reply_markup=kb(rows))

    @r.callback_query(F.data == "menu:topup")
    async def topup(c: CallbackQuery):
        await c.answer()
        await send_topup(c.message)

    @r.callback_query(F.data.startswith("pay:"))
    async def pay(c: CallbackQuery):
        amount = int(c.data.split(":")[1])
        if amount not in st.settings["packages"]:
            return await c.answer("Paket topilmadi", show_alert=True)
        await c.answer()
        await c.bot.send_invoice(c.message.chat.id, title="Balans to'ldirish", description=f"{amount}⭐ balansga",
                                 payload=f"topup:{amount}", currency="XTR", prices=[LabeledPrice(label=f"{amount}⭐", amount=amount)])

    @r.callback_query(F.data == "buyvip")
    async def buyvip(c: CallbackQuery):
        await c.answer()
        p, d = int(st.settings["vip_price"]), int(st.settings["vip_days"])
        await c.bot.send_invoice(c.message.chat.id, title="VIP tarif", description=f"bAI — {d} kun",
                                 payload="vip", currency="XTR", prices=[LabeledPrice(label="VIP", amount=p)])

    @r.pre_checkout_query()
    async def pre(q: PreCheckoutQuery):
        await q.answer(ok=True)

    @r.message(F.successful_payment)
    async def paid(m: Message):
        sp = m.successful_payment
        cid = sp.telegram_payment_charge_id
        if st.seen_payment(cid):
            return
        st.record_payment(cid, m.from_user.id, sp.total_amount)
        if sp.invoice_payload == "vip":
            st.grant_vip(m.from_user.id, int(st.settings["vip_days"]))
            text = "✅ VIP faollashtirildi."
        else:
            st.credit(m.from_user.id, sp.total_amount, "topup", ref=cid)
            text = f"✅ Balans to'ldirildi: +{sp.total_amount}⭐. Hozirgi balans: {st.balance(m.from_user.id)}⭐"
        await ctx.persist.flush()  # pul yozuvi kanalga tushmaguncha tasdiqlamaymiz
        await m.answer(text, reply_markup=menu_kb(st.is_vip(m.from_user.id)))

    # ---------- qidiruv oqimi ----------
    @r.callback_query(F.data == "menu:search")
    async def menu_search(c: CallbackQuery, state: FSMContext):
        await c.answer()
        if st.balance(c.from_user.id) < st.price_for(c.from_user.id):
            await c.message.answer(f"Balans yetarli emas (kamida {price()}⭐ kerak). Avval to'ldiring.")
            return await send_topup(c.message)
        await state.clear()
        await c.message.answer("Qanday nom qidirmoqchisiz?", reply_markup=kb([
            [("Boshi ma'lum so'z bilan", "m:starts")], [("Oxiri ma'lum so'z bilan", "m:ends")], [("Ikkalasi ham", "m:both")]]))

    @r.callback_query(F.data.startswith("m:"))
    async def pick_mode(c: CallbackQuery, state: FSMContext):
        mode = c.data[2:]
        await state.update_data(mode=mode)
        await state.set_state(S.word)
        await c.answer()
        ask = {"starts": "Qaysi so'z bilan BOSHLANSIN? (masalan: UZB)", "ends": "Qaysi so'z bilan TUGASIN?",
               "both": "Avval BOSHLANISH so'zini yozing (masalan: UZB):"}[mode]
        await c.message.answer(ask)

    async def ask_limit(m: Message, state: FSMContext):
        d = await state.get_data()
        fixed = len(d["word"]) + len(d.get("word2", ""))
        await state.update_data(fixed=fixed)
        await m.answer(f"Belgi limiti qancha bo'lsin? (so'z {fixed} belgi)", reply_markup=kb([
            [("Farqi yo'q", "lim:any"), (str(cfg.preset_limit), "lim:preset"), ("O'zim kiritaman", "lim:custom")]]))

    @r.message(S.word, ~F.text.startswith("/"))
    async def got_word(m: Message, state: FSMContext):
        w = normalize_word(m.text or "")
        if not valid_word(w):
            return await m.answer("Faqat a-z, 0-9, _ va . ishlatiladi (1–28 belgi). Qayta yozing:")
        await state.update_data(word=w)
        if (await state.get_data())["mode"] == "both":
            await state.set_state(S.word2)
            return await m.answer("Endi TUGASH so'zini yozing:")
        await state.set_state(None)
        await ask_limit(m, state)

    @r.message(S.word2, ~F.text.startswith("/"))
    async def got_word2(m: Message, state: FSMContext):
        w = normalize_word(m.text or "")
        if not valid_word(w):
            return await m.answer("Faqat a-z, 0-9, _ va . ishlatiladi (1–28 belgi). Qayta yozing:")
        await state.update_data(word2=w)
        await state.set_state(None)
        await ask_limit(m, state)

    async def ask_types(m: Message, state: FSMContext):
        d = await state.get_data()
        sel = set(d.get("types", []))
        rows = [[(("✅ " if k in sel else "") + lbl, f"cs:{k}")] for k, lbl in TYPE_LABELS.items()]
        rows.append([("Davom etish ▶️", "cs:go")])
        word = d["word"] + ("…" + d["word2"] if d.get("word2") else "")
        await m.answer(f"«{html.escape(word)}» dan tashqari qanday belgilar bo'lsin? (bir nechtasini tanlash mumkin)", reply_markup=kb(rows))

    @r.callback_query(F.data.startswith("lim:"))
    async def pick_limit(c: CallbackQuery, state: FSMContext):
        d = await state.get_data()
        kind = c.data[4:]
        await c.answer()
        if kind == "custom":
            await state.set_state(S.custom)
            return await c.message.answer("Limitni chatga raqam bilan yozing (masalan: 10):")
        limit = None if kind == "any" else cfg.preset_limit
        if limit is not None and limit <= d["fixed"]:
            return await c.message.answer(f"So'z {d['fixed']} belgi, limit {limit} dan kichik bo'lolmaydi. Boshqa limit tanlang.")
        await state.update_data(limit=limit, types=[])
        await ask_types(c.message, state)

    @r.message(S.custom, ~F.text.startswith("/"))
    async def got_custom(m: Message, state: FSMContext):
        d = await state.get_data()
        t = (m.text or "").strip()
        if not t.isdigit() or not (d["fixed"] < int(t) <= 30):
            return await m.answer(f"Raqam {d['fixed'] + 1} dan 30 gacha bo'lsin. Qayta yozing:")
        await state.set_state(None)
        await state.update_data(limit=int(t), types=[])
        await ask_types(m, state)

    @r.callback_query(F.data.startswith("cs:"))
    async def toggle_type(c: CallbackQuery, state: FSMContext):
        d = await state.get_data()
        key = c.data[3:]
        if key == "go":
            if not d.get("types"):
                return await c.answer("Kamida bitta belgi turini tanlang", show_alert=True)
            await c.answer()
            return await launch_search(c, state)
        sel = set(d.get("types", []))
        if key in sel:
            sel.discard(key)
        else:
            sel.add(key)
            free = None if d.get("limit") is None else d["limit"] - d["fixed"]
            if cfg.require_each_type and free is not None and len(sel) > free:
                return await c.answer(f"⚠️ Kiritgan limitingizdan oshib ketdingiz: {free} ta bo'sh joyga {len(sel)} xil belgi sig'maydi.", show_alert=True)
        await state.update_data(types=sorted(sel))
        rows = [[(("✅ " if k in sel else "") + lbl, f"cs:{k}")] for k, lbl in TYPE_LABELS.items()] + [[("Davom etish ▶️", "cs:go")]]
        await c.message.edit_reply_markup(reply_markup=kb(rows))
        await c.answer()

    async def launch_search(c: CallbackQuery, state: FSMContext):
        d = await state.get_data()
        frees = ANY_FREE_SLOTS if d.get("limit") is None else (d["limit"] - d["fixed"],)
        specs = [SearchSpec(d["mode"], d["word"], d.get("word2", ""), f, tuple(d["types"]), cfg.require_each_type) for f in frees]
        specs = [s for s in specs if s.feasible()]
        await state.clear()
        if not specs:
            return await c.message.answer("Bu sozlama bilan nomzod yo'q (belgi turlari bo'sh joyga sig'maydi). Boshqatdan urinib ko'ring: /start")
        title = "Qidiruv: " + d["word"] + ("…" + d["word2"] if d.get("word2") else "")
        asyncio.create_task(ctx.searcher.run(c.bot, c.message.chat.id, c.from_user.id, spec_batches(cfg, st, specs), title))

    @r.callback_query(F.data == "stop")
    async def stop(c: CallbackQuery):  # eski xabarlardagi inline tugma uchun
        await c.answer("To'xtatilmoqda…" if ctx.searcher.stop(c.from_user.id) else "Faol qidiruv yo'q")

    # ---------- bAI ----------
    @r.callback_query(F.data == "menu:ai")
    async def menu_ai(c: CallbackQuery, state: FSMContext):
        await c.answer()
        if not st.is_vip(c.from_user.id):
            return await c.message.answer(f"bAI — VIP tarif: {st.settings['vip_price']}⭐ / {st.settings['vip_days']} kun.",
                                          reply_markup=kb([[("VIP sotib olish", "buyvip")]]))
        if st.balance(c.from_user.id) < st.price_for(c.from_user.id):
            return await c.message.answer("Balans yetarli emas. /start → to'ldiring.")
        await state.set_state(S.ai)
        await c.message.answer("Qanday username kerak? Erkin yozing. Masalan: «10 xonali, UZB qatnashgan, belgilar kam, developerlarga»")

    @r.message(S.ai, ~F.text.startswith("/"))
    async def ai_request(m: Message, state: FSMContext):
        await state.clear()
        if not st.is_vip(m.from_user.id):
            return await m.answer("VIP muddati tugagan.")
        text = (m.text or "").strip()[:500]
        asyncio.create_task(ctx.searcher.run(m.bot, m.chat.id, m.from_user.id, ai_batches(cfg, st, text), "bAI qidiruvi", max_found=5))

    # ---------- admin ----------
    def adm(m: Message) -> bool:
        return st.is_admin(m.from_user.id)

    def sup(m: Message) -> bool:
        return st.is_superadmin(m.from_user.id)

    def int_arg(o: CommandObject):
        a = (o.args or "").strip()
        return int(a) if a.lstrip("-").isdigit() else None

    @r.message(Command("setprice"))
    async def setprice(m: Message, command: CommandObject):
        n = int_arg(command)
        if not adm(m) or n is None or n < 0:
            return await m.answer("Admin: /setprice N  (bitta topilgan username narxi, ⭐)")
        st.set_setting("price_per_found", n)
        await m.answer(f"Narx: {n}⭐")

    @r.message(Command("setpackages"))
    async def setpackages(m: Message, command: CommandObject):
        try:
            pk = sorted({int(x) for x in (command.args or "").replace(" ", "").split(",") if x})
        except ValueError:
            pk = []
        if not adm(m) or not pk or min(pk) < 1:
            return await m.answer("Admin: /setpackages 10,25,50,100")
        st.set_setting("packages", pk)
        await m.answer(f"Paketlar: {pk}")

    @r.message(Command("setvipprice"))
    async def setvipprice(m: Message, command: CommandObject):
        n = int_arg(command)
        if not sup(m) or n is None or n < 1:
            return await m.answer("Superadmin: /setvipprice N")
        st.set_setting("vip_price", n)
        await m.answer(f"VIP narxi: {n}⭐")

    @r.message(Command("setvipdays"))
    async def setvipdays(m: Message, command: CommandObject):
        n = int_arg(command)
        if not sup(m) or n is None or n < 1:
            return await m.answer("Superadmin: /setvipdays N")
        st.set_setting("vip_days", n)
        await m.answer(f"VIP muddati: {n} kun")

    @r.message(Command("addadmin"))
    async def addadmin(m: Message, command: CommandObject):
        n = int_arg(command)
        if not sup(m) or n is None:
            return await m.answer("Superadmin: /addadmin USER_ID")
        st.add_admin(n)
        await m.answer("Admin qo'shildi")

    @r.message(Command("deladmin"))
    async def deladmin(m: Message, command: CommandObject):
        n = int_arg(command)
        if not sup(m) or n is None:
            return await m.answer("Superadmin: /deladmin USER_ID")
        st.del_admin(n)
        await m.answer("Admin o'chirildi")

    @r.message(Command("grant"))
    async def grant(m: Message, command: CommandObject):
        parts = (command.args or "").split()
        if not adm(m) or len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            return await m.answer("Admin: /grant USER_ID MIQDOR")
        st.credit(int(parts[0]), int(parts[1]), "grant", ref=str(m.from_user.id))
        await m.answer("Balans o'zgartirildi")

    def stats_text() -> str:
        users = st.d["users"]
        return (f"📊 Foydalanuvchilar: {len(users)}\nUmumiy balans: {sum(u['balance'] for u in users.values())}⭐\n"
                f"VIP: {sum(1 for u in users.values() if u['vip_until'] > time.time())}\n"
                f"Apify oy limiti qoldi: {st.apify_left(cfg.apify_monthly_budget)}\n"
                f"Band keshi: {len(st.d['taken'])}\nFaol qidiruvlar: {len(ctx.searcher.active)}")

    def diag_text() -> str:
        p = ctx.persist
        L = ["🔍 <b>Diagnostika</b>"]
        if p is not None:
            L.append(f"DB manbasi (startda): {html.escape(p.source)}")
            L.append("Kanal ID: " + ("sozlangan" if p.channel_id else "<b>YO'Q</b> (faqat lokal fayl, restartda yo'qoladi!)"))
            L.append("Oxirgi muvaffaqiyatli snapshot: " + (time.strftime("%F %T", time.gmtime(p.last_ok)) + " UTC" if p.last_ok else "hali yo'q"))
            if p.last_error:
                L.append(f"Snapshot xatosi: {html.escape(p.last_error)}")
        for ck in ctx.pipeline.checkers:
            L.append(f"\n<b>{ck.name}</b>" + (" (tasdiqlovchi)" if ck.authoritative else " (taxminiy)"))
            dbg = getattr(ck, "last_debug", "")
            if dbg:
                L.append("so'nggi javob: <code>" + html.escape(dbg[:700]) + "</code>")
        L.append(f"\nPipeline oxirgi xato: {html.escape(ctx.pipeline.last_error or '-')}")
        L.append(f"Apify oy limiti qoldi: {st.apify_left(cfg.apify_monthly_budget)}")
        return "\n".join(L)

    async def do_selftest(m: Message):
        """Tekshiruv aniqligini sinash: mashhur (band) va tasodifiy uzun (bo'sh bo'lishi kerak) nomlar."""
        taken = ["instagram", "cristiano", "google", "nike"]
        free = ["zq" + secrets.token_hex(6) + "x" + secrets.token_hex(3), "uz" + secrets.token_hex(7) + "k"]
        res = await ctx.pipeline.check(taken + free)

        def ok(n):
            return (n in taken and res[n][0] == "taken") or (n in free and res[n][0] == "free")

        lines = [f"{'✅' if ok(n) else '❌'} {n}: {res[n][0]}{' (tasdiqlangan)' if res[n][1] else ''}" for n in taken + free]
        await m.answer("Selftest (kutilgan: birinchi 4 ta taken, oxirgi 2 ta free):\n" + "\n".join(lines))
        if any(res[n][0] == "unknown" for n in res):
            await m.answer(diag_text())

    @r.message(Command("stats"))
    async def stats(m: Message):
        if adm(m):
            await m.answer(stats_text())

    @r.message(Command("diag"))
    async def diag(m: Message):
        if adm(m):
            await m.answer(diag_text())

    @r.message(Command("selftest"))
    async def selftest(m: Message):
        if adm(m):
            await do_selftest(m)

    # ---------- /admin paneli ----------
    PROMPTS = {
        "price": "Yangi narxni yozing (1 ta topilgan username uchun ⭐, butun son):",
        "packages": "Paketlarni vergul bilan yozing. Masalan: 10,25,50,100",
        "grant": "Foydalanuvchi ID va miqdorni yozing. Masalan: 123456789 100 (minus ham mumkin)",
        "vipprice": "VIP narxini yozing (⭐):",
        "vipdays": "VIP muddatini yozing (kun):",
        "addadmin": "Yangi admin Telegram ID'sini yozing:",
        "deladmin": "O'chiriladigan admin ID'sini yozing:",
    }
    SUPER_ONLY = {"vipprice", "vipdays", "addadmin", "deladmin", "admins"}

    def panel_text(uid: int) -> str:
        s = st.settings
        t = (f"🛠 <b>Admin panel</b>\nNarx (1 topilma): <b>{s['price_per_found']}⭐</b>\n"
             f"Paketlar: {', '.join(str(p) for p in s['packages'])}\n")
        if st.is_superadmin(uid):
            t += f"VIP: <b>{s['vip_price']}⭐</b> / {s['vip_days']} kun\n"
        return t

    def panel_kb(uid: int):
        rows = [[("📊 Statistika", "adm:stats"), ("🧪 Selftest", "adm:selftest")],
                [("🔍 Diagnostika", "adm:diag")],
                [("💲 Narx", "adm:price"), ("📦 Paketlar", "adm:packages")],
                [("➕ Balans berish", "adm:grant")]]
        if st.is_superadmin(uid):
            rows += [[("💎 VIP narxi", "adm:vipprice"), ("📅 VIP kunlari", "adm:vipdays")], [("👤 Adminlar", "adm:admins")]]
        return kb(rows)

    @r.message(Command("admin"))
    async def admin_cmd(m: Message, state: FSMContext):
        if not adm(m):
            return
        await state.clear()
        await m.answer(panel_text(m.from_user.id), reply_markup=panel_kb(m.from_user.id))

    @r.callback_query(F.data.startswith("adm:"))
    async def adm_cb(c: CallbackQuery, state: FSMContext):
        uid, act = c.from_user.id, c.data[4:]
        if not st.is_admin(uid):
            return await c.answer("Ruxsat yo'q", show_alert=True)
        if act in SUPER_ONLY and not st.is_superadmin(uid):
            return await c.answer("Faqat superadmin uchun", show_alert=True)
        await c.answer()
        if act == "stats":
            return await c.message.answer(stats_text())
        if act == "selftest":
            return await do_selftest(c.message)
        if act == "diag":
            return await c.message.answer(diag_text())
        if act == "admins":
            lst = ", ".join(str(a) for a in st.d["admins"]) or "yo'q"
            return await c.message.answer(f"Superadminlar: {sorted(st.superadmins)}\nAdminlar: {lst}",
                                          reply_markup=kb([[("➕ Qo'shish", "adm:addadmin"), ("➖ O'chirish", "adm:deladmin")]]))
        if act in PROMPTS:
            await state.set_state(A.input)
            await state.update_data(act=act)
            await c.message.answer(PROMPTS[act] + "\n(Bekor qilish: /admin)")

    @r.message(A.input, ~F.text.startswith("/"))
    async def adm_input(m: Message, state: FSMContext):
        uid = m.from_user.id
        act = (await state.get_data()).get("act")
        if not st.is_admin(uid) or (act in SUPER_ONLY and not st.is_superadmin(uid)):
            return await state.clear()
        nums = [int(x) for x in re.findall(r"-?\d+", m.text or "")]
        if act == "price" and len(nums) == 1 and nums[0] >= 0:
            st.set_setting("price_per_found", nums[0])
        elif act == "packages" and nums and min(nums) >= 1:
            st.set_setting("packages", sorted(set(nums)))
        elif act == "grant" and len(nums) == 2 and st.balance(nums[0]) + nums[1] >= 0:
            st.credit(nums[0], nums[1], "grant", ref=str(uid))
        elif act == "vipprice" and len(nums) == 1 and nums[0] >= 1:
            st.set_setting("vip_price", nums[0])
        elif act == "vipdays" and len(nums) == 1 and nums[0] >= 1:
            st.set_setting("vip_days", nums[0])
        elif act == "addadmin" and len(nums) == 1:
            st.add_admin(nums[0])
        elif act == "deladmin" and len(nums) == 1:
            st.del_admin(nums[0])
        else:
            return await m.answer("Format noto'g'ri. Qayta yozing yoki /admin bilan bekor qiling.")
        await state.clear()
        await m.answer("✅ Saqlandi")
        await m.answer(panel_text(uid), reply_markup=panel_kb(uid))

    return r
