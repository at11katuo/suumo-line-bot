"""
tests/test_evaluate_listing_age.py
築年数の事実固定化（scraper.py 実装指示: プロンプトにPython計算済みの
確定値を渡し、Geminiの出力に矛盾があれば是正する）の回帰テスト。

【背景】
    横断重複グルーピングの通知サンプルを手作業で組み立てた際、
    evaluate_listing() を経由しなかったために _validate_age_consistency
    が発火せず、「サンプルに古い築年数が素通りしている」ように見える
    事故が起きた。_validate_age_consistency 自体にはこれまでテストが
    なかったため、ここで直接カバーする。
"""

from unittest.mock import MagicMock, patch

from scraper import Listing, _validate_age_consistency, evaluate_listing


# ---------------------------------------------------------------------------
# _validate_age_consistency 単体
# ---------------------------------------------------------------------------

def test_corrects_mismatched_age_mentions():
    text = (
        "総合評価：★★★★☆ (4/5)\n"
        "ヤドカリ投資メリット：駅近・築22年で価格底堅い時期\n"
        "懸念点：築22年で旧耐震リスクは低い\n"
        "判定：即内覧推奨"
    )
    result = _validate_age_consistency(text, age_years=23, listing_name="テスト物件")
    assert "築23年" in result
    assert "築22年" not in result


def test_leaves_matching_age_untouched():
    text = "懸念点：築23年で修繕積立金がやや低め"
    result = _validate_age_consistency(text, age_years=23, listing_name="テスト物件")
    assert result == text


def test_leaves_text_without_age_mention_untouched():
    text = "懸念点：駅からの距離がやや遠い"
    result = _validate_age_consistency(text, age_years=23, listing_name="テスト物件")
    assert result == text


# ---------------------------------------------------------------------------
# evaluate_listing() 経由での結線確認（Gemini呼び出しはモック）
# ---------------------------------------------------------------------------

def _make_listing(age="2003年7月") -> Listing:
    return Listing(
        name="紅葉丘２（多磨駅） 5290万円",
        price="5290万円",
        location="東京都府中市紅葉丘２",
        url="https://suumo.jp/test/age-consistency/",
        station="西武多摩川線「多磨」徒歩4分",
        floor_plan="3LDK",
        area="90.02m2（壁芯）",
        age=age,
    )


def test_evaluate_listing_corrects_gemini_wrong_age(monkeypatch):
    """Geminiが確定値と異なる築年数を書いた場合、evaluate_listing() の
    戻り値（テキスト）には正しい築年数だけが残ること。"""
    monkeypatch.setattr("scraper.GEMINI_API_KEY", "dummy-key")

    wrong_year_text = (
        "総合評価：★★★★☆ (4/5)\n"
        "ヤドカリ投資メリット：駅近・築22年で価格底堅い時期\n"
        "懸念点：築22年で旧耐震リスクは低い\n"
        "数年後売却ポテンシャル：高い／駅近\n"
        "判定：即内覧推奨"
    )
    mock_response = MagicMock(text=wrong_year_text)
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("scraper.genai.Client", return_value=mock_client):
        score, text = evaluate_listing(_make_listing(age="2003年7月"))

    assert score == 4
    assert "築22年" not in text
    # 2003年7月築 → age_years は「今年 - 2003」（_parse_age_years と同じ計算）
    from datetime import date
    expected_age = date.today().year - 2003
    assert f"築{expected_age}年" in text


def test_evaluate_listing_leaves_correct_age_untouched(monkeypatch):
    """Geminiが最初から正しい築年数を書いていれば、そのまま変更されない。"""
    monkeypatch.setattr("scraper.GEMINI_API_KEY", "dummy-key")

    from datetime import date
    correct_age = date.today().year - 2003
    correct_text = f"懸念点：築{correct_age}年で修繕積立金がやや低め"

    mock_response = MagicMock(text=correct_text)
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("scraper.genai.Client", return_value=mock_client):
        score, text = evaluate_listing(_make_listing(age="2003年7月"))

    assert text == correct_text
