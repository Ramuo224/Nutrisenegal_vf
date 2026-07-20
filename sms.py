"""
sms.py — Passerelle SMS (Twilio) avec mode simulation intégré.

Si les variables d'environnement TWILIO_SID / TWILIO_TOKEN / TWILIO_NUM sont
présentes, les SMS partent réellement. Sinon, ils sont journalisés dans la
table sms_log : le dashboard les affiche en direct — parfait pour la démo
devant le jury (et pour développer sans compte Twilio).
"""
from __future__ import annotations
import os

TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUM = os.getenv("TWILIO_NUM", "")

_client = None
if TWILIO_SID and TWILIO_TOKEN:
    try:
        from twilio.rest import Client
        _client = Client(TWILIO_SID, TWILIO_TOKEN)
    except Exception:
        _client = None

MODE = "réel (Twilio)" if _client else "simulation (sms_log)"


def envoyer_sms(con, telephone: str, corps: str) -> str:
    """Envoie un SMS (160 caractères max) ou le journalise en simulation."""
    corps = corps[:160]
    statut = "simulé"
    if _client:
        try:
            _client.messages.create(body=corps, from_=TWILIO_NUM, to=telephone)
            statut = "envoyé"
        except Exception:
            statut = "échec"
    con.execute(
        "INSERT INTO sms_log (direction, telephone, corps, statut) VALUES ('OUT',?,?,?)",
        (telephone, corps, statut))
    return statut


def envoyer_alerte(con, enfant: dict, score: str, centre: dict) -> str:
    """Alerte ROUGE NutriScan → centre de santé le plus proche."""
    msg = (f"NUTRISCAN ALERTE {score}: Enfant {enfant['prenom']} "
           f"({enfant['age_mois']}m), region {enfant['region']}. "
           f"Referer en urgence. Ref centre: {centre.get('nom', 'CS principal')}")
    return envoyer_sms(con, centre.get("tel", "+221770000000"), msg)


def journaliser_entrant(con, telephone: str, corps: str):
    con.execute(
        "INSERT INTO sms_log (direction, telephone, corps, statut) VALUES ('IN',?,?, 'reçu')",
        (telephone, corps))
