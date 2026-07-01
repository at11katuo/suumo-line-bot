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
    _build_text_reference,
    _format_listing_age,
    _is_promising,
    notify_line_reference,
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

    def test_shows_age_line_when_provided(self):
        # age_days を渡すと「確認してから N日目」が出る
        text = _build_text_promising(make_listing(), "AI評価", make_est(), 1, age_days=5)
        assert "確認してから 5日目" in text

    def test_no_age_line_when_age_days_none(self):
        # age_days 未指定（None）なら確認継続の行は出ない（既存互換）
        text = _build_text_promising(make_listing(), "AI評価", make_est(), 1)
        assert "確認してから" not in text
        assert "本日はじめて確認" not in text


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

    def test_includes_concern_when_present(self):
        # eval_text に「懸念点：xxx」があれば、その内容が控えめ版にも1行出る
        eval_text = "総合評価：買い\n懸念点：駅から徒歩15分とやや遠い\nおすすめ度：B"
        result = _build_text_compact(make_listing(), 1, eval_text)
        assert "懸念点" in result
        assert "駅から徒歩15分とやや遠い" in result

    def test_no_concern_line_when_eval_text_empty(self):
        # eval_text 未指定（評価スキップ等）でも落ちず、懸念点行は付かない
        result = _build_text_compact(make_listing(), 1, "")
        assert "懸念点" not in result
        # デフォルト引数省略時と完全に同じ結果（既存呼び出しとの互換性）
        assert result == _build_text_compact(make_listing(), 1)

    def test_shows_age_line_when_provided(self):
        # age_days を渡すと控えめ版にも「確認してから N日目」が出る
        result = _build_text_compact(make_listing(), 1, "", age_days=3)
        assert "確認してから 3日目" in result

    def test_no_age_line_when_age_days_none(self):
        # age_days 未指定なら確認継続の行は出ない（既存互換）
        result = _build_text_compact(make_listing(), 1, "", age_days=None)
        assert "確認してから" not in result
        assert result == _build_text_compact(make_listing(), 1)


# ---------------------------------------------------------------------------
# _format_listing_age のテスト（表示文言）
# ---------------------------------------------------------------------------

class TestFormatListingAge:
    """観測日数 → 表示文言の変換。SUUMO掲載日と誤解されない文言であることを確認。"""

    def test_none_returns_none(self):
        # 履歴なし → None（行を出さない合図）
        assert _format_listing_age(None) is None

    def test_zero_is_today_first_seen(self):
        # 本日初出 → 「本日はじめて確認」
        assert _format_listing_age(0) == "本日はじめて確認"

    def test_positive_is_days_since_first_seen(self):
        assert _format_listing_age(7) == "確認してから 7日目"

    def test_wording_does_not_imply_suumo_listing_date(self):
        # 「掲載」という語を使わない（SUUMO実掲載日との誤解防止）
        assert "掲載" not in _format_listing_age(0)
        assert "掲載" not in _format_listing_age(10)


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


# ---------------------------------------------------------------------------
# _build_text_reference のテスト（参考枠の1物件分の整形）
# ---------------------------------------------------------------------------

class TestBuildTextReference:
    """
    参考枠の本文。データ（reinfolib）と AI 判断（Gemini）の両方が
    見えること、過信防止のため AI★数・懸念点が併記されることを検証。
    """

    def test_shows_reinfolib_score_and_vs_fair(self):
        est = make_est(resale_score=75, asking_vs_fair_pct=-15.3)
        text = _build_text_reference(make_listing(), 1, "AI評価", est, 1)
        assert "スコア75/100" in text
        assert "実勢比 -15.3%（割安）" in text

    def test_shows_gemini_star_count(self):
        # AI の★数を必ず併記（なぜ自動抽出外かが分かる）
        text = _build_text_reference(make_listing(), 1, "★☆☆☆☆ (1/5)", make_est(), 1)
        assert "AI評価: 1★（5段階）" in text

    def test_shows_concern_from_eval_text(self):
        eval_text = "★☆☆☆☆ (1/5)\n懸念点：駅から徒歩18分と遠い"
        text = _build_text_reference(make_listing(), 1, eval_text, make_est(), 1)
        assert "懸念点: 駅から徒歩18分と遠い" in text

    def test_gemini_score_zero_shows_undetermined(self):
        # Gemini 応答失敗（score=0）→「判定できず」表示（0★とは出さない）
        text = _build_text_reference(make_listing(), 0, "", make_est(), 1)
        assert "判定できず" in text
        assert "0★" not in text

    def test_no_concern_section_when_absent(self):
        # 懸念点が抽出できないときは懸念点の節を省略（AI★数の行自体は残る）
        text = _build_text_reference(make_listing(), 2, "★★☆☆☆ (2/5)", make_est(), 1)
        assert "懸念点" not in text
        assert "AI評価: 2★（5段階）" in text

    def test_shows_age_line_when_provided(self):
        text = _build_text_reference(make_listing(), 1, "AI評価", make_est(), 1, age_days=5)
        assert "確認してから 5日目" in text

    def test_contains_name_price_url(self):
        text = _build_text_reference(make_listing(), 1, "AI評価", make_est(), 1)
        assert "テスト物件マンション101" in text
        assert "4,200万円" in text
        assert "https://suumo.jp/test/99999/" in text

    def test_not_emphasized_header(self):
        # 参考枠の本文は強調版の見出しを含まない（混同防止）
        text = _build_text_reference(make_listing(), 1, "AI評価", make_est(), 1)
        assert "★★ 有望物件 ★★" not in text


