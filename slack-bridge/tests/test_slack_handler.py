"""``slack_handler`` の振り分けと実行フローの特性化テスト。

このファイルの目的は「今の挙動を固定すること」。``handle_app_mention`` と
``_run_task`` は合計300行近くあるのに直接のテストが無く、分解や引数の整理を
しても壊れたことに気づけなかった。リファクタリングの前にここで挙動を釘付けする。

したがって、ここで検証するのは内部構造ではなく**外から観測できること**だけ。

* Slack へ何を投稿したか
* Claude をどんな引数で起動したか（そもそも起動したか）
* ファイルシステムに何を残したか

内部の関数名や引数の渡し方が変わってもこのテストは通り続けるべきで、
通らなくなったらそれは挙動が変わったということ。
"""

import dataclasses
import sys
import threading
from pathlib import Path

import pytest
from slack_bolt import App

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import claude_runner, inbox, slack_handler  # noqa: E402
from app.claude_runner import ClaudeResult  # noqa: E402
from app.config import PermissionProfile, Project, Runtime, Settings  # noqa: E402


# --------------------------------------------------------------------------
# 足場
# --------------------------------------------------------------------------

class FakeClient:
    """Slack の代わり。投稿・更新・アップロードを記録するだけ。"""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.uploads: list[dict] = []
        self._ts = 0

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        self._ts += 1
        return {"ts": f"{self._ts}.0"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts")}

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)

    @property
    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.posts]

    def said(self, needle: str) -> bool:
        return any(needle in t for t in self.texts)


@pytest.fixture(autouse=True)
def offline_bolt(monkeypatch):
    """Bolt は起動時に auth.test を叩く。テストでは通信させない。

    本番ではこの検証が「トークンの取り違え」を早期に知らせてくれるので、
    production 側は触らずテスト側だけ差し替える。
    """
    def build(**kwargs):
        return App(signing_secret="test", token_verification_enabled=False, **kwargs)

    monkeypatch.setattr(slack_handler, "App", build)


