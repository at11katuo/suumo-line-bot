"""
tests/test_listing_group.py
listing_group.py の単体テスト。

STEP 1: normalize_for_group / group_key の基本正規化・偽陽性防止・実例再現。
STEP 1(rev.2): merge_similar_groups（表記ゆれ吸収のための近接グループマージ）
の実例再現・ガード。
"""

from scraper import Listing
from listing_group import group_key, group_listings, merge_similar_groups, select_representative


def _listing(url, price="5180万円", location="東京都府中市本町１",
             floor_plan="3LDK", area="73.04m2（壁芯）", age="2001年5月"):
    return Listing(
        name=f"物件 {url}",
        price=price,
        location=location,
        url=url,
        floor_plan=floor_plan,
        area=area,
        age=age,
    )


# ---------------------------------------------------------------------------
# 1. 正規化の基本テスト
# ---------------------------------------------------------------------------

def test_area_notation_variants_produce_same_group_key():
    # 「90.02m2（壁芯）」と「90.02㎡」は同一キーになる
    a = _listing("nc_a", area="90.02m2（壁芯）")
    b = _listing("nc_b", area="90.02㎡")
    assert group_key(a) == group_key(b)


def test_location_chome_variants_produce_same_group_key():
    # 「紅葉丘２」と「紅葉丘２-9-10」は同一視される（丁目までで切る）
    a = _listing("nc_a", location="東京都府中市紅葉丘２")
    b = _listing("nc_b", location="東京都府中市紅葉丘２-9-10")
    assert group_key(a) == group_key(b)


# ---------------------------------------------------------------------------
# 2. 偽陽性防止
# ---------------------------------------------------------------------------

def test_same_building_name_different_area_and_floor_plan_are_different_groups():
    # 物件名が同じでも面積・間取りが違う部屋は別グループとして扱われる
    a = Listing(
        name="同じマンション", price="5290万円", location="東京都府中市紅葉丘２",
        url="nc_a", floor_plan="3LDK", area="90.02m2（壁芯）", age="2003年7月",
    )
    b = Listing(
        name="同じマンション", price="4380万円", location="東京都府中市紅葉丘２",
        url="nc_b", floor_plan="2LDK", area="65.00m2（壁芯）", age="2003年7月",
    )
    assert group_key(a) != group_key(b)


# ---------------------------------------------------------------------------
# 3. 実例再現: nc_21251938 / nc_21269843（紅葉丘2の横断重複の実例）
# ---------------------------------------------------------------------------

def test_real_example_momiji_pair_forms_one_group_with_cheapest_representative():
    cheap = Listing(
        name="【本日価格変更♪】", price="5190万円", location="東京都府中市紅葉丘２",
        url="https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21251938/",
        floor_plan="3LDK", area="90.02m2（壁芯）", age="2003年7月",
    )
    expensive = Listing(
        name="≪当日内見予約可能≫", price="5290万円", location="東京都府中市紅葉丘２",
        url="https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21269843/",
        floor_plan="3LDK", area="90.02m2（壁芯）", age="2003年7月",
    )

    groups = group_listings([cheap, expensive])

    assert len(groups) == 1
    (members,) = groups.values()
    assert {l.url for l in members} == {cheap.url, expensive.url}
    assert select_representative(members).url == cheap.url  # 5190万側が代表


def test_merges_month_and_area_rounding_variants_into_one_group():
    """実例再現: nc_20697502（築年月が1ヶ月ズレ）・nc_21019699（面積が丸められ
    73m2表記）は、本町１・73.04㎡・3LDK・2001年5月のグループにマージされる
    べき（本文冒頭のSTEP1報告で見つかった既知の2件）。"""
    base = _listing("nc_base")
    month_variant = _listing("nc_20697502", age="2001年6月")
    area_variant = _listing("nc_21019699", area="73m2（壁芯）")

    groups = group_listings([base, month_variant, area_variant])
    assert len(groups) == 3  # マージ前は厳密キーが3種に分かれている

    merged = merge_similar_groups(groups)

    assert len(merged) == 1
    (only_group,) = merged.values()
    assert {l.url for l in only_group} == {"nc_base", "nc_20697502", "nc_21019699"}


def test_does_not_merge_large_area_difference():
    """ガード: 面積差0.5㎡（73.04 vs 73.54）はマージされない。"""
    base = _listing("nc_base")
    far = _listing("nc_far", area="73.54m2（壁芯）")

    merged = merge_similar_groups(group_listings([base, far]))

    assert len(merged) == 2


def test_does_not_merge_large_age_difference():
    """ガード: 築年月差2ヶ月（2001年5月 vs 2001年7月）はマージされない。"""
    base = _listing("nc_base")
    far = _listing("nc_far", age="2001年7月")

    merged = merge_similar_groups(group_listings([base, far]))

    assert len(merged) == 2


def test_does_not_merge_different_location_or_floor_plan():
    """location・floor_plan が異なる場合は area/age が近くてもマージしない。"""
    base = _listing("nc_base")
    diff_location = _listing("nc_diff_loc", location="東京都府中市是政４")
    diff_plan = _listing("nc_diff_plan", floor_plan="4LDK")

    merged = merge_similar_groups(group_listings([base, diff_location, diff_plan]))

    assert len(merged) == 3


def test_representative_key_after_merge_is_cheapest_listings_exact_key():
    """マージ後の代表キーは、グループ内最安値物件の厳密キーになる。"""
    cheap = _listing("nc_cheap", price="5100万円")
    expensive_month_variant = _listing("nc_expensive", price="5300万円", age="2001年6月")

    merged = merge_similar_groups(group_listings([cheap, expensive_month_variant]))

    (rep_key, members) = next(iter(merged.items()))
    assert rep_key == "東京都府中市本町1|73.04|3LDK|2001-05"  # cheap 側（2001-05）の厳密キー
    assert len(members) == 2
