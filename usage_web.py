"""ログイン済みのChromeから「今月どのくらい使ったか」を取ってくる。

請求メールには金額しか載っていないので、続けるか決める材料にならない。
「Kindle Unlimitedで今月何冊借りたか」のような数字は、そのサービスの
ログイン済みページにしか無いので、ブラウザから読む。
（ブログレポート配信のAdSense取得と同じやり方。launchdからでも動く実績がある）

毎日ブラウザを開くのは邪魔なので、結果は usage_web.json に24時間ためる。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta

import chrome_js
import credentials

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_HERE, "usage_web.json")
CACHE_HOURS = 24

# Kindle Unlimited のレンタル中一覧。各行に「レンタル日: 2026年8月12日」が入っている。
_KU_URL = "https://www.amazon.co.jp/hz/mycd/digital-console/contentlist/kuAll/dateDsc/"
_KU_COUNT_JS = """
(function () {
  return String(document.querySelectorAll('[id^="content-acquired-date-"]').length);
})()
"""
_KU_DATA_JS = """
(function () {
  var out = [];
  var nodes = document.querySelectorAll('[id^="content-acquired-date-"]');
  for (var i = 0; i < nodes.length; i++) {
    var asin = nodes[i].id.replace('content-acquired-date-', '');
    var title = document.getElementById('content-title-' + asin);
    out.push({
      asin: asin,
      date: (nodes[i].innerText || '').trim(),
      title: title ? (title.innerText || '').trim() : ''
    });
  }
  return JSON.stringify(out);
})()
"""

_JP_DATE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")


def _wait_for(tab_id: str, js: str, seconds: int = 40) -> str:
    """ページの読み込みを待つ。Chromeの execute javascript は Promise を待てないのでポーリングする。"""
    deadline = time.time() + seconds
    last = "0"
    while time.time() < deadline:
        last = chrome_js.exec_js(tab_id, js).strip()
        if last and last not in ("0", "null", "undefined"):
            return last
        time.sleep(2)
    return last


def collect_kindle_unlimited(days: int = 30) -> dict:
    """今月レンタルした冊数を数える。

    見えるのは**いま借りている本だけ**（返却すると一覧から消える）。
    KUは同時20冊までなので実態に近い数字になるが、返した本は数に入らない。
    """
    tab_id = chrome_js.new_tab(_KU_URL)
    try:
        found = _wait_for(tab_id, _KU_COUNT_JS)
        if found in ("0", ""):
            return {"ok": False, "reason": "一覧を読めませんでした（Amazonにログインしていない可能性）"}
        raw = chrome_js.read_long(tab_id, _KU_DATA_JS)
    finally:
        chrome_js.close_tab(tab_id)

    try:
        rows = json.loads(raw)
    except ValueError:
        return {"ok": False, "reason": "一覧の読み取りに失敗しました"}

    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for row in rows:
        match = _JP_DATE.search(row.get("date") or "")
        if not match:
            continue
        when = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if when >= cutoff:
            recent.append({"title": row.get("title", ""), "date": when.strftime("%Y-%m-%d")})
    recent.sort(key=lambda r: r["date"], reverse=True)
    return {
        "ok": True,
        "label": f"直近{days}日で{len(recent)}冊レンタル",
        "count": len(recent),
        "held": len(rows),
        "note": f"いま借りているのは{len(rows)}冊（返却済みは数に入らない）",
        "examples": [r["title"] for r in recent[:3] if r["title"]],
    }


COLLECTORS = {
    "kindle-unlimited": collect_kindle_unlimited,
}


def collect(keys: list[str], force: bool = False) -> tuple[dict[str, dict], list[str]]:
    """必要なサービスぶんだけ集める。24時間以内に取ったものは使い回す。"""
    cache = credentials.load_json(CACHE_PATH, {})
    notes: list[str] = []
    now = datetime.now()
    changed = False

    for key in keys:
        collector = COLLECTORS.get(key)
        if not collector:
            continue
        entry = cache.get(key)
        if entry and not force:
            try:
                age = now - datetime.fromisoformat(entry["collected_at"])
                if age < timedelta(hours=CACHE_HOURS):
                    continue
            except (KeyError, ValueError):
                pass
        try:
            result = collector()
        except chrome_js.ChromeError as exc:
            notes.append(f"{key} の利用状況を取れませんでした: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - ここで落ちても他は出したい
            notes.append(f"{key} の利用状況の取得で例外: {exc}")
            continue
        if not result.get("ok"):
            notes.append(f"{key}: {result.get('reason', '取得できませんでした')}")
            continue
        result["collected_at"] = now.isoformat()
        cache[key] = result
        changed = True

    if changed:
        credentials.save_json(CACHE_PATH, cache)
    return {k: v for k, v in cache.items() if v.get("ok")}, notes


if __name__ == "__main__":
    import sys

    data, errs = collect(list(COLLECTORS), force="--force" in sys.argv)
    for e in errs:
        print("!", e)
    print(json.dumps(data, ensure_ascii=False, indent=2))
