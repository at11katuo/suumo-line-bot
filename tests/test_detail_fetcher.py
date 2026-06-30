"""
tests/test_detail_fetcher.py
============================
detail_fetcher モジュールのユニット・統合テスト。

テスト対象:
  1. パース関数（_parse_total_units, _parse_repair_fund_monthly）
  2. ㎡換算（repair_fund_monthly ÷ area_sqm → repair_fund_per_sqm）
  3. スコアへの反映（total_units / repair_fund_per_sqm が Candidate 経由でスコアに影響）
  4. フォールバック（fetch_detail 失敗 → 物件がスキップされない・中立スコアになる）
  5. fetch_detail（requests.get をモック・ネットワークアクセスなし）
  6. DBキャッシュ操作（save_detail_cache / load_detail_cache / get_uncached_urls）

全テストはネットワークアクセスなし（requests.get をモック or 使用しない）。
USE_MOCK_REINFOLIB=1 環境変数は不要（API 呼び出しなし）。
"""

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import requests as req_module

from detail_fetcher import (
    _parse_repair_fund_monthly,
    _parse_total_units,
    DETAIL_TIMEOUT,
    fetch_detail,
    get_uncached_urls,
    load_detail_cache,
    save_detail_cache,
)
from reinfolib_resale import Candidate, DepreciationCurve, estimate_resale
from scraper import Listing
from suumo_adapter import suumo_to_candidate


# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

def _make_listing(
    url: str = "https://suumo.jp/test/nc_99999/",
    area: str = "70.6m²",
    price: str = "5,899万円",
    age: str = "2011年2月",
    station: str = "京王線 中河原駅 徒歩4分",
) -> Listing:
    """テスト用 Listing（クリオ府中中河原 相当）。"""
    return Listing(
        name="クリオ府中中河原",
        price=price,
        location="東京都府中市住吉町２",
        url=url,
        station=station,
        floor_plan="2LDK+S",
        area=area,
        age=age,
    )


def _make_curve() -> DepreciationCurve:
    """スコア計算用のダミー減価カーブ。値の大小は問わない。"""
    curve = DepreciationCurve()
    # 2011年築・2026年評価 → current_age=15 → バケット (11,15)
    # 保有10年後 → future_age=25 → バケット (21,25)
    curve.median_unit_price = {(11, 15): 900_000, (21, 25): 750_000}
    curve.sample_count      = {(11, 15): 10,      (21, 25): 10}
    return curve


def _make_base_candidate() -> Candidate:
    """
    total_units=None / repair_fund_per_sqm=None のベース Candidate。
    スコア計算のベースラインに使う。
    - walk_minutes=4 (≤7) → +15
    - area_sqm=70.6 (65-80) → +10
    - future_age=25 (>20, ≤25) → ±0
    → ベーススコア = 50 + 15 + 10 = 75
    """
    return Candidate(
        asking_price=58_990_000,
        area_sqm=70.6,
        building_year=2011,
        walk_minutes=4,
        total_units=None,
        repair_fund_per_sqm=None,
        floor_plan="2LDK+S",
    )


def _get_score(cand: Candidate) -> int:
    return estimate_resale(cand, _make_curve(), current_year=2026, hold_years=10).resale_score


# ---------------------------------------------------------------------------
# 1. パース関数のユニットテスト
# ---------------------------------------------------------------------------

class TestParseTotalUnits:
    def test_normal_units(self):
        assert _parse_total_units("32戸") == 32

    def test_large_units(self):
        assert _parse_total_units("100戸") == 100

    def test_small_units(self):
        assert _parse_total_units("15戸") == 15

    def test_dash_returns_none(self):
        assert _parse_total_units("-") is None

    def test_empty_returns_none(self):
        assert _parse_total_units("") is None

    def test_no_unit_char_returns_none(self):
        # 「戸」のない文字列はパース失敗
        assert _parse_total_units("32") is None


