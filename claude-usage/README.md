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
claude-usage                  # 要点とペース判定
claude-usage status           # 詳細（値ごとの Confidence 付き）
claude-usage --json           # 機械可読
claude-usage projects         # プロジェクト別のトークン消費
claude-usage alerts           # いまの判定と、鳴った履歴
claude-usage config           # 設定ファイルの雛形を書き出す
claude-usage prune            # 保持期間を過ぎた履歴を捨てる
claude-usage --test-notify    # 通知先の動作確認
```

終了コードは、ペースに問題（使いすぎ・枠切れ）があれば `2`。監視スクリプトから使える。
使い残し（`underuse`）は問題ではないので `0`。

## Slack から聞く

スマホから確認できないと意味が薄いので、`slack-bridge/` 経由で同じ内容が返る。

```
@ClaudeCode usage            @ClaudeCode usage projects
@ClaudeCode usage status     @ClaudeCode usage alerts
```

呼び出し口は `claude_usage/api.py`（`report(command)`）。CLI の中身は argparse と
標準出力に結び付いているので、外から使うにはここを通す。

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

## プロジェクト別の消費

枠が尽きそうなとき「どこを絞るか」を決める材料。

```
$ claude-usage projects
期間: いまの7日枠
  2026-09-26 22:00 以降
  63 ファイル / 748 応答

#####-----  51.0%  propose
             out 820.7k / cache新 4.2M / cache読 227.9M
#####-----  48.5%  slack_integration
             out 443.2k / cache新 4.3M / cache読 128.7M
```

**これは利用枠の配分ではない。** Subscription Quota はプロジェクト単位で提供されて
いないので、出せるのは**トークン数の比率**だけ。そこから「5時間枠の何%をどの
プロジェクトが使ったか」を作って公式値のように見せない（仕様書 §7, §20）。

期間は既定で**いまの7日枠**に合わせる（暦の7日ではない）。`--days 30` で任意の日数。

### 比率の出し方を明示している理由

トークンは4種類あり桁が大きく違う。実測値はこう。

```
cache_read       45億      既に作ったキャッシュの読み直し
cache_creation    2億
output         1,860万
input           2.6万
```

`cache_read` を含めると比率が**セッションの長さに支配されて作業量を表さなくなる**
ので、比率は `input + output + cache作成` で出し、`cache_read` は内訳として別に見せる。
どちらで出しても順位はほぼ同じだった（21.0% と 24.2%）が、どの式かを黙るべきではない。

### 階層は既定でまとめる

`cd:` で階層を移れるので、`propose` と `propose/movie` が別々に記録される。既定では
git リポジトリのルートまで遡ってまとめる。分けて見たいときは `--by-cwd`。

```
$ claude-usage projects --by-cwd
  50.6%  movie
  44.3%  slack_integration
   2.5%  slack-bridge
   1.8%  claude-usage
```

`~/.claude/projects/**/*.jsonl` を毎回読む。実測で 63ファイル 12,718レコードが
**0.7秒**なのでキャッシュは持たない（古い値を出す危険が増えるだけ）。

## アラート

**daemon は無い。** statusline が数百ミリ秒ごとに呼ばれているので、監視の起点は既に
ある。常駐プロセスを足すと二重起動・停止忘れ・使っていないのに鳴る、といった問題を
新たに抱えることになる。

閾値は仕様書 §11 の既定（`70` / `85` / `95`）。それに加えて**使い残し**も鳴らす。
この道具の目的が両側なので、閾値だけでは足りない。

### 同じことを鳴らし続けない

statusline は常に呼ばれているので、素朴に閾値を見ると毎回鳴る。枠ごとに「どのレベル
まで鳴らしたか」を `resets_at` と一緒に覚え、**より重いレベルに上がったときだけ**
鳴らす。枠がリセットされたら記録を捨てて鳴らし直す。

```
50%  閾値未満          -> 鳴らない
72%  warning 到達      -> 鳴った
75%  同じレベル         -> 鳴らない
96%  critical へ       -> 鳴った
 5%  枠がリセット        -> 鳴らない
