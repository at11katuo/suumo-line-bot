"""
tests/test_notify.py
notify_line_two_stage と関連するヘルパー関数の単体テスト。

全テストは LINE API を mock し、実際の送信は行わない。
pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import scraper
from scraper import (
    PROMISING_SCORE_THRESHOLD,
    PROMISING_VS_FAIR_MAX_PCT,
    Listing,
    _build_text_compact,
    _build_text_promising,
    _is_promising,
    notify_line_two_stage,
)


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件マンション101",
        price="4,200万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/99999/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


def make_est(**overrides) -> dict:
    """有望物件デフォルト値の評価結果 dict を返す。"""
    defaults = dict(
        resale_score=75,
        asking_vs_fair_pct=-3.2,
        future_resale_price=40_500_000,
        current_fair_unit_price=700_000,
        hold_years=10,
        notes=json.dumps([]),
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture
def line_env(monkeypatch):
    """LINE認証情報をモジュール変数に注入するフィクスチャ。"""
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")


def _collect_sent_texts(mock_post) -> list[str]:
    """requests.post の全呼び出しから送信テキストを収集する。"""
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


# ---------------------------------------------------------------------------
# _is_promising のテスト
# ---------------------------------------------------------------------------

class TestIsPromising:

    def test_high_score_and_discount_is_promising(self):
        assert _is_promising(make_est(resale_score=75, asking_vs_fair_pct=-3.2)) is True

    def test_exact_threshold_score_is_promising(self):
        assert _is_promising(make_est(resale_score=PROMISING_SCORE_THRESHOLD)) is True

    def test_slight_premium_within_threshold_is_promising(self):
        assert _is_promising(make_est(resale_score=75, asking_vs_fair_pct=5.0)) is True

    def test_low_score_is_not_promising(self):
        assert _is_promising(make_est(resale_score=PROMISING_SCORE_THRESHOLD - 1)) is False

    def test_high_score_but_overpriced_is_not_promising(self):
        assert _is_promising(make_est(resale_score=80, asking_vs_fair_pct=PROMISING_VS_FAIR_MAX_PCT + 0.1)) is False

    def test_high_score_with_none_vs_fair_is_promising(self):
        # カーブデータなし（asking_vs_fair_pct=None）はスコアのみで判定
        assert _is_promising(make_est(resale_score=75, asking_vs_fair_pct=None)) is True

    def test_low_score_with_none_vs_fair_is_not_promising(self):
        assert _is_promising(make_est(resale_score=60, asking_vs_fair_pct=None)) is False

    def test_empty_dict_is_not_promising(self):
        # 評価スキップ物件（est_map に URL なし）は有望でない
        assert _is_promising({}) is False


# ---------------------------------------------------------------------------
# _build_text_promising のテスト
# ---------------------------------------------------------------------------

class TestBuildTextPromising:

    def test_contains_star_marker(self):
        assert "★★ 有望物件 ★★" in _build_text_promising(make_listing(), "AI評価", make_est(), 1)

    def test_contains_listing_name(self):
        assert "テスト物件マンション101" in _build_text_promising(make_listing(), "AI評価", make_est(), 1)

    def test_contains_resale_score_number(self):
        # 【追加テスト】スコアの数値が文中に差し込まれていること
        est = make_est(resale_score=78)
        text = _build_text_promising(make_listing(), "AI評価テキスト", est, 1)
        assert "78" in text
        assert "/100" in text

    def test_contains_vs_fair_pct_string(self):
        # 【追加テスト】割安率の文字列が文中に差し込まれていること
        est = make_est(asking_vs_fair_pct=-3.2)
        text = _build_text_promising(make_listing(), "AI評価テキスト", est, 1)
        assert "-3.2%" in text
        assert "割安" in text

    def test_vs_fair_positive_shows_waridaka(self):
        est = make_est(asking_vs_fair_pct=+2.5)
        text = _build_text_promising(make_listing(), "AI評価テキスト", est, 1)
        assert "割高" in text

    def test_contains_future_resale_price(self):
        est = make_est(future_resale_price=40_500_000, hold_years=10)
        text = _build_text_promising(make_listing(), "AI評価テキスト", est, 1)
        assert "10年後" in text
        assert "4050" in text  # 40_500_000 / 10_000 = 4050万円

    def test_hold_years_is_dynamic(self):
        # f"{hold_years}年後" がハードコードでなく hold_years から生成されること
        est = make_est(future_resale_price=40_000_000, hold_years=7)
        text = _build_text_promising(make_listing(), "AI評価テキスト", est, 1)
        assert "7年後" in text
        assert "10年後" not in text

    def test_contains_url(self):
        assert "https://suumo.jp/test/99999/" in _build_text_promising(make_listing(), "AI評価テキスト", make_est(), 1)

    def test_contains_gemini_eval_text(self):
        assert "★★★★☆ (4/5)" in _build_text_promising(make_listing(), "★★★★☆ (4/5)", make_est(), 1)

    def test_contains_notes_with_warning_mark(self):
        est = make_est(notes=json.dumps(["駅徒歩10分超は買い手が絞られやすい"]))
        text = _build_text_promising(make_listing(), "AI評価", est, 1)
        assert "駅徒歩10分超" in text
        assert "⚠" in text

    def test_none_vs_fair_omits_line(self):
        # asking_vs_fair_pct=None のときは実勢比の行が出ない
        text = _build_text_promising(make_listing(), "AI評価", make_est(asking_vs_fair_pct=None), 1)
        assert "実勢比" not in text


# ---------------------------------------------------------------------------
# _build_text_compact のテスト
# ---------------------------------------------------------------------------

class TestBuildTextCompact:

    def test_contains_listing_name(self):
        assert "テスト物件マンション101" in _build_text_compact(make_listing(), 1)

    def test_contains_price(self):
        assert "4,200万円" in _build_text_compact(make_listing(), 1)

    def test_contains_url(self):
        assert "https://suumo.jp/test/99999/" in _build_text_compact(make_listing(), 1)

    def test_does_not_contain_score(self):
        # 控えめ版にはスコアが出ない
        assert "/100" not in _build_text_compact(make_listing(), 1)

    def test_shorter_than_promising(self):
        compact   = _build_text_compact(make_listing(), 1)
        promising = _build_text_promising(make_listing(), "AI評価テキスト", make_est(), 1)
        assert len(compact) < len(promising)


# ---------------------------------------------------------------------------
# notify_line_two_stage のテスト（LINE API をモック）
# ---------------------------------------------------------------------------

class TestNotifyLineTwoStage:

    def test_promising_property_sends_emphasized(self, line_env):
        # 有望物件は強調版（★★ 有望物件 ★★）で送信される
        listing = make_listing()
        est = make_est(resale_score=75, asking_vs_fair_pct=-3.0)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価テキスト")], {listing.url: est})

        sent = _collect_sent_texts(mock_post)
        assert any("★★ 有望物件 ★★" in t for t in sent)

    def test_normal_property_sends_compact(self, line_env):
        # 非有望物件は控えめ版（スコアなし）で送信される
        listing = make_listing()
        est = make_est(resale_score=60)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価テキスト")], {listing.url: est})

        sent = _collect_sent_texts(mock_post)
        assert not any("★★ 有望物件 ★★" in t for t in sent)
        assert any("テスト物件マンション101" in t for t in sent)

    def test_skipped_listing_still_notified(self, line_env):
        # 評価スキップ物件（est_map に URL がない）も控えめ版で通知される（通知が消えない）
        listing = make_listing()

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価テキスト")], {})

        sent = _collect_sent_texts(mock_post)
        assert any("テスト物件マンション101" in t for t in sent)

    def test_empty_est_map_fallback_all_compact(self, line_env):
        # est_map 空（評価失敗フォールバック）→ 全件が控えめ版（強調版は出ない）
        listings = [
            make_listing(url="https://suumo.jp/test/1/", name="物件A"),
            make_listing(url="https://suumo.jp/test/2/", name="物件B"),
        ]

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(l, "AI評価") for l in listings], {})

        sent = _collect_sent_texts(mock_post)
        assert not any("★★ 有望物件 ★★" in t for t in sent)
        assert any("物件A" in t for t in sent)
        assert any("物件B" in t for t in sent)

    def test_header_contains_total_count(self, line_env):
        listing = make_listing()
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価")], {})

        sent = _collect_sent_texts(mock_post)
        assert any("1件" in t for t in sent)

    def test_header_shows_promising_count_when_present(self, line_env):
        listing = make_listing()
        est = make_est(resale_score=75, asking_vs_fair_pct=-3.0)
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価")], {listing.url: est})

        sent = _collect_sent_texts(mock_post)
        assert any("有望物件" in t and "1件" in t for t in sent)

    def test_no_line_token_skips_without_exception(self, monkeypatch):
        # LINE認証情報が未設定でも例外を出さずスキップ
        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", None)
        monkeypatch.setattr(scraper, "LINE_USER_ID", None)
        with patch("scraper.requests.post") as mock_post:
            notify_line_two_stage([(make_listing(), "AI評価")], {})
        mock_post.assert_not_called()
