"""
tests/test_relisting.py
再掲載検知（STEP 2.5）の単体・統合テスト。

- get_last_observed_attrs（evaluator.py）
- _attrs_match / classify_relisting（scraper.py）
- notify_line_relisted の重複抑制・価格差併記

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー不要。
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import evaluator
from evaluator import get_last_observed_attrs
from scraper import Listing, _attrs_match, classify_relisting, notify_line_relisted


@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


@pytest.fixture
def line_env(monkeypatch):
    import scraper
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")


def _insert_eval_row(
    db_path: Path,
    url: str,
    name: str,
    date_str: str,
    asking_price: float,
    area_sqm: float = 91.13,
    building_year: int = 2008,
    floor_plan: str = "4LDK",
) -> None:
    conn = sqlite3.connect(db_path)
    evaluator._init_db(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO evaluations
            (listing_url, listing_name, city_code,
             evaluated_date, evaluated_at,
             asking_price, area_sqm, building_year, floor_plan,
             resale_score, notes, hold_years)
        VALUES (?, ?, '13206', ?, datetime('now'), ?, ?, ?, ?, 72, '[]', 10)
        """,
        (url, name, date_str, asking_price, area_sqm, building_year, floor_plan),
    )
    conn.commit()
    conn.close()


def _listing(url: str = "https://suumo.jp/test/nc_default/", price: str = "5490万円",
             area: str = "91.13m2（壁芯）", floor_plan: str = "4LDK", age: str = "2008年3月") -> Listing:
    return Listing(
        name=f"是政の物件 {url}",
        price=price,
        location="東京都府中市是政４",
        url=url,
        floor_plan=floor_plan,
        area=area,
        age=age,
    )


# ---------------------------------------------------------------------------
# get_last_observed_attrs
# ---------------------------------------------------------------------------

class TestGetLastObservedAttrs:

    def test_returns_none_when_db_missing(self, tmp_path):
        missing_db = tmp_path / "nonexistent.db"
        assert get_last_observed_attrs("https://suumo.jp/test/x/", db_path=missing_db) is None

    def test_returns_none_when_no_history(self, db_path):
        # DBは存在するがこのURLの行は無い
        conn = sqlite3.connect(db_path)
        evaluator._init_db(conn)
        conn.close()
        assert get_last_observed_attrs("https://suumo.jp/test/unknown/", db_path=db_path) is None

    def test_returns_first_and_last_date_with_latest_attrs(self, db_path):
        url = "https://suumo.jp/test/nc_20893454/"
        _insert_eval_row(db_path, url, "是政の物件", "2026-06-30", 54_900_000)
        _insert_eval_row(db_path, url, "是政の物件", "2026-07-05", 54_900_000)
        _insert_eval_row(db_path, url, "是政の物件", "2026-07-06", 54_900_000)

        result = get_last_observed_attrs(url, db_path=db_path)

        assert result["first_date"] == "2026-06-30"
        assert result["last_date"] == "2026-07-06"
        assert result["area_sqm"] == 91.13
        assert result["building_year"] == 2008
        assert result["floor_plan"] == "4LDK"
        assert result["asking_price"] == 54_900_000.0


# ---------------------------------------------------------------------------
# _attrs_match
# ---------------------------------------------------------------------------

class TestAttrsMatch:

    def _prev(self, **overrides):
        base = dict(area_sqm=91.13, building_year=2008, floor_plan="4LDK")
        base.update(overrides)
        return base

    def test_exact_match(self):
        listing = _listing()
        assert _attrs_match(listing, self._prev()) is True

    def test_area_within_tolerance_matches(self):
        # 91.13 vs 91.10 → 差0.03 (<0.05) は一致とみなす
        listing = _listing(area="91.10m2（壁芯）")
        assert _attrs_match(listing, self._prev()) is True

    def test_area_far_apart_does_not_match(self):
        # 91.13 → 65.00（大きく異なる面積） → URL使い回しの可能性
        listing = _listing(area="65.00m2（壁芯）")
        assert _attrs_match(listing, self._prev()) is False

    def test_building_year_mismatch_does_not_match(self):
        listing = _listing(age="1995年3月")
        assert _attrs_match(listing, self._prev()) is False

    def test_floor_plan_mismatch_does_not_match(self):
        listing = _listing(floor_plan="2LDK")
        assert _attrs_match(listing, self._prev()) is False

    def test_all_attrs_none_falls_back_to_safe_no_match(self):
        # 劣化ケース: prev側の3属性が全てNone → 比較可能なキーが1つもない
        # → 安全側（新着扱い＝不一致）に倒れる
        listing = _listing()
        prev = dict(area_sqm=None, building_year=None, floor_plan=None)
        assert _attrs_match(listing, prev) is False

    def test_partial_none_skips_that_key_only(self):
        # floor_plan側がNone（比較不能）でも、area・building_yearが一致すれば一致とみなす
        listing = _listing()
        prev = self._prev(floor_plan=None)
        assert _attrs_match(listing, prev) is True


