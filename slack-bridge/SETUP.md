# Phase 1 起動手順

所要時間の目安: 15〜20分。うち Slack 側の作業が 10分程度。

## 完了済み（作業不要）

- [x] `slack-bridge/` 実装一式
- [x] venv 作成・依存関係インストール
- [x] テスト 36件 パス
- [x] `propose` の git 初期化

## Step 1. Slack App を作成（ブラウザ）

1. https://api.slack.com/apps を開く
2. **Create New App** → **From an app manifest** を選択
3. ワークスペースを選ぶ
4. **YAML** タブに `config/slack_app_manifest.yaml` の中身をそのまま貼り付ける
5. **Next** → **Create**

マニフェストにより Socket Mode の有効化・Bot スコープ・`app_mention` の購読が
一括で設定される。Request URL の入力欄は出てこない（公開エンドポイント不要のため）。

## Step 2. App-Level Token を発行（`xapp-`）

1. 左メニュー **Basic Information**
2. 下方の **App-Level Tokens** → **Generate Token and Scopes**
3. Token Name: `socket`（任意）
4. **Add Scope** → `connections:write`
5. **Generate** → 表示された `xapp-...` をコピー

> 後から確認したい場合は、App-Level Tokens の一覧でトークン名をクリックすれば
> 値が再表示される。再発行は不要。

## Step 3. ワークスペースへインストール（`xoxb-`）

1. 左メニュー **OAuth & Permissions**
2. **Install to Workspace** → 内容を確認して **許可する**
3. **Bot User OAuth Token** の `xoxb-...` をコピー

## Step 4. チャンネル作成と Bot の招待

Slack で4つのチャンネルを作る（名前は任意、後で channel_id で紐付ける）。

```text
#navigation-core   → work_env/navigation-core
#pet-camera        → work_env/Pet-Camera
#business-contest  → work_env/business_contest
#propose           → work_env/propose
```

各チャンネルで Bot を招待する。**招待しないとメンションが届かない。**

```text
/invite @ClaudeCode
```

## Step 5. トークンを .env に記入

`slack-bridge/.env` を開き、2行を実際の値に置き換える。

> **作業ディレクトリに注意。** `.env` もスクリプトも `slack_integration/` ではなく
> その下の `slack-bridge/` にある。1階層上で実行すると
> `can't open file ... scripts\check_tokens.py` になる。

```text
SLACK_BOT_TOKEN=xoxb-実際の値
SLACK_APP_TOKEN=xapp-実際の値
```

`.env` は `.gitignore` 済み（CLAUDE.md §16）。

記入したら、Slack に接続する前に検証する。

```powershell
cd C:\Users\yutob\work_env\slack_integration\slack-bridge
.\venv\Scripts\python.exe scripts\check_tokens.py
```

`両方のトークンが有効です。` と出れば Step 6 へ進める。
xoxb- と xapp- を取り違えている場合もここで指摘される。

## Step 6. channel_id を取得して設定に反映

```powershell
cd C:\Users\yutob\work_env\slack_integration\slack-bridge
.\venv\Scripts\python.exe scripts\list_channels.py
```

次のように表示される。

```text
channel_id     name
----------------------------------------
C09ABCDEFGH    #business-contest
C09IJKLMNOP    #navigation-core
C09QRSTUVWX    #pet-camera
C09YZABCDEF    #propose
```

`config/projects.yaml` の `C_REPLACE_ME_*` 4箇所を、対応する ID に置き換える。

> チャンネルが表示されない場合は Step 4 の `/invite` が漏れている。

## Step 7. 起動

```powershell
.\venv\Scripts\python.exe -m app.main
```

成功時のログ:

```text
INFO  slack_bridge: claude CLI: C:\Users\yutob\.local\bin\claude.exe
INFO  slack_bridge: 登録プロジェクト:
INFO  slack_bridge:   navigation-core   channel=C09... profile=investigate cwd=...\navigation-core
INFO  slack_bridge:   pet-camera        channel=C09... profile=investigate cwd=...\Pet-Camera
INFO  slack_bridge:   business-contest  channel=C09... profile=investigate cwd=...\business_contest
INFO  slack_bridge:   propose           channel=C09... profile=investigate cwd=...\propose
INFO  slack_bridge: Socket Mode で Slack に接続します（公開ポートは不要）
```

このウィンドウは開いたままにする。停止は Ctrl+C。

## Step 8. 受け入れテスト（CLAUDE.md §24）

### Test 1: navigation-core

`#navigation-core` で投稿する。

```text
@ClaudeCode
現在の構成を調査して、主要なディレクトリと役割をまとめて。
コード変更はしないで。
```

期待動作: 即座に「:mag: 処理を開始しました。Project: navigation-core」がスレッドに付き、
完了後に `navigation-core` の調査結果が同じスレッドへ返る。

### Test 2: pet-camera

`#pet-camera` で投稿する。

```text
@ClaudeCode
現在のシステム構成を調査してまとめて。
コード変更はしないで。
```

期待動作: `Pet-Camera` を対象に実行される（チャンネルだけで対象が切り替わる）。

### Test 3: 未登録チャンネル

`projects.yaml` に無いチャンネルで投稿する。

```text
@ClaudeCode 調査して。
```

期待動作: Claude Code は起動せず、channel_id を添えた警告が返る（CLAUDE.md §15）。

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| 起動時に「channel_id がプレースホルダのままです」 | Step 6 未実施。誤ったチャンネルで動くのを防ぐため意図的に停止している |
| 起動時に「SLACK_APP_TOKEN は xapp- で始まる必要があります」 | Step 2 と Step 3 のトークンを取り違えている。`scripts/check_tokens.py` で切り分けられる |
| `list_channels.py` に何も出ない | Step 4 の `/invite @ClaudeCode` が漏れている |
| メンションしても無反応 | Bot 招待漏れ、または App 再インストール後にトークンが変わっている |
| 同じ返信が2回来る | 想定外。`event_id` による冪等化が効いていないのでログを確認 |
| 返信が途中で切れる | 2900文字で分割投稿する仕様。続きが別メッセージで届く |

## 次フェーズ

Test 1〜3 が通ったら Phase 2（`thread_ts` 単位のセッション継続、`chat.update` に
よる進捗表示）へ進む。
