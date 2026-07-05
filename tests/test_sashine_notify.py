"""
tests/test_sashine_notify.py
====================
STEP4（指値候補のLINE通知組み込み）のテスト。

対象:
    - scraper._find_sashine_candidates（指値候補の発見＋重複抑制）
    - scraper._build_text_sashine（通知メッセージの整形）
    - scraper.notify_line_sashine_candidates（LINE送信）
    - scraper.main() のソース構造（既存通知フローとの隔離・実行順序の非回帰）

全テストは LINE API・国交省API・実DBファイルに一切触れない
（tmp_path と monkeypatch で隔離する）。

【main() を直接実行しない理由（重要）】
    evaluator.evaluate_and_save や get_listing_age_days 等の db_path 引数は
    「関数定義時」にプロジェクト直下の実際の evaluations.db（ユーザーの
    本番相当ファイル）へ束縛されている。main() 自身はこれらを db_path 引数
    なしで呼ぶため、main() を丸ごと実行するテストを書くと、モックだけでは
    本番DBへの書き込みを防げない危険がある（実際に確認済み: このファイル
    作成前に `evaluator.evaluate_and_save.__signature__` を調べたところ、
    db_path のデフォルトはプロジェクト直下の evaluations.db の絶対パス
    そのものだった）。
    このため main() 自体は実行せず、
      (1) 個別関数を db_path を明示指定して安全にテストする
      (2) main() のソースコード構造を検証する
          （try/exceptの隔離・呼び出し順序が保たれているか）
    の2本立てで非回帰を確認する。
"""

import inspect
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import evaluator
import scraper
from reinfolib_resale import DepreciationCurve
from scraper import (
    Listing,
    _build_text_sashine,
    _find_sashine_candidates,
    notify_line_sashine_candidates,
)


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト指値物件マンション",
        price="5,500万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/sashine-1/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


# fair_price_now = 700,000円/㎡ × 72㎡ = 50,400,000円。
# 元の候補（5,500万円）は乖離率+9.13%で非有望・割高（STEP2/3で検証済みの値）。
_FIXED_CURVE = DepreciationCurve(
    median_unit_price={(6, 10): 700_000, (16, 20): 600_000},
    sample_count={(6, 10): 30, (16, 20): 25},
)


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    """
    USE_MOCK_REINFOLIB=1 を設定する。_seed_age_days 内の evaluate_and_save は
    evaluator.py 内で module-level に束縛された get_curve を使うため
    （後述の fixed_curve フィクスチャの対象外）、これが実APIを叩かないよう
    モックモードにしておく必要がある。
    """
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    """カーブキャッシュを一時ディレクトリに差し替える（実cache/に触れない）。"""
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def fixed_curve(monkeypatch):
    """
    build_curves.get_curve をテスト用の固定カーブに差し替える。

    _find_sashine_candidates は関数本体内で `from build_curves import get_curve`
    を毎回実行する（遅延インポート）ため、この差し替えは正しく反映される。
    一方 evaluate_and_save（_seed_age_days が使う）は evaluator.py の
    モジュールレベルで import 済みの get_curve を使うため、この差し替えの
    影響を受けない（実際にはモックモードのランダム生成カーブを使うが、
    _seed_age_days は「履歴の日付」を作るためだけに使うので値のズレは
    テストに影響しない）。
    """
    monkeypatch.setattr(build_curves, "get_curve", lambda **kwargs: _FIXED_CURVE)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


@pytest.fixture
def line_env(monkeypatch):
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")


def _collect_sent_texts(mock_post) -> list[str]:
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


def _seed_age_days(listing: Listing, city_code: str, db_path: Path, age_days: int) -> None:
    """
    evaluations テーブルに「age_days日前に初めて評価された」履歴を作る。
    get_listing_age_days が正しい強気度を判定できるようにするための準備。
    保存される評価数値そのもの（スコア・乖離率）は _find_sashine_candidates
    が再計算する est_now とは無関係なので、ここでは日付だけが意味を持つ。
    """
    past_date = (date.today() - timedelta(days=age_days)).isoformat()
    evaluator.evaluate_and_save(
        [listing], city_code=city_code, db_path=db_path, _evaluated_date=past_date,
    )


# ---------------------------------------------------------------------------
# 1. _find_sashine_candidates: 発見・重複抑制
# ---------------------------------------------------------------------------

