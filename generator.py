"""Nomzod username generatsiyasi va Instagram qoidalari bo'yicha validatsiya."""
from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass
from typing import Iterator

ALPHABETS = {
    "letter": "abcdefghijklmnopqrstuvwxyz",  # Instagram'da katta harf yo'q: UZB == uzb
    "digit": "0123456789",
    "us": "_",
    "dot": ".",
}
_RE = re.compile(r"[a-z0-9._]{1,30}")
ENUM_LIMIT = 200_000


def is_valid_username(u: str) -> bool:
    if not _RE.fullmatch(u):
        return False
    return not (u[0] == "." or u[-1] == "." or ".." in u)


def normalize_word(w: str) -> str:
    return w.strip().lstrip("@").lower()


@dataclass
class SearchSpec:
    mode: str  # starts | ends | both
    word: str
    word2: str = ""  # faqat 'both': tugash so'zi
    free: int = 3  # so'zlardan tashqari bo'sh joylar soni
    types: tuple = ("letter", "digit")
    require_each: bool = True

    @property
    def prefix(self) -> str:
        return self.word if self.mode in ("starts", "both") else ""

    @property
    def suffix(self) -> str:
        if self.mode == "ends":
            return self.word
        return self.word2 if self.mode == "both" else ""

    @property
    def fixed_len(self) -> int:
        return len(self.prefix) + len(self.suffix)

    @property
    def alphabet(self) -> str:
        return "".join(ALPHABETS[t] for t in self.types)

    @property
    def space(self) -> int:
        return len(self.alphabet) ** self.free if self.free else 1

    def feasible(self) -> bool:
        return not (self.require_each and self.free and len(self.types) > self.free)


def iter_candidates(spec: SearchSpec, limit: int, rng: random.Random | None = None) -> Iterator[str]:
    """Tasodifiy tartibda, takrorsiz, valid nomzodlar (ko'pi bilan `limit` ta)."""
    rng = rng or random.Random()
    if spec.free == 0:
        c = spec.prefix + spec.suffix
        if is_valid_username(c):
            yield c
        return
    if not spec.feasible():
        return
    alpha, n = spec.alphabet, spec.free

    def ok(mid: str) -> bool:
        if spec.require_each and not all(any(ch in ALPHABETS[t] for ch in mid) for t in spec.types):
            return False
        return is_valid_username(spec.prefix + mid + spec.suffix)

    yielded = 0
    if spec.space <= ENUM_LIMIT:
        combos = ["".join(p) for p in itertools.product(alpha, repeat=n)]
        rng.shuffle(combos)
        for mid in combos:
            if ok(mid):
                yield spec.prefix + mid + spec.suffix
                yielded += 1
                if yielded >= limit:
                    return
    else:
        seen: set[str] = set()
        attempts = 0
        while yielded < limit and attempts < limit * 60:
            attempts += 1
            mid = "".join(rng.choice(alpha) for _ in range(n))
            if mid in seen:
                continue
            seen.add(mid)
            if ok(mid):
                yield spec.prefix + mid + spec.suffix
                yielded += 1