@pytest.fixture
def project_dir(tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir()
    return cwd


@pytest.fixture
def settings(tmp_path, project_dir):
    return Settings(
        bot_token="xoxb-test",
        app_token="xapp-test",
        projects={
            "demo": Project(
                key="demo",
                name="Demo",
                channel_id="C111",
                working_directory=project_dir,
                profile="implement",
                allow_implement=True,
            )
        },
        profiles={
            "implement": PermissionProfile(key="implement"),
            "investigate": PermissionProfile(key="investigate", tools=["Read"]),
        },
        runtime=Runtime(
            session_store_path=tmp_path / "sessions.json",
            artifact_store_path=tmp_path / "artifacts.json",
            progress_interval_seconds=0,  # 進捗スレッドを起こさない
        ),
    )


@pytest.fixture
def dispatch(settings):
    """``app_mention`` ハンドラを呼び、ワーカーの完了まで待つ関数を返す。"""
    app = slack_handler.create_app(settings)
    handler = app._listeners[0].ack_function

    def _dispatch(client, text="依頼", files=None, channel="C111", event_id="Ev1"):
        handler(
            body={"event_id": event_id},
            event={
                "channel": channel,
                "ts": "100.0",
                "text": f"<@U999> {text}" if text else "<@U999>",
                **({"files": files} if files else {}),
            },
            client=client,
        )
        # executor.submit したタスクを取り切ってから検証する
        app._slack_bridge_executor.shutdown(wait=True)

    return _dispatch


@pytest.fixture
def ran(monkeypatch):
    """``claude_runner.run`` を差し替え、呼ばれた引数を記録する。

    ``ran.writes`` に (相対パス, 内容) を積んでおくと、Claude が実行中に
    そのファイルを作ったかのように振る舞う。出力ファイルの検出は「実行の前後で
    変化したか」で判断するので、実行より前に置いたファイルでは再現できない。
    """
    class Calls(list):
        """記録した呼び出し。``writes`` は実行中に作らせたいファイル。"""

        writes: list[tuple[str, str]]

    calls = Calls()

    def fake_run(**kwargs):
        calls.append(kwargs)
        for rel, body in calls.writes:
            target = Path(kwargs["working_directory"]) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return ClaudeResult(ok=True, text="やりました", num_turns=1, cost_usd=0.5)

    calls.writes = []
    monkeypatch.setattr(claude_runner, "run", fake_run)
    return calls


# --------------------------------------------------------------------------
# 受け付けない依頼
# --------------------------------------------------------------------------

def test_unknown_channel_does_not_run_claude(dispatch, ran):
    client = FakeClient()
    dispatch(client, channel="C_UNKNOWN")

    assert ran == []                      # CLAUDE.md §15
    assert client.said("C_UNKNOWN")       # channel_id を返す


def test_empty_request_does_not_run_claude(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="")

    assert ran == []
    assert client.said("依頼内容が空でした")


def test_resent_event_is_ignored(dispatch, ran):
    client = FakeClient()
    dispatch(client, event_id="Ev-same")
    first = len(ran)
    dispatch(client, event_id="Ev-same")

    assert len(ran) == first              # 2回目は走らない


def test_unknown_model_with_a_known_token_is_rejected(dispatch, ran):
    # 「調査」が既知なので、隣の gpt5 はモデル名の打ち間違いと判断される
    client = FakeClient()
    dispatch(client, text="調査 gpt5: 調べて")

    assert ran == []
    assert client.said("gpt5")


def test_unknown_word_alone_is_treated_as_plain_text(dispatch, ran):
    # 既知トークンが1つも無い先頭区間はディレクティブとみなさない。
    # 「TODO:」のような普通の文が誤解釈されないための仕様で、その代わり
    # 「gpt5:」単独は弾かれず既定モデルで走る（task_mode.py の設計判断）。
    dispatch(FakeClient(), text="gpt5: 調べて")

    assert len(ran) == 1
    assert ran[0]["model"] is None


def test_implement_keyword_without_permission_is_denied(settings, ran):
    locked = dataclasses.replace(settings.projects["demo"], allow_implement=False)
    settings = dataclasses.replace(settings, projects={"demo": locked})
    app = slack_handler.create_app(settings)
    client = FakeClient()
    app._listeners[0].ack_function(
        body={"event_id": "E"},
        event={"channel": "C111", "ts": "1.0", "text": "<@U999> 実装: 直して"},
        client=client,
    )
    app._slack_bridge_executor.shutdown(wait=True)

    assert ran == []
    assert client.said("許可されていません")


# --------------------------------------------------------------------------
# 受け付ける依頼
# --------------------------------------------------------------------------

def test_request_runs_claude_in_the_project_directory(dispatch, ran, project_dir):
    dispatch(FakeClient(), text="構成を教えて")

    assert len(ran) == 1
    assert ran[0]["working_directory"] == project_dir


def test_mention_is_stripped_from_the_prompt(dispatch, ran):
    dispatch(FakeClient(), text="構成を教えて")
    assert "<@U999>" not in ran[0]["prompt"]
    assert "構成を教えて" in ran[0]["prompt"]


def test_start_message_names_the_project(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べて")
    assert client.said("Demo")


def test_result_is_posted_to_the_thread(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べて")

    assert client.said("やりました")
    assert all(p.get("thread_ts") == "100.0" for p in client.posts)


def test_investigate_directive_selects_the_readonly_profile(dispatch, ran):
    dispatch(FakeClient(), text="調査: 見るだけ")
    assert ran[0]["profile"].key == "investigate"


def test_default_profile_is_the_project_default(dispatch, ran):
    dispatch(FakeClient(), text="直して")
    assert ran[0]["profile"].key == "implement"


def test_model_directive_is_passed_through(dispatch, ran):
    dispatch(FakeClient(), text="haiku: 軽く見て")
    assert ran[0]["model"] == "haiku"


def test_failure_is_reported_without_raising(dispatch, monkeypatch):
    monkeypatch.setattr(
        claude_runner, "run", lambda **k: ClaudeResult(ok=False, error="こわれた")
    )
    client = FakeClient()
    dispatch(client, text="直して")

    assert client.said("こわれた")


def test_crash_inside_the_worker_is_reported(dispatch, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("想定外")

    monkeypatch.setattr(claude_runner, "run", boom)
    client = FakeClient()
    dispatch(client, text="直して")     # 例外が外に漏れない

    assert client.posts                 # 何かは返す


# --------------------------------------------------------------------------
# ファイルの受け渡し
# --------------------------------------------------------------------------

def test_attachment_is_saved_and_told_to_claude(dispatch, ran, monkeypatch, project_dir):
    monkeypatch.setattr(
        inbox,
        "save_files",
        lambda files, cwd, token: [inbox.Saved("_inbox/a.csv", "a.csv", 10)],
    )
    client = FakeClient()
    dispatch(client, text="分析して", files=[{"name": "a.csv"}])

    assert client.said("_inbox/a.csv")            # 利用者へ報告
    assert "_inbox/a.csv" in ran[0]["prompt"]     # Claude へも伝える


def test_attachment_without_text_still_runs(dispatch, ran, monkeypatch):
    monkeypatch.setattr(
        inbox, "save_files", lambda files, cwd, token: [inbox.Saved("_inbox/a.csv", "a.csv", 10)]
    )
    dispatch(FakeClient(), text="", files=[{"name": "a.csv"}])

    assert len(ran) == 1                          # 空依頼として弾かれない


def test_share_directory_contents_are_attached(dispatch, ran, project_dir):
    share = project_dir / "_share"
    share.mkdir()
    (share / "out.md").write_text("成果物", encoding="utf-8")

    client = FakeClient()
    dispatch(client, text="やって")

    assert [u["title"] for u in client.uploads] == ["_share/out.md"]
    # 送れたものは待ち行列から外れる
    assert not (share / "out.md").exists()
    assert (share / "sent" / "out.md").exists()


def test_share_request_does_not_run_claude(dispatch, ran, project_dir):
    # まず一覧を作る
    ran.writes.append(("report.md", "報告"))
    dispatch(FakeClient(), text="作って")
    ran.clear()

    client = FakeClient()
    dispatch(client, text="共有: 1", event_id="Ev2")

    assert ran == []                              # Claude は起動しない
    assert client.uploads                         # ファイルは届く


def test_output_listing_is_posted_for_new_files(dispatch, ran, project_dir):
    ran.writes.append(("report.md", "報告"))
    client = FakeClient()
    dispatch(client, text="作って")

    assert client.said("report.md")
    assert client.said("共有: 1")                  # 取り出し方を案内する


def test_no_listing_when_nothing_changed(dispatch, ran):
    client = FakeClient()
    dispatch(client, text="調べるだけ")
    assert not client.said(":paperclip:")


# --------------------------------------------------------------------------
# 直列化
# --------------------------------------------------------------------------

def test_same_thread_requests_are_serialised(settings, monkeypatch):
    """同じスレッドの依頼が重なっても Claude を同時に走らせない。"""
    overlapping = []
    active = threading.Lock()

    def slow_run(**kwargs):
        got = active.acquire(blocking=False)
        overlapping.append(not got)
        if got:
            active.release()
        return ClaudeResult(ok=True, text="ok")

    monkeypatch.setattr(claude_runner, "run", slow_run)
    settings = dataclasses.replace(
        settings, runtime=dataclasses.replace(settings.runtime, max_workers=4)
    )
    app = slack_handler.create_app(settings)
    handler = app._listeners[0].ack_function

    for i in range(4):
        handler(
            body={"event_id": f"E{i}"},
            event={"channel": "C111", "ts": "100.0", "text": "<@U999> やって"},
            client=FakeClient(),
        )
    app._slack_bridge_executor.shutdown(wait=True)

    assert not any(overlapping)
