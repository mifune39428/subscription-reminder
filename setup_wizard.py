"""サブスク更新前リマインダーを、自分のMacで動かすための初期設定。

聞いた内容は、このフォルダの config.json と、自分のMacの launchd 設定にだけ書く。
外には何も送らない。あとから設定を変えたいときは、もう一度これを実行すればよい。
"""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
TEMPLATE_PATH = os.path.join(HERE, "config_template.json")
LABEL = "com.subscription.reminder"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def rule(title: str = "") -> None:
    print("\n" + "=" * 54)
    if title:
        print(title)
        print("=" * 54)


def ask(prompt: str, default: str = "") -> str:
    suffix = f"（未入力なら {default}）" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_secret(prompt: str) -> str:
    """パスワードは画面に出さない。"""
    return getpass.getpass(f"{prompt}: ").strip()


def ask_yes(prompt: str, default: bool = False) -> bool:
    mark = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{mark}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "はい")


def load_base() -> dict:
    """すでに config.json があればそれを土台にする（設定の変更に使えるように）。"""
    for path in (CONFIG_PATH, TEMPLATE_PATH):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    raise SystemExit("config_template.json が見つかりません。フォルダごと置いてください。")


def setup_email(config: dict) -> None:
    rule("① Gmail（必須）")
    print("請求メールを探すのにも、お知らせを送るのにも、同じGmailを使います。")
    print("ふだんのログインパスワードではなく、「アプリパスワード」（16桁）を使ってください。")
    print("  → https://myaccount.google.com/apppasswords")
    print("メールは読むだけで、既読にしたり消したりはしません。")
    email = config.get("email") or {}
    email["sender_email"] = ask("請求メールが届くGmailアドレス", email.get("sender_email", ""))
    email["sender_password"] = ask_secret("アプリパスワード（画面には出ません）") or email.get(
        "sender_password", ""
    )
    email["receiver_email"] = ask(
        "お知らせを受け取るメールアドレス", email.get("receiver_email") or email["sender_email"]
    )
    config["email"] = email


def setup_notify(config: dict) -> None:
    rule("② いつ知らせるか")
    notify = config.get("notify") or {}
    days = ask("更新日の何日前に知らせますか", str(notify.get("days_before_renewal", 5)))
    try:
        notify["days_before_renewal"] = int(days)
    except ValueError:
        notify["days_before_renewal"] = 5
    notify["macos_notification"] = ask_yes(
        "メールと一緒に、Macの画面にも通知を出しますか", bool(notify.get("macos_notification", True))
    )
    config["notify"] = notify


def setup_usage(config: dict) -> None:
    rule("③ 「使っていないか」の判定（任意）")
    print("Chromeの閲覧履歴と、Macアプリの最終起動日から「しばらく使っていないサブスク」を探します。")
    print("読むのはこのMacの中だけで、外には送りません。")
    chrome = config.get("chrome") or {}
    chrome["enabled"] = ask_yes("Chromeの閲覧履歴を使いますか", bool(chrome.get("enabled", True)))
    config["chrome"] = chrome
    apps = config.get("apps") or {}
    apps["enabled"] = ask_yes("Macアプリの起動日を使いますか", bool(apps.get("enabled", True)))
    config["apps"] = apps

    print("\nKindle Unlimited の「直近30日で何冊借りたか」もメールに載せられます。")
    print("お知らせを送るときだけ、ログイン済みのChromeでAmazonのページを一瞬開いて数えます。")
    print("Chromeの「表示 → 開発 / 管理 → Apple Events からの JavaScript を許可」が必要です。")
    web = config.get("web_usage") or {}
    web["enabled"] = ask_yes("KUの冊数を載せますか", bool(web.get("enabled", False)))
    config["web_usage"] = web


def install_launchd(hour: int, minute: int) -> None:
    """毎日この時刻に動くよう、自分のMacのlaunchdに登録する。"""
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, os.path.join(HERE, "watch.py"), "--quiet"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": os.path.join(HERE, "launchd_stdout.log"),
        "StandardErrorPath": os.path.join(HERE, "launchd_stderr.log"),
        "WorkingDirectory": HERE,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"
        },
    }
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    result = subprocess.run(["launchctl", "load", PLIST_PATH], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"\n✅ 毎日 {hour:02d}:{minute:02d} に自動で確認するようにしました。")
        print("   更新が近いものが無い日は、何も届きません。")
        print(f"   （やめたいとき: launchctl unload {PLIST_PATH}）")
    else:
        print(f"\n⚠️ 自動起動の登録に失敗しました: {result.stderr.strip()}")
        print(f"   手動で: launchctl load {PLIST_PATH}")


def first_scan(max_rounds: int = 15) -> None:
    """請求メールを読み切るまで繰り返す。1回で読める数に上限があるため。"""
    import re

    watch = os.path.join(HERE, "watch.py")
    for round_no in range(1, max_rounds + 1):
        print(f"\n--- {round_no}回目 ---")
        result = subprocess.run([sys.executable, watch, "--list"], capture_output=True, text=True)
        output = result.stdout + result.stderr
        match = re.search(r"未取得 (\d+)件 → 今回 (\d+)件", output)
        if match:
            print(f"  残り {match.group(1)}通のうち {match.group(2)}通を読みました")
        if result.returncode != 0 or not match or match.group(1) == match.group(2):
            print(output[output.find("==="):] if "===" in output else output)
            return
    print("まだ読み切れていません。subscription_list.command を何度か実行してください。")


def main() -> int:
    rule("💳 サブスク更新前リマインダー — 初期設定")
    print("Gmailの請求メールから契約中のサブスクを洗い出して、")
    print("更新日の数日前だけ、自分宛てにメールで知らせます。")
    print("入力した内容は、このフォルダの config.json に保存されるだけです。")

    config = load_base()
    setup_email(config)
    setup_notify(config)
    setup_usage(config)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_PATH, 0o600)  # パスワードが入るので自分だけが読める権限にする
    print(f"\n💾 設定を保存しました: {CONFIG_PATH}")

    rule("④ 確認する時刻")
    when = ask("毎日何時何分に確認しますか（HH:MM）", "08:30")
    try:
        hour, minute = (int(x) for x in when.split(":"))
    except ValueError:
        print("読めなかったので 08:30 にします。")
        hour, minute = 8, 30
    install_launchd(hour, minute)

    rule("⑤ 最初の洗い出し")
    print("直近400日ぶんの請求メールを探します。")
    print("メールが多いと、読み終わるまで10分ほどかかります（1回700通ずつ読みます）。")
    if ask_yes("いま洗い出しますか（メールは送りません）", True):
        first_scan()
        subprocess.run([sys.executable, os.path.join(HERE, "watch.py"), "--dry-run", "--quiet"])
        preview = os.path.join(HERE, "report_preview.html")
        if os.path.exists(preview) and ask_yes("届くメールの見本をブラウザで開きますか", True):
            subprocess.run(["open", preview])

    rule("完了")
    print("更新が近いサブスクがあると、メールが届きます。")
    print("間違って拾ったものや、請求メールが来ない契約は overrides.json で直せます（README参照）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n中断しました。")
        sys.exit(1)
