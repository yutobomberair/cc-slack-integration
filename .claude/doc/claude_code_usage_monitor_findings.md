# Usage Monitor 調査結果と推奨アーキテクチャ

`claude_code_usage_monitor_spec.md` の Phase 0（§19, §21）に対する回答。

調査日: 2026-09-27 / 対象: Claude Code **2.1.283** / Windows 11 / サブスクリプション認証

---

## 0. この道具が本当に答えるべき問い

仕様書は「使用量の可視化」として書かれているが、実際の目的は**ペース管理**である。

> 5時間枠を使い切ると、その枠がリセットされるまで作業できなくなる。
> 一方で週の総量を余らせるのはもったいない。

つまり必要なのは「いま何%か」ではなく、次の2つ。

| 問い | 必要な値 |
|---|---|
| このまま作業を続けて、途中で止められないか | 5時間枠の残量と**リセットまでの時間**、直近の消費速度 |
| 週の枠を余らせていないか / 使い切りそうか | 7日枠の残量と**残り時間**、その比（ペース） |

したがって**両側に警告が要る**。使いすぎ（枠切れ）だけでなく、**使い残し**も知らせる。
単なる閾値アラート（70/85/95）だけでは後者を拾えない。

### 導出すべき指標

```
5時間枠
  残量 %                       … 公式値から
  リセットまでの残り時間        … 公式値から
  現在の消費速度（%/時）        … スナップショット履歴の差分から
  この速度での枠切れ予測時刻     … 残量 ÷ 速度
      → リセット時刻より前に尽きるなら警告

7日枠
  残量 %                       … 公式値から
  リセットまでの残り時間        … 公式値から
  基準ペース（%/日）            … 残量 ÷ 残り日数
  実績ペース（%/日）            … 履歴から
      実績 > 基準 → 使いすぎ（週内に尽きる）
      実績 < 基準 → 使い残し（余らせる）
```

「残り X%」より「**この調子だと HH:MM に止まる**」「**あと1日あたり Y% 使える**」が
実用的な出力になる。

---

## 1. 結論

**5時間枠と7日枠の利用率・リセット時刻は、公式に取得できる。**
statusline に渡される JSON に含まれている。

```
rate_limits.five_hour.used_percentage    0〜100
rate_limits.five_hour.resets_at          Unix epoch 秒
rate_limits.seven_day.used_percentage    0〜100
rate_limits.seven_day.resets_at          Unix epoch 秒
rate_limits.spend_limit.used_percentage  ゲートウェイ配下のみ（v2.1.251+）
rate_limits.spend_limit.resets_at        同上
```

同じ JSON から Current Session（仕様書 §3.1）も揃う。

```
model.id / model.display_name
context_window.used_percentage / remaining_percentage
context_window.total_input_tokens / total_output_tokens
context_window.context_window_size
cost.total_cost_usd / total_duration_ms / total_api_duration_ms
session_id / transcript_path / version
workspace.current_dir / workspace.project_dir
```

出典: https://code.claude.com/docs/en/statusline

---

## 2. 日次制限は存在しない

Claude Code の制限は **5時間枠**と**7日枠**の2本だけ。**日次という区切りは無い。**
また7日枠は暦週ではなく**ローリング7日**（`resets_at` が示す時刻に切り替わる）。

仕様書 §3.3 の Daily Usage は、§20 の方針どおり扱う。

* 公式値としては **`Unavailable`**
* transcript から集計できる範囲で **`Estimated`**（トークン数ベース。Quota の%ではない）

**日次の「%」を勝手に作って公式値のように見せない。** 利用者が知りたい「今日使いすぎたか」は、
5時間枠の消費速度と7日枠のペースで表現するほうが実態に合う。

---

## 3. 使えなかった経路

| 経路 | 結果 | 判定 |
|---|---|---|
| `/usage` スラッシュコマンド | 公式ドキュメントに記載なし。**存在しない** | 使用不可 |
| CLI フラグ | `--max-budget-usd` のみ（支出上限の設定。取得ではない） | 使用不可 |
| OpenTelemetry | `claude_code.token.usage` と `claude_code.cost.usage` のみ。**rate limit のメトリクスは無い** | 補助的 |
| `~/.claude/stats-cache.json` | 日次・モデル別トークン集計あり。ただし `lastComputedDate: 2026-09-05` で**更新が止まっている**（調査日は 09-27）。`costUSD` は全モデル 0 | 使用不可 |
| transcript の `quotaLimits` | **制限に到達した瞬間だけ**記録。実測10件すべて `status: "rejected"`。`rateLimitType: "five_hour"` と `resetsAt` は取れるが**利用率は無い** | 補助的 |
| transcript の `usage`（各 API 応答） | `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` が継続的に記録される | **ローカル集計の原資** |
| Anthropic 公式 API | サブスクリプションの利用量は公開されていない。Admin API の usage report は組織向けで別物 | 使用不可 |
| Claude Web / Account API | 非公開。Cookie 依存。§17 に反するため採らない | 採用しない |

### `quotaLimits` の実測値

```json
{
  "status": "rejected",
  "resetsAt": 1790177400,
  "unifiedRateLimitFallbackAvailable": false,
  "rateLimitType": "five_hour",
  "overageStatus": "rejected",
  "overageDisabledReason": "org_level_disabled",
  "upgradePaths": ["upgrade_plan"],
  "isUsingOverage": false
}
```