class TestParseRepairFundMonthly:
    def test_man_plus_remaining(self):
        # "2万4080円／月" → 24080.0
        assert _parse_repair_fund_monthly("2万4080円／月") == pytest.approx(24080.0)

    def test_man_plus_remaining_with_note(self):
        # "1万7230円／月（委託(通勤)）" → 17230.0（括弧内はパースに影響しない）
        assert _parse_repair_fund_monthly("1万7230円／月（委託(通勤)）") == pytest.approx(17230.0)

    def test_comma_format(self):
        # "12,300円／月" → 12300.0
        assert _parse_repair_fund_monthly("12,300円／月") == pytest.approx(12300.0)

    def test_plain_yen(self):
        # "5000円" → 5000.0（「／月」なくてもパースできる）
        assert _parse_repair_fund_monthly("5000円") == pytest.approx(5000.0)

    def test_hankaku_dash_returns_none(self):
        assert _parse_repair_fund_monthly("-") is None

    def test_zenkaku_dash_returns_none(self):
        assert _parse_repair_fund_monthly("－") is None

    def test_empty_returns_none(self):
        assert _parse_repair_fund_monthly("") is None

    def test_none_input_returns_none(self):
        # None は空文字列扱いで None を返す
        assert _parse_repair_fund_monthly(None) is None


# ---------------------------------------------------------------------------
# 2. ㎡換算テスト（repair_fund_monthly ÷ area_sqm = repair_fund_per_sqm）
#
# ここが最重要テスト。健全な修繕積立金が「安すぎ」と誤減点されないことを確認する。
# ---------------------------------------------------------------------------

class TestRepairFundConversion:
    def test_healthy_fund_above_200_threshold(self):
        # 24,080円 ÷ 70.6㎡ ≈ 341円/㎡ → 200円以上 → 減点なし（健全）
        listing = _make_listing(area="70.6m²")
        detail  = {"total_units": 32, "repair_fund_monthly": 24080.0}
        cand    = suumo_to_candidate(listing, detail=detail)

        assert cand is not None
        assert cand.repair_fund_per_sqm == pytest.approx(24080.0 / 70.6, rel=1e-3)
        assert cand.repair_fund_per_sqm >= 200, (
            f"健全な修繕積立金({cand.repair_fund_per_sqm:.1f}円/㎡)が誤減点の閾値を下回っている"
        )

    def test_low_fund_below_200_threshold(self):
        # 10,000円 ÷ 70.6㎡ ≈ 142円/㎡ → 200円未満 → 減点あり
        listing = _make_listing(area="70.6m²")
        detail  = {"total_units": 32, "repair_fund_monthly": 10000.0}
        cand    = suumo_to_candidate(listing, detail=detail)

        assert cand is not None
        assert cand.repair_fund_per_sqm == pytest.approx(10000.0 / 70.6, rel=1e-3)
        assert cand.repair_fund_per_sqm < 200

    def test_none_repair_fund_stays_none(self):
        # 修繕積立金が取得できなかった場合 → repair_fund_per_sqm も None
        listing = _make_listing(area="70.6m²")
        detail  = {"total_units": 32, "repair_fund_monthly": None}
        cand    = suumo_to_candidate(listing, detail=detail)

        assert cand is not None
        assert cand.repair_fund_per_sqm is None

    def test_no_detail_gives_none_fields(self):
        # detail=None（従来の動作）→ total_units=None, repair_fund_per_sqm=None
        listing = _make_listing()
        cand    = suumo_to_candidate(listing, detail=None)

        assert cand is not None
        assert cand.total_units is None
        assert cand.repair_fund_per_sqm is None


# ---------------------------------------------------------------------------
# 3. スコアへの反映テスト
# ---------------------------------------------------------------------------