class TestFindSashineCandidates:

    def test_finds_candidate_when_standard_discount_makes_it_promising(self, db_path):
        # 40日経過(standard)なら、5,500万円→落としどころ5,115万円で
        # 乖離率+1.49%まで縮み有望になる（STEP2/3で検証済みの数値）
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)

        results = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )

        assert len(results) == 1
        found_listing, found, est_now, age_days = results[0]
        assert found_listing.url == listing.url
        assert found["aggressiveness"] == "standard"
        assert found["targets"]["target_price"] == 51_150_000
        assert age_days == 40
        assert est_now.asking_vs_fair_pct == pytest.approx(9.126984, abs=1e-4)

    def test_marks_notified_after_finding(self, db_path):
        # 発見した候補は sashine_notifications に記録される
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)

        _find_sashine_candidates({"13208": [listing]}, detail_cache=None, db_path=db_path)

        notified = evaluator.get_sashine_notified_aggressiveness(listing.url, db_path=db_path)
        assert notified == "standard"

    def test_no_candidates_when_already_promising(self, db_path):
        # 4,800万円 vs fair 5,040万円 → 乖離率-4.8%で既に有望
        # → 条件1(現在非有望であること)で除外され候補にならない
        listing = make_listing(price="4,800万円", url="https://suumo.jp/test/cheap-1/")
        _seed_age_days(listing, "13208", db_path, age_days=40)

        results = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )
        assert results == []

    def test_no_candidates_when_city_groups_empty(self, db_path):
        results = _find_sashine_candidates({}, detail_cache=None, db_path=db_path)
        assert results == []

    def test_no_candidates_when_no_history_defaults_to_mild(self, db_path):
        # 履歴なし(age_days=None)は mild 扱い。5,500万円はちょうど10万円単位
        # のため mild は値引きゼロ(STEP1で検証済みの端数なしケース)となり
        # 有望化しない → 候補にならない
        listing = make_listing()
        results = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )
        assert results == []

    def test_duplicate_suppression_same_aggressiveness_not_renotified(self, db_path):
        # 同じ強気度のまま2回目を実行しても再通知されない
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        city_groups = {"13208": [listing]}

        first  = _find_sashine_candidates(city_groups, detail_cache=None, db_path=db_path)
        second = _find_sashine_candidates(city_groups, detail_cache=None, db_path=db_path)

        assert len(first) == 1
        assert second == []  # 強気度(standard)が変わっていないため再通知されない

    def test_renotified_when_aggressiveness_escalates(self, db_path):
        # 強気度が standard → aggressive に上がったら再通知される
        listing = make_listing()
        city_groups = {"13208": [listing]}

        _seed_age_days(listing, "13208", db_path, age_days=40)  # → standard
        first = _find_sashine_candidates(city_groups, detail_cache=None, db_path=db_path)
        assert len(first) == 1
        assert first[0][1]["aggressiveness"] == "standard"

        # 履歴の最古日をさらに過去にずらし、90日以上経過(aggressive)にする
        _seed_age_days(listing, "13208", db_path, age_days=95)
        second = _find_sashine_candidates(city_groups, detail_cache=None, db_path=db_path)
        assert len(second) == 1
        assert second[0][1]["aggressiveness"] == "aggressive"

    def test_different_listings_tracked_independently(self, db_path):
        # 1件が既に通知済みでも、別のURLの物件は独立して候補になる
        listing_a = make_listing(url="https://suumo.jp/test/sashine-a/")
        listing_b = make_listing(url="https://suumo.jp/test/sashine-b/")
        _seed_age_days(listing_a, "13208", db_path, age_days=40)
        _seed_age_days(listing_b, "13208", db_path, age_days=40)

        _find_sashine_candidates({"13208": [listing_a]}, detail_cache=None, db_path=db_path)
        second = _find_sashine_candidates(
            {"13208": [listing_a, listing_b]}, detail_cache=None, db_path=db_path,
        )
        # listing_a は既に通知済みなので今回は含まれず、listing_b だけ含まれる
        assert len(second) == 1
        assert second[0][0].url == listing_b.url


# ---------------------------------------------------------------------------
# 2. _build_text_sashine: メッセージの整形
# ---------------------------------------------------------------------------

