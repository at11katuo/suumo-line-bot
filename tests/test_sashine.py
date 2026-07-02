"""
tests/test_sashine.py
====================
sashine.calc_negotiation_targets（STEP1）と
sashine.recalc_estimate_at_price / is_promising_at_price（STEP2）のテスト。

期待値は手計算後、実際に関数を実行して数値を照合してから埋め込んでいる
（浮動小数点の丸め誤差を含めて実際の挙動と一致させるため）。
"""

import math

import pytest

from reinfolib_resale import Candidate, DepreciationCurve, estimate_resale
from sashine import (
    calc_negotiation_targets,
    is_promising_at_price,
    recalc_estimate_at_price,
)
from scraper import _is_promising


# ---------------------------------------------------------------------------
# 1. 強気度3段階それぞれで正しい金額が出ること
# ---------------------------------------------------------------------------

class TestThreeAggressivenessLevels:

    def test_aggressive_4200man(self):
        # 4,200万円 × 0.90 = 3,780万円ちょうど
        result = calc_negotiation_targets(42_000_000, "aggressive")
        assert result["target_price"] == 37_800_000
        assert result["opening_offer"] == 35_910_000    # 3780万 × 0.95 = 3591万
        assert result["walk_away_price"] == 38_930_000  # 3780万 × 1.03 = 3893.4万 → 切り捨て3893万

    def test_standard_4200man(self):
        # 4,200万円 × 0.93 = 3,906万円ちょうど
        result = calc_negotiation_targets(42_000_000, "standard")
        assert result["target_price"] == 39_060_000
        assert result["opening_offer"] == 37_100_000    # 3906万 × 0.95 = 3710.7万 → 切り捨て3710万
        assert result["walk_away_price"] == 40_230_000  # 3906万 × 1.03 = 4023.18万 → 切り捨て4023万

    def test_mild_4987man_has_fraction(self):
        # 4,987万円 → 10万円単位切り捨てで 4,980万円（端数7万円切り捨て）
        result = calc_negotiation_targets(49_870_000, "mild")
        assert result["target_price"] == 49_800_000
        assert result["opening_offer"] == 47_310_000    # 4980万 × 0.95 = 4731万
        assert result["walk_away_price"] == 51_290_000  # 4980万 × 1.03 = 5129.4万 → 切り捨て5129万

    def test_default_aggressiveness_is_standard(self):
        # aggressiveness 省略時は "standard" と同じ結果になること
        default_result  = calc_negotiation_targets(42_000_000)
        explicit_result = calc_negotiation_targets(42_000_000, "standard")
        assert default_result == explicit_result

    def test_aggressive_gives_lowest_target_price(self):
        # 同じ売出価格なら aggressive が最も安い落としどころになること
        agg = calc_negotiation_targets(50_000_000, "aggressive")["target_price"]
        std = calc_negotiation_targets(50_000_000, "standard")["target_price"]
        mld = calc_negotiation_targets(50_000_000, "mild")["target_price"]
        assert agg < std < mld

    def test_aggressiveness_is_echoed_back(self):
        for level in ("aggressive", "standard", "mild"):
            result = calc_negotiation_targets(42_000_000, level)
            assert result["aggressiveness"] == level


# ---------------------------------------------------------------------------
# 2. mild の「端数なし＝値引きゼロ」ケース（今回の確認事項）
# ---------------------------------------------------------------------------

class TestMildNoFractionCase:
    """
    売出価格がすでに10万円単位ちょうどのとき、mild は値引きゼロ
    （落としどころ＝売出価格）になる。これは意図した自然な挙動であり、
    target_price == asking_price で判別できることを確認する。
    """

    def test_exact_multiple_gives_zero_discount(self):
        # 4,900万円ちょうど（10万円単位の倍数）→ 切り捨てても変化なし
        result = calc_negotiation_targets(49_000_000, "mild")
        assert result["target_price"] == 49_000_000
        assert result["target_price"] == result["asking_price"]  # 判別方法の確認

    def test_zero_discount_still_computes_opening_and_walk_away(self):
        # 値引きゼロでも初回提示・引き際は target_price を基準に通常どおり計算される
        result = calc_negotiation_targets(49_000_000, "mild")
        assert result["opening_offer"]   == 46_550_000  # 4900万 × 0.95
        assert result["walk_away_price"] == 50_470_000  # 4900万 × 1.03 = 5047万

    def test_non_exact_multiple_gives_nonzero_discount(self):
        # 対照実験: 10万円単位でない価格では値引きが発生することの確認
        result = calc_negotiation_targets(49_050_000, "mild")  # 4905万円（端数5万円）
        assert result["target_price"] < result["asking_price"]
        assert result["target_price"] == 49_000_000


