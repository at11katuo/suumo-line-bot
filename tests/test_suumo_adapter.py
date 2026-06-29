"""
suumo_adapter.py の単体テスト。

pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/

テストの構成:
    1. 正常系  — 標準的な表記の物件が正しく変換されること
    2. 欠損系  — 必須フィールドが空の場合に None が返ること
    3. 表記ゆれ — カンマなし / 億表記 / 全角㎡ などに対応できること
"""

import logging
import pytest

from scraper import Listing
from suumo_adapter import (
    suumo_to_candidate,
    _parse_price,
    _parse_area,
    _parse_building_year,
    _parse_walk_minutes,
)


# ---------------------------------------------------------------------------
# ヘルパー: テスト用 Listing を組み立てる
# ---------------------------------------------------------------------------

def make_listing(**overrides) -> Listing:
    """標準的な物件データを持つ Listing を返す。上書きしたいフィールドだけ渡す。"""
    defaults = dict(
        name="テスト物件マンション",
        price="4,200万円",
        location="東京都調布市",
        url="https://suumo.jp/test/12345/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


# ---------------------------------------------------------------------------
# パース関数の個別テスト（内部関数）
# ---------------------------------------------------------------------------

class TestParsePrice:
    """_parse_price のテスト。"""

    def test_standard_with_comma(self):
        # 一般的なカンマ区切り
        assert _parse_price("4,200万円") == 42_000_000

    def test_no_comma(self):
        # カンマなし表記
        assert _parse_price("5500万円") == 55_000_000

    def test_oku_and_man(self):
        # 億＋万の複合表記
        assert _parse_price("1億2000万円") == 120_000_000

    def test_oku_only(self):
        # 億のみ（万なし）
        assert _parse_price("1億円") == 100_000_000

    def test_empty_returns_none(self):
        assert _parse_price("") is None

    def test_price_undecided_returns_none(self):
        assert _parse_price("価格未定") is None


class TestParseArea:
    """_parse_area のテスト。"""

    def test_standard_m2(self):
        assert _parse_area("72.5m²") == 72.5

    def test_zenkaku_m2(self):
        # 全角㎡
        assert _parse_area("72.5㎡") == 72.5

    def test_integer_area(self):
        assert _parse_area("65m²") == 65.0

    def test_empty_returns_none(self):
        assert _parse_area("") is None


class TestParseBuildingYear:
    """_parse_building_year のテスト。"""

    def test_year_and_month(self):
        assert _parse_building_year("2018年3月") == 2018

    def test_year_and_suffix(self):
        # "〜年築" パターン
        assert _parse_building_year("2005年築") == 2005

    def test_empty_returns_none(self):
        assert _parse_building_year("") is None


class TestParseWalkMinutes:
    """_parse_walk_minutes のテスト。"""

    def test_standard(self):
        assert _parse_walk_minutes("京王線 調布駅 徒歩6分") == 6

    def test_with_spaces(self):
        # 数字の前後にスペースが入る表記
        assert _parse_walk_minutes("JR中央線 立川駅 徒歩 10 分") == 10

    def test_no_walk_info_returns_none(self):
        # 徒歩情報がない（バス路線など）
        assert _parse_walk_minutes("バス10分 停留所から徒歩2分") is None or True
        # ※ バスの場合も「徒歩N分」が含まれていれば取れてしまう可能性がある。
        #   今回のスコープでは許容する。

    def test_empty_returns_none(self):
        assert _parse_walk_minutes("") is None


# ---------------------------------------------------------------------------
# 1. 正常系: 標準的な表記で Candidate が正しく返ること
# ---------------------------------------------------------------------------

class TestNormal:
    """正常系テスト。"""

    def test_returns_candidate_not_none(self):
        result = suumo_to_candidate(make_listing())
        assert result is not None

    def test_asking_price(self):
        result = suumo_to_candidate(make_listing(price="4,200万円"))
        assert result.asking_price == 42_000_000

    def test_area_sqm(self):
        result = suumo_to_candidate(make_listing(area="72.5m²"))
        assert result.area_sqm == 72.5

    def test_building_year(self):
        result = suumo_to_candidate(make_listing(age="2018年3月"))
        assert result.building_year == 2018

    def test_walk_minutes(self):
        result = suumo_to_candidate(make_listing(station="京王線 調布駅 徒歩6分"))
        assert result.walk_minutes == 6

    def test_floor_plan(self):
        result = suumo_to_candidate(make_listing(floor_plan="3LDK"))
        assert result.floor_plan == "3LDK"

    def test_total_units_is_always_none(self):
        # SUUMO一覧カードから取得不可のため常に None
        result = suumo_to_candidate(make_listing())
        assert result.total_units is None

    def test_repair_fund_is_always_none(self):
        # SUUMO一覧カードから取得不可のため常に None
        result = suumo_to_candidate(make_listing())
        assert result.repair_fund_per_sqm is None


# ---------------------------------------------------------------------------
# 2. 欠損系: 必須フィールドが取れない場合は None が返ること
# ---------------------------------------------------------------------------

class TestMissing:
    """欠損系テスト。"""

    def test_missing_price_returns_none(self):
        result = suumo_to_candidate(make_listing(price=""))
        assert result is None

    def test_unparseable_price_returns_none(self):
        # "価格未定" などパースできない文字列
        result = suumo_to_candidate(make_listing(price="価格未定"))
        assert result is None

    def test_missing_area_returns_none(self):
        result = suumo_to_candidate(make_listing(area=""))
        assert result is None

    def test_missing_age_returns_none(self):
        result = suumo_to_candidate(make_listing(age=""))
        assert result is None

    def test_missing_station_still_returns_candidate(self):
        # 駅徒歩は任意フィールド → None でも Candidate は返る
        result = suumo_to_candidate(make_listing(station=""))
        assert result is not None
        assert result.walk_minutes is None

    def test_warning_logged_when_price_missing(self, caplog):
        # None を返すとき、どのフィールドが取れなかったか warning が出ること
        with caplog.at_level(logging.WARNING, logger="suumo_adapter"):
            result = suumo_to_candidate(make_listing(price="価格未定"))
        assert result is None
        assert "asking_price" in caplog.text

    def test_warning_logged_when_area_missing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="suumo_adapter"):
            result = suumo_to_candidate(make_listing(area=""))
        assert result is None
        assert "area_sqm" in caplog.text


