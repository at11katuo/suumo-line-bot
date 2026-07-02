"""
tests/test_sashine.py
====================
sashine.calc_negotiation_targets のテスト。

期待値は手計算後、実際に関数を実行して数値を照合してから埋め込んでいる
（浮動小数点の丸め誤差を含めて実際の挙動と一致させるため）。
"""

import math

from sashine import calc_negotiation_targets


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
