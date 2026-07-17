# 実装指示書: resale_scoreへの割安度（実勢比）組み込み

対象リポジトリ: suumo-line-bot
作成日: 2026-07-16
実装担当: Claude Code / 設計・レビュー: Claude(会話) + しゅん

---

## 0. 目的（最重要・全判断の基準）

**このシステムの目的は「割安な物件を買う」こと。**
スコアは「割安な買い物ランキング」として読める数字であるべきで、
流動性が高くても割高な物件が高得点に見える現状は目的に反する。

発端の実例: 是政4LDK（nc_20893454）が実勢比+38.0%の大幅割高なのに
スコア61/100と表示された。

## 1. 現状の事実（2026-07-16 mainブランチで確認済み）

- `_resale_score`（reinfolib_resale.py L366-408）の入力は
  駅徒歩・面積帯・総戸数・修繕積立金・将来築年数の5要素のみ。
  **asking_vs_fair_pct（実勢比）はスコアに一切入っていない**
- `estimate_resale` 内で asking_vs_fair は計算済みだが、
  `_resale_score` 呼び出しに渡していない
- `_is_promising`（scraper.py）は
  `resale_score >= PROMISING_SCORE_THRESHOLD(=70)` かつ
  `asking_vs_fair_pct <= PROMISING_VS_FAIR_MAX_PCT` の二段ゲート。
  つまり有望枠には既に割高除外がある（表示スコアだけが割安度を含まない）
- `detect_changes`（evaluator.py）は
  `price_drop >= min_price_drop OR score_gain >= SCORE_GAIN_THRESHOLD`
  でアラート発火。スコア**上昇**のみ発火し、下落では発火しない
- **sashine.py のdocstringが「resale_scoreはasking_priceに
  依存しない」ことを設計前提として明文化している**（指値シミュレーションは
  asking_priceを差し替えてestimate_resaleを丸ごと再実行する方式）
- resale_scoreはevaluationsテーブルに日次保存され、過去行との比較に使われる

## 2. 設計決定

### 採用案: _resale_scoreに実勢比の段階加減点を組み込む（案A）

```python
# _resale_score に引数 asking_vs_fair_pct: Optional[float] を追加
if asking_vs_fair_pct is not None:
    pct = asking_vs_fair_pct
    if pct >= 30:
        score -= 20
        notes.append(f"実勢比+{pct:.1f}%は大幅割高。出口での値下がり余地が大きい。")
    elif pct >= 15:
        score -= 12
        notes.append(f"実勢比+{pct:.1f}%は割高。指値交渉の前提で検討要。")
    elif pct >= 8:
        score -= 5
    elif pct <= -10:
        score += 8
        notes.append(f"実勢比{pct:.1f}%は割安圏。")
# None（カーブ欠損）の場合は加減点なし（現状スコアと同じ挙動）
```

- 減点を加点より重くする非対称設計は意図的（目的が「割安を買う」なので、
  割高を掴まないことを優先）
- `estimate_resale` 内の呼び出し順は既に asking_vs_fair 計算が
  score 計算より先なので、引数を1本増やすだけでよい

### 不採用案（記録として残す）: 表示のみ変更（案B）

resale_scoreは純粋な流動性スコアのまま維持し、通知側で
「流動性53点・割高+10%」のような2軸表示にする案。
不採用理由: detect_changesのscore_gainアラート・_is_promisingの
閾値・参考枠の判定など、下流はすべて単一スコアを前提にしており、
2軸化はほぼ全通知経路の改修になる。また案Aには
「値下げ→スコア自動上昇→score_gainアラートが割安化検知として
機能し始める」という目的直結の副次効果がある。

## 3. 影響範囲の一覧（実装前に必ず全数確認）

