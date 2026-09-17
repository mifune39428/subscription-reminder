"""課金メール＋利用状況 → サブスク一覧をつくる。

まとめ方: **送信元ドメイン＋件名（注文番号などを除く）＋金額（1割以内は同額）** で束ねる。
同じ送信元でも中身は別の請求のことがあり（Appleは全部 apple.com から来る）、
ドル建ては円の請求額が毎月動く（Claude Pro）ので、この3つを組み合わせている。

サブスクと言える条件（どれかが欠けたら単発の買い物として参考欄へ）:
  1. 注文確認・発送連絡の件名ではない
  2. 直近の課金が週・月・年などの周期どおりに並んでいる（メール1回の欠けは許す）
  3. 周期どおりの間隔を2回以上見た、または課金2回でも「サブスクだ」という根拠がある

判定の実例は test_subscriptions.py にある。判定を触ったら必ず走らせること。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import credentials
import usage_apps
import usage_chrome

_HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES_PATH = os.path.join(_HERE, "overrides.json")

_CYCLES = [
    (6, 8, "週額", 7),
    (13, 16, "隔週", 14),
    (26, 35, "月額", 30),
    (55, 65, "2か月", 60),
    (85, 95, "四半期", 91),
    (170, 195, "半年", 182),
    (350, 380, "年額", 365),
]


# 「サブスクらしい」件名。1回しか課金が見えない年額サブスクを拾うのに使う。
# 1回しか課金が見えなくても、この語が件名にあれば「年額の更新」とみなして候補に出す。
# ドメイン代やサーバーの年間更新は、400日ぶん見ても1回しか出ないことがある。
# 「更新」だけだと、値上げの告知や「プレミアムリスナーを更新しました」まで拾ってしまう。
# 請求そのものを指す語に絞る。
_RENEWAL_WORDS = ["請求書", "契約更新", "ご請求", "invoice", "renewal notice"]

_SUBSCRIPTION_WORDS = [
    "更新", "自動更新", "サブスク", "プラン", "会員", "月額", "年額", "年間",
    "premium", "プレミアム", "subscription", "renew", "membership", "plan",
]

# 注文確認・発送連絡の件名。これは「買い物」の領収書で、サブスクの請求ではない。
# Amazonで同じ値段帯の本を月に1冊ずつ買うと、間隔だけ見れば月額サブスクと区別がつかない
# （¥690・¥700・¥727の本が「月額¥727」と出た）。
_ORDER_WORDS = [
    "ご注文", "注文済み", "注文を承り", "発送", "配達", "お届け", "出荷",
    "your order", "order confirmation", "has shipped", "shipped",
]

# 可変部分（注文番号・巻数・レーベル名）だけ落とす。商品名は残す — 消すと別々の本が
# 同じ見出しになり、1割以内の金額差で束ねたときに1つの「契約」に化ける。
_NOISE_RE = re.compile(r"[0-9０-９]+|[#＃][A-Za-z0-9\-]+|【[^】]*】|\([^)]*\)|（[^）]*）")


def _amount_key(amount: dict) -> str:
    return f"{amount['currency']}:{round(amount['value'])}"


def _subject_signature(subject: str) -> str:
    """件名から注文番号などを落として、同じ請求を束ねるための見出しを作る。"""
    text = _NOISE_RE.sub("", subject)
    return re.sub(r"\s+", " ", text).strip().lower()[:60]


def _detect_cycle(dates: list[datetime]) -> dict:
    """直近から遡って、既知の周期どおりに課金されている並びを探す。

    - 値上げや一時停止をはさむと全期間では間隔がバラつくので、直近の並びだけを見る
      （Voicyは550→330→550と動いた）。
    - 請求メールは1回飛ぶことがある（Xserverの2026-07-21分が届いていない）。月単位の
      周期で間隔がちょうど2周期ぶんなら「メールの欠け」とみなして並びを切らない。
      切ると6/21→8/21の61日だけが残り「2か月ごと」と誤判定する。
    - 各間隔が周期の許容幅に1つずつ収まっていることを確かめる。中央値だけ見ると、
      単発の買い物がたまたま並んだものを周期と取り違える。
    """
    best = None
    for low, high, label, days in _CYCLES:
        index = len(dates) - 1
        on_cycle = missed = 0
        while index >= 1:
            gap = (dates[index] - dates[index - 1]).days
            if low <= gap <= high:
                on_cycle += 1
            elif days in (30, 60, 91) and 2 * low <= gap <= 2 * high:
                missed += 1
            else:
                break
            index -= 1
        if on_cycle == 0 or missed > on_cycle:
            continue
        score = (on_cycle, -days)  # 周期どおりの間隔が多いほうを採る。同数なら短い周期
        if best is None or score > best["score"]:
            best = {"score": score, "label": label, "days": days,
                    "run": dates[index:], "on_cycle": on_cycle, "missed": missed}
    if best is None:
        return {"label": None, "days": None, "run": dates[-1:], "on_cycle": 0, "missed": 0}
    return best


def _advance(when: datetime, cycle_days: int) -> datetime:
    """次の課金日を出す。月単位のものは「毎月21日」を保つ（30日足すとずれていく）。"""
    months = {30: 1, 60: 2, 91: 3, 182: 6, 365: 12}.get(cycle_days)
    if not months:
        return when + timedelta(days=cycle_days)
    month = when.month - 1 + months
    year = when.year + month // 12
    month = month % 12 + 1
    day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return when.replace(year=year, month=month, day=day)


def _service_for(domain: str, services: list[dict]) -> dict | None:
    domain = domain.lower()
    for service in services:
        for mail_domain in service.get("mail_domains", []):
            mail_domain = mail_domain.lower()
            if domain == mail_domain or domain.endswith("." + mail_domain):
                return service
    return None


# 周期 → 1年あたり何回課金されるか。「30日ごと」は暦の1か月なので12回であって、
# 365/30 = 12.17回ではない。日数で割ると月額が1.5%ふくらむ。
_PER_YEAR = {7: 52.0, 14: 26.0, 30: 12.0, 60: 6.0, 91: 4.0, 182: 2.0, 365: 1.0}


def _monthly(value: float, cycle_days: int | None) -> float | None:
    """1か月あたりに均した額。月額はそのまま、年額は12で割る。"""
    if not cycle_days or not value:
        return None
    per_year = _PER_YEAR.get(cycle_days, 365.0 / cycle_days)
    return value * per_year / 12.0


def build(config: dict, records: list[dict], services: list[dict],
          chrome_history: dict, app_usage: dict, mail_engagement: dict,
          web_usage: dict | None = None) -> dict:
    """サブスク一覧と、参考情報（単発課金など）を返す。"""
    now = datetime.now()
    min_charges = int(config["judgement"].get("min_charges_for_cycle", 2))
    unused_days = int(config["judgement"].get("unused_days", 60))
    overrides_file = credentials.load_json(OVERRIDES_PATH, {})
    overrides = overrides_file.get("subscriptions", {})
    web_usage = web_usage or {}
    manual_usage_keys = {
        key: (manual.get("web_usage") or key)
        for key, manual in (overrides_file.get("manual") or {}).items()
    }

    # 「Your Pro subscription is confirmed」のような加入通知が届いている送信元。
    # 領収書の件名だけでは定期課金か分からないとき（Anthropicの「Your receipt from
    # Anthropic, PBC」）の手がかりにする。ただし使えるのは、その送信元が1種類の請求しか
    # 送ってこない場合だけ（下の single_product_domains）。Amazonには
    # 「Kindle Unlimited：自動更新キャンセル」が届くが、本の注文までサブスクにしてはいけない。
    subscription_domains = {
        r["domain"] for r in records
        if any(w in (r.get("subject") or "").lower() for w in _SUBSCRIPTION_WORDS)
    }

    buckets: dict[str, list[dict]] = {}
    for record in records:
        if not record.get("billing") or not record.get("amount"):
            continue
        key = f"{record['domain']}|{_subject_signature(record['subject'])}"
        buckets.setdefault(key, []).append(record)

    # ドル建てのサブスクは為替で毎月の円額が動く（Claude Proは¥3,700→¥3,637）。
    # 金額をそのまま鍵にすると同じ契約が別物に割れるので、1割以内の差は同じ値段とみなす。
    clustered: dict[str, list[dict]] = {}
    for key, group in buckets.items():
        group.sort(key=lambda r: r["amount"]["value"])
        clusters: list[list[dict]] = []
        for record in group:
            value = record["amount"]["value"]
            currency = record["amount"]["currency"]
            for cluster in clusters:
                base = cluster[0]["amount"]
                if base["currency"] == currency and value <= base["value"] * 1.1:
                    cluster.append(record)
                    break
            else:
                clusters.append([record])
        for cluster in clusters:
            amount = cluster[0]["amount"]
            clustered[f"{key}|{_amount_key(amount)}"] = cluster
    buckets = clustered

    bucket_count: dict[str, int] = {}
    for charges in buckets.values():
        bucket_count[charges[0]["domain"]] = bucket_count.get(charges[0]["domain"], 0) + 1
    single_product_domains = {d for d, n in bucket_count.items() if n == 1}

    subscriptions: list[dict] = []
    one_offs: list[dict] = []

    for key, charges in buckets.items():
        charges.sort(key=lambda r: r["date"])
        domain = charges[0]["domain"]
        amount = charges[-1]["amount"]  # 値段は動くので直近のものを出す
        dates = [datetime.fromisoformat(c["date"]).replace(tzinfo=None) for c in charges]
        # 同じ日に届いた重複メールは1回に畳む
        unique_dates: list[datetime] = []
        for when in dates:
            if not unique_dates or (when - unique_dates[-1]).days >= 3:
                unique_dates.append(when)

        service = _service_for(domain, services)
        name = service["name"] if service else domain
        hints = [c.get("recurring_hint") for c in charges]
        entry = {
            "key": key,
            "recurring_hint": True if any(h is True for h in hints) else (
                False if any(h is False for h in hints) else None
            ),
            "domain": domain,
            "name": name,
            "amount": amount["value"],
            "currency": amount["currency"],
            "charge_count": len(unique_dates),
            "last_charge": unique_dates[-1],
            "first_charge": unique_dates[0],
            "subjects": list(dict.fromkeys(c["subject"] for c in charges))[:3],
            "cancel_url": (service or {}).get("cancel_url", ""),
            "web_domains": (service or {}).get("web_domains", []),
            "apps": (service or {}).get("apps", []),
        }

        subject_blob = " ".join(entry["subjects"]).lower()
        subject_says_sub = any(w in subject_blob for w in _SUBSCRIPTION_WORDS)
        is_order = any(w in subject_blob for w in _ORDER_WORDS) and not subject_says_sub

        cycle = _detect_cycle(unique_dates)
        run = cycle["run"]
        label, cycle_days = cycle["label"], cycle["days"]
        if is_order or len(run) < min_charges:
            label, cycle_days = None, None  # 注文確認は買い物。何回並んでもサブスクにしない

        if (not cycle_days and not is_order and len(unique_dates) == 1
                and any(w in subject_blob for w in _RENEWAL_WORDS)):
            label, cycle_days = "年額（推定）", 365
            run = unique_dates

        # 「サブスクだ」と言える根拠がこの請求そのものにあるか。
        # 件名にサブスクの語がある / 本文に「月額」「自動更新」がある / 1種類の請求しか
        # 送ってこない送信元から加入通知が届いている、のどれか。
        has_evidence = (
            subject_says_sub
            or entry["recurring_hint"] is True
            or (domain in subscription_domains and domain in single_product_domains)
        )

        confidence = "低"
        if label == "年額（推定）":
            confidence = "低"
        elif cycle_days and cycle["on_cycle"] >= 2:
            # 周期どおりの間隔を2回以上確認できた。ただし根拠が無く、本文に定期課金の語も
            # 無いなら、同じ値段の単発購入が並んだだけかもしれない（Appleの曲がこれ）
            confidence = "高" if (has_evidence or entry["recurring_hint"] is not False) else "低"
        elif cycle_days:
            # 間隔を1回しか見ていない（課金2回）。月イチ程度なら偶然そろうので根拠が要る
            if has_evidence:
                confidence = "中"
            elif cycle_days <= 35:
                label, cycle_days = None, None
        entry["confidence"] = confidence
        entry["missed_charges"] = cycle["missed"] if cycle_days and label != "年額（推定）" else 0
        entry["charge_count"] = len(run)
        entry["last_charge"] = run[-1]
        entry["first_charge"] = run[0]

        if not cycle_days:
            entry["cycle"] = "単発" if len(unique_dates) < min_charges else "不定期"
            entry["cycle_days"] = None
            entry["next_renewal"] = None
            entry["monthly"] = None
            one_offs.append(entry)
            continue

        entry["cycle"] = label
        entry["cycle_days"] = cycle_days
        # 請求メールが1回届かないと更新日が過去のままになり、二度と通知できない。
        # 過ぎていたら周期ぶん進めて、推定であることを覚えておく。
        renewal = _advance(run[-1], cycle_days)
        entry["renewal_estimated"] = renewal.date() < now.date()
        while renewal.date() < now.date():
            renewal = _advance(renewal, cycle_days)
        entry["next_renewal"] = renewal
        entry["monthly"] = _monthly(amount["value"], cycle_days)
        # 最後の課金から2周期以上あいていたら、もう止まっているとみなす
        entry["stale"] = (now - run[-1]).days > cycle_days * 2 + 10
        subscriptions.append(entry)

    # 請求メールが来ない契約（カード明細にしか出ない、App内課金でまとめられている等）は
    # overrides.json の manual に書いてもらう。利用状況の判定や通知は自動検出と同じ扱い。
    for key, manual in (credentials.load_json(OVERRIDES_PATH, {}).get("manual") or {}).items():
        cycle_days = int(manual.get("cycle_days") or 30)
        label = {7: "週額", 30: "月額", 91: "四半期", 182: "半年",
                 365: "年額"}.get(cycle_days, f"{cycle_days}日ごと")
        renewal = None
        if manual.get("next_renewal"):
            try:
                renewal = datetime.fromisoformat(manual["next_renewal"])
            except ValueError:
                renewal = None
        estimated = False
        while renewal and renewal.date() < now.date():
            renewal = _advance(renewal, cycle_days)
            estimated = True
        subscriptions.append({
            "key": key,
            "domain": manual.get("domain", key),
            "name": manual.get("name", key),
            "amount": float(manual.get("amount") or 0),
            "currency": manual.get("currency", "JPY"),
            "charge_count": 0,
            "last_charge": renewal - timedelta(days=cycle_days) if renewal else now,
            "first_charge": now,
            "subjects": ["手動登録（請求メールなし）"],
            "cancel_url": manual.get("cancel_url", ""),
            "web_domains": manual.get("web_domains", []),
            "apps": manual.get("apps", []),
            "cycle": label,
            "cycle_days": cycle_days,
            "next_renewal": renewal,
            "renewal_estimated": estimated,
            "monthly": _monthly(float(manual.get("amount") or 0), cycle_days),
            "usage_note": manual.get("usage_note", ""),
            "recurring_hint": True,
            "confidence": "手動",
            "manual": True,
            "stale": False,
        })

    # 同じサービスの一覧をまとめて上書き設定を当て、利用状況をくっつける
    for entry in subscriptions + one_offs:
        override = overrides.get(entry["key"]) or overrides.get(entry["domain"]) or {}
        entry["ignored"] = bool(override.get("ignore"))
        if override.get("name"):
            entry["name"] = override["name"]
        if override.get("cancel_url"):
            entry["cancel_url"] = override["cancel_url"]
        if override.get("web_domains"):
            entry["web_domains"] = override["web_domains"]
        if override.get("apps"):
            entry["apps"] = override["apps"]
        if override.get("next_renewal"):
            try:
                entry["next_renewal"] = datetime.fromisoformat(override["next_renewal"])
            except ValueError:
                pass

        entry["web_usage_key"] = (
            override.get("web_usage")
            or (manual_usage_keys.get(entry["key"]))
            or (entry["key"] if entry["key"] in web_usage else None)
        )
        entry["web_usage"] = web_usage.get(entry["web_usage_key"] or "")

        signals: list[tuple[str, datetime, str]] = []
        web = usage_chrome.lookup(chrome_history, entry["web_domains"]) if entry["web_domains"] else None
        entry["visits_30d"] = web.get("visits_30d") if web else None
        if web:
            signals.append(("Chrome", web["last"], f"30日{web['visits_30d']}回"))
        app = usage_apps.lookup(app_usage, entry["apps"]) if entry["apps"] else None
        if app:
            signals.append(("アプリ", app["last"], app["source"]))
        mail = mail_engagement.get(entry["domain"])
        if mail and mail.get("last_read"):
            last_read = mail["last_read"].replace(tzinfo=None)
            signals.append(("メール既読", last_read, f"{mail['read']}/{mail['total']}通"))

        entry["signals"] = [
            {"kind": kind, "last": when, "detail": detail} for kind, when, detail in signals
        ]
        if signals:
            last_used = max(when for _, when, _ in signals)
            entry["last_used"] = last_used
            entry["unused_days"] = (now - last_used).days
        else:
            entry["last_used"] = None
            entry["unused_days"] = None

        renewal = entry.get("next_renewal")
        entry["days_to_renewal"] = (renewal - now).days if renewal else None

        if entry["ignored"]:
            entry["status"] = "除外"
        elif entry.get("stale"):
            entry["status"] = "解約済み？"
        elif entry["unused_days"] is None:
            entry["status"] = "判定不能"
        elif entry["unused_days"] >= unused_days:
            entry["status"] = "解約候補"
        else:
            entry["status"] = "利用中"

    ended = [e for e in subscriptions if e["status"] == "解約済み？"]
    unsure = [e for e in subscriptions
              if e["status"] != "解約済み？" and e.get("confidence") == "低"]
    active = [e for e in subscriptions
              if e["status"] != "解約済み？" and e.get("confidence") != "低"]
    # 手動ぶんは更新日が無くても契約中として数える（合計金額に入れるため）。
    # 通知が飛ばないだけで、契約していることは分かっているので。
    active.sort(key=lambda e: (e["days_to_renewal"] is None, e["days_to_renewal"] or 0))
    ended.sort(key=lambda e: e["last_charge"], reverse=True)
    one_offs.sort(key=lambda e: e["last_charge"], reverse=True)
    unsure.sort(key=lambda e: e["last_charge"], reverse=True)
    return {
        "subscriptions": active,
        "unsure": unsure,
        "ended": ended,
        "one_offs": one_offs,
        "generated_at": now,
    }


def gather(config: dict, verbose: bool = True, collect_web: bool = False,
           force_web: bool = False) -> dict:
    """メール・Chrome・アプリを全部集めてサブスク一覧を返す。"""
    import mail_scan

    services = credentials.load_services()
    notes: list[str] = []

    if verbose:
        print("Gmailから請求メールを探しています…")
    records = mail_scan.scan(config, verbose=verbose)

    if verbose:
        print("Chromeの閲覧履歴を読んでいます…")
    chrome_history, chrome_problems = usage_chrome.collect(config)
    notes += chrome_problems

    if verbose:
        print("アプリの最終使用日を調べています…")
    app_usage, app_problems = usage_apps.collect(config)
    notes += app_problems

    # ブラウザから取る利用量は、通知を出す予定があるときだけ集める（毎日タブを開かない）
    web_usage: dict = {}
    if collect_web and (config.get("web_usage") or {}).get("enabled"):
        import usage_web

        wanted = list(usage_web.COLLECTORS)
        if verbose:
            print(f"ブラウザから利用量を取ります（{', '.join(wanted)}）…")
        web_usage, web_notes = usage_web.collect(wanted, force=force_web)
        notes += web_notes
    else:
        web_usage = credentials.load_json(
            os.path.join(_HERE, "usage_web.json"), {})
        web_usage = {k: v for k, v in web_usage.items() if v.get("ok")}

    domains = sorted({r["domain"] for r in records if r.get("billing")})
    if verbose:
        print(f"通知メールの既読状況を確認しています（{len(domains)}ドメイン）…")
    try:
        mail_engagement = mail_scan.engagement(config, domains)
    except Exception as exc:  # noqa: BLE001 - ここが落ちても他の判定は使える
        mail_engagement = {}
        notes.append(f"通知メールの既読チェックに失敗: {exc}")

    result = build(config, records, services, chrome_history, app_usage,
                   mail_engagement, web_usage)
    result["notes"] = notes
    result["record_count"] = len(records)
    return result


if __name__ == "__main__":
    cfg = credentials.load_config()
    data = gather(cfg)
    for note in data["notes"]:
        print("!", note)
    print(f"\n=== サブスク {len(data['subscriptions'])}件 ===")
    for s in data["subscriptions"]:
        renewal = s["next_renewal"].strftime("%Y-%m-%d") if s["next_renewal"] else "-"
        used = f"{s['unused_days']}日前" if s["unused_days"] is not None else "不明"
        print(f"  {s['status']:<6} {renewal}  {s['currency']} {s['amount']:>8,.0f} "
              f"{s['cycle']:<8} 最終利用:{used:<8} {s['name']}")
    print(f"\n=== サブスクかも（要確認） {len(data['unsure'])}件 ===")
    for s in data["unsure"]:
        renewal = s["next_renewal"].strftime("%Y-%m-%d") if s["next_renewal"] else "-"
        print(f"  {renewal}  {s['currency']} {s['amount']:>8,.0f} {s['cycle']:<6} {s['name']}  {s['subjects'][0][:36]}")
    print(f"\n=== 止まっていそう {len(data['ended'])}件 ===")
    for s in data["ended"]:
        print(f"  {s['last_charge']:%Y-%m-%d} 最後  {s['currency']} {s['amount']:>8,.0f} {s['cycle']:<6} {s['name']}")
    print(f"\n=== 単発の買い物・不定期 {len(data['one_offs'])}件（サブスクではない） ===")
    for s in data["one_offs"][:15]:
        print(f"  {s['last_charge']:%Y-%m-%d}  {s['currency']} {s['amount']:>8,.0f}  {s['name']}  {s['subjects'][0][:40]}")