72%  新しい枠で再到達    -> 鳴った
```

1つの枠で「70%に達した」と「このペースだと尽きる」を2通出すと読まれなくなるので、
重いほうだけを出す。

### 送信は statusline で待たない

Slack への HTTP や PowerShell の起動に1〜3秒かかることがある。数百ミリ秒ごとに
呼ばれる statusline をそこで止めると画面が固まるので、**別プロセスへ投げて忘れる**。
鳴る頻度は低いので起動のコストは問題にならない。

### 通知先（仕様書 §12）

| | 既定 | 備考 |
|---|---|---|
| `terminal` | 有効 | 標準エラーへ。statusline の1行を汚さない |
| `file` | 有効 | `~/.claude-usage/alerts.log`。いつ何が鳴ったか分からないと閾値が妥当か判断できない |
| `windows` | 無効 | `NotifyIcon` のバルーン。追加モジュール不要 |
| `slack` | 無効 | チャンネルとトークンの両方が必要 |

Slack のトークンは次の順で探す。

1. `slack_token_file` が指すファイル（`KEY=value` 形式でも生のトークンでも読む）
2. 環境変数 `SLACK_BOT_TOKEN`
3. `slack_token`（設定ファイルへ直接書いた場合）

**1 を勧める。** 既に `.env` にトークンがあるならそれを指せば済み、平文の複製を
増やさずに一元化できる。`slack-bridge/` と同じトークンが使える。

```json
{
  "notifiers": {
    "slack": true,
    "slack_channel": "C0C2QTTL4D9",
    "slack_token_file": "C:\Users\you\work\slack-bridge\.env"
  }
}
```

**Bot をそのチャンネルに招待しておくこと。** 公開チャンネルでも `chat:write` だけでは
非参加のチャンネルへ投稿できず、`not_in_channel` で失敗する。

```
/invite @ClaudeCode
```

## 記録の置き場

```
~/.claude-usage/
├── config.json       設定（無くても既定値で動く）
├── latest.json       最新の1件（statusline の生 JSON 付き）
├── snapshots.csv     履歴（追記のみ、1行 170 バイト弱）
├── alert-state.json  どのレベルまで鳴らしたか（枠ごと）
└── alerts.log        鳴った履歴
```

**SQLite は使わない。** 90日ぶんでも数MBで、SQL の利点が出る規模ではない。CSV なら
Excel でそのまま開けて、中身を目で確かめられる。プロジェクト別の集計を月単位で出す
段階になったら移行を検討する（追記専用なので読んで INSERT するだけ）。

生 JSON は `latest.json` にだけ置く。フィールドが増えたときに追うのが目的で、履歴の
全行に持たせる意味が無い。当初は履歴にも入れていたが**1行の 79% を占め、90日で
107MB になる見込み**だったので外した（今は 7MB 程度）。

`prune(retention_days=90)` で保持期間を過ぎた行を捨てる（仕様書 §16）。

ターミナルを複数開くとそれぞれの Claude Code が statusline を呼ぶので、書き込みが
重なりうる。Windows では追記の原子性が保証されないため、ロックファイルで直列化する。
**取れなければ履歴を諦める**（statusline を待たせる害のほうが大きい）。取り残された
ロックは5秒で無効とみなす。

## テスト

```bash
python -m pytest tests/ -q          # 125 件
python -m ruff check claude_usage tests --select F,E501 --line-length 100
```

重点はペース判定（使いすぎ・使い残し・枠切れ予測・リセット跨ぎ）、アラートの重複抑制
（同じレベルで鳴らない・上がれば鳴る・枠が変われば鳴らし直す）、壊れた入力への耐性
（フィールド欠落・型違い・壊れた設定）、記録の安全性（混ざった行・取り残されたロック）。

## この先

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | 公式値の取得、statusline、CLI、ペース判定 | **済** |
| 2 | ~~SQLite~~ → CSV で蓄積、保持期間（90日） | **済** |
| 3 | 閾値・ペースのアラートと通知（terminal / file / Windows / Slack） | **済** |
| 4 | プロジェクト別・モデル別集計（transcript のトークン数から） | **済** |
| — | Discord 通知 | 未（`notifiers.py` に1関数足すだけ） |
| 5 | Dashboard | 必要になったら |

Phase 3 の Slack 通知は、同じリポジトリの `slack-bridge/` と組み合わせられる。