class TestBuildTextSashine:

    @pytest.fixture
    def sample(self, db_path):
        """発見済み候補1件（listing, found, est_now, age_days）を返す。"""
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        results = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )
        assert len(results) == 1
        return results[0]

    def test_contains_listing_name_and_price(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "テスト指値物件マンション" in text
        assert "5,500万円" in text

    def test_contains_current_vs_fair(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "+9.1%" in text
        assert "割高" in text

    def test_contains_three_negotiation_targets_with_correct_amounts(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "初回提示" in text
        assert "落としどころ" in text
        assert "引き際" in text
        # STEP1/2で検証済みの金額（万円換算）
        assert "4859万円" in text  # opening_offer = 48,590,000円
        assert "5115万円" in text  # target_price   = 51,150,000円
        assert "5268万円" in text  # walk_away      = 52,680,000円

    def test_contains_target_vs_fair_after_discount(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "+1.5%" in text  # 指値後の乖離率（+1.488...% を丸めた表示）

    def test_contains_aggressiveness_label(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "強気度: 標準" in text

    def test_contains_age_days_display(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert "確認してから 40日目" in text

    def test_contains_url(self, sample):
        listing, found, est_now, age_days = sample
        text = _build_text_sashine(listing, found, est_now, age_days, 1)
        assert listing.url in text

    def test_no_age_line_when_age_days_none(self, sample):
        # age_days=None（履歴なし）でも落ちず、確認日数の行だけ出ない
        listing, found, est_now, _ = sample
        text = _build_text_sashine(listing, found, est_now, None, 1)
        assert "確認してから" not in text
        assert "強気度: 標準" in text  # 強気度自体は別行で表示される


# ---------------------------------------------------------------------------
# 3. notify_line_sashine_candidates: LINE送信
# ---------------------------------------------------------------------------

class TestNotifyLineSashineCandidates:

    def test_sends_when_candidates_present(self, line_env, db_path):
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        candidates = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_sashine_candidates(candidates)

        sent = _collect_sent_texts(mock_post)
        assert any("💰 指値候補" in t for t in sent)
        assert any("テスト指値物件マンション" in t for t in sent)

    def test_no_send_when_empty(self, line_env):
        with patch("scraper.requests.post") as mock_post:
            notify_line_sashine_candidates([])
        mock_post.assert_not_called()

    def test_no_line_token_skips_without_exception(self, monkeypatch):
        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", None)
        monkeypatch.setattr(scraper, "LINE_USER_ID", None)
        with patch("scraper.requests.post") as mock_post:
            notify_line_sashine_candidates([])
        mock_post.assert_not_called()

    def test_message_contains_age_disclaimer(self, line_env, db_path):
        # マニュアル要件: 観測日数がbot確認開始からの目安であるという
        # 注記が必ず入ること（実掲載期間との誤解防止）
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        candidates = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_sashine_candidates(candidates)

        blob = "\n".join(_collect_sent_texts(mock_post))
        assert "bot確認開始からの目安" in blob
        assert "実際の掲載期間はより長い可能性があります" in blob

    def test_message_contains_caution_about_negotiation(self, line_env, db_path):
        # 交渉が通らない可能性がある旨の注意書きも入ること
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        candidates = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_sashine_candidates(candidates)

        blob = "\n".join(_collect_sent_texts(mock_post))
        assert "あくまで交渉の目安" in blob

    def test_not_confused_with_emphasized_header(self, line_env, db_path):
        # 強調版（★★有望物件★★）の見出しとは混同しない
        listing = make_listing()
        _seed_age_days(listing, "13208", db_path, age_days=40)
        candidates = _find_sashine_candidates(
            {"13208": [listing]}, detail_cache=None, db_path=db_path,
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_sashine_candidates(candidates)

        blob = "\n".join(_collect_sent_texts(mock_post))
        assert "★★ 有望物件 ★★" not in blob


# ---------------------------------------------------------------------------
# 4. main() の呼び出し順序・隔離構造（ソース検証。最重要の非回帰テスト）
# ---------------------------------------------------------------------------
#
# main() を直接実行せず、ソースコードの構造を検証することで非回帰を確認する
# 理由はファイル冒頭のdocstring参照（本番DBへの書き込みリスクを避けるため）。

class TestMainSashineWiring:

    def test_sashine_candidates_initialized_before_outer_try(self):
        # sashine_candidates の初期化が評価パイプラインのtryより前にある。
        # これがないと、outer try が丸ごと失敗したとき
        # notify_line_sashine_candidates(sashine_candidates) が
        # NameError で落ちてしまう（＝既存の値下げ通知にも影響が出る）。
        source = inspect.getsource(scraper.main)
        idx_init = source.index("sashine_candidates: list[tuple] = []")
        idx_first_try = source.index("try:", idx_init)
        assert idx_init < idx_first_try

    def test_sashine_finding_is_wrapped_in_its_own_try_except(self):
        # _find_sashine_candidates の呼び出し行の直前の行が "try:" である
        # （evaluate_and_save 等を包む outer try とは別の、内側の隔離）。
        # 呼び出しは "sashine_candidates = _find_sashine_candidates(...)" と
        # 代入文の一部なので、行単位で直前の行を見る（文字列位置の直前では
        # 代入部分の文字列が挟まるため一致しない）。
        source = inspect.getsource(scraper.main)
        lines = source.splitlines()
        call_line_idx = next(
            i for i, line in enumerate(lines)
            if "_find_sashine_candidates(city_groups, detail_cache)" in line
        )
        assert lines[call_line_idx - 1].strip() == "try:"

    def test_sashine_except_does_not_reraise(self):
        # sashine呼び出し直後の except ブロックが例外を再送出していないこと
        # （再送出すると outer try に伝播し、既存の通知まで止まりかねない）
        source = inspect.getsource(scraper.main)
        idx_call = source.index("_find_sashine_candidates(city_groups, detail_cache)")
        following = source[idx_call:idx_call + 400]
        assert "except Exception" in following
        except_idx = following.index("except Exception")
        block = following[except_idx:except_idx + 200]
        assert "raise" not in block
        # 失敗時に空リストへフォールバックしていること
        assert "sashine_candidates = []" in block

    def test_sashine_finding_runs_before_detect_changes(self):
        # sashine判定が detect_changes より前にあること（evaluate_and_save
        # 完了直後、値下げ検知の前に置く、というフェーズ1計画どおりの配置）
        source = inspect.getsource(scraper.main)
        idx_sashine = source.index("_find_sashine_candidates(city_groups, detail_cache)")
        idx_detect  = source.index("price_drop_alerts = detect_changes(")
        assert idx_sashine < idx_detect

    def test_sashine_notify_call_exists_and_no_new_listings_gate_remains(self):
        # 指値候補通知の呼び出しが main() に存在すること。
        #
        # ※ Gemini評価件数上限対応（優先評価・data.csv保存の早期化/無条件化）
        #   の実装により、以前あった「if not new_listings: return」という
        #   早期returnは不要になり削除された（data.csv保存がGemini評価より
        #   前で常に実行されるようになったため）。このテストはその削除が
        #   復活していないかの回帰チェックを兼ねる。
        #   「新着0件でも指値候補通知が実際に実行される」という実質的な
        #   保証は test_main_integration.py の実行ベーステスト
        #   （TestSashineRunsEvenWithoutNewListings）で確認済み。
        source = inspect.getsource(scraper.main)
        assert "notify_line_sashine_candidates(sashine_candidates)" in source
        assert "if not new_listings:" not in source

    def test_price_drop_notify_runs_before_sashine_notify(self):
        # 実行順序: 値下げ通知 → 指値候補通知（フェーズ1で承認した順序）
        source = inspect.getsource(scraper.main)
        idx_price_drop = source.index("notify_line_price_drops(price_drop_alerts)")
        idx_sashine    = source.index("notify_line_sashine_candidates(sashine_candidates)")
        assert idx_price_drop < idx_sashine

    def test_two_stage_and_reference_notify_still_present(self):
        # 新着がある日に実行される既存の通知が、コード上まだ存在すること
        # （sashine追加によって誤って消されていないかの簡易確認）。
        #
        # ※ 参考枠の既知物件対応（gemini_evaluations永続化）実装により、
        #   notify_line_two_stage / notify_line_reference の呼び出しは
        #   意図的に「if not new_listings:」より前に移動している
        #   （参考枠を new_listings の有無に関わらず実行するため）。
        #   このテストはその位置関係ではなく、呼び出し自体が存在することの
        #   確認に絞る。
        # ※ notify_line_two_stage は AI★数表示のため gemini_score_map 引数が
        #   追加されたので、関数名の呼び出しがある事実だけを確認する
        #   （厳密な引数リストの文字列一致は見ない）。
        source = inspect.getsource(scraper.main)
        assert "notify_line_two_stage(scored, est_map, gemini_score_map)" in source
        assert "notify_line_reference(reference_candidates, est_map)" in source