# ---------------------------------------------------------------------------
# 3. 丸め処理の仕様確認（切り捨て。四捨五入ではない）
# ---------------------------------------------------------------------------

class TestFlooringBehavior:

    def test_fractional_price_floors_not_rounds(self):
        # 4,283万5千円 × 0.93 = 3,983万6,550円 → 四捨五入なら3984万だが、
        # 切り捨て仕様なので 3,983万円になること（買い手有利の確認）
        result = calc_negotiation_targets(42_835_000, "standard")
        assert result["target_price"] == 39_830_000
        assert result["target_price"] != 39_840_000  # 四捨五入した場合の値ではない

    def test_walk_away_also_floors(self):
        # 引き際も切り捨てで統一されていること（3906万 × 1.03 = 4023.18万 → 4023万）
        result = calc_negotiation_targets(42_000_000, "standard")
        assert result["walk_away_price"] == 40_230_000

    def test_all_amounts_are_multiples_of_10000_yen(self):
        # aggressive/standard は必ず万円単位（10,000円の倍数）になること
        for level in ("aggressive", "standard"):
            result = calc_negotiation_targets(42_835_000, level)
            for key in ("opening_offer", "target_price", "walk_away_price"):
                assert result[key] % 10_000 == 0, f"{level}/{key} が万円単位でない"


# ---------------------------------------------------------------------------
# 4. 境界値（安い物件・高い物件・端数のある価格）
# ---------------------------------------------------------------------------

class TestBoundaryValues:

    def test_cheap_listing(self):
        # 500万円の安い物件でも正しく計算されること
        result = calc_negotiation_targets(5_000_000, "standard")
        assert result["target_price"] == 4_650_000
        assert result["opening_offer"] == 4_410_000
        assert result["walk_away_price"] == 4_780_000

    def test_expensive_listing(self):
        # 3億円の高額物件でも正しく計算されること
        result = calc_negotiation_targets(300_000_000, "aggressive")
        assert result["target_price"] == 270_000_000
        assert result["opening_offer"] == 256_500_000
        assert result["walk_away_price"] == 278_100_000

    def test_very_small_price_just_above_zero(self):
        # 極端に小さい売出価格（1万円）でも例外を出さず計算できること
        result = calc_negotiation_targets(10_000, "standard")
        assert result is not None
        assert result["target_price"] >= 0

    def test_price_just_below_rounding_unit(self):
        # 10万円未満の価格（mild で切り捨てると0円になり得るケース）
        result = calc_negotiation_targets(90_000, "mild")
        assert result is not None
        assert result["target_price"] == 0  # 10万円単位未満はすべて切り捨てられ0円


# ---------------------------------------------------------------------------
# 5. 不正入力（例外を投げず None を返すこと）
# ---------------------------------------------------------------------------

class TestInvalidInputs:

    def test_zero_price_returns_none(self):
        assert calc_negotiation_targets(0, "standard") is None

    def test_negative_price_returns_none(self):
        assert calc_negotiation_targets(-1_000_000, "standard") is None

    def test_none_price_returns_none(self):
        assert calc_negotiation_targets(None, "standard") is None

    def test_string_price_returns_none(self):
        assert calc_negotiation_targets("4200万円", "standard") is None

    def test_nan_price_returns_none(self):
        assert calc_negotiation_targets(float("nan"), "standard") is None

    def test_infinite_price_returns_none(self):
        assert calc_negotiation_targets(float("inf"), "standard") is None

    def test_unknown_aggressiveness_returns_none(self):
        assert calc_negotiation_targets(42_000_000, "super_aggressive") is None

    def test_none_aggressiveness_returns_none(self):
        assert calc_negotiation_targets(42_000_000, None) is None

    def test_empty_string_aggressiveness_returns_none(self):
        assert calc_negotiation_targets(42_000_000, "") is None

    def test_invalid_input_does_not_raise(self):
        # 例外を一切投げないことの確認（None が返るだけ）
        for bad_price in (None, -1, 0, "abc", float("nan"), float("inf"), []):
            try:
                calc_negotiation_targets(bad_price, "standard")
            except Exception as e:
                assert False, f"asking_price={bad_price!r} で例外が発生: {e}"