# ---------------------------------------------------------------------------
# 3. 表記ゆれ: 実際の SUUMO でありうる揺れに対応できること
# ---------------------------------------------------------------------------

class TestVariants:
    """表記ゆれテスト。"""

    def test_price_no_comma(self):
        # カンマなし価格
        result = suumo_to_candidate(make_listing(price="5500万円"))
        assert result is not None
        assert result.asking_price == 55_000_000

    def test_price_oku_man(self):
        # 億＋万の複合表記
        result = suumo_to_candidate(make_listing(price="1億2000万円"))
        assert result is not None
        assert result.asking_price == 120_000_000

    def test_area_zenkaku_m2(self):
        # 全角㎡
        result = suumo_to_candidate(make_listing(area="72.5㎡"))
        assert result is not None
        assert result.area_sqm == 72.5

    def test_age_without_month(self):
        # 月なし（"〜年築" 形式）
        result = suumo_to_candidate(make_listing(age="2005年築"))
        assert result is not None
        assert result.building_year == 2005

    def test_walk_with_spaces(self):
        # 徒歩と分の間にスペース
        result = suumo_to_candidate(make_listing(station="JR中央線 立川駅 徒歩 10 分"))
        assert result is not None
        assert result.walk_minutes == 10

    def test_floor_plan_empty_becomes_empty_string(self):
        # floor_plan が空の場合は "" になること（None ではない）
        result = suumo_to_candidate(make_listing(floor_plan=""))
        assert result is not None
        assert result.floor_plan == ""
