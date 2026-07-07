"""
tests/test_reference_known_listings.py
====================
参考枠（notify_line_reference）を既知物件にも拡大する機能のテスト。

【背景】
    従来、参考枠は new_listings（新着）のみを対象にしていたため、
    一度Geminiに4★未満をつけられた既知物件は、その後reinfolib評価が
    改善しても二度と参考枠に浮上できなかった。この欠陥は本番実行で
    実際に確認された（調布市の物件「多摩川の自然に寄り添う」）。

    scraper._find_reference_candidates + gemini_cache.py（Gemini評価の
    永続化）+ evaluator.is_reference_notified/mark_reference_notified
    （重複抑制）により、既知物件も参考枠の対象にする。

【最重要】
    TestChofuPropertyRegression クラスで、実際に発見の発端となった
    調布物件（URL: nc_20988160、スコア75・乖離率-15.3%・Gemini1★）が
    実装後に参考枠として拾えることを名指しで確認する。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import detail_fetcher
import evaluator
import gemini_cache
import scraper
from build_curves import CurveBundle
from gemini_cache import load_gemini_evaluations, save_gemini_evaluation
from reinfolib_resale import DepreciationCurve
from scraper import Listing, _find_reference_candidates, _is_promising


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件",
        price="4,800万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/ref-1/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age="2013年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


def make_est(**overrides) -> dict:
    defaults = dict(resale_score=75, asking_vs_fair_pct=-15.3)
    defaults.update(overrides)
    return defaults


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


def _collect_sent_texts(mock_post) -> list[str]:
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


# ---------------------------------------------------------------------------
# 1. _find_reference_candidates: フィルタ・重複抑制の単体テスト
# ---------------------------------------------------------------------------

class TestFindReferenceCandidates:

    def test_promising_and_not_notified_is_included(self, db_path):
        listing  = make_listing()
        rejected = [(listing, 1, "★☆☆☆☆ (1/5)\n懸念点：バス便")]
        est_map  = {listing.url: make_est()}

        result = _find_reference_candidates(rejected, est_map, db_path=db_path)

        assert len(result) == 1
        assert result[0][0].url == listing.url

    def test_not_promising_is_excluded(self, db_path):
        listing  = make_listing()
        rejected = [(listing, 1, "text")]
        est_map  = {listing.url: make_est(resale_score=60)}  # score不足→非有望

        result = _find_reference_candidates(rejected, est_map, db_path=db_path)
        assert result == []

    def test_marks_notified_after_inclusion(self, db_path):
        listing  = make_listing()
        rejected = [(listing, 1, "text")]
        est_map  = {listing.url: make_est()}

        _find_reference_candidates(rejected, est_map, db_path=db_path)

        assert evaluator.is_reference_notified(listing.url, db_path=db_path) is True

    def test_already_notified_is_suppressed(self, db_path):
        # 一度通知したら、以後同じ物件は再通知されない（単純な永続抑制方式）
        listing  = make_listing()
        rejected = [(listing, 1, "text")]
        est_map  = {listing.url: make_est()}

        first  = _find_reference_candidates(rejected, est_map, db_path=db_path)
        second = _find_reference_candidates(rejected, est_map, db_path=db_path)

        assert len(first) == 1
        assert second == []

    def test_empty_rejected_returns_empty(self, db_path):
        assert _find_reference_candidates([], {}, db_path=db_path) == []

    def test_does_not_mark_when_not_promising(self, db_path):
        # 非有望で除外された物件は「通知済み」にはならない
        # （後日reinfolib評価が改善したときに正しく拾えるようにするため）
        listing  = make_listing()
        rejected = [(listing, 1, "text")]
        est_map  = {listing.url: make_est(resale_score=60)}

        _find_reference_candidates(rejected, est_map, db_path=db_path)
        assert evaluator.is_reference_notified(listing.url, db_path=db_path) is False

    def test_multiple_listings_independently_filtered(self, db_path):
        promising_l = make_listing(url="https://suumo.jp/test/ref-p/")
        cheap_l      = make_listing(url="https://suumo.jp/test/ref-c/")  # 非有望
        rejected = [
            (promising_l, 1, "text"),
            (cheap_l, 2, "text"),
        ]
        est_map = {
            promising_l.url: make_est(resale_score=80, asking_vs_fair_pct=-10.0),
            cheap_l.url:      make_est(resale_score=50, asking_vs_fair_pct=-1.0),
        }

        result = _find_reference_candidates(rejected, est_map, db_path=db_path)
        assert len(result) == 1
        assert result[0][0].url == promising_l.url


# ---------------------------------------------------------------------------
# 2. 最重要回帰テスト: 調布物件（今回の調査の発端）が参考枠に拾えること
# ---------------------------------------------------------------------------

class TestChofuPropertyRegression:
    """
    実際に発見された欠陥の回帰テスト。

    URL: https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/
    （「多摩川の自然に寄り添う」調布市の物件）

    事実関係（調査で確認済み）:
        - Gemini評価: 新着時に1★（バス便・1LDK+S表記が理由）
        - reinfolib評価（詳細データ込み）: スコア75/100・乖離率-15.3%
          → 有望条件（スコア70以上・乖離率5%以下）を満たす
        - 従来の実装では、この物件は「既知物件」になった時点で
          Gemini評価・参考枠判定の対象から完全に外れ、reinfolib評価が
          改善しても二度と参考枠に浮上しなかった

    この物件が実装後に参考枠として拾えることを、実際のURL・実際の
    数値を使って確認する。
    """

    CHOFU_URL = "https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/"

    def test_chofu_property_becomes_reference_candidate(self, db_path):
        """
        シナリオ: この物件は「既知物件」（new_listingsには入らない）。
        Gemini評価は過去の新着時に1★として gemini_evaluations に保存済み。
        reinfolib評価は最新の est_map で スコア75・乖離率-15.3%。
        → _find_reference_candidates が拾えることを確認する。
        """
        listing = make_listing(
            name="多摩川の自然に寄り添う",
            url=self.CHOFU_URL,
            location="東京都調布市多摩川",
        )

        # 過去の新着時に保存されたGemini評価（実際の記録に基づく内容）
        save_gemini_evaluation(
            self.CHOFU_URL, 1,
            "総合評価：★☆☆☆☆ (1/5)\n"
            "懸念点：バス便・1LDK+Sのため資産性に不安",
            db_path=db_path,
        )

        # main() 内のロジックを模した最小再現:
        # 既知物件の保存済みGemini評価をロードして rejected を組み立てる
        known_gemini = load_gemini_evaluations([self.CHOFU_URL], db_path=db_path)
        assert known_gemini[self.CHOFU_URL][0] == 1  # 前提確認: 1★のまま保存されている

        known_rejected = [
            (listing, known_gemini[self.CHOFU_URL][0], known_gemini[self.CHOFU_URL][1])
        ]

        # 詳細データ込みの最新 reinfolib 評価（調査時に確認された実際の数値）
        est_map = {self.CHOFU_URL: {"resale_score": 75, "asking_vs_fair_pct": -15.3}}

        # 前提確認: 有望条件（スコア70以上・乖離率5%以下）を満たしている
        assert _is_promising(est_map[self.CHOFU_URL]) is True

        result = _find_reference_candidates(known_rejected, est_map, db_path=db_path)

        # ★ここが今回の作業全体の目的地: 実際に参考枠の対象になること
        assert len(result) == 1
        assert result[0][0].url == self.CHOFU_URL
        assert result[0][1] == 1  # Gemini★数がそのまま引き継がれている

    def test_chofu_property_message_actually_sent_with_correct_content(self, db_path, monkeypatch):
        # _find_reference_candidates → notify_line_reference まで通した
        # 場合の、実際の送信内容を確認する
        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
        monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")

        listing = make_listing(
            name="多摩川の自然に寄り添う",
            url=self.CHOFU_URL,
            location="東京都調布市多摩川",
        )
        save_gemini_evaluation(
            self.CHOFU_URL, 1,
            "懸念点：バス便・1LDK+Sのため資産性に不安",
            db_path=db_path,
        )
        known_gemini = load_gemini_evaluations([self.CHOFU_URL], db_path=db_path)
        known_rejected = [
            (listing, known_gemini[self.CHOFU_URL][0], known_gemini[self.CHOFU_URL][1])
        ]
        est_map = {self.CHOFU_URL: {"resale_score": 75, "asking_vs_fair_pct": -15.3}}

        candidates = _find_reference_candidates(known_rejected, est_map, db_path=db_path)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.notify_line_reference(candidates, est_map)

        blob = "\n".join(_collect_sent_texts(mock_post))

        assert "📋 参考" in blob
        assert "多摩川の自然に寄り添う" in blob
        assert "スコア75/100" in blob
        assert "-15.3%" in blob
        assert "AI評価: 1★" in blob
        assert "バス便" in blob  # 懸念点の抽出も機能している

    def test_chofu_property_not_renotified_on_second_run(self, db_path):
        # 一度参考枠に出た後、状況が変わらなければ2回目は再通知されない
        # （重複抑制の確認。調布物件という具体名で確認する）
        listing = make_listing(
            name="多摩川の自然に寄り添う", url=self.CHOFU_URL, location="東京都調布市多摩川",
        )
        save_gemini_evaluation(self.CHOFU_URL, 1, "懸念点：バス便", db_path=db_path)
        known_gemini = load_gemini_evaluations([self.CHOFU_URL], db_path=db_path)
        known_rejected = [(listing, known_gemini[self.CHOFU_URL][0], known_gemini[self.CHOFU_URL][1])]
        est_map = {self.CHOFU_URL: {"resale_score": 75, "asking_vs_fair_pct": -15.3}}

        first_run  = _find_reference_candidates(known_rejected, est_map, db_path=db_path)
        second_run = _find_reference_candidates(known_rejected, est_map, db_path=db_path)

        assert len(first_run) == 1
        assert second_run == []  # 2回目は抑制される


# ---------------------------------------------------------------------------
# 3. main() レベルの統合テスト: Gemini API呼び出し回数・既知物件の実際の通知
# ---------------------------------------------------------------------------
#
# db_path のデフォルト値遅延解決（STEP4で修正済み）のおかげで、main() を
# 実際に実行して確認できる（test_main_integration.py と同じ手法）。

_FIXED_CURVE = DepreciationCurve(
    median_unit_price={(11, 15): 700_000, (21, 25): 600_000},
    sample_count={(11, 15): 30, (21, 25): 25},
)


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """scraper.main() を安全に実行するための共通モック環境。"""
    db_path = tmp_path / "evaluations.db"
    monkeypatch.setattr(evaluator, "DB_PATH", db_path)
    monkeypatch.setattr(detail_fetcher, "DB_PATH", db_path)
    monkeypatch.setattr(gemini_cache, "DB_PATH", db_path)
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")

    monkeypatch.setattr(
        evaluator, "get_curve_bundle",
        lambda **kwargs: CurveBundle(city_curve=_FIXED_CURVE, district_curves={}),
    )
    monkeypatch.setattr(build_curves, "get_curve", lambda **kwargs: _FIXED_CURVE)

    monkeypatch.setattr(scraper, "DATA_FILE", str(tmp_path / "data.csv"))
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")

    monkeypatch.setattr(detail_fetcher, "fetch_detail", lambda url, **kw: None)
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)

    return db_path


class TestMainIntegrationKnownListingReference:

    def test_gemini_not_called_for_known_listing(self, main_env, monkeypatch):
        """
        既知物件に対して Gemini API（evaluate_listing）が呼ばれないこと。
        呼び出し回数を実際に数えて確認する（現状維持であることの証明）。
        """
        db_path = main_env

        known_listing = make_listing(
            name="多摩川の自然に寄り添う",
            url="https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/",
        )
        # 既知物件として data.csv に登録
        scraper.save_listings(scraper.DATA_FILE, [known_listing])
        # 過去にGemini評価済み（1★）として永続化
        save_gemini_evaluation(known_listing.url, 1, "懸念点：バス便", db_path=db_path)

        monkeypatch.setattr(scraper, "scrape", lambda url: [known_listing])

        call_count = {"n": 0}
        def counting_evaluate_listing(listing):
            call_count["n"] += 1
            return (5, "★★★★★ (5/5)")
        monkeypatch.setattr(scraper, "evaluate_listing", counting_evaluate_listing)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        # 既知物件のみ・new_listingsが0件のため、Geminiは1回も呼ばれない
        assert call_count["n"] == 0

    def test_known_chofu_listing_actually_notified_via_main(self, main_env, monkeypatch):
        """
        main() を実際に実行し、既知の調布物件が参考枠として実際に
        LINE送信されることを確認する（最終的な統合確認）。
        """
        db_path = main_env
        chofu_url = "https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/"

        # 築13年（fair_price_now=700,000円/㎡×72㎡=50,400,000円のバケットに
        # 合わせる。apply_filtersの築10-25年条件も満たす）
        import datetime
        building_year = datetime.date.today().year - 13
        listing = Listing(
            name="多摩川の自然に寄り添う",
            price="4,270万円",  # 42,700,000円。fair 50,400,000円に対し-15.3%
            location="東京都調布市多摩川",
            url=chofu_url,
            station="京王線 調布駅 徒歩6分",
            floor_plan="1LDK+S",
            area="72m²",
            age=f"{building_year}年3月",
        )

        # 既知物件として data.csv に登録（new_listingsに入らないようにする）
        scraper.save_listings(scraper.DATA_FILE, [listing])
        # 過去の新着時にGeminiが1★をつけた記録
        save_gemini_evaluation(
            chofu_url, 1,
            "総合評価：★☆☆☆☆ (1/5)\n懸念点：バス便・1LDK+Sのため資産性に不安",
            db_path=db_path,
        )

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))  # 呼ばれないはず

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        blob = "\n".join(_collect_sent_texts(mock_post))

        # ★最終確認: 調布物件が実際に参考枠として通知されている
        assert "📋 参考" in blob
        assert "多摩川の自然に寄り添う" in blob
        assert "AI評価: 1★" in blob

    def test_known_chofu_listing_not_renotified_on_second_main_run(self, main_env, monkeypatch):
        """
        main() を2回連続実行しても、2回目は同じ調布物件を再通知しない
        （重複抑制が main() 経由でも機能することの確認）。
        """
        db_path = main_env
        chofu_url = "https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/"
        import datetime
        building_year = datetime.date.today().year - 13
        listing = Listing(
            name="多摩川の自然に寄り添う", price="4,270万円",
            location="東京都調布市多摩川", url=chofu_url,
            station="京王線 調布駅 徒歩6分", floor_plan="1LDK+S",
            area="72m²", age=f"{building_year}年3月",
        )
        scraper.save_listings(scraper.DATA_FILE, [listing])
        save_gemini_evaluation(chofu_url, 1, "懸念点：バス便", db_path=db_path)

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 1回目
        first_blob = "\n".join(_collect_sent_texts(mock_post))
        assert "📋 参考" in first_blob  # 1回目は通知される

        with patch("scraper.requests.post") as mock_post2:
            mock_post2.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 2回目（同じ状況で再実行）
        second_blob = "\n".join(_collect_sent_texts(mock_post2))
        assert "📋 参考" not in second_blob  # 2回目は抑制される