# ---------------------------------------------------------------------------
# STEP2 用の共通フィクスチャ
# ---------------------------------------------------------------------------
#
# 築8年（2018年築・評価基準2026年）→ バケット(6,10)、
# 保有10年後は築18年 → バケット(16,20)。
# 実勢㎡単価: 現在70万円/㎡、将来60万円/㎡ という設定で、
# fair_price_now = 70万 × 72㎡ = 5,040万円 になるようにしている。

@pytest.fixture
def curve() -> DepreciationCurve:
    return DepreciationCurve(
        median_unit_price={(6, 10): 700_000, (16, 20): 600_000},
        sample_count={(6, 10): 30, (16, 20): 25},
    )


@pytest.fixture
def candidate() -> Candidate:
    # walk<=7(+15) / 65<=area<=80(+10) / units>=50(+8) / repair>=200(±0) / future_age18<=20(+7)
    # → resale_score = 50+15+10+8+7 = 90（asking_price には一切依存しない）
    return Candidate(
        asking_price=55_000_000,  # 5,500万円（fair 5,040万円に対し約+9.1%の割高）
        area_sqm=72,
        building_year=2018,
        walk_minutes=6,
        total_units=80,
        repair_fund_per_sqm=230,
        floor_plan="3LDK",
    )


# ---------------------------------------------------------------------------
# STEP2-1. recalc_estimate_at_price: 落としどころ価格での乖離率の再計算
# ---------------------------------------------------------------------------

class TestRecalcEstimateAtPrice:

    def test_matches_manual_estimate_resale_call(self, candidate, curve):
        # 「Candidateのasking_priceだけ差し替えてestimate_resaleを再実行」
        # したものと完全に一致すること（式を複製していないことの確認）
        import dataclasses
        manual_cand = dataclasses.replace(candidate, asking_price=51_150_000)
        manual_est  = estimate_resale(manual_cand, curve, 2026, 10)

        result = recalc_estimate_at_price(candidate, curve, 2026, 10, 51_150_000)
        assert result.asking_vs_fair_pct == manual_est.asking_vs_fair_pct
        assert result.resale_score == manual_est.resale_score
        assert result.current_fair_price == manual_est.current_fair_price

    def test_vs_fair_recalculated_correctly_at_target_price(self, candidate, curve):
        # 元の売出価格(5500万)は fair(5040万)に対し+9.13%割高。
        # 標準指値の落としどころ(5115万)まで下がると +1.49% まで縮む。
        result = recalc_estimate_at_price(candidate, curve, 2026, 10, 51_150_000)
        assert result.asking_vs_fair_pct == pytest.approx(1.488095, abs=1e-4)

    def test_resale_score_unchanged_by_price(self, candidate, curve):
        # resale_score は asking_price に依存しないため、価格を変えても
        # スコアは元の評価と完全に同じになること（今回の設計の核心）
        original = estimate_resale(candidate, curve, 2026, 10)
        at_target = recalc_estimate_at_price(candidate, curve, 2026, 10, 51_150_000)
        assert original.resale_score == at_target.resale_score == 90

    def test_original_asking_price_not_mutated(self, candidate, curve):
        # 元の candidate オブジェクトは変更されない（dataclasses.replace は
        # 新しいインスタンスを作るだけ）
        recalc_estimate_at_price(candidate, curve, 2026, 10, 51_150_000)
        assert candidate.asking_price == 55_000_000

    def test_no_fair_price_data_propagates_none_without_exception(self, candidate):
        # 実勢価格が算出不能（カーブにデータなし）でも例外にはならず、
        # asking_vs_fair_pct=None を含む ResaleEstimate が返ること
        empty_curve = DepreciationCurve()
        result = recalc_estimate_at_price(candidate, empty_curve, 2026, 10, 50_000_000)
        assert result is not None
        assert result.asking_vs_fair_pct is None


# ---------------------------------------------------------------------------
# STEP2-2. 不正入力（例外を投げず None を返す）
# ---------------------------------------------------------------------------

