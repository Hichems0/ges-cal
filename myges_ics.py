#!/usr/bin/env python3
"""
myges_ics.py — Génère un fichier .ics depuis le planning myGES (Réseau GES / backend Kordis).

Pensé pour tourner en cron : à chaque exécution il ré-authentifie, récupère
l'agenda sur une fenêtre glissante et réécrit le .ics. Apple Calendar (ou tout
autre agenda) s'abonne ensuite à l'URL où tu sers ce fichier -> sync vivante.

Utilisation :
    export MYGES_USER="prenom.nom"          # ton identifiant myGES
    export MYGES_PASS="ton_mot_de_passe"
    python3 myges_ics.py --out planning.ics --past 30 --future 150

Astuce première fois : lance avec --debug pour voir la structure brute d'un
cours et vérifier que les champs (salle, intervenant...) sont bien mappés.
"""

import os
import sys
import json
import base64
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import requests

AUTH_URL = ("https://authentication.kordis.fr/oauth/authorize"
            "?response_type=token&client_id=skolae-app")
API_BASE = "https://api.kordis.fr"
UA = "myges-ics/1.0 (+perso)"


def get_token(user: str, password: str) -> str:
    """Auth Basic -> l'API répond par une redirection dont le fragment porte le token."""
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    r = requests.get(
        AUTH_URL,
        headers={"Authorization": f"Basic {creds}", "User-Agent": UA},
        allow_redirects=False,          # la redirection est en scheme custom, on la lit à la main
        timeout=30,
    )
    loc = r.headers.get("Location") or r.headers.get("location")
    if not loc:
        raise SystemExit(
            f"Auth echouee (HTTP {r.status_code}). "
            "Verifie MYGES_USER / MYGES_PASS (ce sont tes identifiants myGES)."
        )
    # loc ~= comreseaugesskolae:/oauth2redirect#access_token=XXX&token_type=bearer&expires_in=...
    if "#" not in loc:
        raise SystemExit(f"Reponse d'auth inattendue : {loc}")
    params = parse_qs(loc.split("#", 1)[1])
    token = params.get("access_token", [None])[0]
    if not token:
        raise SystemExit(f"Pas d'access_token dans la reponse : {loc}")
    return token


def fetch_agenda(token: str, start_ms: int, end_ms: int) -> list:
    r = requests.get(
        f"{API_BASE}/me/agenda",
        headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
        params={"start": start_ms, "end": end_ms},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        return data.get("result") or data.get("agenda") or []
    return data or []


# ---------- helpers ----------

def get_field(item: dict, *names):
    for n in names:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return None


def ms_to_utc(ms) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def ics_escape(text) -> str:
    if text is None:
        return ""
    return (str(text)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n"))


def rooms_str(item: dict) -> str:
    """La salle peut arriver sous 'rooms' (liste), 'room' (objet) ou en champ plat."""
    parts = []
    rooms = item.get("rooms")
    if isinstance(rooms, list):
        for room in rooms:
            if isinstance(room, dict):
                label = room.get("name") or ""
                if room.get("floor"):
                    label += f" ({room['floor']})"
                if room.get("campus"):
                    label = f"{label}, {room['campus']}" if label else room["campus"]
                if label:
                    parts.append(label)
    elif isinstance(item.get("room"), dict):
        room = item["room"]
        label = " ".join(x for x in (room.get("name") or "",
                                     room.get("campus") or "") if x)
        if label:
            parts.append(label)
    else:
        flat = get_field(item, "room_name", "salle", "room")
        if flat:
            parts.append(str(flat))
    return ", ".join(parts)


def make_uid(item: dict, start_ms) -> str:
    """UID stable pour qu'une modif remplace l'event au lieu d'en creer un doublon."""
    raw = str(get_field(item, "id", "activity_instance_id", "reservation_id")
              or f"{start_ms}-{get_field(item, 'name')}")
    return hashlib.md5(raw.encode()).hexdigest() + "@myges"


def build_ics(items: list, calname: str = "myGES") -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//myges-ics//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(calname)}",
        "X-WR-TIMEZONE:Europe/Paris",
        "REFRESH-INTERVAL;VALUE=DURATION:PT2H",
        "X-PUBLISHED-TTL:PT2H",
    ]
    kept = 0
    for item in items:
        start_ms = get_field(item, "start_date", "starts", "start", "startDate")
        end_ms = get_field(item, "end_date", "ends", "end", "endDate")
        if not start_ms or not end_ms:
            continue
        name = get_field(item, "name", "label") or "Cours"
        teacher = get_field(item, "teacher", "teacher_name", "prof")
        typ = get_field(item, "type", "nature")
        modality = get_field(item, "modality")
        loc = rooms_str(item)

        disc = item.get("discipline")
        group = disc.get("student_group_name") if isinstance(disc, dict) else None
        comment = get_field(item, "comment")

        desc = []
        if teacher:
            desc.append(f"Intervenant : {teacher}")
        if typ:
            desc.append(f"Type : {typ}")
        if modality:
            desc.append(f"Modalite : {modality}")
        if group:
            desc.append(f"Groupe : {group}")
        if comment:
            desc.append(f"Note : {comment}")
        description = "\n".join(desc)

        out += [
            "BEGIN:VEVENT",
            f"UID:{make_uid(item, start_ms)}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{ms_to_utc(start_ms).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{ms_to_utc(end_ms).strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(name)}",
        ]
        if loc:
            out.append(f"LOCATION:{ics_escape(loc)}")
        if description:
            out.append(f"DESCRIPTION:{ics_escape(description)}")
        out.append("END:VEVENT")
        kept += 1

    out.append("END:VCALENDAR")
    build_ics.kept = kept  # petit hack pour le log
    return "\r\n".join(out) + "\r\n"   # CRLF requis par la RFC 5545


def main():
    ap = argparse.ArgumentParser(description="Genere un .ics depuis myGES.")
    ap.add_argument("--out", default="planning.ics")
    ap.add_argument("--past", type=int, default=30, help="jours dans le passe (defaut 30)")
    ap.add_argument("--future", type=int, default=150, help="jours dans le futur (defaut 150)")
    ap.add_argument("--name", default="myGES", help="nom du calendrier affiche")
    ap.add_argument("--debug", action="store_true", help="affiche le 1er cours brut (stderr)")
    args = ap.parse_args()

    user = os.environ.get("MYGES_USER")
    password = os.environ.get("MYGES_PASS")
    if not user or not password:
        raise SystemExit("Definis MYGES_USER et MYGES_PASS dans l'environnement.")

    token = get_token(user, password)
    now = datetime.now(tz=timezone.utc)
    start_ms = int((now - timedelta(days=args.past)).timestamp() * 1000)
    end_ms = int((now + timedelta(days=args.future)).timestamp() * 1000)

    items = fetch_agenda(token, start_ms, end_ms)
    if args.debug and items:
        print(json.dumps(items[0], indent=2, ensure_ascii=False), file=sys.stderr)

    ics = build_ics(items, calname=args.name)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(ics)
    print(f"{getattr(build_ics, 'kept', 0)} cours ecrits dans {args.out} "
          f"(sur {len(items)} recus)")


if __name__ == "__main__":
    main()
