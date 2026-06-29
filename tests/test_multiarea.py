"""
tests/test_multiarea.py
多エリア対応（調布・府中・稲城）のユニット・統合テスト。

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー不要。
pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/
"""

import sqlite3
from pathlib import Path

import pytest

import build_curves
from evaluator import resolve_city_code, evaluate_and_save
from scraper import Listing

CHOFU_CODE = "13208"
FUCHU_CODE  = "13206"
INAGI_CODE  = "13225"


# ---------------------------------------------------------------------------
# 共通フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    """全テストでキャッシュディレクトリを一時ディレクトリに差し替える。"""
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    """全テストで USE_MOCK_REINFOLIB=1 を設定し、APIキー不要にする。"""
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_multiarea.db"


def make_listing(
    location: str = "東京都調布市曙町",
    url: str = "https://suumo.jp/test/99999/",
    **overrides,
) -> Listing:
    defaults = dict(
        name="テスト物件マンション",
        price="4,200万円",
        location=location,
        url=url,
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


def _get_city_codes(db_path: Path, url: str) -> list[str]:
    """DB から指定 URL の city_code カラム値をすべて返す。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT city_code FROM evaluations WHERE listing_url = ?", (url,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# resolve_city_code のユニットテスト
# ---------------------------------------------------------------------------

class TestResolveCityCode:
    """住所文字列から市区町村コードを判定できること。"""

    def test_chofu_full_address(self):
        # "東京都調布市..." → 調布市コード
        assert resolve_city_code("東京都調布市曙町") == CHOFU_CODE

    def test_fuchu_full_address(self):
        # "東京都府中市..." → 府中市コード
        assert resolve_city_code("東京都府中市中町") == FUCHU_CODE

    def test_inagi_full_address(self):
        # "東京都稲城市..." → 稲城市コード
        assert resolve_city_code("東京都稲城市矢野口") == INAGI_CODE

    def test_city_name_only_matches(self):
        # 都道府県なしの "調布市" だけでも判定できる
        assert resolve_city_code("調布市") == CHOFU_CODE

    def test_outside_three_cities_returns_none(self):
        # 3市外（世田谷区など）は None
        assert resolve_city_code("東京都世田谷区砧") is None

    def test_unknown_location_string_returns_none(self):
        # scraper が所在地を取得できなかった場合の文字列
        assert resolve_city_code("（所在地不明）") is None

    def test_empty_string_returns_none(self):
        assert resolve_city_code("") is None


# ---------------------------------------------------------------------------
# 多エリア評価の統合テスト
# ---------------------------------------------------------------------------

class TestMultiAreaEvaluate:
    """
    各市の物件が正しいエリアコードのカーブで評価・保存されること。
    DB の city_code カラムで確認する。
    """

    def test_chofu_listing_saved_with_chofu_code(self, db_path):
        listing = make_listing("東京都調布市曙町", url="https://suumo.jp/test/chofu/")
        evaluate_and_save([listing], city_code=CHOFU_CODE, db_path=db_path)
        assert _get_city_codes(db_path, listing.url) == [CHOFU_CODE]

    def test_fuchu_listing_saved_with_fuchu_code(self, db_path):
        listing = make_listing("東京都府中市中町", url="https://suumo.jp/test/fuchu/")
        evaluate_and_save([listing], city_code=FUCHU_CODE, db_path=db_path)
        assert _get_city_codes(db_path, listing.url) == [FUCHU_CODE]

    def test_inagi_listing_saved_with_inagi_code(self, db_path):
        listing = make_listing("東京都稲城市矢野口", url="https://suumo.jp/test/inagi/")
        evaluate_and_save([listing], city_code=INAGI_CODE, db_path=db_path)
        assert _get_city_codes(db_path, listing.url) == [INAGI_CODE]

    def test_unknown_city_not_saved_to_db(self, db_path):
        """
        市コードが判定できない物件はグルーピングから除外され DB に保存されない。
        scraper.main() のグルーピングロジックを直接再現して確認する。
        """
        chofu_listing   = make_listing("東京都調布市曙町",    url="https://suumo.jp/test/ok/")
        unknown_listing = make_listing("東京都世田谷区砧",    url="https://suumo.jp/test/ng/")

        # scraper.main() のグルーピングを再現
        city_groups: dict[str, list[Listing]] = {}
        for l in [chofu_listing, unknown_listing]:
            code = resolve_city_code(l.location)
            if code is not None:
                city_groups.setdefault(code, []).append(l)
        for city_code, listings_for_city in city_groups.items():
            evaluate_and_save(listings_for_city, city_code=city_code, db_path=db_path)

        # 調布は保存されている
        assert _get_city_codes(db_path, chofu_listing.url) == [CHOFU_CODE]
        # 世田谷区はグルーピングされていないため DB に行がない
        assert _get_city_codes(db_path, unknown_listing.url) == []

    def test_mixed_three_cities_each_correct_code(self, db_path):
        """
        調布・府中・稲城の3市を混在させてグルーピングしたとき、
        それぞれ正しいエリアコードで保存されること。
        """
        listings = [
            make_listing("東京都調布市曙町",    url="https://suumo.jp/test/mix-chofu/"),
            make_listing("東京都府中市中町",    url="https://suumo.jp/test/mix-fuchu/"),
            make_listing("東京都稲城市矢野口",  url="https://suumo.jp/test/mix-inagi/"),
        ]

        # scraper.main() のグルーピングを再現
        city_groups: dict[str, list[Listing]] = {}
        for l in listings:
            code = resolve_city_code(l.location)
            if code is not None:
                city_groups.setdefault(code, []).append(l)
        for city_code, listings_for_city in city_groups.items():
            evaluate_and_save(listings_for_city, city_code=city_code, db_path=db_path)

        chofu_url = "https://suumo.jp/test/mix-chofu/"
        fuchu_url = "https://suumo.jp/test/mix-fuchu/"
        inagi_url = "https://suumo.jp/test/mix-inagi/"

        assert _get_city_codes(db_path, chofu_url) == [CHOFU_CODE], "調布は13208で保存"
        assert _get_city_codes(db_path, fuchu_url) == [FUCHU_CODE], "府中は13206で保存"
        assert _get_city_codes(db_path, inagi_url) == [INAGI_CODE], "稲城は13225で保存"

    def test_grouping_does_not_cross_contaminate(self, db_path):
        """
        府中物件に調布のカーブを使った場合、city_code が異なる行が残らないこと。
        （正しいグルーピングの副作用テスト）
        """
        listing = make_listing("東京都府中市中町", url="https://suumo.jp/test/fuchu-only/")
        evaluate_and_save([listing], city_code=FUCHU_CODE, db_path=db_path)
        # 調布コードの行は存在しない
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE listing_url = ? AND city_code != ?",
            (listing.url, FUCHU_CODE),
        ).fetchone()[0]
        conn.close()
        assert count == 0
