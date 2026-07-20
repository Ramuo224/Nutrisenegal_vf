"""
seed_demo.py — Génère un jeu de données de démonstration réaliste.

  • 90 dépistages NutriScan sur 30 jours, avec une prévalence plus forte
    dans les régions critiques (Diourbel, Tambacounda, Matam) ;
  • 12 mères abonnées MamaMenu (fr/wo) ;
  • contexte régional initial (pluie/prix de repli) ;
  • alertes + SMS simulés cohérents.

Usage : python seed_demo.py
"""
import datetime as dt
import json
import random
from pathlib import Path

from db import db, init_db
import auth
import nutriscan
import mamamenu
from sms import envoyer_sms

random.seed(221)
DATA = Path(__file__).parent / "data"
REGIONS = json.loads((DATA / "regions.json").read_text(encoding="utf-8"))
PRIX = json.loads((DATA / "prix.json").read_text(encoding="utf-8"))

PRENOMS_F = ["Awa", "Fatou", "Aïssatou", "Mariama", "Khady", "Ndeye", "Sokhna", "Adja", "Bineta", "Coumba"]
PRENOMS_M = ["Moussa", "Ibrahima", "Ousmane", "Mamadou", "Cheikh", "Abdou", "Modou", "Pape", "Serigne", "Lamine"]
CRITIQUES = {"Diourbel": .38, "Tambacounda": .42, "Matam": .40, "Kaffrine": .30,
             "Sédhiou": .28, "Kolda": .26, "Kédougou": .25}


def poids_pour_profil(sexe, age, profil: str):
    """profil : 'sain' | 'modere' | 'severe'."""
    med = nutriscan.mediane_poids_age(sexe, age)
    f = {"severe": random.uniform(.50, .58),
         "modere": random.uniform(.63, .73),
         "sain": random.uniform(.85, 1.12)}[profil]
    return round(med * f, 1)


