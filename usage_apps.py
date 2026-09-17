"""Macアプリの「最後に使った日」を出す。

mdls の kMDItemLastUsedDate はこの環境では null なので当てにしない。
本命は knowledgeC.db（macOSがアプリ使用時間を記録しているDB。読むにはこの
スクリプトを実行するプロセスにフルディスクアクセスが要る）。
読めないときは、アプリごとの設定ファイル・コンテナ・保存状態の更新時刻の
いちばん新しいものを「推定の最終使用日」として使う。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
import tempfile
from datetime import datetime

HOME = os.path.expanduser("~")
KNOWLEDGE_DB = os.path.join(HOME, "Library/Application Support/Knowledge/knowledgeC.db")
_APPLE_EPOCH = 978_307_200  # 2001-01-01


def _bundle_id(app_path: str) -> str | None:
    info = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.exists(info):
        return None
    try:
        with open(info, "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def list_apps(config: dict) -> dict[str, dict]:
    """{アプリ名（.app抜き）: {path, bundle_id}}"""
    apps: dict[str, dict] = {}
    for directory in config.get("apps", {}).get("app_dirs", []):
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".app"):
                continue
            path = os.path.join(directory, entry)
            name = entry[:-4]
            apps[name] = {"path": path, "bundle_id": _bundle_id(path)}
    return apps


def _knowledge_usage() -> tuple[dict[str, datetime], str | None]:
    """{bundle_id: 最終使用日時}。読めなければ ({}, 理由)。"""
    if not os.path.exists(KNOWLEDGE_DB):
        return {}, "knowledgeC.db が見つかりません"
    tmpdir = tempfile.mkdtemp(prefix="subwatch-knowledge-")
    try:
        copy = os.path.join(tmpdir, "knowledgeC.db")
        shutil.copy2(KNOWLEDGE_DB, copy)
        for suffix in ("-wal", "-shm"):
            side = KNOWLEDGE_DB + suffix
            if os.path.exists(side):
                try:
                    shutil.copy2(side, copy + suffix)
                except OSError:
                    pass
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT ZVALUESTRING, MAX(ZENDDATE) FROM ZOBJECT "
                "WHERE ZSTREAMNAME = '/app/usage' AND ZVALUESTRING IS NOT NULL "
                "GROUP BY ZVALUESTRING"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, PermissionError) as exc:
        return {}, f"knowledgeC.db を読めません（フルディスクアクセス未許可の可能性）: {exc}"
    except sqlite3.Error as exc:
        return {}, f"knowledgeC.db の読み取りに失敗: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    out: dict[str, datetime] = {}
    for bundle_id, end_date in rows:
        if not end_date:
            continue
        try:
            out[bundle_id] = datetime.fromtimestamp(end_date + _APPLE_EPOCH)
        except (ValueError, OverflowError, OSError):
            continue
    return out, None


def _mtime_guess(name: str, bundle_id: str | None) -> datetime | None:
    """設定・コンテナ・保存状態の更新時刻から最終使用日を推定する。"""
    candidates: list[str] = []
    if bundle_id:
        candidates += [
            os.path.join(HOME, "Library/Preferences", f"{bundle_id}.plist"),
            os.path.join(HOME, "Library/Containers", bundle_id),
            os.path.join(HOME, "Library/Application Support", bundle_id),
            os.path.join(HOME, "Library/Saved Application State", f"{bundle_id}.savedState"),
            os.path.join(HOME, "Library/HTTPStorages", bundle_id),
            os.path.join(HOME, "Library/Caches", bundle_id),
        ]
    candidates.append(os.path.join(HOME, "Library/Application Support", name))
    newest = None
    for path in candidates:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        when = datetime.fromtimestamp(max(stat.st_mtime, stat.st_atime))
        if newest is None or when > newest:
            newest = when
    return newest


def collect(config: dict) -> tuple[dict[str, dict], list[str]]:
    """{アプリ名: {last_used, source}} と注意書きを返す。"""
    problems: list[str] = []
    if not config.get("apps", {}).get("enabled", True):
        return {}, ["アプリ利用状況は config で無効"]
    apps = list_apps(config)
    knowledge, why = _knowledge_usage()
    if why:
        problems.append(why + " → 設定ファイルの更新時刻から推定します（精度は落ちます）")

    out: dict[str, dict] = {}
    for name, meta in apps.items():
        bundle_id = meta.get("bundle_id")
        when = knowledge.get(bundle_id) if bundle_id else None
        source = "knowledgeC"
        if when is None:
            when = _mtime_guess(name, bundle_id)
            source = "推定"
        if when is None:
            continue
        out[name] = {"last_used": when, "source": source, "bundle_id": bundle_id}
    return out, problems


def lookup(usage: dict[str, dict], app_names: list[str]) -> dict | None:
    last = None
    source = None
    for name in app_names:
        entry = usage.get(name)
        if not entry:
            continue
        if last is None or entry["last_used"] > last:
            last = entry["last_used"]
            source = entry["source"]
    if last is None:
        return None
    return {"last": last, "source": source}


if __name__ == "__main__":
    import credentials

    data, errs = collect(credentials.load_config())
    for e in errs:
        print("!", e)
    print(f"アプリ数: {len(data)}")
    for name, entry in sorted(data.items(), key=lambda kv: kv[1]["last_used"], reverse=True)[:25]:
        print(f"  {entry['last_used']:%Y-%m-%d}  {entry['source']:<10} {name}")
