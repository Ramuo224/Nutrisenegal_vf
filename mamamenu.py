"""
mamamenu.py — Prévention nutritionnelle par SMS (Module B).

Moteur de recommandation : filtre les recettes par âge de l'enfant, région,
saison et budget, puis ordonne par densité nutritionnelle (kcal + protéines
+ fer + vitamine A pondérés) rapportée au coût. Rotation hebdomadaire
déterministe pour ne jamais renvoyer deux fois la même recette de suite.

Bot SMS entrant (webhook Twilio) :
  "9 DIOURBEL"        → inscription (âge en mois + région)
  "WO" / "FR"         → changement de langue
  "AIDE"              → conseils
  "STOP"              → désinscription
"""
from __future__ import annotations
import datetime as dt
import re
from sms import envoyer_sms, journaliser_entrant

# Pondérations nutritionnelles (priorités santé publique Sénégal :
# énergie, protéines, fer — anémie très répandue — et vitamine A)
POIDS_NUTRI = {"kcal": 1.0, "proteines_g": 30.0, "fer_mg": 40.0, "vit_a_ug": 0.5}


def saison_actuelle(date: dt.date | None = None) -> str:
    """Hivernage (saison des pluies) ≈ juillet → octobre au Sénégal."""
    m = (date or dt.date.today()).month
    return "hivernage" if 7 <= m <= 10 else "seche"


def densite_nutritionnelle(r: dict) -> float:
    score = sum((r.get(k) or 0) * w for k, w in POIDS_NUTRI.items())
    return score / max(r.get("cout_fcfa") or 250, 50)


def recommander(con, age_mois: int, region: str, semaine: int | None = None,
                budget_max: int = 500) -> dict | None:
    """Meilleure recette pour cet enfant, avec rotation hebdomadaire."""
    saison = saison_actuelle()
    rows = con.execute(
        """SELECT * FROM recettes
           WHERE age_min_mois <= ? AND age_max_mois >= ?
             AND cout_fcfa <= ?
             AND (saison = 'toute' OR saison = ?)
             AND (regions = 'toutes' OR instr(regions, ?) > 0)""",
        (age_mois, age_mois, budget_max, saison, region)).fetchall()
    if not rows:
        return None
    classees = sorted((dict(r) for r in rows),
                      key=densite_nutritionnelle, reverse=True)
    semaine = semaine if semaine is not None else dt.date.today().isocalendar()[1]
    return classees[semaine % len(classees)]


def composer_sms_recette(recette: dict, langue: str, semaine: int) -> str:
    if langue == "wo" and recette.get("sms_wo"):
        return f"MAMAMENU AYUBES {semaine}: {recette['sms_wo']}"[:160]
    base = recette.get("sms_fr") or (
        f"{recette['nom']}. {recette['ingredients']}. {recette['instructions']} "
        f"Valeur: {recette['kcal']} kcal, {recette['proteines_g']}g prot.")
    return f"MAMAMENU SEM {semaine}: {base} Tapez AIDE pour conseils."[:160]


def envoyer_campagne_hebdo(con) -> int:
    """Envoie la recette de la semaine à toutes les abonnées. Retourne le nb d'envois."""
    semaine = dt.date.today().isocalendar()[1]
    n = 0
    for ab in con.execute("SELECT * FROM abonnees").fetchall():
        r = recommander(con, ab["age_enfant_mois"], ab["region"], semaine)
        if r:
            envoyer_sms(con, ab["telephone"],
                        composer_sms_recette(r, ab["langue"], semaine))
            n += 1
    return n


def inscrire_abonnee(con, telephone: str, age_mois: int, region: str,
                     langue: str = "fr") -> str:
    """Inscrit (ou met à jour) une mère, envoie le SMS de bienvenue avec la
    première recette. Utilisée par le formulaire web ET le bot SMS."""
    region, langue = region.strip().title(), (langue if langue in ("fr", "wo") else "fr")
    con.execute(
        """INSERT INTO abonnees (telephone, age_enfant_mois, region, langue)
           VALUES (?,?,?,?)
           ON CONFLICT(telephone) DO UPDATE
           SET age_enfant_mois = excluded.age_enfant_mois,
               region = excluded.region,
               langue = excluded.langue""", (telephone, age_mois, region, langue))
    r = recommander(con, age_mois, region)
    if langue == "wo":
        premiere = f" Recette bu njekk: {r['nom']}." if r else ""
        rep = (f"MAMAMENU: Bind bi sotti na (xale {age_mois} weer, {region})."
               f"{premiere} 1 recette/ayubes. FR ngir francais, STOP ngir taxawal.")
    else:
        premiere = f" 1ere recette: {r['nom']}." if r else ""
        rep = (f"MAMAMENU: Inscription OK (enfant {age_mois} mois, {region})."
               f"{premiere} 1 recette/semaine. WO pour wolof, STOP pour arreter.")
    envoyer_sms(con, telephone, rep)
    return rep


def intensifier_suivi(con, region: str, age_mois: int):
    """Déclenché quand NutriScan détecte un cas ORANGE/ROUGE : les mères de la
    même région avec un enfant d'âge proche passent en mode intensif."""
    cibles = con.execute(
        """SELECT * FROM abonnees WHERE region = ?
           AND ABS(age_enfant_mois - ?) <= 6""", (region, age_mois)).fetchall()
    for ab in cibles:
        con.execute("UPDATE abonnees SET mode_intensif = 1 WHERE id = ?", (ab["id"],))
        msg = ("MAMAMENU: Des cas de malnutrition sont signales dans votre zone. "
               "Passez au centre de sante pour une pesee gratuite de votre enfant.")
        if ab["langue"] == "wo":
            msg = ("MAMAMENU: Am na ay xale yu feebar ci sa gox. "
                   "Demal ci poste de sante bi, nu natt sa doom, amul fey.")
        envoyer_sms(con, ab["telephone"], msg)
    return len(cibles)


# --- Bot SMS entrant ---------------------------------------------------------
RE_INSCRIPTION = re.compile(r"^\s*(\d{1,2})\s+([A-Za-zÉéÈè\- ]{3,})\s*$")

AIDE_FR = ("MAMAMENU: Envoyez AGE REGION (ex: 9 DIOURBEL) pour vous inscrire. "
           "WO=wolof, FR=francais, STOP=arret. 1 recette locale/semaine, <500F.")


def traiter_sms_entrant(con, telephone: str, corps: str) -> str:
    """Webhook Twilio entrant : route le message et retourne la réponse."""
    journaliser_entrant(con, telephone, corps)
    texte = corps.strip().upper()

    if texte == "STOP":
        con.execute("DELETE FROM abonnees WHERE telephone = ?", (telephone,))
        rep = "MAMAMENU: Desinscription confirmee. A bientot."
    elif texte in ("WO", "FR"):
        con.execute("UPDATE abonnees SET langue = ? WHERE telephone = ?",
                    (texte.lower(), telephone))
        rep = ("MAMAMENU: Lakk wi soppi na, jerejef!" if texte == "WO"
               else "MAMAMENU: Langue changee en francais.")
    elif m := RE_INSCRIPTION.match(corps.strip()):
        age, region = int(m.group(1)), m.group(2).strip().title()
        langue = con.execute("SELECT langue FROM abonnees WHERE telephone = ?",
                             (telephone,)).fetchone()
        rep = inscrire_abonnee(con, telephone, age, region,
                               langue["langue"] if langue else "fr")
        return rep  # inscrire_abonnee a déjà envoyé le SMS
    else:
        rep = AIDE_FR

    envoyer_sms(con, telephone, rep)
    return rep