def main():
    init_db()
    with db() as con:
        # Ordre imposé par les clés étrangères :
        # journal et enfants référencent agents ; alertes référence enfants.
        con.execute("DELETE FROM journal")
        con.execute("DELETE FROM alertes"); con.execute("DELETE FROM enfants")
        con.execute("DELETE FROM abonnees"); con.execute("DELETE FROM sms_log")
        con.execute("DELETE FROM agents")

        # Comptes de démonstration
        agents_ids = [
            auth.creer_agent(con, nom="Awa Diallo", identifiant="awa",
                             mdp="demo2026", region="Diourbel", zone="Touba"),
            auth.creer_agent(con, nom="Moussa Sarr", identifiant="moussa",
                             mdp="demo2026", region="Matam", zone="Ourossogui"),
        ]
        auth.creer_agent(con, nom="Dr Aïssatou Kane", identifiant="admin",
                         mdp="admin2026", region="Dakar",
                         zone="Coordination nationale", role="admin")

        # Contexte régional initial (repli prix.json)
        for nom, meta in REGIONS.items():
            p = PRIX[nom]
            con.execute(
                """INSERT OR REPLACE INTO contexte_regions
                   (region, pluie_deficit, prix_alimentaires_hausse,
                    densite_medicale, acces_eau_potable, taux_pauvrete)
                   VALUES (?,?,?,?,?,?)""",
                (nom, p["pluie_deficit"], p["prix_hausse"],
                 meta["densite_medicale"], meta["acces_eau_potable"], meta["taux_pauvrete"]))

        # 12 abonnées MamaMenu
        for i in range(12):
            region = random.choice(list(REGIONS))
            con.execute(
                """INSERT INTO abonnees (telephone, age_enfant_mois, region, langue)
                   VALUES (?,?,?,?)""",
                (f"+2217712345{i:02d}", random.randint(6, 48), region,
                 random.choice(["fr", "fr", "wo"])))

        # 90 dépistages sur 30 jours
        nb_rouge = nb_orange = 0
        for _ in range(90):
            region = random.choice(list(REGIONS))
            p_maln = CRITIQUES.get(region, .10)
            if random.random() < p_maln:
                profil = "severe" if random.random() < .35 else "modere"
            else:
                profil = "sain"
            sexe = random.choice("MF")
            prenom = random.choice(PRENOMS_F if sexe == "F" else PRENOMS_M)
            age = random.randint(6, 59)
            poids = poids_pour_profil(sexe, age, profil)
            taille = round(55 + age * 0.95 + random.uniform(-3, 3), 1)
            pb = round({"severe": random.uniform(98, 113),
                        "modere": random.uniform(116, 124),
                        "sain": random.uniform(127, 165)}[profil], 0)
            oed = profil == "severe" and random.random() < .15

            eid, res = nutriscan.enregistrer_depistage(
                con, prenom=prenom, sexe=sexe, age_mois=age, region=region,
                poids_kg=poids, taille_cm=taille, pb_mm=pb, oedemes=oed,
                agent_id=random.choice(agents_ids))
            # Antidater pour étaler sur 30 jours
            quand = (dt.datetime.now() - dt.timedelta(days=random.uniform(0, 30))).isoformat(" ", "seconds")
            con.execute("UPDATE enfants SET date_saisie = ? WHERE id = ?", (quand, eid))
            con.execute("UPDATE alertes SET date_alerte = ? WHERE enfant_id = ?", (quand, eid))

            if res.niveau == "ROUGE":
                nb_rouge += 1
                envoyer_sms(con, "+221770000000",
                            f"NUTRISCAN ALERTE ROUGE: {prenom} ({age}m), {region}. "
                            f"PB {pb:.0f}mm. Referer en urgence au CS {region}.")
            elif res.niveau == "ORANGE":
                nb_orange += 1

        # Cycle de vie réaliste des cas : plus une alerte est ancienne, plus
        # elle a de chances d'avoir été prise en charge puis traitée.
        AGENTS_CS = ["Dr Ndiaye", "Dr Fall", "Inf. Diop", "Inf. Ba",
                     "Sage-femme Sarr", "Inf. Gueye", "Dr Sow", "Inf. Cissé"]
        NOTES_CLOTURE = [
            "Réhabilitation CRENAS terminée, PB remonté à 128 mm.",
            "Sortie guérie après 3 semaines de suivi, poids +1,2 kg.",
            "ATPE 4 semaines, courbe de poids normalisée.",
            "Référée au CREN régional, sortie confirmée guérie.",
            "Suivi PCMA achevé, mère inscrite à MamaMenu.",
            "Récupération complète, contrôle PB dans 1 mois.",
        ]
        n_statuts = {"NOUVEAU": 0, "PRIS_EN_CHARGE": 0, "TRAITE": 0}
        for a in con.execute("SELECT id, date_alerte FROM alertes").fetchall():
            anciennete = (dt.datetime.now()
                          - dt.datetime.fromisoformat(a["date_alerte"])).days
            r = random.random()
            if anciennete > 14:
                statut = "TRAITE" if r < .72 else "PRIS_EN_CHARGE"
            elif anciennete > 5:
                statut = ("TRAITE" if r < .35 else
                          "PRIS_EN_CHARGE" if r < .75 else "NOUVEAU")
            else:
                statut = "NOUVEAU" if r < .65 else "PRIS_EN_CHARGE"
            if statut != "NOUVEAU":
                maj = (dt.datetime.fromisoformat(a["date_alerte"])
                       + dt.timedelta(days=random.uniform(1, 4)))
                traite_par = note = None
                if statut == "TRAITE":
                    traite_par = random.choice(AGENTS_CS)
                    note = random.choice(NOTES_CLOTURE)
                con.execute(
                    """UPDATE alertes SET statut=?, date_maj=?,
                           traite_par=?, note=? WHERE id=?""",
                    (statut, maj.isoformat(" ", "seconds"),
                     traite_par, note, a["id"]))
            n_statuts[statut] += 1

        # Journal d'activité rétroactif : chaque dépistage seedé y figure
        con.execute(
            """INSERT INTO journal (agent_id, action, details, date_action)
               SELECT agent_id, 'dépistage',
                      prenom || ' (' || age_mois || ' m, ' || region || ') → '
                             || score_risque,
                      date_saisie
               FROM enfants WHERE agent_id IS NOT NULL""")

        # Quelques échanges SMS de démonstration
        mamamenu.traiter_sms_entrant(con, "+221779990001", "9 Diourbel")
        mamamenu.traiter_sms_entrant(con, "+221779990001", "WO")
        mamamenu.traiter_sms_entrant(con, "+221779990002", "14 Matam")
        mamamenu.envoyer_campagne_hebdo(con)

    print(f"Démo prête : 90 dépistages ({nb_rouge} ROUGE, {nb_orange} ORANGE), "
          f"14 abonnées, contexte régional chargé.\n"
          f"Suivi des cas : {n_statuts['NOUVEAU']} nouveaux, "
          f"{n_statuts['PRIS_EN_CHARGE']} pris en charge, "
          f"{n_statuts['TRAITE']} traités.\n"
          f"Comptes : awa / demo2026 (agent) · moussa / demo2026 (agent) · "
          f"admin / admin2026 (ADMIN)")


if __name__ == "__main__":
    main()
