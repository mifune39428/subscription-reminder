"""config.json をひとつ読むだけの、小さなローダー。

Gmailのアプリパスワードは、このフォルダの config.json の中にだけ置く。
config.json はセットアップウィザード（setup_wizard.command）が作る。手で直しても構わない。
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "config.json")


class CredentialError(RuntimeError):
    """必要な設定が見つからなかった。呼び出し側はその情報源だけを諦める。"""


def load_config(path: str | None = None) -> dict:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise CredentialError(
            f"config.json が見つかりません: {path}\n"
            "  → setup_wizard.command をダブルクリックして、先に初期設定をしてください。"
        )
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    # 「~」で書いた場所を、そのMacの実際のパスに直す
    chrome = config.get("chrome") or {}
    if chrome.get("user_data_dir"):
        chrome["user_data_dir"] = os.path.expanduser(chrome["user_data_dir"])
    apps = config.get("apps") or {}
    apps["app_dirs"] = [os.path.expanduser(d) for d in apps.get("app_dirs", [])]
    return config


def load_services(path: str | None = None) -> list[dict]:
    path = path or os.path.join(_HERE, "services.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("services", [])


def load_email_settings(_unused: str | None = None) -> dict:
    """送受信に使うGmailの設定。config.json の email から読む。"""
    settings = load_config().get("email") or {}
    for key in ("sender_email", "sender_password", "receiver_email"):
        if not settings.get(key):
            raise CredentialError(f"config.json の email.{key} が空です")
    return settings


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
