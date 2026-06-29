"""
tests/test_price_drop.py
値下げ・スコア改善検知と価格変動通知の単体・統合テスト。

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー不要。
pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/

テストの構成:
    1. TestDetectChanges         — detect_changes のユニットテスト（DB 直接操作）
    2. TestPriceDropIntegration  — evaluate_and_save 経由の統合テスト
       ★ 重複抑制の回帰テスト（2日連続同価格→2日目は通知しない）を含む
    3. TestBuildTextPriceDrop    — 通知文面の内容テスト
    4. TestNotifyLinePriceDrops  — LINE 送信のテスト（API モック）
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import evaluator
import scraper
from evaluator import (
    PRICE_DROP_THRESHOLD,
    SCORE_GAIN_THRESHOLD,
    detect_changes,
    evaluate_and_save,
)
from scraper import (
    Listing,
    _build_text_price_drop,
    notify_line_price_drops,
)

# ---------------------------------------------------------------------------
# 共通定数・フィクスチャ
# ---------------------------------------------------------------------------

CHOFU_CODE = "13208"
BASE_URL   = "https://suumo.jp/test/99999/"


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件マンション101",
        price="4,200万円",
        location="東京都調布市曙町",
        url=BASE_URL,
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


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
    return tmp_path / "test_evaluations.db"


@pytest.fixture
def line_env(monkeypatch):
    """LINE 認証情報をモジュール変数に注入する。"""
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")


def _insert_row(
    db_path: Path,
    url: str,
    name: str,
    date_str: str,
    asking_price: float,
    resale_score: int,
) -> None:
    """
    detect_changes のテスト用に最小限の評価行を直接 DB に挿入する。
    evaluate_and_save を経由しないため、resale_score を任意に指定できる。
    """
    conn = sqlite3.connect(db_path)
    # テーブルが未作成の場合に備えて初期化
    evaluator._init_db(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO evaluations
            (listing_url, listing_name, city_code,
             evaluated_date, evaluated_at,
             asking_price, resale_score, notes, hold_years)
        VALUES (?, ?, '13208', ?, datetime('now'), ?, ?, '[]', 10)
        """,
        (url, name, date_str, asking_price, resale_score),
    )
    conn.commit()
    conn.close()


def _collect_sent_texts(mock_post) -> list[str]:
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


# ---------------------------------------------------------------------------
# 1. detect_changes のユニットテスト（DB 行を直接挿入）
# ---------------------------------------------------------------------------

