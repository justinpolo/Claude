import time

import pytest

from sniper.models import Listing, Watch, normalize_condition
from sniper.rules import check, matches


def L(**kw):
    base = dict(platform="vinted", id="1", title="Nike Dunk Low Panda", url="u", price=40.0,
                currency="GBP", brand="Nike", size="UK 9", condition="very_good",
                condition_raw="Very good")
    base.update(kw)
    return Listing(**base)


def W(**kw):
    return Watch.from_dict({"id": "w", "query": "nike dunk", **kw})


def test_price_bounds():
    assert matches(L(price=40), W(max_price=40))
    assert not matches(L(price=40.01), W(max_price=40))
    assert not matches(L(price=9), W(min_price=10))


def test_total_price_option():
    l = L(price=40, total_price=44)
    assert matches(l, W(max_price=42))
    assert not matches(l, W(max_price=42, use_total_price=True))


def test_min_condition_floor():
    assert matches(L(condition="very_good"), W(min_condition="good"))
    assert matches(L(condition="new_with_tags"), W(min_condition="good"))
    assert not matches(L(condition="fair"), W(min_condition="good"))
    assert "unknown" in check(L(condition=None), W(min_condition="good"))


def test_condition_list_and_aliases():
    w = W(conditions=["nwt", "brand new"])
    assert w.conditions == ["new_with_tags", "new"]
    assert matches(L(condition="new"), w)
    assert not matches(L(condition="like_new"), w)


def test_keywords_word_boundaries():
    assert not matches(L(title="Nike dunk low kids"), W(exclude=["kids"]))
    assert matches(L(title="Nike dunk skidsy"), W(exclude=["kids"]))  # no partial-word hits
    assert matches(L(title="Nike Dunk Low Panda"), W(must_include=["low", "panda"]))
    assert not matches(L(title="Nike Dunk High"), W(must_include=["low"]))
    assert not matches(L(title="dunk", description="replica, not genuine"), W(exclude=["replica"]))


def test_sizes():
    assert matches(L(size="UK 9 / EU 44"), W(sizes=["UK 9"]))
    assert matches(L(size="M"), W(sizes=["s", "m"]))
    assert not matches(L(size="UK 9.5"), W(sizes=["UK 9"]))
    assert not matches(L(size="XL"), W(sizes=["L"]))
    assert not matches(L(size=""), W(sizes=["L"]))


def test_brand():
    assert matches(L(brand="Nike"), W(brands=["nike"]))
    assert not matches(L(brand="Adidas", title="Samba"), W(brands=["nike"]))


def test_max_age():
    now = time.time()
    assert matches(L(created_ts=now - 60), W(max_age_minutes=5), now)
    assert not matches(L(created_ts=now - 600), W(max_age_minutes=5), now)
    assert matches(L(created_ts=None), W(max_age_minutes=5), now)  # unknown age -> allow


def test_platform():
    assert not matches(L(platform="depop"), W(platforms=["vinted"]))
    assert W(platforms="all").platforms == ["vinted", "depop"]


def test_validation_errors():
    with pytest.raises(ValueError):
        Watch.from_dict({"id": "x", "query": "a", "colour": "red"})
    with pytest.raises(ValueError):
        Watch.from_dict({"id": "x", "query": "a", "min_condition": "mint"})
    with pytest.raises(ValueError):
        Watch.from_dict({"id": "x", "query": "a", "platforms": ["ebay"]})
    with pytest.raises(ValueError):
        Watch.from_dict({"id": "x", "query": ""})


def test_normalize_condition():
    assert normalize_condition("New with tags") == "new_with_tags"
    assert normalize_condition("used_excellent") == "very_good"
    assert normalize_condition({"id": "used_like_new"}) == "like_new"
    assert normalize_condition("Satisfactory") == "fair"
    assert normalize_condition("weird") is None


def test_fingerprint_changes_with_search_but_not_local_rules():
    a = W(max_price=50)
    assert a.fingerprint("vinted") != W(max_price=60).fingerprint("vinted")
    assert a.fingerprint("vinted") == W(max_price=50, exclude=["kids"]).fingerprint("vinted")
    assert a.fingerprint("vinted") != a.fingerprint("depop")


def test_size_decimals_and_numbers():
    assert matches(L(size="UK 9.5"), W(sizes=["UK 9.5"]))
    assert not matches(L(size="UK 19"), W(sizes=["9"]))
    assert not matches(L(size="W30 L32"), W(sizes=["32"]))  # "L32" is not "32"
    assert matches(L(size="10"), W(sizes=["10"]))
    assert not matches(L(size="EU 9.5"), W(sizes=["5"]))


def test_default_max_age_and_promoted():
    now = time.time()
    old = L(created_ts=now - 3 * 3600)
    assert matches(old, W(), now)                                   # no limit by default in rules
    assert not matches(old, W(), now, default_max_age=120)
    assert matches(old, W(max_age_minutes=600), now, default_max_age=120)  # watch overrides
    assert not matches(L(created_ts=now - 1800, promoted=True), W(), now)
    assert matches(L(created_ts=now - 60, promoted=True), W(), now)
    assert not matches(L(created_ts=None, promoted=True), W(), now)


def test_any_of_and_blank_list_entries():
    w = W(query="coach", brands=["coach"], any_of=["bag", "purse", "handbag", "crossbody", "tote"])
    assert matches(L(title="Coach Tabby shoulder bag", brand="Coach", size=""), w)
    assert matches(L(title="Vintage coach purse brown", brand="Coach", size=""), w)
    assert not matches(L(title="Coach leather wallet", brand="Coach"), w)
    assert not matches(L(title="Nike coach jacket", brand="Nike"), w)
    blank = W(sizes=[""], brands=["", None])
    assert blank.sizes == [] and blank.brands == []
    assert matches(L(size=""), blank)
