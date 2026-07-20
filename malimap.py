"""
malimap.py — Carte de risque nutritionnel en temps réel (Module C).

Score composite 0–100 par région, croisant :
  + déficit pluviométrique (Open-Meteo)        max 30 pts
  + hausse des prix alimentaires (FAO GIEWS)   max 20 pts
  + cas NutriScan sur 30 jours (terrain)       max 30 pts
  + taux de pauvreté (ANSD)                    max 10 pts
  − densité médicale                           bonus jusqu'à −10
  − accès à l'eau potable                      bonus jusqu'à −10

Rendu : carte Folium à symboles proportionnels (centroïdes régionaux).
Si data/sen_adm1.geojson est présent, une choroplèthe est superposée
automatiquement (mode "carte pleine" pour la finale).
"""
from __future__ import annotations
import json
from pathlib import Path

import folium

DATA = Path(__file__).parent / "data"
REGIONS = json.loads((DATA / "regions.json").read_text(encoding="utf-8"))
GEOJSON = DATA / "sen_adm1.geojson"

COULEURS = {"CRITIQUE": "#C8102E", "ELEVE": "#E87722", "MODERE": "#F2A900", "FAIBLE": "#00853F"}


def calculer_score_risque(region: str, data: dict) -> float:
    """Score composite (0–100) pour une région. Plus haut = plus de risque."""
    score = 0.0
    score += min(max(data.get("pluie_deficit", 0), 0) * 0.6, 30)             # max 30
    score += min(max(data.get("prix_alimentaires_hausse", 0), 0) * 1.2, 20)  # max 20
    score += min(data.get("cas_nutriscan_30j", 0) * 2.5, 30)                 # max 30
    score += min(max(data.get("taux_pauvrete", 0), 0) * 0.15, 10)            # max 10
    score -= min(data.get("densite_medicale", 0) * 0.5, 5)                   # bonus
    score -= min(data.get("acces_eau_potable", 0) * 0.05, 5)                 # bonus
    return round(max(0.0, min(100.0, score)), 1)


def niveau(score: float) -> str:
    if score >= 60: return "CRITIQUE"
    if score >= 40: return "ELEVE"
    if score >= 20: return "MODERE"
    return "FAIBLE"


def scores_par_region(con) -> list[dict]:
    """Agrège contexte + cas terrain et calcule le score de chaque région."""
    # Cas ACTIFS uniquement : un cas marqué TRAITE sur la page Suivi des cas
    # sort du calcul → la carte reflète la situation réelle, pas l'historique.
    cas = dict(con.execute(
        """SELECT region, COUNT(*) FROM alertes
           WHERE statut != 'TRAITE'
             AND date_alerte >= datetime('now', '-30 days')
           GROUP BY region""").fetchall())
    ctx = {r["region"]: dict(r) for r in
           con.execute("SELECT * FROM contexte_regions").fetchall()}

    resultats = []
    for nom, meta in REGIONS.items():
        d = ctx.get(nom, {})
        d["cas_nutriscan_30j"] = cas.get(nom, 0)
        d.setdefault("taux_pauvrete", meta.get("taux_pauvrete", 0))
        d.setdefault("densite_medicale", meta.get("densite_medicale", 0))
        d.setdefault("acces_eau_potable", meta.get("acces_eau_potable", 0))
        s = calculer_score_risque(nom, d)
        resultats.append({
            "region": nom, "score": s, "niveau": niveau(s),
            "cas_30j": d["cas_nutriscan_30j"],
            "pluie_deficit": round(d.get("pluie_deficit", 0), 1),
            "prix_hausse": round(d.get("prix_alimentaires_hausse", 0), 1),
            "lat": meta["lat"], "lon": meta["lon"],
            "population": meta.get("population", 0),
        })
        con.execute("INSERT OR REPLACE INTO scores_regions (region, score, niveau) VALUES (?,?,?)",
                    (nom, s, niveau(s)))
    return sorted(resultats, key=lambda r: r["score"], reverse=True)


def generer_carte(scores: list[dict]) -> str:
    """Carte Folium (HTML autonome, intégrée en iframe dans le dashboard)."""
    m = folium.Map(location=[14.4, -14.5], zoom_start=7, tiles="cartodbpositron",
                   attr="NutriSénégal — CartoDB")

    # Choroplèthe automatique si un GeoJSON des régions valide est disponible
    if GEOJSON.exists():
        try:
            geo = json.loads(GEOJSON.read_text(encoding="utf-8"))
            assert geo.get("features")
        except Exception:
            geo = None
        if geo:
            idx = {s["region"]: s for s in scores}
            def style(feat):
                nom = feat["properties"].get("shapeName") or feat["properties"].get("name", "")
                s = idx.get(nom)
                return {"fillColor": COULEURS[s["niveau"]] if s else "#cccccc",
                        "fillOpacity": 0.55, "color": "#ffffff", "weight": 1.2}
            folium.GeoJson(geo, style_function=style).add_to(m)

    for s in scores:
        rayon = 8 + s["score"] * 0.45
        folium.CircleMarker(
            location=[s["lat"], s["lon"]], radius=rayon,
            color=COULEURS[s["niveau"]], fill=True,
            fill_color=COULEURS[s["niveau"]], fill_opacity=0.75, weight=2,
            tooltip=f"{s['region']} — score {s['score']} ({s['niveau']})",
            popup=folium.Popup(
                f"<b style='font-size:14px'>{s['region']}</b><br>"
                f"Score de risque : <b>{s['score']}/100</b> ({s['niveau']})<br>"
                f"Cas actifs (30 j) : <b>{s['cas_30j']}</b><br>"
                f"Déficit pluie : {s['pluie_deficit']}%<br>"
                f"Hausse prix : {s['prix_hausse']}%", max_width=260),
        ).add_to(m)
        folium.map.Marker(
            [s["lat"], s["lon"]],
            icon=folium.DivIcon(html=(
                f"<div style='font:600 10px Inter,sans-serif;color:#1B2A33;"
                f"transform:translate(-50%,{int(rayon)+6}px);text-align:center;"
                f"white-space:nowrap'>{s['region']}</div>"), icon_size=(0, 0)),
        ).add_to(m)

    legende = """
    <div style="position:fixed;bottom:18px;left:18px;z-index:9999;background:#fff;
         padding:10px 14px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.18);
         font:12px Inter,sans-serif;line-height:1.9">
      <b>Risque nutritionnel</b><br>
      <span style="color:#C8102E">●</span> Critique (≥60) &nbsp;
      <span style="color:#E87722">●</span> Élevé (40–59)<br>
      <span style="color:#F2A900">●</span> Modéré (20–39) &nbsp;
      <span style="color:#00853F">●</span> Faible (&lt;20)
    </div>"""
    m.get_root().html.add_child(folium.Element(legende))
    return m.get_root().render()


def alertes_predictives(scores: list[dict]) -> list[str]:
    """Anticipe la dégradation : pluie en baisse + prix en hausse = pré-alerte,
    même si les cas terrain n'ont pas encore explosé."""
    msgs = []
    for s in scores:
        if s["pluie_deficit"] >= 25 and s["prix_hausse"] >= 8 and s["niveau"] in ("MODERE", "ELEVE", "CRITIQUE"):
            msgs.append(
                f"PRÉ-ALERTE {s['region']} : déficit pluviométrique {s['pluie_deficit']}% "
                f"+ prix alimentaires +{s['prix_hausse']}% → risque de dégradation "
                f"nutritionnelle sous 4–8 semaines. Renforcer le dépistage NutriScan.")
    return msgs
