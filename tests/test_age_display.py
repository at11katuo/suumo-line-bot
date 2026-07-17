"""
tests/test_age_display.py
====================
現在の築年数・想定売却額の通知表示テスト。

【背景】
    ある物件（現在築18年）について、10年・15年保有した場合の想定売却額と
    ローン残債を手計算で比較したところ、保守的な試算では売却額がローン
    残債を下回る可能性があった。「現在の築年数が古いほど、保有後の
    資産性リスクが大きい」という教訓を、既存の通知でも分かりやすく
    伝えるため、全ての通知種別（強調版・控えめ版・参考枠・指値候補）に
    「現在築○年」と想定売却額を表示するようにした。

    ⚠ ローンの残債計算（頭金・金利・返済期間はユーザー固有の前提が必要）
    は範囲外。あくまで既存の「想定売却額」計算を見やすくするだけで、
    新しい推測要素は増やしていない。既存のスコアリング
    （将来築25年超で-12点）も変更していない。

対象:
    - scraper._current_age_from_est（DB行からの現在築年数の計算）
    - scraper._build_age_line（表示行・注記の組み立て）
    - _build_text_promising / _build_text_compact / _build_text_reference /
      _build_text_sashine への現在築年数・想定売却額の表示
    - reinfolib_resale.ResaleEstimate.current_age
    - 既存スコアリング（将来築25年超で-12点）の非回帰確認

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー・実DBファイル不要。
"""

from datetime import date

import pytest

from reinfolib_resale import Candidate, DepreciationCurve, estimate_resale
from scraper import (
    AGE_WARNING_THRESHOLD_YEARS,
    Listing,
    _build_age_line,
    _build_text_compact,
    _build_text_promising,
    _build_text_reference,
    _build_text_sashine,
    _current_age_from_est,
)

_CURVE = DepreciationCurve(
    median_unit_price={(11, 15): 700_000, (16, 20): 600_000, (21, 25): 550_000, (26, 30): 500_000},
    sample_count={(11, 15): 30, (16, 20): 25, (21, 25): 20, (26, 30): 15},
)


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件マンション",
        price="4,800万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/age-display/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age="2008年3月",  # 呼び出し時点の年から見て築古よりの想定
    )
    defaults.update(overrides)
    return Listing(**defaults)


