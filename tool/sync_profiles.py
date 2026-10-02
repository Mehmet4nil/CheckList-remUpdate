#!/usr/bin/env python3
"""Yonetici paneli kullanici listesini Firebase hesaplariyla tamamlar.

Panel listesi `profiles/{uid}` kayitlarindan olusur; kaydi uygulama acilista
yazar (1.0.19+). Yeni surumu henuz acmamis kullanicilar listede yoktu (ve
aramada cikmiyordu). Bu betik Firebase Auth'taki her hesap icin profil
YOKSA olusturur (e-posta, ad, son giris). Var olan profile DOKUNMAZ
(Firestore `exists: false` on kosulu); uygulama acilinca kendi kaydini
gunceller.

    python3 tool/sync_profiles.py            # eksikleri olustur
    python3 tool/sync_profiles.py --dry-run  # yalniz listele

GitHub Actions "Duyurular" isi de her calismada bunu calistirir.
Kimlik: tool/notify.py ile ayni servis hesabi anahtari.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify import PROJECT, access_token, load_key  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DOCS = (f"projects/{PROJECT}/databases/(default)/documents")
FIRESTORE = "https://firestore.googleapis.com/v1/"
AUTH = f"https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}"


def call(token: str, method: str, url: str, body=None) -> dict:
    req = urllib.request.Request(
        url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def auth_users(token: str) -> list:
    users, page = [], None
    while True:
        url = f"{AUTH}/accounts:batchGet?maxResults=500"
        if page:
            url += f"&nextPageToken={page}"
        res = call(token, "GET", url)
        users += res.get("users", [])
        page = res.get("nextPageToken")
        if not page:
            return users


def profile_ids(token: str) -> set:
    ids, page = set(), None
    while True:
        url = f"{FIRESTORE}{DOCS}/profiles?pageSize=300&mask.fieldPaths=email"
        if page:
            url += f"&pageToken={page}"
        res = call(token, "GET", url)
        ids |= {d["name"].rsplit("/", 1)[1] for d in res.get("documents", [])}
        page = res.get("nextPageToken")
        if not page:
            return ids


def last_login(u: dict) -> str:
    ms = int(u.get("lastLoginAt") or u.get("createdAt") or 0)
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).isoformat()


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

    have = profile_ids(token)
    missing = [u for u in auth_users(token) if u["localId"] not in have]
    print(f"Profili eksik hesap: {len(missing)}")
    for u in missing:
        email = u.get("email", "")
        print(f"- {email or u['localId']}")
        if a.dry_run:
            continue
        write = {
            "update": {
                "name": f"{DOCS}/profiles/{u['localId']}",
                "fields": {
                    "email": {"stringValue": email},
                    "displayName": {"stringValue": u.get("displayName", "")},
                    "lastSeen": {"timestampValue": last_login(u)},
                },
            },
            # Bu arada uygulama kendi kaydini yazdiysa ustune yazma.
            "currentDocument": {"exists": False},
        }
        try:
            call(token, "POST", f"{FIRESTORE}{DOCS}:commit",
                 {"writes": [write]})
        except urllib.error.HTTPError as e:
            if e.code in (400, 409):  # on kosul: profil artik var
                print("  atlandi (profil olusmus)")
                continue
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
