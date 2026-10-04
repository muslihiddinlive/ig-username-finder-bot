import asyncio
import json
import random
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from ai import parse_ai_json
from checkers import FREE, TAKEN, UNKNOWN, Pipeline, parse_apify_items
from config import Config
from generator import SearchSpec, is_valid_username, iter_candidates
from search import Searcher, spec_batches
from storage import Store


def mkcfg(**kw):
    base = dict(bot_token="x", db_channel_id=0, superadmins=set(), checkers=(), apify_token="", apify_actor="",
                apify_monthly_budget=100, proxies_file="", max_checks=1000, chunk_size=10, preset_limit=6,
                require_each_type=True, ai_base_url="", ai_key="", ai_model="", ai_rounds=2, port=1, startup_grace=0)
    base.update(kw)
    return Config(**base)


def test_validation():
    assert is_valid_username("uzb.dev_1")
    for bad in [".uzb", "uzb.", "u..b", "UZB", "a b", "", "x" * 31, "uz-b"]:
        assert not is_valid_username(bad)


def test_generator_rules():
    spec = SearchSpec("starts", "uzb", free=3, types=("letter", "digit", "dot"))
    names = list(iter_candidates(spec, 500, random.Random(1)))
    assert len(names) == len(set(names)) > 0
    for n in names:
        assert n.startswith("uzb") and len(n) == 6 and is_valid_username(n)
        mid = n[3:]
        assert any(c.isalpha() for c in mid) and any(c.isdigit() for c in mid) and "." in mid


def test_generator_both_and_ends():
    spec = SearchSpec("both", "uz", "dev", free=2, types=("letter",))
    n = next(iter(iter_candidates(spec, 5, random.Random(2))))
    assert n.startswith("uz") and n.endswith("dev") and len(n) == 7
    spec = SearchSpec("ends", "uzb", free=2, types=("digit",))
    assert all(x.endswith("uzb") and x[:2].isdigit() for x in iter_candidates(spec, 20))


def test_infeasible_when_types_exceed_slots():
    spec = SearchSpec("starts", "uzb", free=2, types=("letter", "digit", "us"))
    assert not spec.feasible() and list(iter_candidates(spec, 10)) == []


def test_big_space_random_path():
    spec = SearchSpec("starts", "uzb", free=7, types=("letter", "digit"))
    names = list(iter_candidates(spec, 100, random.Random(3)))
    assert len(names) == 100 and len(set(names)) == 100


def test_store_billing_and_roundtrip():
    s = Store({1})
    s.credit(5, 10)
    assert s.debit(5, 3) and s.balance(5) == 7
    assert not s.debit(5, 8) and s.balance(5) == 7
    s.record_payment("c1", 5, 10)
    s2 = Store()
    s2.load_json(s.dump_json())
    assert s2.balance(5) == 7 and s2.seen_payment("c1") and s2.settings["price_per_found"] == 1
    s.grant_vip(5, 30)
    assert s.is_vip(5)


def test_parsers():
    items = [{"username": "A", "platform": "Instagram", "available": True},
             {"username": "b", "platform": "Instagram", "available": False},
             {"username": "c", "platform": "Instagram", "available": None},
             {"username": "a", "platform": "GitHub", "available": False}]
    assert parse_apify_items(items, ["a", "b", "c", "d"]) == {"a": FREE, "b": TAKEN, "c": UNKNOWN, "d": UNKNOWN}
    txt = 'xx {"length": 6, "must_contain": ["uzb"], "candidates": ["uzbdev", "UZBDEV", "uzb", "dev.uzb", "uzb..a", "uzbcod"]} yy'
    assert parse_ai_json(txt, avoid={"uzbcod"}) == ["uzbdev"]
    assert parse_ai_json("not json") == []


class Fake:
    def __init__(self, name, mapping, authoritative):
        self.name, self.m, self.authoritative = name, mapping, authoritative

    def available(self, n):
        return True

    async def check_batch(self, names):
        return {n: self.m.get(n, UNKNOWN) for n in names}


def test_pipeline_probe_prefilter_then_authoritative():
    probe = Fake("probe", {"a": TAKEN, "b": FREE, "c": FREE, "d": UNKNOWN}, False)
    api = Fake("api", {"b": FREE, "c": TAKEN, "d": FREE}, True)
    res = asyncio.run(Pipeline([probe, api]).check(["a", "b", "c", "d"]))
    assert res == {"a": (TAKEN, True), "b": (FREE, True), "c": (TAKEN, True), "d": (FREE, True)}


def test_pipeline_unverified_free_not_charged():
    probe = Fake("probe", {"b": FREE}, False)
    res = asyncio.run(Pipeline([probe]).check(["b"]))
    assert res["b"] == (FREE, False)


class FakeBot:
    def __init__(self):
        self.sent = []
        self._id = 0

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        self._id += 1
        return type("M", (), {"message_id": self._id})()

    async def edit_message_text(self, text, **kw):
        return None


