"""設定（仕様書 §11, §15, §16）。

置き場は ``~/.claude-usage/config.json``。**JSON にしたのは依存を増やさないため**で、
仕様書の例は YAML だが PyYAML を入れるほどの内容ではない（項目が10個未満）。

ファイルが無くても既定値で動く。壊れていても既定値で動く。設定の不備で statusline が
落ちると利用者の画面が壊れるので、読めなければ黙って既定へ倒す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from claude_usage.snapshot import data_dir


def config_path():
    return data_dir() / "config.json"


@dataclass(frozen=True)
class Thresholds:
    """利用率の閾値（仕様書 §11）。

    これは「使いすぎ」側だけを見る指標。使い残しはペース比で判断するので
    ``underuse`` は閾値ではなく有効/無効だけを持つ。
    """

    warning: float = 70.0
    high: float = 85.0
    critical: float = 95.0
    #: 使い残しを知らせるか。この道具の目的の半分なので既定で有効。
    underuse: bool = True


@dataclass(frozen=True)
class Notifiers:
    """通知先（仕様書 §12）。

    ``file`` は既定で有効。いつ何が鳴ったかを後から確かめられないと、閾値が妥当か
    判断できない。外向きの通知（Slack）は明示的に有効化させる。
    """

    terminal: bool = True
    file: bool = True
    windows: bool = False
    slack: bool = False
    #: Slack の投稿先チャンネル ID。未設定なら Slack へは送らない。
    slack_channel: str = ""
    #: Slack の bot トークン。空なら環境変数 SLACK_BOT_TOKEN を見る。
    slack_token: str = ""


@dataclass(frozen=True)
class Config:
    thresholds: Thresholds = field(default_factory=Thresholds)
    notifiers: Notifiers = field(default_factory=Notifiers)
    #: 履歴の保持日数（仕様書 §16）。0 で無効。
    retention_days: int = 90
    #: 通知を出すか。うるさければ全部止められるようにしておく。
    alerts_enabled: bool = True


def _merge(base, body: dict):
    """既定値に、読めた項目だけを重ねる。

    知らないキーは無視し、型が合わないものは既定のままにする。設定ファイルの
    書き間違いで落とさないため。
    """
    if not isinstance(body, dict):
        return base
    updates = {}
    for name, current in vars(base).items():
        if name not in body:
            continue
        value = body[name]
        if isinstance(current, bool):
            if isinstance(value, bool):
                updates[name] = value
        elif isinstance(current, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                updates[name] = type(current)(value)
        elif isinstance(current, str):
            if isinstance(value, str):
                updates[name] = value
    return replace(base, **updates) if updates else base


def load() -> Config:
    try:
        body = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Config()
    if not isinstance(body, dict):
        return Config()

    raw_retention = body.get("retention_days")
    retention = (
        int(raw_retention)
        if isinstance(raw_retention, (int, float)) and not isinstance(raw_retention, bool)
        else Config().retention_days
    )
    raw_enabled = body.get("alerts_enabled")
    enabled = raw_enabled if isinstance(raw_enabled, bool) else True

    return Config(
        thresholds=_merge(Thresholds(), body.get("thresholds")),
        notifiers=_merge(Notifiers(), body.get("notifiers")),
        retention_days=retention,
        alerts_enabled=enabled,
    )


#: ``claude-usage config`` が書き出す雛形。既定値をそのまま並べる。
TEMPLATE = {
    "alerts_enabled": True,
    "retention_days": 90,
    "thresholds": {"warning": 70, "high": 85, "critical": 95, "underuse": True},
    "notifiers": {
        "terminal": True,
        "file": True,
        "windows": False,
        "slack": False,
        "slack_channel": "",
        "slack_token": "",
    },
}


def write_template() -> bool:
    """設定ファイルが無ければ雛形を書く。既存は上書きしない。"""
    path = config_path()
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except OSError:
        return False
