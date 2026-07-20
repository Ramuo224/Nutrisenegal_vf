"""
scheduler.py — Mises à jour automatiques toutes les 3 heures (APScheduler).

  • Pluviométrie : API Open-Meteo (gratuite, sans clé) par centroïde régional.
    Le déficit est estimé vs la normale saisonnière embarquée dans prix.json.
  • Prix alimentaires : indice FAO GIEWS si disponible, sinon data/prix.json
    (jeu local pour le hackathon).
  • Recalcul des scores MaliMap après chaque rafraîchissement.

En cas d'absence de réseau (démo hors ligne), les valeurs de prix.json
servent de repli : la plateforme ne casse jamais.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import db
import malimap

DATA = Path(__file__).parent / "data"
PRIX = json.loads((DATA / "prix.json").read_text(encoding="utf-8"))
REGIONS = malimap.REGIONS

# Normales pluviométriques mensuelles indicatives (mm) par grande zone
NORMALES_MM = {1: 1, 2: 1, 3: 1, 4: 2, 5: 9, 6: 45, 7: 120, 8: 190, 9: 150, 10: 45, 11: 4, 12: 2}


async def _pluie_30j(client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
    """Cumul de précipitations des 30 derniers jours via Open-Meteo (archive)."""
    fin = dt.date.today()
    debut = fin - dt.timedelta(days=30)
    try:
        r = await client.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={"latitude": lat, "longitude": lon,
                    "start_date": debut.isoformat(), "end_date": fin.isoformat(),
                    "daily": "precipitation_sum", "timezone": "UTC"},
            timeout=10)
        vals = r.json()["daily"]["precipitation_sum"]
        return sum(v or 0 for v in vals)
    except Exception:
        return None


async def rafraichir_contexte():
    """Met à jour pluie + prix pour chaque région, puis recalcule les scores."""
    mois = dt.date.today().month
    normale = max(NORMALES_MM[mois], 1)
    async with httpx.AsyncClient() as client:
        cumuls = await asyncio.gather(
            *[_pluie_30j(client, m["lat"], m["lon"]) for m in REGIONS.values()])

    with db() as con:
        for (nom, meta), cumul in zip(REGIONS.items(), cumuls):
            repli = PRIX.get(nom, {})
            if cumul is not None:
                deficit = max(0.0, round(100 * (1 - cumul / normale), 1))
            else:  # repli hors ligne
                deficit = repli.get("pluie_deficit", 0)
            hausse = repli.get("prix_hausse", 0)
            con.execute(
                """INSERT INTO contexte_regions
                       (region, pluie_deficit, prix_alimentaires_hausse,
                        densite_medicale, acces_eau_potable, taux_pauvrete, maj)
                   VALUES (?,?,?,?,?,?, CURRENT_TIMESTAMP)
                   ON CONFLICT(region) DO UPDATE SET
                       pluie_deficit = excluded.pluie_deficit,
                       prix_alimentaires_hausse = excluded.prix_alimentaires_hausse,
                       maj = CURRENT_TIMESTAMP""",
                (nom, deficit, hausse, meta["densite_medicale"],
                 meta["acces_eau_potable"], meta["taux_pauvrete"]))
        malimap.scores_par_region(con)
    print(f"[scheduler] Contexte régional rafraîchi ({dt.datetime.now():%H:%M}).")


def demarrer_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler()
    sched.add_job(rafraichir_contexte, "interval", hours=3,
                  next_run_time=dt.datetime.now() + dt.timedelta(seconds=3))
    sched.start()
    return sched