| 箇所 | 影響 | 対応 |
|---|---|---|
| reinfolib_resale.py `_resale_score` / `estimate_resale` | 本体変更 | 引数追加・段階加減点 |
| sashine.py docstring | **設計前提が変わる** | docstring書き換え。「scoreはasking_priceに依存するようになったが、指値シミュレーションはCandidate差し替え→estimate_resale再実行方式のため計算整合は保たれる（むしろ指値後スコア改善が正しく反映される)」旨に更新 |
| sashine.py 指値探索ロジック | 挙動変化（良い方向） | 指値を下げるとスコアも上がるため、_is_promising到達判定が現実に近づく。既存テストの期待値要確認 |
| scraper.py `_is_promising` | 二重ゲート化 | **変更しない**。スコア内の減点とVS_FAIR_MAXゲートは意図的に併存させる。閾値70の再調整はSTEP 2の分布確認後に判断 |
| evaluator.py `detect_changes` score_gain | デプロイ初日に断層 | 新旧式の差で割安物件が+8跳ねて偽score_gainアラートが出る恐れ。デプロイ初回runのみscore_gainアラートを抑制する一時措置を入れる（STEP 3） |
| evaluationsテーブルの過去行 | 遡及なし | 過去行は旧式のまま残す（再計算しない）。日次比較の断層は初日のみ |
| tests/ 各種 | スコア期待値のハードコード | 影響テストを全数洗い出して期待値更新。**期待値を新実装の出力に合わせて書き換えるのではなく、手計算で正しい値を先に決めてから直すこと** |
| 通知本文の表示 | 変更なし | 「売りやすさスコア」の名称は据え置き可（割安度込みになった旨はREADMEに記載） |

## 4. 実装ステップ（段階的承認・各STEP完了ごとに停止）

### STEP 1: 影響テストの全数リストと現行スコア分布の取得
- 変更前に、現行36件（evaluations最新日）の
  「URL・現行スコア・実勢比・新式での再計算スコア」対照表を作る
  （読み取り専用スクリプト。本番DB書き込み禁止）
- 有望枠（>=70）への出入りが何件発生するかを表で報告
- スコア期待値をハードコードしている既存テストの全数リストを報告

### STEP 2: 本体実装
- `_resale_score` 引数追加＋段階加減点＋notes
- sashine.py docstring更新
- STEP 1の対照表と突き合わせて、意図どおりの変化かしゅんが承認

### STEP 3: デプロイ初日対策
- 環境変数 `SUPPRESS_SCORE_GAIN_ALERTS=1` のとき、detect_changes呼び出し側で
  score_gain起因のアラートのみ抑制する（price_drop起因は通常どおり）
- デプロイ初回のスケジュール実行のみこの変数を立て、翌日外す運用手順を
  README（運用メモ）に1行残す

### STEP 4: テストとdry_run
- 既存テストの期待値更新＋新規テスト:
  1. +38%割高 → 20点減点される（是政の実例再現: 61→41相当）
  2. -10%超割安 → 8点加点
  3. pct=None → スコア不変（現行と同値）
  4. 指値シミュレーション: 指値を深くするとスコアが単調非減少
  5. デプロイ初日抑制: SUPPRESS時にscore_gainアラートが出ず、price_dropは出る
- dry_runは本番データ・環境健全性チェック付き。
  **サンプルには経路ごとに「実データ/注入」のタグを必ず明記すること**

## 5. 禁止事項

- `_is_promising` の閾値・ゲート構造の独断変更（分布確認後にしゅんが判断）
- evaluationsテーブル過去行の再計算・書き換え
- sashine.pyの指値探索アルゴリズム自体の変更（docstringと期待値のみ）
- 新規APIコールの追加

## 6. 受け入れ条件（DoD)

- [ ] 是政実例: +38%割高の物件のスコアが大幅減点され、割安度がスコアに反映される
- [ ] 現行36件の新旧スコア対照表をしゅんが承認済み
- [ ] sashine docstringの設計前提が実装と一致
- [ ] デプロイ初日の偽score_gainアラート対策が入っている
- [ ] 全既存テスト＋新規テストがパス（期待値は手計算で先に確定）
- [ ] 本番データdry_run（実/注入タグ明記）の目視承認
