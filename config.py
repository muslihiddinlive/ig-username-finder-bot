from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _ids(v: str) -> set[int]:
    """Vergul, probel, yangi qator, nuqtali vergul — qaysi ajratgich bilan yozilsa ham ID'larni topadi."""
    return {int(x) for x in re.findall(r"-?\d+", v or "")}


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_channel_id: int
    superadmins: set
    checkers: tuple
    apify_token: str
    apify_actor: str
    apify_monthly_budget: int
    proxies_file: str
    max_checks: int
    chunk_size: int
    preset_limit: int
    require_each_type: bool
    ai_base_url: str
    ai_key: str
    ai_model: str
    ai_rounds: int
    port: int
    startup_grace: int


def load() -> Config:
    e = os.environ.get
    return Config(
        bot_token=e("BOT_TOKEN", ""),
        db_channel_id=int(e("DB_CHANNEL_ID", "0") or 0),
        superadmins=_ids(e("SUPERADMIN_IDS", "")),
        checkers=tuple(x.strip() for x in e("CHECKERS", "apify").split(",") if x.strip()),
        apify_token=e("APIFY_TOKEN", ""),
        apify_actor=e("APIFY_ACTOR", "sync-network/username-availability-checker"),
        apify_monthly_budget=int(e("APIFY_MONTHLY_BUDGET", "8000")),
        proxies_file=e("PROXIES_FILE", "proxies.txt"),
        max_checks=int(e("MAX_CHECKS", "3000")),
        chunk_size=int(e("CHUNK_SIZE", "40")),
        preset_limit=int(e("PRESET_LIMIT", "6")),
        require_each_type=e("REQUIRE_EACH_TYPE", "1") not in ("0", "false", "no"),
        ai_base_url=e("AI_BASE_URL", "").rstrip("/"),
        ai_key=e("AI_API_KEY", ""),
        ai_model=e("AI_MODEL", ""),
        ai_rounds=int(e("AI_ROUNDS", "3")),
        port=int(e("PORT", "10000")),
        startup_grace=int(e("STARTUP_GRACE", "20")),
    )