class TestScoreReflection:
    """total_units / repair_fund_per_sqm が Candidate 経由でスコアに正しく反映される。"""

    def test_base_score_is_75(self):
        # ベーススコア確認: 50 + 徒歩4分(+15) + 70.6㎡(+10) + future_age25(±0) = 75
        cand = _make_base_candidate()
        assert _get_score(cand) == 75

    def test_large_complex_adds_8_points(self):
        # 総戸数 >= 50 → +8点
        cand = _make_base_candidate()
        cand.total_units = 50
        assert _get_score(cand) == 75 + 8

        cand2 = _make_base_candidate()
        cand2.total_units = 100
        assert _get_score(cand2) == 75 + 8

    def test_small_complex_subtracts_8_points(self):
        # 総戸数 < 20 → -8点
        cand = _make_base_candidate()
        cand.total_units = 19
        assert _get_score(cand) == 75 - 8

        cand2 = _make_base_candidate()
        cand2.total_units = 1
        assert _get_score(cand2) == 75 - 8

    def test_mid_complex_no_change(self):
        # 20 ≤ 総戸数 < 50 → ±0（調布物件の32戸はここ）
        cand = _make_base_candidate()
        cand.total_units = 32
        assert _get_score(cand) == 75

        cand2 = _make_base_candidate()
        cand2.total_units = 20
        assert _get_score(cand2) == 75

        cand3 = _make_base_candidate()
        cand3.total_units = 49
        assert _get_score(cand3) == 75

    def test_low_repair_fund_subtracts_8_points(self):
        # repair_fund_per_sqm < 200円/㎡ → -8点
        cand = _make_base_candidate()
        cand.repair_fund_per_sqm = 150.0
        assert _get_score(cand) == 75 - 8

    def test_healthy_repair_fund_no_deduction(self):
        # repair_fund_per_sqm >= 200円/㎡ → ±0（24,080円÷70.6㎡≈341円/㎡）
        cand = _make_base_candidate()
        cand.repair_fund_per_sqm = 341.0
        assert _get_score(cand) == 75

    def test_real_values_fuchu_complex(self):
        # 府中物件の実値（32戸・24080円/月・70.6㎡）でスコアを確認
        # 32戸 → 20≤32<50 → ±0
        # 341円/㎡ → ≥200 → ±0
        # ベース75のまま
        listing = _make_listing(area="70.6m²")
        detail  = {"total_units": 32, "repair_fund_monthly": 24080.0}
        cand    = suumo_to_candidate(listing, detail=detail)
        assert cand is not None
        score = _get_score(cand)
        assert score == 75  # total_units=32（±0）, repair_fund_per_sqm≈341（±0）


# ---------------------------------------------------------------------------
# 4. フォールバックテスト（取得失敗でも物件がスキップされない）
# ---------------------------------------------------------------------------