def run_search(balance, free_set, price=2, max_found=None, cfg=None):
    cfg = cfg or mkcfg()
    store = Store()
    store.set_setting("price_per_found", price)
    store.credit(7, balance)
    api = Fake("api", {n: FREE for n in free_set}, True)
    api.m_default = TAKEN

    async def check_batch(names):
        return {n: (FREE if n in free_set else TAKEN) for n in names}

    api.check_batch = check_batch
    searcher = Searcher(cfg, store, Pipeline([api]))
    spec = SearchSpec("starts", "uzb", free=2, types=("letter",), require_each=True)
    bot = FakeBot()
    asyncio.run(searcher.run(bot, 1, 7, spec_batches(cfg, store, [spec]), "t", max_found))
    return store, bot


def test_charges_only_found_and_stops_on_balance():
    free = {"uzbaa", "uzbbb", "uzbcc", "uzbdd"}
    store, bot = run_search(balance=5, free_set=free, price=2)
    found = [m for m in bot.sent if m.startswith("✅")]
    assert len(found) == 2 and store.balance(7) == 1  # 5 - 2*2, uchinchisiga yetmadi


def test_no_charge_when_nothing_found():
    store, bot = run_search(balance=5, free_set=set())
    assert store.balance(7) == 5 and not any(m.startswith("✅") for m in bot.sent)
    assert all(store.is_taken(n) for n in list(store.d["taken"]))


def test_max_found_cap():
    free = {"uzbaa", "uzbbb", "uzbcc"}
    store, bot = run_search(balance=100, free_set=free, price=1, max_found=1)
    assert sum(m.startswith("✅") for m in bot.sent) == 1 and store.balance(7) == 99


def test_superadmin_ids_parsing():
    from config import _ids
    want = {111, 222, 333}
    for raw in ["111,222,333", "111, 222, 333", "111 222 333", "111\n222\n333", "111;222 ; 333\r\n", "[111, 222, 333]"]:
        assert _ids(raw) == want, raw
    assert _ids("") == set() and _ids(None) == set()


def test_superadmin_unlimited_and_free():
    store = Store({7})
    assert store.is_vip(7) and store.price_for(7) == 0 and store.balance(7) == 0
    assert not store.is_vip(8) and store.price_for(8) == 1
    cfg = mkcfg()
    free = {"uzbaa", "uzbbb", "uzbcc", "uzbdd", "uzbee"}
    api = Fake("api", {}, True)

    async def cb(names):
        return {n: (FREE if n in free else TAKEN) for n in names}

    api.check_batch = cb
    searcher = Searcher(cfg, store, Pipeline([api]))
    spec = SearchSpec("starts", "uzb", free=2, types=("letter",), require_each=True)
    bot = FakeBot()
    asyncio.run(searcher.run(bot, 1, 7, spec_batches(cfg, store, [spec]), "t"))
    found = [m for m in bot.sent if m.startswith("✅")]
    assert len(found) == 5 and all("⭐" not in m for m in found)  # balans 0 bo'lsa ham hammasi topildi, pul yechilmadi
    assert store.balance(7) == 0 and "∞" in bot.sent[-1]


def test_router_builds_with_admin_panel():
    from bot import Ctx, build_router
    cfg = mkcfg()
    st = Store({1})
    ctx = Ctx(cfg, st, None, Pipeline([]), Searcher(cfg, st, Pipeline([])))
    r = build_router(ctx)
    names = {h.callback.__name__ for o in r.observers.values() for h in o.handlers}
    assert {"admin_cmd", "adm_cb", "adm_input", "selftest", "stats"} <= names


def test_env_values_are_stripped(monkeypatch):
    import config
    monkeypatch.setenv("AI_API_KEY", "gsk_abc123\n")
    monkeypatch.setenv("AI_MODEL", ' "openai/gpt-oss-120b" ')
    monkeypatch.setenv("AI_BASE_URL", "https://api.groq.com/openai/v1/\r\n")
    monkeypatch.setenv("APIFY_TOKEN", "\tapify_api_x \n")
    monkeypatch.setenv("BOT_TOKEN", "123:abc\n")
    c = config.load()
    assert c.ai_key == "gsk_abc123" and c.ai_model == "openai/gpt-oss-120b"
    assert c.ai_base_url == "https://api.groq.com/openai/v1" and c.apify_token == "apify_api_x" and c.bot_token == "123:abc"