# ---------------------------------------------------------------------------
# notify_line_reference のテスト（LINE API をモック）
# ---------------------------------------------------------------------------

class TestNotifyLineReference:

    def test_sent_for_gemini_low_but_reinfolib_promising(self, line_env):
        # Gemini 1★（4★未満）だが reinfolib 有望 → 参考枠が送信される
        listing  = make_listing()
        rejected = [(listing, 1, "★☆☆☆☆ (1/5)\n懸念点：駅から遠い")]
        est      = make_est(resale_score=75, asking_vs_fair_pct=-15.3)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference(rejected, {listing.url: est})

        sent = _collect_sent_texts(mock_post)
        assert any("📋 参考" in t for t in sent)
        assert any("テスト物件マンション101" in t for t in sent)

    def test_message_has_caution_and_ai_info(self, line_env):
        # 「過信注意」の一文と、AI★数・懸念点が必ず併記される（設計の要）
        listing  = make_listing()
        rejected = [(listing, 1, "★☆☆☆☆ (1/5)\n懸念点：1階で陽当たり難")]

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference(rejected, {listing.url: make_est()})

        blob = "\n".join(_collect_sent_texts(mock_post))
        assert "過信" in blob               # 過信注意の一文
        assert "AI評価: 1★" in blob          # Gemini の★数
        assert "1階で陽当たり難" in blob      # Gemini の懸念点

    def test_not_confused_with_emphasized(self, line_env):
        # 参考枠は強調版の見出しを一切含まない
        listing  = make_listing()
        rejected = [(listing, 1, "★☆☆☆☆ (1/5)")]

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference(rejected, {listing.url: make_est()})

        blob = "\n".join(_collect_sent_texts(mock_post))
        assert "★★ 有望物件 ★★" not in blob

    def test_no_message_when_none_promising(self, line_env):
        # rejected はあるが reinfolib 有望が0件 → メッセージを一切送らない
        listing  = make_listing()
        rejected = [(listing, 1, "★☆☆☆☆ (1/5)")]
        est      = make_est(resale_score=60)  # 70未満 → 非有望

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference(rejected, {listing.url: est})

        mock_post.assert_not_called()

    def test_empty_rejected_sends_nothing(self, line_env):
        # rejected 自体が空 → 何も送らない
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference([], {})
        mock_post.assert_not_called()

    def test_no_line_token_skips_without_exception(self, monkeypatch):
        # LINE 認証情報が未設定でも例外を出さずスキップ
        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", None)
        monkeypatch.setattr(scraper, "LINE_USER_ID", None)
        listing = make_listing()
        with patch("scraper.requests.post") as mock_post:
            notify_line_reference([(listing, 1, "AI評価")], {listing.url: make_est()})
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# 非回帰: 参考枠の追加で強調版/控えめ版が変わらない・重複しないこと
# ---------------------------------------------------------------------------

class TestReferenceDoesNotAffectTwoStage:
    """
    参考枠（第3カテゴリ）が既存の2段階通知に影響しないこと、
    同一物件が両方の通知に出ないことを確認する。
    """

    def test_promising_in_scored_still_emphasized_and_no_reference(self, line_env):
        # 有望物件が scored（Gemini通過）にある通常ケース:
        # 強調版で出て、参考枠（rejected 空）は何も送らない
        listing = make_listing()
        est     = make_est(resale_score=75, asking_vs_fair_pct=-3.0)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_two_stage([(listing, "AI評価")], {listing.url: est})
        two_stage_blob = "\n".join(_collect_sent_texts(mock_post))
        assert "★★ 有望物件 ★★" in two_stage_blob  # 強調版は従来どおり

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference([], {listing.url: est})  # scored のものは rejected に入らない
        mock_post.assert_not_called()

    def test_same_listing_not_notified_by_both(self, line_env):
        # 同一の有望物件を、両ルートに同時投入することは main() では起きないが、
        # 「rejected 経由なら参考枠のみ・scored 経由なら強調版のみ」で出力が
        # 重複しない（片方に入れたらもう片方の入力には現れない）ことを確認。
        listing = make_listing()
        est     = make_est(resale_score=75, asking_vs_fair_pct=-15.3)

        # rejected 経由 → 参考枠のみ（強調版の見出しは出ない）
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_reference([(listing, 1, "★☆☆☆☆ (1/5)")], {listing.url: est})
        ref_blob = "\n".join(_collect_sent_texts(mock_post))
        assert "📋 参考" in ref_blob
        assert "★★ 有望物件 ★★" not in ref_blob