# ---------------------------------------------------------------------------
# classify_relisting
# ---------------------------------------------------------------------------

class TestClassifyRelisting:

    def test_relisted_example_nc_20893454(self, db_path):
        """実例再現: data.csvに不在・evaluationsに14日前からの履歴・属性一致
        → new_listingsに入らずrelistedに入る。"""
        url = "https://suumo.jp/test/nc_20893454/"
        _insert_eval_row(db_path, url, "是政の物件", "2026-06-30", 54_900_000)
        _insert_eval_row(db_path, url, "是政の物件", "2026-07-05", 54_900_000)

        listing = _listing(url)
        candidates, relisted, url_reused = classify_relisting(
            [listing], known_urls=set(), db_path=db_path,
        )

        assert candidates == []
        assert len(relisted) == 1
        assert relisted[0][0].url == url
        assert relisted[0][1]["first_date"] == "2026-06-30"
        assert url_reused == []

    def test_url_reused_defense_large_area_mismatch(self, db_path):
        """URL使い回し防御: 同一URLでarea_sqmが大きく異なる
        （90.02→65.00）→ 新着として扱われ url_reused に記録される。"""
        url = "https://suumo.jp/test/url-reused/"
        _insert_eval_row(db_path, url, "旧物件", "2026-06-30", 45_000_000, area_sqm=90.02)

        new_listing = _listing(url, area="65.00m2（壁芯）")
        candidates, relisted, url_reused = classify_relisting(
            [new_listing], known_urls=set(), db_path=db_path,
        )

        assert len(candidates) == 1
        assert candidates[0].url == url
        assert relisted == []
        assert url_reused == [url]

    def test_known_url_is_skipped_entirely(self, db_path):
        # data.csv（known_urls）に既にあるURLは判定対象外
        url = "https://suumo.jp/test/known/"
        listing = _listing(url)
        candidates, relisted, url_reused = classify_relisting(
            [listing], known_urls={url}, db_path=db_path,
        )
        assert candidates == []
        assert relisted == []
        assert url_reused == []

    def test_genuinely_new_with_no_history_goes_to_candidates(self, db_path):
        url = "https://suumo.jp/test/brand-new/"
        listing = _listing(url)
        candidates, relisted, url_reused = classify_relisting(
            [listing], known_urls=set(), db_path=db_path,
        )
        assert candidates == [listing]
        assert relisted == []
        assert url_reused == []


# ---------------------------------------------------------------------------
# notify_line_relisted
# ---------------------------------------------------------------------------

class TestNotifyLineRelisted:

    def test_price_diff_shown_as_effective_discount(self, db_path, line_env):
        """再掲載の価格差併記: 前回観測時5,490万→今回5,290万なら
        「実質値下げ ↓200万円」が本文に入る。"""
        url = "https://suumo.jp/test/nc_relist_price/"
        listing = _listing(url, price="5290万円")
        prev = {
            "area_sqm": 91.13, "building_year": 2008, "floor_plan": "4LDK",
            "asking_price": 54_900_000.0,
            "first_date": "2026-06-30", "last_date": "2026-07-07",
        }

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_relisted([(listing, prev)], db_path=db_path)

        sent_text = mock_post.call_args.kwargs["json"]["messages"][0]["text"]
        assert "実質値下げ ↓200万円" in sent_text
        assert "5490万円" in sent_text

    def test_same_last_date_relist_not_notified_twice(self, db_path, line_env):
        """再掲載通知の重複抑制: 同じlast_dateからの再出現は2回目以降通知されない。"""
        url = "https://suumo.jp/test/nc_relist_dedup/"
        listing = _listing(url)
        prev = {
            "area_sqm": 91.13, "building_year": 2008, "floor_plan": "4LDK",
            "asking_price": 54_900_000.0,
            "first_date": "2026-06-30", "last_date": "2026-07-07",
        }

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_relisted([(listing, prev)], db_path=db_path)
            assert mock_post.call_count == 1

            # 同じ last_date からの再出現を2回目実行 → 通知されない
            notify_line_relisted([(listing, prev)], db_path=db_path)
            assert mock_post.call_count == 1  # 増えない

    def test_different_last_date_relist_notifies_again(self, db_path, line_env):
        # last_date が異なる（=別の消滅サイクル）なら再度通知してよい
        url = "https://suumo.jp/test/nc_relist_new_cycle/"
        listing = _listing(url)
        prev1 = {
            "area_sqm": 91.13, "building_year": 2008, "floor_plan": "4LDK",
            "asking_price": 54_900_000.0,
            "first_date": "2026-06-30", "last_date": "2026-07-07",
        }
        prev2 = {**prev1, "last_date": "2026-07-10"}

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_relisted([(listing, prev1)], db_path=db_path)
            notify_line_relisted([(listing, prev2)], db_path=db_path)
            assert mock_post.call_count == 2
