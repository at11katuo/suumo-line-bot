"""
tests/test_suppress_score_gain.py
====================
docs/score-fairness-spec.md STEP3: SUPPRESS_SCORE_GAIN_ALERTS のテスト。

scraper._filter_score_gain_only_alerts の単体テスト。
score_gain起因「のみ」のアラートを抑制し、price_drop起因のアラートは
（score_gainも同時に条件を満たしていても）常に残すことを確認する。
"""

from evaluator import PRICE_DROP_THRESHOLD, SCORE_GAIN_THRESHOLD
from scraper import _filter_score_gain_only_alerts


def _make_alert(price_drop: int, score_gain: int) -> dict:
    return {
        "url": "https://suumo.jp/test/nc_00000/",
        "name": "テスト物件",
        "today": {},
        "prev": {},
        "price_drop": price_drop,
        "score_gain": score_gain,
    }


class TestFilterScoreGainOnlyAlerts:

    def test_score_gain_only_alert_is_suppressed(self):
        # price_dropは閾値未満・score_gainのみ閾値以上 → 抑制される
        alert = _make_alert(price_drop=0, score_gain=SCORE_GAIN_THRESHOLD)
        result = _filter_score_gain_only_alerts([alert], PRICE_DROP_THRESHOLD)
        assert result == []

    def test_price_drop_only_alert_is_kept(self):
        # price_dropが閾値以上・score_gainは閾値未満 → 通常どおり残る
        alert = _make_alert(price_drop=PRICE_DROP_THRESHOLD, score_gain=0)
        result = _filter_score_gain_only_alerts([alert], PRICE_DROP_THRESHOLD)
        assert result == [alert]

    def test_both_conditions_met_is_kept_not_suppressed(self):
        # 【境界条件】値下げによって新式スコアも+8以上跳ね、price_drop・
        # score_gainの両方が閾値を満たすケース。price_drop起因として扱い、
        # SUPPRESS中でも通知される（値下げ情報を握りつぶさない設計）。
        alert = _make_alert(price_drop=PRICE_DROP_THRESHOLD, score_gain=SCORE_GAIN_THRESHOLD)
        result = _filter_score_gain_only_alerts([alert], PRICE_DROP_THRESHOLD)
        assert result == [alert]

    def test_neither_condition_met_is_kept(self):
        # detect_changes は本来どちらか一方を満たしたものしか返さないが、
        # フィルタ単体としては price_drop 未満のものは除外しないことだけを
        # 保証する（このケースは実運用では発生しない防御的テスト）。
        alert = _make_alert(price_drop=0, score_gain=0)
        result = _filter_score_gain_only_alerts([alert], PRICE_DROP_THRESHOLD)
        assert result == []

    def test_empty_list_returns_empty(self):
        assert _filter_score_gain_only_alerts([], PRICE_DROP_THRESHOLD) == []

    def test_mixed_batch_keeps_only_price_drop_triggered(self):
        price_drop_alert = _make_alert(price_drop=PRICE_DROP_THRESHOLD, score_gain=0)
        score_gain_alert = _make_alert(price_drop=0, score_gain=SCORE_GAIN_THRESHOLD)
        both_alert = _make_alert(price_drop=PRICE_DROP_THRESHOLD, score_gain=SCORE_GAIN_THRESHOLD)

        result = _filter_score_gain_only_alerts(
            [price_drop_alert, score_gain_alert, both_alert], PRICE_DROP_THRESHOLD
        )

        assert result == [price_drop_alert, both_alert]
