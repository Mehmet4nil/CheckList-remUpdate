#!/usr/bin/env python3
"""FCM ile duyuru gonderir (Firebase Cloud Messaging HTTP v1).

    # Yeni surum bildirimi (release.sh bunu kendisi cagirir):
    python3 tool/notify.py --release build/release/version.json

    # Elle duyuru (Console yerine terminalden):
    python3 tool/notify.py --topic all --title "Baslik" --body "Metin"

    # Gondermeden ne gidecegini gor:
    python3 tool/notify.py --release build/release/version.json --dry-run

Kimlik: servis hesabi anahtari (JSON). Varsayilan yer
~/.android-keys/checklist-fcm.json, ya da CHECKLIST_FCM_KEY ortam degiskeni.
Anahtar GIT DISI; Firebase Console > Proje ayarlari > Hizmet hesaplari >
"Yeni ozel anahtar olustur" ile bir kez indirilir (KOMUTLAR.md).

Ek paket yok: JWT imzasi macOS'taki `openssl` ile atilir.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = "check-list-9ade7"
DEFAULT_KEY = os.path.expanduser("~/.android-keys/checklist-fcm.json")
# Uygulamadaki kAnnouncementChannelId ve manifestteki varsayilan kanal.
CHANNEL = "announcements"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_jwt(key: dict, now: int, scope: str = SCOPE) -> str:
    """Servis hesabi icin RS256 imzali JWT (imza: openssl)."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": key["client_email"],
        "scope": scope,
        "aud": key.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(key["private_key"])
        pem = f.name
    try:
        os.chmod(pem, 0o600)
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem],
            input=signing_input.encode(),
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.unlink(pem)
    return signing_input + "." + b64url(sig)


def load_key() -> dict:
    """Servis hesabi anahtari; yoksa FileNotFoundError (yolu mesajda)."""
    key_path = os.environ.get("CHECKLIST_FCM_KEY", DEFAULT_KEY)
    with open(key_path, encoding="utf-8") as f:
        return json.load(f)


def access_token(key: dict, scope: str = SCOPE) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed_jwt(key, int(time.time()), scope),
    }).encode()
    req = urllib.request.Request(
        key.get("token_uri", "https://oauth2.googleapis.com/token"), data=body)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


def message(topic: str, title: str, body: str, data: dict) -> dict:
    return {
        "message": {
            "topic": topic,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in data.items()},
            "android": {
                "priority": "high",
                "notification": {"channel_id": CHANNEL},
            },
        }
    }


def release_messages(manifest: dict) -> list:
    """Yeni surum: Turkce lang-tr'ye, Ingilizce lang-en'e (surum notlariyla
    ayni dil kurali). Govde = son surumun ilk 3 maddesi."""
    name = manifest.get("versionName", "")
    code = manifest.get("versionCode", 0)
    history = manifest.get("history") or []
    notes = next((h["notes"] for h in history
                  if h.get("versionCode") == code), {})
    data = {"type": "update", "versionCode": code}

    def body(items: list, fallback: str) -> str:
        items = [i for i in items if i.strip()][:3]
        return "\n".join("• " + i for i in items) if items else fallback

    tr = notes.get("tr") or []
    en = notes.get("en") or tr
    return [
        message("lang-tr", f"CheckList {name} hazır",
                body(tr, "Yeni sürümü yüklemek için dokun."), data),
        message("lang-en", f"CheckList {name} is available",
                body(en, "Tap to install the new version."), data),
    ]


def send(token: str, msg: dict) -> str:
    req = urllib.request.Request(
        f"https://fcm.googleapis.com/v1/projects/{PROJECT}/messages:send",
        data=json.dumps(msg).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("name", "")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--release", help="version.json yolu (yeni surum bildirimi)")
    p.add_argument("--topic", default="all", help="all | lang-tr | lang-en")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--dry-run", action="store_true",
                   help="gonderme, yalnizca mesajlari yazdir")
    a = p.parse_args()

    if a.release:
        with open(a.release, encoding="utf-8") as f:
            msgs = release_messages(json.load(f))
    elif a.title and a.body:
        msgs = [message(a.topic, a.title, a.body, {"type": "announcement"})]
    else:
        p.error("--release ya da --title + --body gerekli")

    if a.dry_run:
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return 0

    key_path = os.environ.get("CHECKLIST_FCM_KEY", DEFAULT_KEY)
    if not os.path.exists(key_path):
        print(f"UYARI: FCM anahtari yok ({key_path}); bildirim gonderilmedi.\n"
              "Firebase Console > Proje ayarlari > Hizmet hesaplari > "
              "'Yeni ozel anahtar olustur' ile indirip bu yola koy "
              "(KOMUTLAR.md).", file=sys.stderr)
        return 2
    with open(key_path, encoding="utf-8") as f:
        key = json.load(f)
    try:
        token = access_token(key)
        for m in msgs:
            print("Gonderildi:", m["message"]["topic"], send(token, m))
    except urllib.error.HTTPError as e:
        print(f"HATA: FCM {e.code}: {e.read().decode(errors='replace')}",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, subprocess.CalledProcessError) as e:
        print(f"HATA: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
