"""
nutriscan.py — Moteur de dépistage nutritionnel (Module A).

Protocole de classification aligné sur le dépistage communautaire OMS/UNICEF :
  1. Œdèmes bilatéraux         → ROUGE immédiat (malnutrition aiguë sévère)
  2. Périmètre brachial (PB)   → critère principal terrain :
        PB < 115 mm  → ROUGE (MAS)   |   115 ≤ PB < 125 mm → ORANGE (MAM)
  3. Poids-pour-âge (% médiane OMS, interpolation linéaire par sexe)
        < 60 %  → ROUGE   |   60–75 % → ORANGE      (classification de Gomez)
  4. Indice poids/taille rapide en filet de sécurité.

Le verdict retenu est le PIRE des indicateurs (principe de précaution),
et chaque indicateur est retourné en détail pour la traçabilité médicale.
"""
from __future__ import annotations
import json
from bisect import bisect_left
from dataclasses import dataclass, field, asdict

# Médianes OMS poids-pour-âge (kg) — points d'ancrage, interpolation linéaire.
# Source : WHO Child Growth Standards (valeurs médianes arrondies).
_WFA_MEDIANE = {
    "M": [(0, 3.3), (3, 6.4), (6, 7.9), (9, 8.9), (12, 9.6), (18, 10.9),
          (24, 12.2), (36, 14.3), (48, 16.3), (60, 18.3)],
    "F": [(0, 3.2), (3, 5.8), (6, 7.3), (9, 8.2), (12, 8.9), (18, 10.2),
          (24, 11.5), (36, 13.9), (48, 16.1), (60, 18.2)],
}

NIVEAUX = {"VERT": 0, "ORANGE": 1, "ROUGE": 2}
_ORDRE = {v: k for k, v in NIVEAUX.items()}


def mediane_poids_age(sexe: str, age_mois: int) -> float:
    """Médiane OMS poids-pour-âge interpolée linéairement."""
    pts = _WFA_MEDIANE.get(sexe, _WFA_MEDIANE["M"])
    ages = [a for a, _ in pts]
    i = bisect_left(ages, age_mois)
    if i == 0:
        return pts[0][1]
    if i >= len(pts):
        return pts[-1][1]
    (a0, p0), (a1, p1) = pts[i - 1], pts[i]
    return p0 + (p1 - p0) * (age_mois - a0) / (a1 - a0)


@dataclass
class ResultatDepistage:
    niveau: str                 # VERT / ORANGE / ROUGE
    message: str
    indicateurs: dict = field(default_factory=dict)
    conduite: str = ""

    def json_details(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def score_risque(poids_kg: float, taille_cm: float, age_mois: int,
                 sexe: str = "M", pb_mm: float | None = None,
                 oedemes: bool = False) -> ResultatDepistage:
    """Classifie un enfant selon le pire indicateur (principe de précaution)."""
    verdicts: list[tuple[str, str]] = []
    ind: dict = {}

    # 1) Œdèmes bilatéraux = MAS quel que soit le reste
    if oedemes:
        verdicts.append(("ROUGE", "Œdèmes bilatéraux (signe de kwashiorkor)"))
        ind["oedemes"] = True

    # 2) Périmètre brachial — critère communautaire OMS (6–59 mois)
    if pb_mm and age_mois >= 6:
        ind["pb_mm"] = round(pb_mm, 1)
        if pb_mm < 115:
            verdicts.append(("ROUGE", f"PB {pb_mm:.0f} mm < 115 mm (MAS)"))
        elif pb_mm < 125:
            verdicts.append(("ORANGE", f"PB {pb_mm:.0f} mm < 125 mm (MAM)"))
        else:
            verdicts.append(("VERT", f"PB {pb_mm:.0f} mm normal"))

    # 3) Poids-pour-âge en % de la médiane OMS (Gomez)
    med = mediane_poids_age(sexe, age_mois)
    pct = round(100 * poids_kg / med, 1)
    ind["poids_pct_mediane_oms"] = pct
    ind["mediane_oms_kg"] = round(med, 2)
    if pct < 60:
        verdicts.append(("ROUGE", f"Poids = {pct}% de la médiane OMS (< 60%)"))
    elif pct < 75:
        verdicts.append(("ORANGE", f"Poids = {pct}% de la médiane OMS (< 75%)"))
    else:
        verdicts.append(("VERT", f"Poids = {pct}% de la médiane OMS"))

    # 4) Filet de sécurité poids/taille (IMC pédiatrique indicatif)
    if taille_cm > 0:
        imc = round(poids_kg / (taille_cm / 100) ** 2, 1)
        ind["imc"] = imc
        if imc < 11.5:
            verdicts.append(("ROUGE", f"IMC {imc} extrêmement bas"))
        elif imc < 13.0:
            verdicts.append(("ORANGE", f"IMC {imc} bas"))

    pire = max(verdicts, key=lambda v: NIVEAUX[v[0]])
    niveau = pire[0]
    raisons = " · ".join(m for n, m in verdicts if NIVEAUX[n] == NIVEAUX[niveau])

    conduites = {
        "ROUGE": "Référer IMMÉDIATEMENT au centre de récupération nutritionnelle. "
                 "Alerte SMS envoyée au centre de santé le plus proche.",
        "ORANGE": "Suivi renforcé : pesée hebdomadaire + conseils MamaMenu quotidiens. "
                  "Recontrôle du PB sous 14 jours.",
        "VERT": "Statut nutritionnel normal. Poursuivre l'allaitement/diversification "
                "et les recettes hebdomadaires MamaMenu.",
    }
    return ResultatDepistage(niveau=niveau, message=raisons,
                             indicateurs=ind, conduite=conduites[niveau])


def enregistrer_depistage(con, *, prenom, sexe, age_mois, region, poids_kg,
                          taille_cm, pb_mm=None, oedemes=False, agent_id=None
                          ) -> tuple[int, ResultatDepistage]:
    """Calcule le score, enregistre l'enfant et crée l'alerte si nécessaire."""
    res = score_risque(poids_kg, taille_cm, age_mois, sexe, pb_mm, oedemes)
    cur = con.execute(
        """INSERT INTO enfants (prenom, sexe, age_mois, region, poids_kg,
               taille_cm, perimetre_brachial, oedemes, score_risque, score_details, agent_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (prenom, sexe, age_mois, region, poids_kg, taille_cm, pb_mm,
         int(oedemes), res.niveau, res.json_details(), agent_id))
    enfant_id = cur.lastrowid
    if res.niveau in ("ROUGE", "ORANGE"):
        con.execute(
            "INSERT INTO alertes (enfant_id, region, score) VALUES (?,?,?)",
            (enfant_id, region, res.niveau))
    return enfant_id, res