**事後の記録**であって監視には使えない。ただし「実際に枠切れした履歴」として価値があるので、
予測の精度を検証する材料に使える。

---

## 4. 設計を左右する制約：statusline は push 型

**任意のタイミングで取得できない。** Claude Code がスクリプトを起動して stdin に JSON を
渡す形なので、こちらから引きに行く手段がない。

したがって statusline スクリプトが**唯一の公式値の入口**になる。

```
Claude Code ──(stdin JSON)──> statusline スクリプト   ← Official Provider
                                      │ スナップショットを書く
                                      ▼
                               usage.db (SQLite)
                                      ▲
transcript *.jsonl ──(独自集計)────> Local Provider    ← Estimated
                                      │
                          ┌───────────┼───────────┐
                          ▼           ▼           ▼
                         CLI      Statusline     Alert
```

### 帰結

* **Claude Code を使っていない間は更新されない。** 使っていなければ消費もされないので実害は
  小さいが、`claude-usage` は**必ず最終更新時刻を表示する**こと。古い値を現在値として
  見せてはいけない。
* 消費速度の算出にはスナップショットが2点以上必要。**履歴の蓄積が Phase 1 から要る**
  （仕様書では Phase 2 だが、ペース管理が目的なら前倒しになる）。
* statusline は頻繁に呼ばれる。**毎回 SQLite へ書くと重くなる**ので、値が変わったときだけ
  記録する（`used_percentage` が同じなら書かない）。

---

## 5. 未検証の1点

ドキュメントに載っている `rate_limits` が、**この環境で実際に値が入るか**は statusline を
設定しないと確認できない。API キー認証では出ない可能性がある。

**Phase 1 の最初のステップとして実測する。** ここが空なら Official Provider は成立せず、
設計が変わる（transcript のトークン集計による Estimated だけになる）。

---

## 6. 推奨アーキテクチャ

### Provider

| Provider | 取得元 | Confidence | 取れるもの |
|---|---|---|---|
| `StatuslineProvider` | statusline JSON のスナップショット | `official` | 5時間枠・7日枠の利用率とリセット時刻、Context、モデル、コスト |
| `TranscriptProvider` | `~/.claude/projects/**/*.jsonl` | `local_aggregation` | トークン消費量（プロジェクト別・モデル別・時間帯別） |
| `PaceCalculator` | 上記の履歴 | `estimated` | 消費速度、枠切れ予測、基準ペースとの差 |

Confidence は値ごとに持つ（仕様書 §5）。**`official` と `estimated` を同じ見た目で並べない。**

### プロジェクト別の集計（仕様書 §7）

Subscription quota はプロジェクト単位で提供されないので、**トークン数の比率**で出す。
`Quota の X%` とは書かない。transcript のディレクトリ名がプロジェクトに対応するので集計できる。

### 出力

```
claude-usage              要点だけ（ペース判定を含む）
claude-usage status       詳細（Confidence 付き）
claude-usage --json       機械可読（仕様書 §10）
claude-usage statusline   statusline 用の1行（stdin から JSON を受ける）
```

### 保存

**SQLite は使わない**（仕様書 §6 からの逸脱）。実測すると履歴は1行 170 バイト弱、
90日で 7MB 程度にしかならず、SQL の利点が出る規模ではない。CSV なら Excel で開けて
中身を目で確かめられる。プロジェクト別の集計を月単位で出す段階（Phase 4）になったら
移行を検討する（追記専用なので読んで INSERT するだけ）。

生 JSON は `latest.json` の1件だけに持つ。当初は履歴の全行に入れていたが、**1行の
79% を占め 90日で 107MB** になる見込みだったので外した。フィールドが増えたときに
追うという目的には最新1件で足りる。

複数セッションが同時に statusline を呼ぶので、追記はロックファイルで直列化する。
Windows では追記の原子性が保証されない。

**§17 のとおり認証情報は一切保存しない。** statusline JSON に認証情報は含まれないので、
`raw_data` をそのまま保存しても問題ないことを確認済み。

---

## 7. 実装フェーズ（ペース管理を優先して並べ替え）

| Phase | 内容 | 仕様書 |
|---|---|---|
| **1a** | probe で `rate_limits` の実在を確認 | §19 Phase 0 の残り |
| **1b** | statusline スクリプト（スナップショット保存＋1行出力） | §13 を前倒し |
| **1c** | `claude-usage` / `status` / `--json` | §8, §9, §10 |
| **2** | SQLite 蓄積と保持期間（90日） | §6, §16 |
| **3** | ペース判定（枠切れ予測・使い残し検出） | §0（本書で追加） |
| **4** | 閾値アラート＋通知 Provider | §11, §12 |
| **5** | 必要になったら Dashboard | §18 |

§13（statusline 連携）を Phase 1 に上げているのは、**それが公式値の唯一の入口**であり、
後回しにすると Phase 1 で表示する値そのものが手に入らないため。

Phase 3 のペース判定を独立させたのは、これが本来の目的（上限に当てず、余らせない）に
直接答える部分だから。閾値アラートより先に来る。
