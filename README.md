# suumo-line-bot

SUUMOの新着物件を定期チェックし、国交省reinfolibデータによる評価とGemini AI評価を付けてLINEに通知するボット。

## 運用メモ

### SUPPRESS_SCORE_GAIN_ALERTS（デプロイ初日限定の一時変数）

`resale_score`の算出式を変更してデプロイした直後は、新旧スコア式の差分により
`score_gain`（スコア改善）が閾値以上跳ねる偽アラートが出ることがある
（docs/score-fairness-spec.md STEP3参照）。

- デプロイ当日の最初のスケジュール実行（またはworkflow_dispatch）のみ、
  GitHub Actionsのリポジトリ変数またはsecretsで `SUPPRESS_SCORE_GAIN_ALERTS=1` を設定する。
- **翌日には必ず外す。** この変数は「デプロイ初日の断層を1回だけ吸収する」ための
  一時的なものであり、常設する変数ではない。
- 抑制されるのは `score_gain` 起因のみのアラート。`price_drop`（50万円以上の値下げ）が
  同時に閾値を満たしている場合は、`score_gain`も条件を満たしていても抑制されない
  （値下げ情報を握りつぶさない設計。`scraper._filter_score_gain_only_alerts`参照）。
- **外し忘れ検知**: `SUPPRESS_SCORE_GAIN_ALERTS=1` が立ったままの状態で実行すると、
  `scraper.py`実行ログの冒頭に `[警告] SUPPRESS_SCORE_GAIN_ALERTS=1 が設定されています...`
  という警告行が毎回出力される。立てっぱなしだとscore_gain起因のアラートが永久に
  沈黙するため、スケジュール実行のログにこの警告が出続けていないか確認すること。
