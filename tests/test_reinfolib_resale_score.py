"""
tests/test_reinfolib_resale_score.py
====================
reinfolib_resale._resale_score の asking_vs_fair_pct 段階加減点の直接テスト
（docs/score-fairness-spec.md STEP4 新規テスト1〜3）。

_resale_score は current_age・curve を計算に使わない（future_age・cand・
asking_vs_fair_pct のみで決まる）ため、ダミー値を渡してよい。
"""

from reinfolib_resale import Candidate, DepreciationCurve, _resale_score

_DUMMY_CURVE = DepreciationCurve()


def _base_candidate(**overrides) -> Candidate:
    defaults = dict(
        asking_price=54_900_000, area_sqm=72, building_year=2018,
        walk_minutes=6, total_units=80, repair_fund_per_sqm=230,
        floor_plan="4LDK",
    )
    defaults.update(overrides)
    return Candidate(**defaults)


class TestResaleScorePctDelta:

    def test_severe_overprice_38pct_subtracts_20_points(self):
        # 是政4LDK実例の再現（実勢比+38.0%割高 → -20点。指示書0章の発端実例）
        cand = _base_candidate()
        notes: list[str] = []
        base_notes: list[str] = []
        base_score = _resale_score(cand, 8, 18, _DUMMY_CURVE, base_notes, asking_vs_fair_pct=None)
        score = _resale_score(cand, 8, 18, _DUMMY_CURVE, notes, asking_vs_fair_pct=38.0)

        assert score == base_score - 20
        assert any("大幅割高" in n for n in notes)

    def test_underprice_over_10pct_adds_8_points(self):
        # 実勢比-10%超の割安圏 → +8点
        cand = _base_candidate()
        notes: list[str] = []
        base_score = _resale_score(cand, 8, 18, _DUMMY_CURVE, [], asking_vs_fair_pct=None)
        score = _resale_score(cand, 8, 18, _DUMMY_CURVE, notes, asking_vs_fair_pct=-15.0)

        assert score == base_score + 8
        assert any("割安圏" in n for n in notes)

    def test_pct_exactly_minus_10_does_not_get_bonus(self):
        # 境界値: pct <= -10 が条件のため、ちょうど-10.0は加点対象（境界含む）
        cand = _base_candidate()
        base_score = _resale_score(cand, 8, 18, _DUMMY_CURVE, [], asking_vs_fair_pct=None)
        score_at_boundary = _resale_score(cand, 8, 18, _DUMMY_CURVE, [], asking_vs_fair_pct=-10.0)
        score_just_inside = _resale_score(cand, 8, 18, _DUMMY_CURVE, [], asking_vs_fair_pct=-9.9)

        assert score_at_boundary == base_score + 8
        assert score_just_inside == base_score  # -10%に届かないため無加減点ゾーン

    def test_pct_none_leaves_score_unchanged(self):
        # カーブ欠損等でpct算出不能(None)の場合、加減点なし＝現行スコアと同値
        cand = _base_candidate()
        score_with_explicit_none = _resale_score(cand, 8, 18, _DUMMY_CURVE, [], asking_vs_fair_pct=None)
        score_with_default = _resale_score(cand, 8, 18, _DUMMY_CURVE, [])  # 引数省略時のデフォルトもNone

        assert score_with_explicit_none == score_with_default

    def test_pct_none_adds_no_notes(self):
        cand = _base_candidate()
        notes: list[str] = []
        _resale_score(cand, 8, 18, _DUMMY_CURVE, notes, asking_vs_fair_pct=None)
        assert notes == []
