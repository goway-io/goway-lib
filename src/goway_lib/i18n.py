import re
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request


@dataclass(frozen=True)
class Language:
    code: str
    bcp47: str
    name: str
    fallbacks: tuple[str, ...]
    enabled: bool = True


LANGUAGES: dict[str, Language] = {
    lang.code: lang
    for lang in (
        Language("uz", "uz-Latn", "O'zbekcha", ("ru", "en")),
        Language("uzc", "uz-Cyrl", "Ўзбекча", ("uz", "ru", "en")),
        Language("ru", "ru", "Русский", ("en",)),
        Language("en", "en", "English", ()),
        Language("kaa", "kaa", "Qaraqalpaqsha", ("uz", "ru", "en")),
        Language("zh", "zh-Hans", "中文", ("en",)),
    )
}

DEFAULT_LANGUAGE = "uz"

_BCP47_ALIASES = {
    "uz-latn": "uz",
    "uz-cyrl": "uzc",
    "zh-hans": "zh",
    "zh-cn": "zh",
    "zh-hant": "zh",
}


def is_supported(code: str) -> bool:
    return code in LANGUAGES and LANGUAGES[code].enabled


def normalize_language(raw: str) -> str | None:
    tag = raw.strip().lower()
    if tag in LANGUAGES:
        return tag
    if tag in _BCP47_ALIASES:
        return _BCP47_ALIASES[tag]
    subtags = tag.split("-")
    prefix = "-".join(subtags[:2])
    if prefix in _BCP47_ALIASES:
        return _BCP47_ALIASES[prefix]
    base = subtags[0]
    if base in LANGUAGES:
        return base
    return _BCP47_ALIASES.get(base)


def parse_accept_language(header: str) -> str | None:
    candidates: list[tuple[float, str]] = []
    for part in header.split(","):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        quality = 1.0
        match = re.search(r"q=([0-9.]+)", params)
        if match:
            try:
                quality = float(match.group(1))
            except ValueError:
                quality = 0.0
        candidates.append((quality, tag.strip()))
    for _, tag in sorted(candidates, key=lambda c: -c[0]):
        code = normalize_language(tag)
        if code and is_supported(code):
            return code
    return None


async def request_language(request: Request) -> str:
    header = request.headers.get("accept-language", "")
    return parse_accept_language(header) or DEFAULT_LANGUAGE


Locale = Annotated[str, Depends(request_language)]


def resolve_localized(mapping: dict[str, str], language: str) -> str | None:
    if not mapping:
        return None
    chain = (language, *LANGUAGES[language].fallbacks) if language in LANGUAGES else (DEFAULT_LANGUAGE,)
    for code in chain:
        value = mapping.get(code)
        if value:
            return value
        if code == "uzc":
            latin = mapping.get("uz")
            if latin:
                return to_cyrillic(latin)
    value = mapping.get(DEFAULT_LANGUAGE)
    if value:
        return value
    return next(iter(mapping.values()))


def validate_localized(mapping: dict[str, str]) -> dict[str, str]:
    unknown = sorted(set(mapping) - set(LANGUAGES))
    if unknown:
        raise ValueError(f"unknown language codes: {', '.join(unknown)}")
    return mapping


_APOSTROPHES = re.compile(r"[ʻʼ'`´‘’]")
_VOWELS = "aeiouAEIOU"

_DIGRAPHS = {
    "sh": "ш",
    "Sh": "Ш",
    "SH": "Ш",
    "ch": "ч",
    "Ch": "Ч",
    "CH": "Ч",
    "yo": "ё",
    "Yo": "Ё",
    "YO": "Ё",
    "yu": "ю",
    "Yu": "Ю",
    "YU": "Ю",
    "ya": "я",
    "Ya": "Я",
    "YA": "Я",
    "ye": "е",
    "Ye": "Е",
    "YE": "Е",
}

_SINGLES = {
    "a": "а",
    "b": "б",
    "d": "д",
    "f": "ф",
    "g": "г",
    "h": "ҳ",
    "i": "и",
    "j": "ж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "қ",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "x": "х",
    "y": "й",
    "z": "з",
}
_SINGLES |= {k.upper(): v.upper() for k, v in _SINGLES.items()}


@dataclass
class Transliterator:
    """Rule-based uz (Latin) → uzc (Cyrillic). Overrides win over rules —
    the exception table for loanwords the rules cannot know."""

    overrides: dict[str, str] = field(default_factory=dict)

    def convert(self, text: str) -> str:
        if text in self.overrides:
            return self.overrides[text]
        s = _APOSTROPHES.sub("'", text)
        s = s.replace("o'", "ў").replace("O'", "Ў").replace("g'", "ғ").replace("G'", "Ғ")
        out: list[str] = []
        i = 0
        while i < len(s):
            pair = s[i : i + 2]
            if pair in _DIGRAPHS:
                out.append(_DIGRAPHS[pair])
                i += 2
                continue
            ch = s[i]
            if ch in ("e", "E"):
                word_start = i == 0 or not (s[i - 1].isalpha() or s[i - 1] in "ўғЎҒ")
                out.append(("э" if ch == "e" else "Э") if word_start else ("е" if ch == "e" else "Е"))
            elif ch == "'":
                out.append("ъ")
            else:
                out.append(_SINGLES.get(ch, ch))
            i += 1
        return "".join(out)


_default_transliterator = Transliterator()


def to_cyrillic(text: str) -> str:
    return _default_transliterator.convert(text)
