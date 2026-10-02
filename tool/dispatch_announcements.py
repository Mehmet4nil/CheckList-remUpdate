#!/usr/bin/env python3
"""Yonetici panelinden yazilan duyurulari gonderir (sunucu yok).

Panel `announcements/{id}` dokumanini status "pending" ile yazar. Bu betik
bekleyenleri bulur, FCM ile yollar ve durumu "sent" / "error" yapar.
GitHub Actions'ta 5 dakikada bir calisir (CheckList-remUpdate reposu,
.github/workflows/announcements.yml). Elle de calistirilabilir:

    python3 tool/dispatch_announcements.py            # bekleyenleri gonder
    python3 tool/dispatch_announcements.py --dry-run  # yalniz listele

Cift gonderim olmasin diye her duyuru once "sending" durumuna KOSULLU
gecirilir (Firestore updateTime on kosulu): ayni anda iki calisma olsa
bile yalniz biri kazanir. 24 saatten eski bekleyen duyuru gonderilmez
("expired"): is uzun sure durmussa eski haber gec gitmesin.

Kimlik: tool/notify.py ile ayni servis hesabi anahtari (CHECKLIST_FCM_KEY).
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify import PROJECT, access_token, load_key, message, send  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# Testte emulatore yonlendirilir (tool/test_dispatch.py).
API_ROOT = "https://firestore.googleapis.com/v1/"
BASE = f"{API_ROOT}projects/{PROJECT}/databases/(default)/documents"
TOPICS = {"all", "lang-tr", "lang-en"}  # firestore.rules ile ayni
MAX_AGE = datetime.timedelta(hours=24)


def call(token: str, method: str, url: str, body=None) -> dict:
    req = urllib.request.Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def pending(token: str) -> list:
    """status == "pending" olan duyurular (tek alan esitligi: indeks yok)."""
    res = call(token, "POST", f"{BASE}:runQuery", {"structuredQuery": {
        "from": [{"collectionId": "announcements"}],
        "where": {"fieldFilter": {
            "field": {"fieldPath": "status"},
            "op": "EQUAL",
            "value": {"stringValue": "pending"},
        }},
        "limit": 20,
    }})
    return [r["document"] for r in res if "document" in r]


def text(doc: dict, field: str) -> str:
    return doc.get("fields", {}).get(field, {}).get("stringValue", "")


def created(doc: dict):
    v = doc.get("fields", {}).get("createdAt", {}).get("timestampValue")
    if not v:
        return None
    return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))


def patch(token: str, doc: dict, fields: dict, require_unchanged=False):
    """Alanlari yazar (yalniz verilenler). [require_unchanged]: dokuman
    okundugundan beri degismediyse (baska calisma almadiysa) yaz, yoksa
    HTTPError (FAILED_PRECONDITION). Kosul `:commit` govdesinde: sorgu
    parametresi olarak verilen on kosul emulatorde okunmuyordu (testte
    yakalandi). Donus: guncel `name` + `updateTime`."""
    values = {}
    for k, v in fields.items():
        values[k] = ({"timestampValue": v.isoformat()}
                     if isinstance(v, datetime.datetime)
                     else {"stringValue": str(v)})
    write = {
        "update": {"name": doc["name"], "fields": values},
        "updateMask": {"fieldPaths": list(fields)},
    }
    if require_unchanged:
        write["currentDocument"] = {"updateTime": doc["updateTime"]}
    res = call(token, "POST", f"{BASE}:commit", {"writes": [write]})
    return {"name": doc["name"],
            "updateTime": res["writeResults"][0]["updateTime"]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    try:
        token = access_token(load_key(), SCOPE)
    except FileNotFoundError as e:
        print(f"HATA: servis hesabi anahtari yok: {e.filename}", file=sys.stderr)
        return 2

    docs = pending(token)
    print(f"Bekleyen duyuru: {len(docs)}")
    now = datetime.datetime.now(datetime.timezone.utc)
    failures = 0
    for d in docs:
        doc_id = d["name"].rsplit("/", 1)[1]
        title, body, topic = text(d, "title"), text(d, "body"), text(d, "topic")
        when = created(d)
        print(f"- {doc_id}: [{topic}] {title!r}")
        if a.dry_run:
            continue
        if topic not in TOPICS or not title or not body:
            patch(token, d, {"status": "error", "error": "gecersiz duyuru"})
            continue
        if when is not None and now - when > MAX_AGE:
            patch(token, d, {"status": "error", "error": "expired (24 saat)"})
            print("  suresi gecti, gonderilmedi")
            continue
        try:
            claimed = patch(token, d, {"status": "sending"},
                            require_unchanged=True)
        except urllib.error.HTTPError as e:
            # Baska bir calisma once davrandi (on kosul tutmadi): atla.
            print(f"  atlandi (baska calisma aldi: HTTP {e.code})")
            continue
        try:
            msg_id = send(token, message(topic, title, body,
                                         {"type": "announcement"}))
            patch(token, claimed, {"status": "sent", "messageId": msg_id,
                                   "sentAt": now})
            print(f"  gonderildi: {msg_id}")
        except Exception as e:  # FCM ya da ag hatasi: durumu yaz, devam et
            failures += 1
            detail = e.read().decode(errors="replace") \
                if isinstance(e, urllib.error.HTTPError) else str(e)
            patch(token, claimed, {"status": "error", "error": detail[:500]})
            print(f"  HATA: {detail[:200]}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