def make_est_row(building_year: int, evaluated_date: str, **overrides) -> dict:
    """est_map[url] 相当の DB 行 dict を組み立てる。"""
    row = dict(
        building_year=building_year,
        evaluated_date=evaluated_date,
        resale_score=70,
        asking_vs_fair_pct=-5.0,
        future_resale_price=40_000_000.0,
        hold_years=10,
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. _current_age_from_est
# ---------------------------------------------------------------------------

class TestCurrentAgeFromEst:

    def test_computes_age_from_building_year_and_evaluated_date(self):
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        assert _current_age_from_est(est) == 18

    def test_returns_none_when_building_year_missing(self):
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        del est["building_year"]
        assert _current_age_from_est(est) is None

    def test_returns_none_when_evaluated_date_missing(self):
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        del est["evaluated_date"]
        assert _current_age_from_est(est) is None

    def test_returns_none_when_building_year_is_none(self):
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        est["building_year"] = None
        assert _current_age_from_est(est) is None

    def test_returns_none_for_malformed_evaluated_date(self):
        est = make_est_row(building_year=2008, evaluated_date="not-a-date")
        assert _current_age_from_est(est) is None


# ---------------------------------------------------------------------------
# 2. _build_age_line（表示行・注記の閾値判定）
# ---------------------------------------------------------------------------

class TestBuildAgeLine:

    def test_none_age_returns_none(self):
        assert _build_age_line(None) is None

    def test_shows_current_age(self):
        assert "現在築18年" in _build_age_line(18)

    def test_no_warning_below_threshold(self):
        line = _build_age_line(AGE_WARNING_THRESHOLD_YEARS - 1)
        assert "⚠" not in line

    def test_warning_at_exact_threshold(self):
        line = _build_age_line(AGE_WARNING_THRESHOLD_YEARS)
        assert "⚠" in line
        assert "目減り" in line

    def test_warning_above_threshold(self):
        line = _build_age_line(AGE_WARNING_THRESHOLD_YEARS + 10)
        assert "⚠" in line

    def test_include_warning_false_suppresses_warning_even_above_threshold(self):
        line = _build_age_line(AGE_WARNING_THRESHOLD_YEARS + 10, include_warning=False)
        assert "現在築" in line
        assert "⚠" not in line


# ---------------------------------------------------------------------------
# 3. 各通知種別への表示（4種別すべて）
# ---------------------------------------------------------------------------

class TestNotificationsShowAgeAndFutureValue:

    def test_promising_shows_age_and_future_value(self):
        listing = make_listing()
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        text = _build_text_promising(listing, "評価コメント", est, 1)
        assert "現在築18年" in text
        assert "想定売却額" in text
        assert "4000万円" in text  # future_resale_price=40,000,000円 → 約4000万円

    def test_promising_shows_warning_for_old_building(self):
        listing = make_listing()
        est = make_est_row(building_year=2000, evaluated_date="2026-07-08")  # 築26年
        text = _build_text_promising(listing, "評価コメント", est, 1)
        assert "⚠" in text

    def test_compact_shows_age_and_future_value(self):
        listing = make_listing()
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        text = _build_text_compact(listing, 1, est=est)
        assert "現在築18年" in text
        assert "想定売却額" in text

    def test_compact_without_est_omits_age_and_future_value(self):
        # est=None（評価スキップ物件）のときは行自体を出さない（フォールバック維持）
        listing = make_listing()
        text = _build_text_compact(listing, 1, est=None)
        assert "現在築" not in text
        assert "想定売却額" not in text

    def test_reference_shows_age_and_future_value(self):
        listing = make_listing()
        est = make_est_row(building_year=2008, evaluated_date="2026-07-08")
        text = _build_text_reference(listing, gemini_score=2, eval_text="懸念点：立地", est=est, idx=1)
        assert "現在築18年" in text
        assert "想定売却額" in text

    def test_sashine_shows_age_and_future_value(self):
        listing = make_listing()
        candidate = Candidate(asking_price=48_000_000, area_sqm=72, building_year=2008)
        est_now = estimate_resale(candidate, _CURVE, current_year=2026, hold_years=10)
        found = {
            "aggressiveness": "standard",
            "targets": {
                "opening_offer": 45_000_000,
                "target_price": 46_000_000,
                "walk_away_price": 44_000_000,
            },
            "est_at_target": est_now,
        }
        text = _build_text_sashine(listing, found, est_now, age_days=5, idx=1)
        assert "現在築18年" in text
        assert "想定売却額" in text

    def test_sashine_does_not_show_warning_even_for_old_building(self):
        # 指値候補は情報量が多いため、注記は省略する設計（現在築○年のみ表示）
        listing = make_listing()
        candidate = Candidate(asking_price=48_000_000, area_sqm=72, building_year=1995)  # 築31年
        est_now = estimate_resale(candidate, _CURVE, current_year=2026, hold_years=10)
        found = {
            "aggressiveness": "standard",
            "targets": {
                "opening_offer": 45_000_000,
                "target_price": 46_000_000,
                "walk_away_price": 44_000_000,
            },
            "est_at_target": est_now,
        }
        text = _build_text_sashine(listing, found, est_now, age_days=5, idx=1)
        assert "現在築31年" in text
        assert "⚠" not in text


# ---------------------------------------------------------------------------
# 4. ResaleEstimate.current_age（reinfolib_resale.py）
# ---------------------------------------------------------------------------

class TestResaleEstimateCurrentAge:

    def test_current_age_is_current_year_minus_building_year(self):
        candidate = Candidate(asking_price=48_000_000, area_sqm=72, building_year=2008)
        est = estimate_resale(candidate, _CURVE, current_year=2026, hold_years=10)
        assert est.current_age == 18

    def test_current_age_changes_with_current_year(self):
        candidate = Candidate(asking_price=48_000_000, area_sqm=72, building_year=2008)
        est = estimate_resale(candidate, _CURVE, current_year=2030, hold_years=10)
        assert est.current_age == 22


# ---------------------------------------------------------------------------
# 5. 既存スコアリング（将来築25年超で-12点）の非回帰確認
# ---------------------------------------------------------------------------

class TestScoringUnaffectedByAgeDisplayChange:
    """
    今回の変更は current_age の「表示」を追加しただけで、スコア計算には
    一切使っていないことを確認する（既存のスコアリングへの非回帰）。
    """

    def test_future_age_over_25_still_penalized(self):
        # 現在築18年+保有10年=将来築28年（25年超）→ -12点相当のペナルティ
        candidate = Candidate(
            asking_price=48_000_000, area_sqm=72, building_year=2008,
            walk_minutes=6, total_units=60, repair_fund_per_sqm=300,
        )
        est = estimate_resale(candidate, _CURVE, current_year=2026, hold_years=10)
        assert any("築25年超" in note for note in est.notes)

    def test_future_age_20_or_under_still_bonused(self):
        # 現在築5年+保有10年=将来築15年（20年以下）→ +7点相当のボーナス
        candidate = Candidate(
            asking_price=48_000_000, area_sqm=72, building_year=2021,
            walk_minutes=6, total_units=60, repair_fund_per_sqm=300,
        )
        est_young = estimate_resale(candidate, _CURVE, current_year=2026, hold_years=10)

        # 同条件で建築年だけ変え、将来築25年超になるケースと比較する
        candidate_old = Candidate(
            asking_price=48_000_000, area_sqm=72, building_year=2000,
            walk_minutes=6, total_units=60, repair_fund_per_sqm=300,
        )
        est_old = estimate_resale(candidate_old, _CURVE, current_year=2026, hold_years=10)

        # 若い方がスコアが高くなることを確認。
        # 【2026-07-17〜】resale_scoreはasking_vs_fair_pctの加減点も含むため
        # 差の絶対値は元の「+7 vs -12」の19点ちょうどではなくなったが、
        # 不等号（young > old）は本テストの主眼であり変わらず成立する。
        assert est_young.resale_score > est_old.resale_score
