# claude-usage

Claude Code の利用枠を「**上限に当てず、かつ余らせない**」ために見る道具。

仕様は [`../.claude/doc/claude_code_usage_monitor_spec.md`](../.claude/doc/claude_code_usage_monitor_spec.md)、
調査結果と設計判断は [`../.claude/doc/claude_code_usage_monitor_findings.md`](../.claude/doc/claude_code_usage_monitor_findings.md)。

## 何を見る道具か

知りたいのは「いま何%か」ではない。

| 問い | 出すもの |
|---|---|
| このまま続けて途中で止められないか | 5時間枠の残量、リセットまでの時間、**この調子だと何時に尽きるか** |
| 週の枠を余らせていないか | 7日枠の残量、残り日数、**1日あたりいくら使えるか**、実績との差 |

だから警告は**両側**にある。使いすぎだけでなく**使い残し**も知らせる。

```
Pace
  !! このペースだと 15:42 に5時間枠を使い切ります
     リセットは 17:10（24.3%/時 で消費中）
  -- 7日枠を使い残しそうです（実績 2.4%/日 < 目安 18.0%/日）
     あと 5.0 日で残り 90%。1日 18.0% まで使えます
```

## 使い方

```bash
claude-usage           # 要点とペース判定
claude-usage status    # 詳細（値ごとの Confidence 付き）
claude-usage --json    # 機械可読
```

終了コードは、ペースに問題（使いすぎ・枠切れ）があれば `2`。監視スクリプトから使える。
使い残し（`underuse`）は問題ではないので `0`。

## セットアップ

利用枠の値は **statusline スクリプトの stdin にしか流れてこない**。こちらから取りに
行く手段がないので、statusline を設定して記録させる。

`~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "\"C:\\path\\to\\python.exe\" -m claude_usage.statusline",
    "padding": 0
  },
  "env": {
    "PYTHONPATH": "C:\\path\\to\\claude-usage"
  }
}
```

これで Claude Code を使うたびにスナップショットが `~/.claude-usage/` に溜まり、
statusline には1行が出る。

```
Opus 5 | ctx 62% | 5h 78% (reset 1:12) | 7d 49%
```

依存は標準ライブラリだけ。Python 3.13 で動く。

## 取れる値と取れない値

**公式に取れる**（`confidence: official`）

```
rate_limits.five_hour.used_percentage / resets_at
rate_limits.seven_day.used_percentage / resets_at
```

**取れない**

- **日次制限は存在しない。** Claude Code の枠は5時間と7日の2本だけ。`claude-usage status`
  では `Daily: Unavailable` と出す。勝手に日次の%を作らない（仕様書 §20）
- `/usage` スラッシュコマンドは無い
- OpenTelemetry に rate limit のメトリクスは無い
- `~/.claude/stats-cache.json` は更新が止まっており使えない

**推定値**（`confidence: estimated`）

消費速度と枠切れ予測。スナップショットの差分から出すので、2点以上溜まるまでは
「計測待ち」と表示する。

## 設計上の要点

**取りに行けないので、いつの値かを必ず出す。** Claude Code を使っていない間は更新
されない。`Updated:` を常に表示し、10分以上古ければその旨を添える。古い値を現在値
として見せない。

**枠のリセットを跨いで速度を計算しない。** リセットすると利用率が下がるので、跨いだ
区間で差分を取ると「消費が減った」ことになって見通しが壊れる。`resets_at` の変化と
利用率の低下の両方でリセットを検出し、そこで履歴を切る。

**文字コードに追従する。** Windows の既定コンソールは cp932 で、`█` や `↺` を
encode できない。素朴に `print` すると `UnicodeEncodeError` で**何も出ないまま終わる**
（実際に `↺` で踏んだ）。出せるかを確かめて ASCII に落とす（`app/display.py`）。

**statusline は絶対に落とさない。** 失敗すると利用者の画面が壊れるので、値が無ければ
省き、例外は飲む。記録の失敗より表示の継続を優先する。

**高頻度の呼び出しを前提にする。** statusline は数百ミリ秒ごとに呼ばれうる。利用率が
動いていないスナップショットは履歴に足さない。

## テスト

```bash
python -m pytest tests/ -q          # 52 件
python -m ruff check claude_usage tests --select F,E501 --line-length 100
```

重点はペース判定（使いすぎ・使い残し・枠切れ予測・リセット跨ぎ）と、壊れた入力への
耐性（フィールド欠落・型違い）。

## この先

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | 公式値の取得、statusline、CLI、ペース判定 | **済** |
| 2 | SQLite への蓄積と保持期間（90日） | 未 |
| 3 | 閾値アラートと通知（Windows / Slack / Discord） | 未 |
| 4 | プロジェクト別集計（transcript のトークン数から） | 未 |
| 5 | Dashboard | 必要になったら |

Phase 3 の Slack 通知は、同じリポジトリの `slack-bridge/` と組み合わせられる。
