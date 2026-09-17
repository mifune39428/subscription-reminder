#!/usr/bin/env python3
"""サブスク監視の入口。

  python3 watch.py            … 更新が近いものがあればメールを送る（launchdはこれ）
  python3 watch.py --dry-run  … 送らずに report_preview.html を書き出す
  python3 watch.py --force    … 更新が近くなくても必ず送る
  python3 watch.py --list     … 端末に一覧を出すだけ
  python3 watch.py --init-overrides … いまの検出結果から overrides.json のひな形を作る

毎日走らせて構わない。更新が近いサブスクが無ければ黙って終わる。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import credentials
import reminder_mail
import subscriptions

_HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(_HERE, "watch.lock")
PREVIEW_PATH = os.path.join(_HERE, "report_preview.html")
LAST_SENT_PATH = os.path.join(_HERE, "last_sent.json")


class _Lock:
    """launchdと手動実行がぶつかっても二重に走らないようにする。"""

    def __enter__(self):
        try:
            self._fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = datetime.now().timestamp() - os.path.getmtime(LOCK_PATH)
            if age < 3600:
                print("すでに実行中のようです（watch.lock）。何もしません。")
                sys.exit(0)
            os.unlink(LOCK_PATH)
            self._fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return self

    def __exit__(self, *exc):
        try:
            os.close(self._fd)
            os.unlink(LOCK_PATH)
        except OSError:
            pass


def _print_list(data: dict) -> None:
    def show(title: str, entries: list[dict], renewal: bool = True) -> None:
        print(f"\n=== {title} {len(entries)}件 ===")
        for entry in entries:
            when = entry["next_renewal"].strftime("%Y-%m-%d") if entry.get("next_renewal") else "-"
            used = f"{entry['unused_days']}日前" if entry["unused_days"] is not None else "不明"
            column = when if renewal else f"{entry['last_charge']:%Y-%m-%d}"
            print(f"  {entry['status']:<7} {column}  {entry['currency']} {entry['amount']:>8,.0f} "
                  f"{entry['cycle']:<7} 最終利用:{used:<7} {entry['name']}")

    show("契約中のサブスク", data["subscriptions"])
    show("サブスクかも（要確認）", data.get("unsure", []))
    show("止まっていそう", data.get("ended", []), renewal=False)
    print(f"\n単発の買い物・不定期: {len(data.get('one_offs', []))}件（--dry-run のHTMLには出ません）")


def _init_overrides(data: dict) -> None:
    path = subscriptions.OVERRIDES_PATH
    existing = credentials.load_json(path, {}).get("subscriptions", {})
    out = dict(existing)
    for entry in data["subscriptions"] + data.get("unsure", []) + data.get("ended", []):
        out.setdefault(entry["key"], {
            "name": entry["name"],
            "ignore": False,
            "cancel_url": entry.get("cancel_url", ""),
            "web_domains": entry.get("web_domains", []),
            "apps": entry.get("apps", []),
        })
    credentials.save_json(path, {
        "_note": "検出結果を手で直すファイル。ignore:true で一覧から外す。"
                 "name/cancel_url/web_domains/apps/next_renewal(YYYY-MM-DD) を上書きできる。"
                 "鍵はドメイン単体でも効く。",
        "subscriptions": out,
    })
    print(f"{path} を書きました（{len(out)}件）")


def _set_renewal(key: str, when: str) -> int:
    """手動サブスクの更新日を入れる。JSONを手で開かなくて済むように。"""
    try:
        datetime.strptime(when, "%Y-%m-%d")
    except ValueError:
        print(f"日付は YYYY-MM-DD で指定してください: {when}")
        return 1
    path = subscriptions.OVERRIDES_PATH
    data = credentials.load_json(path, {})
    manual = data.setdefault("manual", {})
    if key not in manual:
        print(f"manual に {key} がありません。いまあるのは: {', '.join(manual) or '（なし）'}")
        return 1
    manual[key]["next_renewal"] = when
    credentials.save_json(path, data)
    print(f"{manual[key].get('name', key)} の次回更新日を {when} にしました。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="使っていないサブスクを見つけて更新前に知らせる")
    parser.add_argument("--dry-run", action="store_true", help="送信せずHTMLだけ書き出す")
    parser.add_argument("--force", action="store_true", help="更新が近くなくても送る")
    parser.add_argument("--list", action="store_true", help="端末に一覧を出すだけ")
    parser.add_argument("--init-overrides", action="store_true", help="overrides.json のひな形を作る")
    parser.add_argument("--set-renewal", nargs=2, metavar=("KEY", "YYYY-MM-DD"),
                        help="手動サブスクの次回更新日を入れる（例: --set-renewal amazon-prime 2026-11-04）")
    parser.add_argument("--quiet", action="store_true", help="途中経過を出さない")
    parser.add_argument("--collect-usage", action="store_true",
                        help="ブラウザを開いて利用量（KUのレンタル冊数など）を取り直す")
    args = parser.parse_args()

    config = credentials.load_config()

    if args.set_renewal:
        return _set_renewal(*args.set_renewal)

    # 送るとき（と明示されたとき）だけブラウザを開く。--list/--dry-run は前回の値を使う。
    collect_web = args.collect_usage or not (args.list or args.dry_run or args.init_overrides)
    with _Lock():
        data = subscriptions.gather(config, verbose=not args.quiet,
                                    collect_web=collect_web,
                                    force_web=args.collect_usage)

    # 材料が欠けたことは --quiet でも必ず残す（launchdのログに出したい）
    for note in data.get("notes", []):
        print("!", note)

    if args.list:
        _print_list(data)
        return 0
    if args.init_overrides:
        _init_overrides(data)
        return 0

    subject, body_html = reminder_mail.build_html(data, config)
    days_before = int(config["notify"].get("days_before_renewal", 5))
    upcoming = [s for s in data["subscriptions"]
                if s["days_to_renewal"] is not None
                and 0 <= s["days_to_renewal"] <= days_before
                and s["status"] != "除外"]

    if args.dry_run:
        with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
            f.write(body_html)
        print(f"件名: {subject}")
        print(f"プレビュー: {PREVIEW_PATH}")
        print(f"更新間近: {len(upcoming)}件 / 解約候補: "
              f"{len([s for s in data['subscriptions'] if s['status'] == '解約候補'])}件")
        return 0

    if not upcoming and not args.force:
        print(f"{days_before}日以内に更新されるサブスクはありません。メールは送りません。")
        return 0

    # 同じ更新について毎日送らない（更新日が変わったときだけ送る）
    fingerprint = sorted(f"{s['key']}@{s['next_renewal']:%Y-%m-%d}" for s in upcoming)
    last = credentials.load_json(LAST_SENT_PATH, {})
    if not args.force and last.get("fingerprint") == fingerprint:
        print("同じ内容をすでに知らせています。メールは送りません。")
        return 0

    settings = credentials.load_email_settings()
    reminder_mail.send(subject, body_html, settings)
    if config["notify"].get("macos_notification", True):
        reminder_mail.notify_macos(upcoming)
    credentials.save_json(LAST_SENT_PATH, {
        "fingerprint": fingerprint,
        "sent_at": datetime.now().isoformat(),
        "subject": subject,
    })
    print(f"送信しました: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