class TestFallback:
    """
    fetch_detail が失敗（None を返す）しても、物件が評価・通知から消えないこと。
    スコアは「中立」（total_units / repair_fund_per_sqm が None のまま）になる。
    """

    def test_none_detail_does_not_skip_listing(self):
        # detail=None のとき suumo_to_candidate は None を返さず Candidate を返す
        listing = _make_listing()
        cand    = suumo_to_candidate(listing, detail=None)
        assert cand is not None, "detail取得失敗時に物件がスキップされた（フォールバック失敗）"

    def test_none_detail_gives_neutral_fields(self):
        # detail=None → total_units=None, repair_fund_per_sqm=None（中立スコア）
        listing = _make_listing()
        cand    = suumo_to_candidate(listing, detail=None)
        assert cand.total_units is None
        assert cand.repair_fund_per_sqm is None

    @patch("detail_fetcher.requests.get", side_effect=req_module.RequestException("接続失敗"))
    @patch("detail_fetcher.time.sleep")
    def test_fetch_detail_exception_returns_none(self, _mock_sleep, _mock_get):
        # requests.RequestException → fetch_detail が None を返す（bot は止まらない）
        result = fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)
        assert result is None

    @patch("detail_fetcher.requests.get")
    @patch("detail_fetcher.time.sleep")
    def test_fetch_detail_http_error_returns_none(self, _mock_sleep, mock_get):
        # HTTP 4xx/5xx → fetch_detail が None を返す
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_module.HTTPError("404")
        mock_get.return_value = mock_resp
        result = fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)
        assert result is None

    @patch("detail_fetcher.requests.get")
    @patch("detail_fetcher.time.sleep")
    def test_fetch_detail_timeout_returns_none(self, _mock_sleep, mock_get):
        # タイムアウト → fetch_detail が None を返す（Actions が固まらない）
        mock_get.side_effect = req_module.exceptions.Timeout("timeout")
        result = fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)
        assert result is None

    @patch("detail_fetcher.requests.get")
    @patch("detail_fetcher.time.sleep")
    def test_fetch_detail_uses_configured_timeout(self, _mock_sleep, mock_get):
        # timeout=DETAIL_TIMEOUT がリクエストに渡されていること
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = "<html><body><dt>総戸数ヒント</dt><dd>32戸</dd></body></html>"
        mock_resp.apparent_encoding = "utf-8"
        mock_get.return_value = mock_resp

        fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)

        _call_kwargs = mock_get.call_args[1]
        assert _call_kwargs.get("timeout") == DETAIL_TIMEOUT, (
            "timeout が設定されていない。GitHub Actions が無限待ちになる可能性がある。"
        )


# ---------------------------------------------------------------------------
# 5. fetch_detail のモックテスト（HTML 解析ロジック確認）
# ---------------------------------------------------------------------------

class TestFetchDetail:
    """requests.get をモックして fetch_detail の HTML 解析ロジックをテスト。"""

    def _make_html(self, total_units_val: str, repair_fund_val: str) -> str:
        """SUUMO 詳細ページ風 HTML（"ヒント"サフィックス付きラベル）。"""
        return (
            "<html><body>"
            f"<dt>総戸数ヒント</dt><dd>{total_units_val}</dd>"
            f"<dt>修繕積立金ヒント</dt><dd>{repair_fund_val}</dd>"
            "</body></html>"
        )

    @patch("detail_fetcher.requests.get")
    @patch("detail_fetcher.time.sleep")
    def test_fetch_success_normal_values(self, _mock_sleep, mock_get):
        # 正常取得: 総戸数=32, 修繕積立金=24080
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = self._make_html("32戸", "2万4080円／月")
        mock_resp.apparent_encoding = "utf-8"
        mock_get.return_value = mock_resp

        result = fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)

        assert result is not None
        assert result["total_units"] == 32
        assert result["repair_fund_monthly"] == pytest.approx(24080.0)

    @patch("detail_fetcher.requests.get")
    @patch("detail_fetcher.time.sleep")
    def test_fetch_dash_values_returns_none_fields(self, _mock_sleep, mock_get):
        # 値が "-" のとき → total_units=None, repair_fund_monthly=None（辞書自体は返る）
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = self._make_html("-", "-")
        mock_resp.apparent_encoding = "utf-8"
        mock_get.return_value = mock_resp

        result = fetch_detail("https://suumo.jp/test/nc_99999/", sleep_sec=0)

        assert result is not None
        assert result["total_units"] is None
        assert result["repair_fund_monthly"] is None


# ---------------------------------------------------------------------------
# 6. DBキャッシュ操作テスト
# ---------------------------------------------------------------------------

