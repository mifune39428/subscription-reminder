"""Gmail(IMAP)から「請求メールらしきもの」を集める。

Gmailのアプリパスワードでそのまま IMAP に入れるので、OAuth もGCPも要らない。
BODY.PEEK を使うので、読み取りで未読が既読になることはない（既読フラグは
「使っているか」の判定材料に使うので、勝手に触ってはいけない）。

取り方:
  1. ASCII だけで書ける Gmail 検索（category:purchases など）で候補UIDを集める
  2. 日本語キーワードの検索も試し、サーバに蹴られたら黙って諦める
  3. ヘッダ(From/Subject/Date)とFLAGSだけ先に一括取得
  4. 件名が請求っぽいものだけ本文の先頭64KBを取り、金額を抜く
  5. 取ったものは mail_cache.json に貯めるので、次回は増分だけ取りに行く
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

import credentials

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_HERE, "mail_cache.json")

imaplib._MAXLINE = 10_000_000

# 請求メールらしさ。件名か本文にこのどれかがあり、かつ金額が取れたら「課金」とみなす。
BILLING_WORDS = [
    "領収書", "領収", "請求", "お支払", "支払い", "支払完了", "決済", "ご利用明細",
    "利用明細", "明細", "自動更新", "更新のお知らせ", "サブスクリプション", "課金",
    "レシート", "ご注文", "購入完了", "契約更新", "請求書",
    "receipt", "invoice", "payment", "billing", "subscription", "renewal",
    "renewed", "charged", "your order", "has been paid", "statement",
]

# 「請求ではない普通のお知らせ」を落とす（利用シグナル側で使う）
NOT_BILLING_WORDS = [
    "支払い方法", "カードの有効期限", "決済できません", "failed", "declined",
    "パスワード", "ログイン", "verify", "confirm your email",
    # 金額が本文に載っているだけで課金ではないもの（iCloudの容量警告など）
    "上限に達", "空き容量", "容量が", "アップグレードしませんか", "キャンペーン",
    "アラート", "ニュースレター", "newsletter", "digest", "週刊", "まとめ読み",
]

# 金額。合計行を優先したいので、行ごとに見る。
_AMOUNT_PATTERNS = [
    (re.compile(r"[¥￥]\s*([0-9][0-9,]*)(?:\.\d+)?"), "JPY"),
    (re.compile(r"([0-9][0-9,]*)\s*円"), "JPY"),
    (re.compile(r"\bJPY\s*([0-9][0-9,]*)(?:\.\d+)?"), "JPY"),
    (re.compile(r"\$\s*([0-9][0-9,]*\.\d{2})"), "USD"),
    (re.compile(r"\bUSD\s*([0-9][0-9,]*(?:\.\d{2})?)"), "USD"),
    (re.compile(r"€\s*([0-9][0-9,]*(?:[.,]\d{2})?)"), "EUR"),
]

# 本文にこれがあれば「定期課金の領収書」らしい。Appleの曲の購入には出てこない。
RECURRING_WORDS = [
    "月額", "年額", "1か月", "1ヶ月", "1年", "自動更新", "次回請求", "更新日",
    "サブスクリプションの更新", "毎月", "毎年", "per month", "per year",
    "monthly", "yearly", "annual", "renews", "next billing", "billing period",
]

TOTAL_WORDS = ["合計", "ご請求", "請求額", "請求金額", "お支払い金額", "総額",
               "total", "amount due", "amount charged", "grand total", "charged"]

_UID_RE = re.compile(rb"UID\s+(\d+)")
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def _domain_of(from_header: str) -> str:
    addr = email.utils.parseaddr(from_header)[1] or ""
    domain = addr.split("@")[-1].lower().strip(">").strip()
    # no-reply@mail.example.com → example.com までは落とすが、co.jp は2段残す
    parts = [p for p in domain.split(".") if p]
    if len(parts) <= 2:
        return domain
    if parts[-2] in ("co", "or", "ne", "ac", "com") and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&yen;", "¥").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", text)


def body_text(raw: bytes) -> str:
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - 途中で切れた本文でも落とさない
        return raw.decode("utf-8", "replace")
    chunks: list[str] = []
    html_chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        (chunks if ctype == "text/plain" else html_chunks).append(text)
    if not chunks and html_chunks:
        chunks = [_strip_html(h) for h in html_chunks]
    return "\n".join(chunks)[:200_000]


def extract_amount(text: str) -> dict | None:
    """合計らしい行の金額を優先して1つ返す。無ければ本文中の最大額。"""
    candidates: list[tuple[int, float, str]] = []  # (優先度, 額, 通貨)
    for line in text.splitlines():
        low = line.lower()
        is_total = any(w in line or w in low for w in TOTAL_WORDS)
        for pattern, currency in _AMOUNT_PATTERNS:
            for m in pattern.finditer(line):
                raw = m.group(1).replace(",", "")
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if value <= 0 or value > 1_000_000:
                    continue
                if currency == "JPY" and value < 50:
                    continue  # 「1個」「2 円」みたいな誤爆を落とす
                candidates.append((1 if is_total else 0, value, currency))
    if not candidates:
        return None
    best_priority = max(c[0] for c in candidates)
    pool = [c for c in candidates if c[0] == best_priority]
    priority, value, currency = max(pool, key=lambda c: c[1])
    return {"value": value, "currency": currency, "from_total_line": bool(priority)}


def looks_billing(subject: str) -> bool:
    """件名だけで判断する。

    本文を根拠にしてはいけない。ニュースレターやGoogleアラートの本文には
    商品の値段がふつうに載っていて、「購入」「プラン」といった語も出てくるので、
    本文を見ると記事がまるごと請求メールに化ける（実際に化けた）。
    """
    subject_low = subject.lower()
    if any(w.lower() in subject_low for w in NOT_BILLING_WORDS):
        return False
    return any(w.lower() in subject_low for w in BILLING_WORDS)


# ---------------------------------------------------------------- IMAP

def connect(settings: dict, server: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(server)
    conn.login(settings["sender_email"], settings["sender_password"])
    return conn


class Session:
    """切れても勝手につなぎ直すIMAPセッション。

    Gmailは長いFETCHの途中で接続を落としてくることがある（実際に落ちた）。
    launchdから毎日走らせるものが1回の切断で丸ごと失敗すると困るので、
    コマンド単位で1度だけ張り直して再試行する。
    """

    RETRYABLE = (imaplib.IMAP4.abort, ssl.SSLError, OSError)

    def __init__(self, settings: dict, server: str, mailbox: str):
        self._settings = settings
        self._server = server
        self._want = mailbox
        self.mailbox = ""
        self.conn: imaplib.IMAP4_SSL | None = None
        self.uidvalidity = ""
        self.open()

    def open(self) -> None:
        self.conn = connect(self._settings, self._server)
        self.mailbox = pick_mailbox(self.conn, self._want)
        name = f'"{self.mailbox}"' if " " in self.mailbox else self.mailbox
        typ, _ = self.conn.select(name, readonly=True)
        if typ != "OK":
            self.conn.select("INBOX", readonly=True)
            self.mailbox = "INBOX"
        self.uidvalidity = (self.conn.response("UIDVALIDITY")[1] or [b""])[0].decode()

    def uid(self, *args):
        for attempt in (0, 1):
            try:
                return self.conn.uid(*args)
            except self.RETRYABLE:
                if attempt:
                    raise
                self.close()
                self.open()
        raise RuntimeError("unreachable")

    def close(self) -> None:
        try:
            if self.conn:
                self.conn.logout()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None


def pick_mailbox(conn: imaplib.IMAP4_SSL, want: str) -> str:
    """すべてのメール(\\All)を探す。日本語アカウントだと名前が modified UTF-7 なので自力で探す。"""
    if want and want != "auto":
        return want
    typ, boxes = conn.list()
    if typ == "OK":
        for raw in boxes or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                name = line.split(' "/" ')[-1].strip()
                return name
    return "INBOX"


def _search(session, raw_query: str) -> list[str]:
    """X-GM-RAW で検索。日本語が通らないサーバ設定なら空で返す。"""
    try:
        arg = ('"%s"' % raw_query.replace('"', '\\"')).encode("utf-8")
        typ, data = session.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW", arg)
    except (imaplib.IMAP4.error, ssl.SSLError, OSError):
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].decode().split()


def _fetch_headers(session, uids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    part = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    for i in range(0, len(uids), 100):
        chunk = uids[i:i + 100]
        try:
            typ, data = session.uid("FETCH", ",".join(chunk), part)
        except (imaplib.IMAP4.error, ssl.SSLError, OSError) as exc:
            print(f"  ヘッダ取得を中断しました（{exc}）")
            break
        if typ != "OK":
            continue
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            head, raw = item[0], item[1]
            m = _UID_RE.search(head)
            if not m:
                continue
            uid = m.group(1).decode()
            flags = _FLAGS_RE.search(head)
            seen = b"\\Seen" in (flags.group(1) if flags else b"")
            msg = email.message_from_bytes(raw)
            date = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
            if date and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            out[uid] = {
                "uid": uid,
                "from": _decode(msg.get("From")),
                "domain": _domain_of(msg.get("From", "")),
                "subject": _decode(msg.get("Subject")),
                "date": date.astimezone().isoformat() if date else None,
                "seen": seen,
            }
    return out


def _fetch_bodies(session, uids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for uid in uids:
        try:
            typ, data = session.uid("FETCH", uid, "(BODY.PEEK[]<0.65536>)")
        except (imaplib.IMAP4.error, ssl.SSLError, OSError) as exc:
            print(f"  本文取得を中断しました（{exc}）")
            break
        if typ != "OK":
            continue
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                out[uid] = body_text(item[1])
                break
    return out


def scan(config: dict, verbose: bool = True) -> list[dict]:
    """請求メールのレコード一覧を返す（キャッシュ込み）。"""
    conf = config["email"]
    settings = credentials.load_email_settings()
    cache = credentials.load_json(CACHE_PATH, {})
    days = int(conf.get("scan_days", 400))

    session = Session(settings, conf.get("imap_server", "imap.gmail.com"),
                      conf.get("mailbox", "auto"))
    try:
        mailbox, uidvalidity = session.mailbox, session.uidvalidity
        if cache.get("mailbox") != mailbox or cache.get("uidvalidity") != uidvalidity:
            cache = {"mailbox": mailbox, "uidvalidity": uidvalidity, "messages": {}}
        cache.setdefault("purchase_uids", [])
        messages: dict[str, dict] = cache.setdefault("messages", {})

        # 日本語のキーワード検索は X-GM-RAW に非ASCIIを渡せず必ず0件になるので使わない。
        # 代わりに、カタログに載っているサービスの送信元を直接引く。
        queries = [
            f"newer_than:{days}d category:purchases",
            f"newer_than:{days}d (subject:receipt OR subject:invoice OR subject:subscription "
            f"OR subject:renewal OR subject:payment OR subject:billing OR subject:charged)",
        ]
        known_domains = sorted({
            d for service in credentials.load_services()
            for d in service.get("mail_domains", [])
        })
        for i in range(0, len(known_domains), 20):
            chunk = known_domains[i:i + 20]
            queries.append(f"newer_than:{days}d from:({' OR '.join(chunk)})")
        uids: list[str] = []
        seen_uids: set[str] = set()
        purchases: set[str] = set()
        for index, query in enumerate(queries):
            found = _search(session, query)
            if verbose:
                print(f"  検索: {query[:48]}… → {len(found)}件")
            # Gmailが「購入」に分類したものだけ、件名を問わず本文まで見る。
            # 既知サービスの from: 検索はニュースレターも大量に拾うので、
            # そちらは件名が請求らしいものだけ本文を取りに行く。
            if index == 0:
                purchases.update(found)
                cache["purchase_uids"] = sorted(purchases)
            for uid in found:
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    uids.append(uid)

        fresh = [u for u in uids if u not in messages]
        limit = int(conf.get("max_fetch_per_run", 400))
        fresh_sorted = sorted(fresh, key=int, reverse=True)[:limit]
        if verbose:
            print(f"  候補 {len(uids)}件 / 未取得 {len(fresh)}件 → 今回 {len(fresh_sorted)}件を取得")

        if fresh_sorted:
            headers = _fetch_headers(session, fresh_sorted)
            need_body = [
                u for u, r in headers.items()
                if u in purchases or looks_billing(r["subject"])
            ]
            bodies = _fetch_bodies(session, need_body)
            for uid, record in headers.items():
                record["in_purchases"] = uid in purchases
                body = bodies.get(uid, "")
                # 判定を後から見直せるよう、本文の頭だけ残す（Apple の領収書のように
                # 件名だけでは中身が分からないものがあるため）
                if body:
                    record["body_excerpt"] = re.sub(r"\s+", " ", body)[:3000]
                record["amount"] = extract_amount(body) if body else None
                if not record["amount"]:
                    record["amount"] = extract_amount(record["subject"])
                messages[uid] = record

        # 既読フラグは後から変わるので、キャッシュ済みのぶんも安いヘッダ取得で更新する。
        # ここが失敗しても集計そのものはできるので、握りつぶして先へ進む。
        known = [u for u in uids if u in messages]
        if known:
            try:
                refreshed = _fetch_headers(session, sorted(known, key=int, reverse=True)[:600])
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  既読フラグの更新をとばしました（{exc}）")
                refreshed = {}
            for uid, record in refreshed.items():
                if uid in messages:
                    messages[uid]["seen"] = record["seen"]
    finally:
        session.close()

    credentials.save_json(CACHE_PATH, cache)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    purchase_uids = set(cache.get("purchase_uids") or [])
    for uid, record in messages.items():
        # 判定ルールは後から変わるので、キャッシュ済みのぶんも毎回やり直す。
        # 根拠は「件名」か「Gmailが購入に分類したこと」のどちらかだけ。
        in_purchases = record.get("in_purchases")
        if in_purchases is None:
            in_purchases = uid in purchase_uids
        subject = record.get("subject") or ""
        vetoed = any(w.lower() in subject.lower() for w in NOT_BILLING_WORDS)
        record["billing"] = bool(
            not vetoed
            and (looks_billing(subject) or (in_purchases and record.get("amount")))
        )
        excerpt = record.get("body_excerpt") or ""
        record["recurring_hint"] = any(w in excerpt for w in RECURRING_WORDS) if excerpt else None
        if not record.get("date"):
            continue
        try:
            when = datetime.fromisoformat(record["date"])
        except ValueError:
            continue
        if when < cutoff:
            continue
        out.append(record)
    out.sort(key=lambda r: r["date"])
    return out


def engagement(config: dict, domains: list[str], days: int = 120) -> dict[str, dict]:
    """請求以外の通知メールが「既読になっているか」を利用シグナルとして取る。

    届いているだけでは使っている証拠にならないので、\\Seen が付いたものだけを見る。
    ヘッダしか取らないので安い。
    """
    if not domains:
        return {}
    conf = config["email"]
    settings = credentials.load_email_settings()
    session = Session(settings, conf.get("imap_server", "imap.gmail.com"),
                      conf.get("mailbox", "auto"))
    out: dict[str, dict] = {}
    try:
        for i in range(0, len(domains), 15):
            chunk = domains[i:i + 15]
            query = f"newer_than:{days}d from:({' OR '.join(chunk)})"
            uids = _search(session, query)
            if not uids:
                continue
            headers = _fetch_headers(session, sorted(uids, key=int, reverse=True)[:400])
            for record in headers.values():
                domain = record["domain"]
                entry = out.setdefault(domain, {"total": 0, "read": 0, "last_read": None})
                entry["total"] += 1
                if record["seen"] and record.get("date"):
                    entry["read"] += 1
                    when = datetime.fromisoformat(record["date"])
                    if entry["last_read"] is None or when > entry["last_read"]:
                        entry["last_read"] = when
    finally:
        session.close()
    return out


if __name__ == "__main__":
    cfg = credentials.load_config()
    records = scan(cfg)
    charges = [r for r in records if r.get("billing") and r.get("amount")]
    print(f"\n全 {len(records)}件 / 課金とみなせるもの {len(charges)}件")
    for r in charges[-25:]:
        amount = r["amount"]
        print(f"  {r['date'][:10]}  {r['domain']:<28} {amount['currency']} {amount['value']:>9,.0f}  {r['subject'][:44]}")
    sys.exit(0)
