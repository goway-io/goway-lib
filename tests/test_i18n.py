import pytest

from goway_lib.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Transliterator,
    normalize_language,
    parse_accept_language,
    resolve_localized,
    to_cyrillic,
    validate_localized,
)


def test_registry_has_all_six():
    assert set(LANGUAGES) == {"uz", "uzc", "ru", "en", "kaa", "zh"}
    assert DEFAULT_LANGUAGE == "uz"


def test_normalize_accepts_internal_and_bcp47():
    assert normalize_language("uzc") == "uzc"
    assert normalize_language("uz-Cyrl") == "uzc"
    assert normalize_language("uz-Latn") == "uz"
    assert normalize_language("zh-Hans") == "zh"
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("ru-RU") == "ru"
    assert normalize_language("kaa") == "kaa"
    assert normalize_language("de") is None


def test_accept_language_q_ordering():
    assert parse_accept_language("uz-Cyrl-UZ,ru;q=0.8,en;q=0.5") == "uzc"
    assert parse_accept_language("de,fr;q=0.9") is None
    assert parse_accept_language("de;q=1.0,ru;q=0.3") == "ru"
    assert parse_accept_language("") is None


def test_resolve_walks_fallback_chain():
    mapping = {"en": "Truck", "ru": "Фура"}
    assert resolve_localized(mapping, "ru") == "Фура"
    assert resolve_localized(mapping, "kaa") == "Фура"
    assert resolve_localized(mapping, "zh") == "Truck"
    assert resolve_localized({}, "uz") is None
    assert resolve_localized({"zh": "卡车"}, "uz") == "卡车"


def test_resolve_uzc_transliterates_from_latin():
    mapping = {"uz": "Yuk mashinasi", "en": "Truck"}
    assert resolve_localized(mapping, "uzc") == "Юк машинаси"
    explicit = {"uz": "Yuk mashinasi", "uzc": "Юк моши"}
    assert resolve_localized(explicit, "uzc") == "Юк моши"


def test_validate_localized_rejects_unknown():
    assert validate_localized({"uz": "x", "zh": "y"}) == {"uz": "x", "zh": "y"}
    with pytest.raises(ValueError):
        validate_localized({"uz": "x", "de": "y"})


def test_transliteration_rules():
    cases = {
        "Toshkent": "Тошкент",
        "O'zbekiston": "Ўзбекистон",
        "yo'l": "йўл",
        "yangi": "янги",
        "Farg'ona": "Фарғона",
        "ertaga": "эртага",
        "ma'no": "маъно",
        "e'lon": "эълон",
        "Yuk mashinasi": "Юк машинаси",
        "Qoraqalpog'iston": "Қорақалпоғистон",
    }
    for latin, cyrillic in cases.items():
        assert to_cyrillic(latin) == cyrillic


def test_transliteration_overrides_win():
    t = Transliterator(overrides={"sement": "цемент"})
    assert t.convert("sement") == "цемент"
    assert t.convert("shahar") == "шаҳар"


def test_apostrophe_variants_normalized():
    assert to_cyrillic("oʻzbek") == to_cyrillic("o'zbek") == to_cyrillic("o‘zbek") == "ўзбек"