class TestDetailCacheDB:
    def test_save_and_load(self, tmp_path):
        url  = "https://suumo.jp/test/nc_99999/"
        data = {"total_units": 32, "repair_fund_monthly": 24080.0}
        db   = tmp_path / "test.db"

        save_detail_cache(url, data, db_path=db)
        result = load_detail_cache([url], db_path=db)

        assert url in result
        assert result[url]["total_units"] == 32
        assert result[url]["repair_fund_monthly"] == pytest.approx(24080.0)

    def test_load_missing_url_returns_empty(self, tmp_path):
        # DB 未作成の場合は空 dict を返す（例外を出さない）
        db     = tmp_path / "test.db"
        result = load_detail_cache(["https://suumo.jp/test/nc_00000/"], db_path=db)
        assert result == {}

    def test_save_null_values_as_failure_record(self, tmp_path):
        # 取得失敗（NULL値）も保存できる（「試み済み」フラグとして使う）
        url  = "https://suumo.jp/test/nc_99999/"
        data = {"total_units": None, "repair_fund_monthly": None}
        db   = tmp_path / "test.db"

        save_detail_cache(url, data, db_path=db)
        result = load_detail_cache([url], db_path=db)

        assert url in result
        assert result[url]["total_units"] is None
        assert result[url]["repair_fund_monthly"] is None

    def test_replace_on_duplicate_url(self, tmp_path):
        # 同じURLを2回保存すると上書き（INSERT OR REPLACE）
        url = "https://suumo.jp/test/nc_99999/"
        db  = tmp_path / "test.db"

        save_detail_cache(url, {"total_units": 30, "repair_fund_monthly": 10000.0}, db_path=db)
        save_detail_cache(url, {"total_units": 32, "repair_fund_monthly": 24080.0}, db_path=db)
        result = load_detail_cache([url], db_path=db)

        assert result[url]["total_units"] == 32
        assert result[url]["repair_fund_monthly"] == pytest.approx(24080.0)

    def test_load_multiple_urls(self, tmp_path):
        # 複数URLを一括ロードできる
        url_a = "https://suumo.jp/test/nc_1/"
        url_b = "https://suumo.jp/test/nc_2/"
        db    = tmp_path / "test.db"

        save_detail_cache(url_a, {"total_units": 32, "repair_fund_monthly": 24080.0}, db_path=db)
        save_detail_cache(url_b, {"total_units": 60, "repair_fund_monthly": 15000.0}, db_path=db)
        result = load_detail_cache([url_a, url_b], db_path=db)

        assert result[url_a]["total_units"] == 32
        assert result[url_b]["total_units"] == 60


# ---------------------------------------------------------------------------
# 7. get_uncached_urls テスト（新着のみ fetch する補強1の確認）
# ---------------------------------------------------------------------------

class TestGetUncachedUrls:
    def test_all_uncached_when_db_absent(self, tmp_path):
        # DB がない → 全件を未登録として返す
        urls   = ["https://suumo.jp/test/nc_1/", "https://suumo.jp/test/nc_2/"]
        result = get_uncached_urls(urls, db_path=tmp_path / "nonexistent.db")
        assert set(result) == set(urls)

    def test_cached_url_excluded(self, tmp_path):
        # 登録済みURLはスキップ、未登録URLは返す
        url_a = "https://suumo.jp/test/nc_1/"
        url_b = "https://suumo.jp/test/nc_2/"
        db    = tmp_path / "test.db"

        save_detail_cache(url_a, {"total_units": 32, "repair_fund_monthly": 24080.0}, db_path=db)
        result = get_uncached_urls([url_a, url_b], db_path=db)

        assert url_a not in result, "登録済みURLが再fetchされる（重複アクセス）"
        assert url_b in result,     "未登録URLが誤ってスキップされた"

    def test_null_cached_url_also_excluded(self, tmp_path):
        # NULL値（取得失敗）で保存されたURLも「試み済み」としてスキップ
        url = "https://suumo.jp/test/nc_99999/"
        db  = tmp_path / "test.db"

        save_detail_cache(url, {"total_units": None, "repair_fund_monthly": None}, db_path=db)
        result = get_uncached_urls([url], db_path=db)

        assert url not in result, "取得失敗記録があるURLが再fetchされた（重複アクセス）"

    def test_empty_urls_returns_empty(self, tmp_path):
        result = get_uncached_urls([], db_path=tmp_path / "test.db")
        assert result == []
