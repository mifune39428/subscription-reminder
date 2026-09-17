"""Chromeの閲覧履歴から「そのサービスを最後にいつ見たか」を出す。

履歴DBはChrome起動中もロックされているので、必ずコピーしてから読む。
プロファイルが複数あるので全部見て、いちばん新しい訪問を採用する。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta

# Chromeの時刻は 1601-01-01 からのマイクロ秒
_EPOCH_DELTA = 11_644_473_600


def _chrome_time_to_dt(value: int):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000 - _EPOCH_DELTA)
    except (ValueError, OverflowError, OSError):
        return None


def _read_profile(history_path: str, since: datetime) -> dict[str, dict]:
    tmpdir = tempfile.mkdtemp(prefix="subwatch-chrome-")
    out: dict[str, dict] = {}
    try:
        copy = os.path.join(tmpdir, "History")
        shutil.copy2(history_path, copy)
        for suffix in ("-wal", "-shm"):
            side = history_path + suffix
            if os.path.exists(side):
                try:
                    shutil.copy2(side, copy + suffix)
                except OSError:
                    pass
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            cutoff = int((since.timestamp() + _EPOCH_DELTA) * 1_000_000)
            # urls.visit_count は「累計」なので期間で数えられない。
            # 1訪問=1行の visits テーブルを結合して、期間内の実回数を数える。
            rows = conn.execute(
                "SELECT u.url, v.visit_time FROM visits v JOIN urls u ON u.id = v.url "
                "WHERE v.visit_time > ? ORDER BY v.visit_time",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        month_ago = datetime.now() - timedelta(days=30)
        for url, visit_time in rows:
            host = ""
            if "://" in url:
                host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0].lower()
            if not host:
                continue
            when = _chrome_time_to_dt(visit_time)
            if not when:
                continue
            entry = out.setdefault(host, {"last_visit": None, "visits": 0, "visits_30d": 0})
            entry["visits"] += 1
            if when >= month_ago:
                entry["visits_30d"] += 1
            if entry["last_visit"] is None or when > entry["last_visit"]:
                entry["last_visit"] = when
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"{history_path}: {exc}") from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def collect(config: dict) -> tuple[dict[str, dict], list[str]]:
    """{ホスト名: {last_visit, visits}} と、読めなかった理由の一覧を返す。"""
    conf = config.get("chrome", {})
    problems: list[str] = []
    if not conf.get("enabled", True):
        return {}, ["Chrome履歴は config で無効"]
    base = conf.get("user_data_dir", "")
    since = datetime.now() - timedelta(days=int(conf.get("lookback_days", 120)))
    merged: dict[str, dict] = {}
    found_any = False
    profiles = conf.get("profiles") or []
    if not profiles and os.path.isdir(base):
        # 指定が無ければ、履歴を持っているプロファイルを全部見る
        profiles = sorted(p for p in os.listdir(base)
                          if os.path.exists(os.path.join(base, p, "History")))
    for profile in profiles:
        path = os.path.join(base, profile, "History")
        if not os.path.exists(path):
            continue
        found_any = True
        try:
            data = _read_profile(path, since)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        for host, entry in data.items():
            current = merged.setdefault(host, {"last_visit": None, "visits": 0, "visits_30d": 0})
            current["visits"] += entry["visits"]
            current["visits_30d"] += entry["visits_30d"]
            if current["last_visit"] is None or entry["last_visit"] > current["last_visit"]:
                current["last_visit"] = entry["last_visit"]
    if not found_any:
        problems.append(f"Chromeの履歴が見つかりません: {base}")
    return merged, problems


def lookup(history: dict[str, dict], domains: list[str]) -> dict | None:
    """サービスのドメイン群に対して、いちばん新しい訪問と合計回数を返す。"""
    last = None
    visits = 0
    visits_30d = 0
    for host, entry in history.items():
        for domain in domains:
            domain = domain.lower()
            if host == domain or host.endswith("." + domain):
                visits += entry["visits"]
                visits_30d += entry.get("visits_30d", 0)
                if last is None or entry["last_visit"] > last:
                    last = entry["last_visit"]
                break
    if last is None:
        return None
    return {"last": last, "visits": visits, "visits_30d": visits_30d}


if __name__ == "__main__":
    import credentials

    hist, errs = collect(credentials.load_config())
    for e in errs:
        print("!", e)
    print(f"ホスト数: {len(hist)}")
    top = sorted(hist.items(), key=lambda kv: kv[1]["visits"], reverse=True)[:20]
    for host, entry in top:
        print(f"  {entry['last_visit']:%Y-%m-%d}  {entry['visits']:>6}回  "
              f"(30日: {entry['visits_30d']:>5}回)  {host}")