class TestDetectChanges:
    """detect_changes 単体テスト。DB行を直接操作して境界値を正確に検証する。"""

    def test_price_drop_above_threshold_detected(self, db_path):
        # 300万円の値下げ（50万円の閾値を超える）→ アラート発生
        url = "https://suumo.jp/test/drop-300/"
        _insert_row(db_path, url, "値下げ物件", "2026-06-01", 45_000_000, 72)
        _insert_row(db_path, url, "値下げ物件", "2026-06-02", 42_000_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["price_drop"] == 3_000_000

    def test_price_drop_below_threshold_not_detected(self, db_path):
        # 10万円の値下げ（閾値 50万円未満）→ 通知しない
        url = "https://suumo.jp/test/drop-10/"
        _insert_row(db_path, url, "小値下げ物件", "2026-06-01", 42_000_000, 72)
        _insert_row(db_path, url, "小値下げ物件", "2026-06-02", 41_900_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 0

    def test_price_drop_exactly_at_threshold_detected(self, db_path):
        # ちょうど 50万円の値下げ（閾値以上）→ アラート発生
        url = "https://suumo.jp/test/drop-exact/"
        _insert_row(db_path, url, "ちょうど閾値", "2026-06-01", 42_500_000, 72)
        _insert_row(db_path, url, "ちょうど閾値", "2026-06-02", 42_000_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["price_drop"] == PRICE_DROP_THRESHOLD

    def test_score_gain_above_threshold_detected(self, db_path):
        # 11点のスコア改善（10点の閾値を超える）→ アラート発生
        url = "https://suumo.jp/test/score-11/"
        _insert_row(db_path, url, "スコア改善物件", "2026-06-01", 42_000_000, 65)
        _insert_row(db_path, url, "スコア改善物件", "2026-06-02", 42_000_000, 76)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["score_gain"] == 11

    def test_score_gain_below_threshold_not_detected(self, db_path):
        # 5点のスコア改善（閾値 10点未満）→ 通知しない
        url = "https://suumo.jp/test/score-5/"
        _insert_row(db_path, url, "小スコア改善", "2026-06-01", 42_000_000, 65)
        _insert_row(db_path, url, "小スコア改善", "2026-06-02", 42_000_000, 70)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 0

    def test_score_gain_exactly_at_threshold_detected(self, db_path):
        # ちょうど 10点の改善（閾値以上）→ アラート発生
        url = "https://suumo.jp/test/score-exact/"
        _insert_row(db_path, url, "ちょうど閾値", "2026-06-01", 42_000_000, 65)
        _insert_row(db_path, url, "ちょうど閾値", "2026-06-02", 42_000_000, 75)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["score_gain"] == SCORE_GAIN_THRESHOLD

    def test_no_previous_evaluation_no_alert(self, db_path):
        # 前回評価がない（初日のみ）→ 通知しない
        _insert_row(db_path, BASE_URL, "初日物件", "2026-06-01", 42_000_000, 72)
        alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-01")
        assert len(alerts) == 0

    def test_same_price_next_day_no_alert(self, db_path):
        # 重複抑制ユニットテスト: 2日連続同価格で差分=0 → 通知しない
        url = "https://suumo.jp/test/same-price/"
        _insert_row(db_path, url, "同価格物件", "2026-06-01", 42_000_000, 72)
        _insert_row(db_path, url, "同価格物件", "2026-06-02", 42_000_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 0

    def test_price_increase_not_detected(self, db_path):
        # 値上がりは通知しない（price_drop が負になるため閾値を超えない）
        url = "https://suumo.jp/test/price-up/"
        _insert_row(db_path, url, "値上がり物件", "2026-06-01", 42_000_000, 72)
        _insert_row(db_path, url, "値上がり物件", "2026-06-02", 45_000_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 0

    def test_alert_contains_required_fields(self, db_path):
        # アラート dict に必要なキーが揃っていること
        url = "https://suumo.jp/test/fields/"
        _insert_row(db_path, url, "フィールド確認物件", "2026-06-01", 45_000_000, 72)
        _insert_row(db_path, url, "フィールド確認物件", "2026-06-02", 42_000_000, 72)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        alert = alerts[0]
        for key in ("url", "name", "today", "prev", "price_drop", "score_gain"):
            assert key in alert, f"アラートに '{key}' キーがない"

    def test_empty_url_list_returns_empty(self, db_path):
        assert detect_changes([], db_path=db_path) == []

    def test_nonexistent_db_returns_empty(self, tmp_path):
        assert detect_changes([BASE_URL], db_path=tmp_path / "no.db") == []

    def test_both_price_drop_and_score_gain_in_one_alert(self, db_path):
        # 値下げとスコア改善が同時に起きた場合も1アラートに含まれること
        url = "https://suumo.jp/test/both/"
        _insert_row(db_path, url, "両方変動", "2026-06-01", 45_000_000, 65)
        _insert_row(db_path, url, "両方変動", "2026-06-02", 42_000_000, 76)
        alerts = detect_changes([url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["price_drop"] == 3_000_000
        assert alerts[0]["score_gain"] == 11


# ---------------------------------------------------------------------------
# 2. evaluate_and_save 経由の統合テスト（毎日全件評価の状況を再現）
# ---------------------------------------------------------------------------

class TestPriceDropIntegration:
    """
    evaluate_and_save と detect_changes の統合テスト。
    「毎日 current 全件が評価され DB 行が積まれる」状況を再現する。
    """

    def test_no_alert_on_same_price_next_day(self, db_path):
        """
        ★ 重複抑制の回帰テスト（必須）

        同じ物件を2日連続で同じ価格で evaluate_and_save したとき、
        2日目に detect_changes を呼んでもアラートが出ないこと。

        「直前の別日との差分=0 → 通知なし」という設計が
        evaluate_and_save 経由でも成立することを確認する。
        """
        listing = make_listing(price="4,200万円")

        # 1日目: current 全件として評価（今後の比較基準になる行）
        evaluate_and_save([listing], CHOFU_CODE, db_path=db_path, _evaluated_date="2026-06-01")
        # 2日目: 同じ価格で再評価（DB に 6/2 の行が追加される）
        evaluate_and_save([listing], CHOFU_CODE, db_path=db_path, _evaluated_date="2026-06-02")

        # 2日目の detect_changes → 前回(6/1)との差は 0 → アラートなし
        alerts = detect_changes([listing.url], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 0, (
            "2日連続で同価格のとき、2日目は値下げ通知されてはいけない"
        )

    def test_price_drop_detected_via_evaluate(self, db_path):
        # evaluate_and_save を通じた値下げ検知
        evaluate_and_save(
            [make_listing(price="4,500万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-06-01",
        )
        evaluate_and_save(
            [make_listing(price="4,200万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-06-02",
        )
        alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-02")
        assert len(alerts) == 1
        assert alerts[0]["price_drop"] == 3_000_000

    def test_drop_then_same_price_no_second_alert(self, db_path):
        """
        3日間シナリオ: 値下げ検知翌日に同価格でも再通知しないこと。

        6/1: 4,500万 → 6/2: 4,200万（値下げ検知）
        6/2: 4,200万 → 6/3: 4,200万（差分0 → 通知なし）
        """
        evaluate_and_save(
            [make_listing(price="4,500万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-06-01",
        )
        evaluate_and_save(
            [make_listing(price="4,200万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-06-02",
        )
        evaluate_and_save(
            [make_listing(price="4,200万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-06-03",
        )

        alerts_day2 = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-02")
        alerts_day3 = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-03")

        assert len(alerts_day2) == 1, "6/2 は値下げを検知すること"
        assert len(alerts_day3) == 0, "6/3 は同価格のため通知しないこと"

    def test_no_alert_on_first_day_only(self, db_path):
        # 1日分しか履歴がない（初回評価）→ 通知しない
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path, _evaluated_date="2026-06-01")
        alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-01")
        assert len(alerts) == 0

    def test_three_days_same_price_all_silent(self, db_path):
        # 3日連続同価格でもすべての日に通知しない
        for d in ["2026-06-01", "2026-06-02", "2026-06-03"]:
            evaluate_and_save(
                [make_listing(price="4,200万円")], CHOFU_CODE,
                db_path=db_path, _evaluated_date=d,
            )
        alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-06-03")
        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# 3. 通知文面のテスト
# ---------------------------------------------------------------------------

class TestBuildTextPriceDrop:

    def _make_alert(self, price_drop: int = 3_000_000, score_gain: int = 0) -> dict:
        return {
            "url":        BASE_URL,
            "name":       "テスト物件マンション101",
            "price_drop": price_drop,
            "score_gain": score_gain,
            "today": {
                "asking_price":   42_000_000.0,
                "resale_score":   72,
                "evaluated_date": "2026-06-27",
            },
            "prev": {
                "asking_price":   45_000_000.0,
                "resale_score":   72,
                "evaluated_date": "2026-06-26",
            },
        }

    def test_contains_listing_name(self):
        assert "テスト物件マンション101" in _build_text_price_drop(self._make_alert())

    def test_contains_url(self):
        assert BASE_URL in _build_text_price_drop(self._make_alert())

    def test_contains_both_prices(self):
        text = _build_text_price_drop(self._make_alert())
        assert "4500万円" in text   # 前回価格
        assert "4200万円" in text   # 今回価格

    def test_contains_drop_amount(self):
        text = _build_text_price_drop(self._make_alert(price_drop=3_000_000))
        assert "300万円" in text

    def test_contains_drop_pct(self):
        # 300万 / 4500万 ≈ 6.7%
        text = _build_text_price_drop(self._make_alert(price_drop=3_000_000))
        assert "6.7%" in text

    def test_contains_both_dates(self):
        text = _build_text_price_drop(self._make_alert())
        assert "2026-06-26" in text   # 前回評価日
        assert "2026-06-27" in text   # 今回評価日

    def test_score_gain_shown_with_plus(self):
        alert = self._make_alert(score_gain=8)
        alert["today"]["resale_score"] = 73
        alert["prev"]["resale_score"]  = 65
        text = _build_text_price_drop(alert)
        assert "+8点" in text

    def test_score_no_change_shown(self):
        text = _build_text_price_drop(self._make_alert(score_gain=0))
        assert "変化なし" in text

    def test_price_no_change_when_drop_zero(self):
        alert = self._make_alert(price_drop=0, score_gain=11)
        alert["today"]["asking_price"] = 42_000_000.0
        alert["prev"]["asking_price"]  = 42_000_000.0
        text = _build_text_price_drop(alert)
        assert "変化なし" in text


# ---------------------------------------------------------------------------
# 4. notify_line_price_drops のテスト（LINE API をモック）
# ---------------------------------------------------------------------------

class TestNotifyLinePriceDrops:

    def _make_alert(self) -> dict:
        return {
            "url":        BASE_URL,
            "name":       "テスト物件マンション101",
            "price_drop": 3_000_000,
            "score_gain": 0,
            "today": {
                "asking_price":   42_000_000.0,
                "resale_score":   72,
                "evaluated_date": "2026-06-27",
            },
            "prev": {
                "asking_price":   45_000_000.0,
                "resale_score":   72,
                "evaluated_date": "2026-06-26",
            },
        }

    def test_empty_alerts_no_api_call(self, line_env):
        with patch("scraper.requests.post") as mock_post:
            notify_line_price_drops([])
        mock_post.assert_not_called()

    def test_header_contains_emoji_and_count(self, line_env):
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops([self._make_alert()])
        sent = _collect_sent_texts(mock_post)
        assert any("📉" in t and "1件" in t for t in sent)

    def test_body_contains_listing_name(self, line_env):
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops([self._make_alert()])
        sent = _collect_sent_texts(mock_post)
        assert any("テスト物件マンション101" in t for t in sent)

    def test_no_line_token_skips_without_exception(self, monkeypatch):
        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", None)
        monkeypatch.setattr(scraper, "LINE_USER_ID", None)
        with patch("scraper.requests.post") as mock_post:
            notify_line_price_drops([self._make_alert()])
        mock_post.assert_not_called()