def test_apify_budget_refunded_on_failure_and_spent_on_success(monkeypatch):
    import checkers

    class Resp:
        def __init__(self, status, payload):
            self.status, self.payload = status, payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "payment required"

        async def json(self):
            return self.payload

    state = {"status": 402, "payload": None}

    class Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            return Resp(state["status"], state["payload"])

    monkeypatch.setattr(checkers.aiohttp, "ClientSession", Sess)
    store = Store()
    ck = checkers.ApifyChecker("tok", "a/b", 100, store)
    with pytest.raises(checkers.CheckerError):
        asyncio.run(ck.check_batch(["aa", "bb"]))
    assert store.apify_left(100) == 100                      # muvaffaqiyatsiz so'rov byudjetni yemaydi
    state.update(status=200, payload=[{"username": "aa", "platform": "Instagram", "available": True}])
    res = asyncio.run(ck.check_batch(["aa", "bb"]))
    assert res == {"aa": FREE, "bb": UNKNOWN} and store.apify_left(100) == 98
    assert "elementlar=1" in ck.last_debug and "aa" in ck.last_debug
    p = Pipeline([ck])
    state.update(status=500, payload=None)
    out = asyncio.run(p.check(["aa"]))
    assert out["aa"] == (UNKNOWN, False) and "500" in p.last_error


def test_channel_persistence_roundtrip_with_fake_bot(tmp_path):
    import io
    from storage import ChannelPersistence

    class Doc:
        file_id = "f1"

    class Pm:
        document = Doc()
        message_id = 11

    class FakeTgBot:
        def __init__(self):
            self.blob = None
            self.pinned = None
            self.deleted = []
            self.n = 10

        async def send_document(self, chat_id, doc, **kw):
            self.blob = doc.data
            self.n += 1
            return type("M", (), {"message_id": self.n})()

        async def pin_chat_message(self, chat_id, mid, **kw):
            self.pinned = type("Pm", (), {"document": Doc(), "message_id": mid})()

        async def delete_message(self, chat_id, mid):
            self.deleted.append(mid)

        async def get_chat(self, chat_id):
            return type("C", (), {"pinned_message": self.pinned})()

        async def download(self, file_id):
            return io.BytesIO(self.blob)

        async def send_message(self, *a, **k):
            return None

    async def go():
        bot = FakeTgBot()
        s1 = Store()
        s1.credit(42, 77)
        s1.mark_taken("abc")
        p1 = ChannelPersistence(bot, -100123, s1, local_path=str(tmp_path / "a.json"))
        assert await p1.flush() and p1.last_ok
        s2 = Store()
        p2 = ChannelPersistence(bot, -100123, s2, local_path=str(tmp_path / "b.json"))
        assert await p2.load() == "channel" and p2.source.startswith("kanal")
        assert s2.balance(42) == 77 and s2.is_taken("abc")
        s3 = Store()
        p3 = ChannelPersistence(FakeTgBot(), 0, s3, local_path=str(tmp_path / "none.json"))
        assert await p3.load() == "fresh" and "DB_CHANNEL_ID" in p3.source

    asyncio.run(go())


def test_probe_channel_reports_cause_and_chan_parsing():
    from storage import probe_channel
    from config import _chan

    class NoChat:
        async def get_chat(self, cid):
            raise RuntimeError("Bad Request: chat not found")

    assert "chat not found" in asyncio.run(probe_channel(NoChat(), -1001))

    class Ok:
        async def get_chat(self, cid):
            return type("C", (), {"id": cid, "title": "DB", "type": "channel"})()

        async def get_me(self):
            return type("U", (), {"id": 9})()

        async def get_chat_member(self, cid, uid):
            return type("M", (), {"status": "administrator", "can_post_messages": True, "can_edit_messages": False,
                                  "can_delete_messages": True, "can_pin_messages": None})()

    out = asyncio.run(probe_channel(Ok(), -1001))
    assert "administrator" in out and "edit_messages=False" in out
    assert _chan("-1001234567890") == -1001234567890 and _chan(" @mydb ") == "@mydb" and _chan("") == 0
    assert _chan('"-100555"\n') == -100555


def test_apify_null_results_explain_the_cause(monkeypatch):
    import checkers

    items = [{"username": "aa", "platform": "Instagram", "available": None, "error": "HTTP 429 rate limited"},
             {"username": "bb", "platform": "Instagram", "available": None, "error": "HTTP 429 rate limited"}]

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return items

    class Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, **k):
            Sess.body = json
            return Resp()

    monkeypatch.setattr(checkers.aiohttp, "ClientSession", Sess)
    store = Store()
    ck = checkers.ApifyChecker("tok", "a/b", 100, store, concurrency=2, delay=2.0, proxy=True)
    p = Pipeline([ck])
    out = asyncio.run(p.check(["aa", "bb"]))
    assert out["aa"] == (UNKNOWN, False)
    assert "429" in p.last_error and "2/2 natija null" in p.last_error
    assert Sess.body["concurrency"] == 2 and Sess.body["delayBetweenRequests"] == 2.0
    assert Sess.body["proxyConfiguration"] == {"useApifyProxy": True}
    items[:] = [{"username": "aa", "platform": "GitHub", "available": True}]
    asyncio.run(p.check(["aa"]))
    assert "Instagram natijalari qaytmadi" in p.last_error