class TestRecalcEstimateAtPriceInvalidInputs:

    def test_curve_none_returns_none(self, candidate):
        assert recalc_estimate_at_price(candidate, None, 2026, 10, 50_000_000) is None

    def test_candidate_none_returns_none(self, curve):
        assert recalc_estimate_at_price(None, curve, 2026, 10, 50_000_000) is None

    def test_negotiated_price_zero_returns_none(self, candidate, curve):
        assert recalc_estimate_at_price(candidate, curve, 2026, 10, 0) is None

    def test_negotiated_price_negative_returns_none(self, candidate, curve):
        assert recalc_estimate_at_price(candidate, curve, 2026, 10, -1_000_000) is None

    def test_negotiated_price_none_returns_none(self, candidate, curve):
        assert recalc_estimate_at_price(candidate, curve, 2026, 10, None) is None

    def test_negotiated_price_nan_returns_none(self, candidate, curve):
        assert recalc_estimate_at_price(candidate, curve, 2026, 10, float("nan")) is None

    def test_negotiated_price_string_returns_none(self, candidate, curve):
        assert recalc_estimate_at_price(candidate, curve, 2026, 10, "5000万円") is None

    def test_invalid_inputs_do_not_raise(self, candidate, curve):
        bad_cases = [
            (None, curve, 2026, 10, 50_000_000),
            (candidate, None, 2026, 10, 50_000_000),
            (candidate, curve, 2026, 10, 0),
            (candidate, curve, 2026, 10, -1),
            (candidate, curve, 2026, 10, None),
        ]
        for args in bad_cases:
            try:
                recalc_estimate_at_price(*args)
            except Exception as e:
                assert False, f"引数={args!r} で例外が発生: {e}"


# ---------------------------------------------------------------------------
# STEP2-3. is_promising_at_price: 指値なら有望になるかの判別
# ---------------------------------------------------------------------------

class TestIsPromisingAtPrice:

    def test_original_price_not_promising(self, candidate, curve):
        # 元の売出価格(5500万)は乖離率+9.13%で有望しきい値(+5%)を超えるため非有望
        original = estimate_resale(candidate, curve, 2026, 10)
        assert _is_promising({
            "resale_score": original.resale_score,
            "asking_vs_fair_pct": original.asking_vs_fair_pct,
        }) is False

    def test_becomes_promising_at_negotiated_target_price(self, candidate, curve):
        # 核心のテストケース: 標準指値の落としどころ(5115万)まで下がると
        # 乖離率+1.49%・スコア90で有望になる（「今は割高だが指値が通れば
        # 有望」を判別できることの確認）
        targets = calc_negotiation_targets(candidate.asking_price, "standard")
        assert targets["target_price"] == 51_150_000

        result = is_promising_at_price(candidate, curve, 2026, 10, targets["target_price"])
        assert result is True

    def test_still_not_promising_when_too_expensive_even_discounted(self, curve):
        # 対照ケース: 売出7000万円の物件は標準指値(6510万)まで下げても
        # 乖離率+29.2%と大きく、指値が通っても割高のまま＝非有望
        expensive_cand = Candidate(
            asking_price=70_000_000, area_sqm=72, building_year=2018,
            walk_minutes=6, total_units=80, repair_fund_per_sqm=230,
        )
        targets = calc_negotiation_targets(expensive_cand.asking_price, "standard")
        assert targets["target_price"] == 65_100_000

        result = is_promising_at_price(expensive_cand, curve, 2026, 10, targets["target_price"])
        assert result is False

    def test_returns_none_when_recalc_fails(self, candidate):
        # 再評価自体が失敗する入力（curve=None）では None を返す
        assert is_promising_at_price(candidate, None, 2026, 10, 50_000_000) is None

    def test_none_vs_fair_still_judged_by_score_only(self, candidate):
        # 実勢価格が不明（vs_fair=None）でも _is_promising の既存仕様どおり
        # スコアだけで判定される（sashine.py 側で新しい判定基準を作っていない）
        empty_curve = DepreciationCurve()
        result = is_promising_at_price(candidate, empty_curve, 2026, 10, 50_000_000)
        assert result is True  # score=90 は閾値以上、vs_fair不明はスキップされる


# ---------------------------------------------------------------------------
# STEP2-4. しきい値の一貫性（scraper._is_promising との整合）
# ---------------------------------------------------------------------------

class TestThresholdConsistencyWithScraper:
    """
    sashine.py は独自のしきい値を持たず、scraper._is_promising を
    そのまま再利用していることを確認する（しきい値の二重管理防止）。
    """

    def test_is_promising_at_price_matches_manual_is_promising_call(self, candidate, curve):
        # is_promising_at_price の結果は、同じ ResaleEstimate を
        # 手動で scraper._is_promising に渡した結果と完全一致すること
        negotiated_price = 51_150_000
        est = recalc_estimate_at_price(candidate, curve, 2026, 10, negotiated_price)
        manual_result = _is_promising({
            "resale_score": est.resale_score,
            "asking_vs_fair_pct": est.asking_vs_fair_pct,
        })
        assert is_promising_at_price(candidate, curve, 2026, 10, negotiated_price) == manual_result
