"""
auth.py — Authentification des agents.

Les données manipulées (dépistages nominatifs d'enfants) sont des données de
santé : l'accès à la plateforme est donc réservé aux agents connectés.

Choix techniques — volontairement 100 % bibliothèque standard :
  • Mots de passe hachés PBKDF2-HMAC-SHA256, 600 000 itérations (reco OWASP),
    sel aléatoire de 16 octets. Jamais stockés en clair.
  • Comparaisons en temps constant (hmac.compare_digest). Si l'identifiant
    n'existe pas, on vérifie quand même contre un hash factice : le temps de
    réponse ne révèle pas quels identifiants existent.
  • Sessions : cookies signés (SessionMiddleware / itsdangerous), HttpOnly,
    SameSite=Lax, expiration 8 h — la journée de terrain d'un agent.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3

ITERATIONS = 600_000
RE_IDENTIFIANT = re.compile(r"^[a-z0-9_.\-]{3,20}$")

# Hash d'un mot de passe aléatoire jeté : sert uniquement à garder un temps
# de réponse constant quand l'identifiant demandé n'existe pas.
HASH_FACTICE = ("pbkdf2_sha256$600000$fc9ee05340902fc8f3c429a865eb05ac$"
                "ffdace421eb35abb79f2e6b9849e1bc434682fdf4bd9dc7763493d701392f548")


def hacher_mdp(mdp: str) -> str:
    sel = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${sel.hex()}${h.hex()}"


def verifier_mdp(mdp: str, stocke: str) -> bool:
    try:
        _algo, iters, sel_hex, h_hex = stocke.split("$")
        h = hashlib.pbkdf2_hmac("sha256", mdp.encode(),
                                bytes.fromhex(sel_hex), int(iters))
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def creer_agent(con, *, nom: str, identifiant: str, mdp: str,
                region: str, zone: str = "", telephone: str | None = None,
                role: str = "agent") -> int:
    """Crée un compte agent. Lève ValueError avec un message affichable."""
    nom, identifiant = nom.strip(), identifiant.strip().lower()
    if not nom:
        raise ValueError("Le nom complet est requis.")
    if not RE_IDENTIFIANT.match(identifiant):
        raise ValueError("Identifiant invalide : 3 à 20 caractères, "
                         "lettres minuscules, chiffres, . _ - uniquement.")
    if len(mdp) < 8:
        raise ValueError("Le mot de passe doit contenir au moins 8 caractères.")
    try:
        cur = con.execute(
            """INSERT INTO agents (nom, identifiant, mdp_hash, zone, region,
                                   telephone, role)
               VALUES (?,?,?,?,?,?,?)""",
            (nom, identifiant, hacher_mdp(mdp), zone.strip(), region, telephone,
             role if role in ("agent", "admin") else "agent"))
    except sqlite3.IntegrityError:
        raise ValueError("Cet identifiant est déjà utilisé — choisissez-en un autre.")
    return cur.lastrowid


def authentifier(con, identifiant: str, mdp: str):
    """Retourne la ligne agent si identifiant + mot de passe valides, sinon None."""
    a = con.execute("SELECT * FROM agents WHERE identifiant = ?",
                    (identifiant.strip().lower(),)).fetchone()
    if a is None or not a["mdp_hash"]:
        verifier_mdp(mdp, HASH_FACTICE)   # temps constant, anti-énumération
        return None
    return a if verifier_mdp(mdp, a["mdp_hash"]) else None
