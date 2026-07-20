"""
main.py — NutriSénégal · point d'entrée FastAPI unique.

Lancement :  uvicorn main:app --reload --port 8000
Démo data :  python seed_demo.py
"""
from __future__ import annotations
import csv
import datetime as dt
import hashlib
import io
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, RedirectResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import db as database
import nutriscan
import mamamenu
import malimap
import sms as sms_mod
from scheduler import demarrer_scheduler

BASE = Path(__file__).parent

# Clé de signature des sessions : variable d'env en production, sinon fichier
# local auto-généré (les sessions survivent aux redémarrages du serveur).
_SECRET_FILE = BASE / ".secret_key"
SECRET = os.environ.get("NUTRISENEGAL_SECRET", "").strip()
if not SECRET:
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    SECRET = _SECRET_FILE.read_text(encoding="utf-8").strip()

app = FastAPI(title="NutriSénégal", version="1.2",
              description="Dépistage · Prévention · Cartographie")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# Version du CSS = hash du fichier : l'URL change à chaque modification,
# le navigateur ne peut plus servir une vieille feuille de style en cache.
try:
    CSS_V = hashlib.md5((BASE / "static" / "style.css").read_bytes()).hexdigest()[:8]
except OSError:
    CSS_V = "1"


def _contexte_agent(request: Request) -> dict:
    """Injecte l'agent connecté + la version du CSS dans tous les templates."""
    sess = request.scope.get("session") or {}
    return {"agent_connecte": sess.get("agent_nom"),
            "agent_role": sess.get("agent_role"),
            "agent_zone": sess.get("agent_zone"),
            "css_v": CSS_V}


templates = Jinja2Templates(directory=BASE / "templates",
                            context_processors=[_contexte_agent])

REGIONS = sorted(malimap.REGIONS.keys())

# ── Contrôle d'accès : données de santé → tout est protégé par défaut ───────
# Restent publics : la connexion/inscription, les fichiers statiques, la sonde
# /health et le webhook Twilio (les SMS entrants n'ont pas de session).
CHEMINS_PUBLICS = ("/connexion", "/inscription", "/deconnexion",
                   "/static/", "/health", "/sms/webhook", "/favicon")

# Périmètre du rôle « agent » (terrain) : dépistage + prévention uniquement.
# Tout le reste (dashboard, suivi des cas, carte, admin) est réservé aux admins.
CHEMINS_AGENT = ("/nutriscan", "/mamamenu", "/api/sms", "/api/campagne-hebdo")


@app.middleware("http")
async def exiger_connexion(request: Request, call_next):
    chemin = request.url.path
    if not chemin.startswith(CHEMINS_PUBLICS):
        sess = request.scope.get("session") or {}
        if not sess.get("agent_id"):
            return RedirectResponse(f"/connexion?suivant={quote(chemin)}",
                                    status_code=303)
        if sess.get("agent_role") != "admin" \
                and not chemin.startswith(CHEMINS_AGENT):
            return RedirectResponse("/nutriscan", status_code=303)
    return await call_next(request)


# Ajouté APRÈS le middleware ci-dessus → s'exécute AVANT lui (pile Starlette) :
# la session est donc toujours disponible au moment du contrôle d'accès.
app.add_middleware(SessionMiddleware, secret_key=SECRET,
                   max_age=8 * 3600, same_site="lax", https_only=False)


@app.on_event("startup")
async def startup():
    database.init_db()
    _charger_recettes()
    demarrer_scheduler()


def _charger_recettes():
    """Importe data/recettes.json en base (idempotent)."""
    with database.db() as con:
        if con.execute("SELECT COUNT(*) FROM recettes").fetchone()[0]:
            return
        recettes = json.loads((BASE / "data" / "recettes.json").read_text(encoding="utf-8"))
        for r in recettes:
            con.execute(
                """INSERT INTO recettes (nom, age_min_mois, age_max_mois, kcal,
                       proteines_g, fer_mg, vit_a_ug, cout_fcfa, saison, regions,
                       ingredients, instructions, sms_fr, sms_wo)
                   VALUES (:nom,:age_min_mois,:age_max_mois,:kcal,:proteines_g,
                           :fer_mg,:vit_a_ug,:cout_fcfa,:saison,:regions,
                           :ingredients,:instructions,:sms_fr,:sms_wo)""", r)


# ── Authentification ─────────────────────────────────────────────────────────
def _suivant_sur(suivant: str) -> str:
    """N'autorise que des redirections internes (anti open-redirect)."""
    return suivant if suivant.startswith("/") and not suivant.startswith("//") else "/"


def _ouvrir_session(request: Request, agent) -> None:
    request.session.clear()                      # nouveau cookie signé
    request.session.update({"agent_id": agent["id"],
                            "agent_nom": agent["nom"],
                            "agent_role": agent["role"] or "agent",
                            "agent_zone": agent["zone"] or agent["region"]})


@app.get("/connexion", response_class=HTMLResponse)
def connexion_form(request: Request, suivant: str = "/"):
    if request.session.get("agent_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "connexion.html",
                                      {"erreur": None, "identifiant": "",
                                       "suivant": _suivant_sur(suivant)})


@app.post("/connexion", response_class=HTMLResponse)
def connexion(request: Request, identifiant: str = Form(...),
              mdp: str = Form(...), suivant: str = Form("/")):
    with database.db() as con:
        agent = auth.authentifier(con, identifiant, mdp)
    if agent is None:
        return templates.TemplateResponse(request, "connexion.html",
            {"erreur": "Identifiant ou mot de passe incorrect.",
             "identifiant": identifiant, "suivant": _suivant_sur(suivant)},
            status_code=401)
    _ouvrir_session(request, agent)
    with database.db() as con:
        database.journaliser(con, agent["id"], "connexion",
                             f"rôle {agent['role'] or 'agent'}")
    cible = _suivant_sur(suivant)
    if (agent["role"] or "agent") != "admin" \
            and not cible.startswith(CHEMINS_AGENT):
        cible = "/nutriscan"
    return RedirectResponse(cible, status_code=303)


@app.get("/inscription", response_class=HTMLResponse)
def inscription_form(request: Request):
    return templates.TemplateResponse(request, "inscription.html",
        {"erreur": None, "regions": REGIONS, "v": {}})


@app.post("/inscription", response_class=HTMLResponse)
def inscription(request: Request, nom: str = Form(...),
                identifiant: str = Form(...), mdp: str = Form(...),
                mdp2: str = Form(...), region: str = Form(...),
                zone: str = Form("")):
    v = {"nom": nom, "identifiant": identifiant, "region": region, "zone": zone}
    erreur = "Les deux mots de passe ne correspondent pas." if mdp != mdp2 else None
    if erreur is None:
        try:
            with database.db() as con:
                aid = auth.creer_agent(con, nom=nom, identifiant=identifiant,
                                       mdp=mdp, region=region, zone=zone)
                agent = con.execute("SELECT * FROM agents WHERE id=?",
                                    (aid,)).fetchone()
        except ValueError as e:
            erreur = str(e)
    if erreur:
        return templates.TemplateResponse(request, "inscription.html",
            {"erreur": erreur, "regions": REGIONS, "v": v}, status_code=400)
    _ouvrir_session(request, agent)
    with database.db() as con:
        database.journaliser(con, agent["id"], "création de compte",
                             f"{agent['nom']} — {agent['region']}")
    return RedirectResponse("/nutriscan", status_code=303)


@app.api_route("/deconnexion", methods=["GET", "POST"])
def deconnexion(request: Request):
    request.session.clear()
    return RedirectResponse("/connexion", status_code=303)


# ── Dashboard ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with database.db() as con:
        stats = {
            "depistages": con.execute("SELECT COUNT(*) FROM enfants").fetchone()[0],
            "rouges": con.execute("SELECT COUNT(*) FROM enfants WHERE score_risque='ROUGE'").fetchone()[0],
            "oranges": con.execute("SELECT COUNT(*) FROM enfants WHERE score_risque='ORANGE'").fetchone()[0],
            "abonnees": con.execute("SELECT COUNT(*) FROM abonnees").fetchone()[0],
            "sms": con.execute("SELECT COUNT(*) FROM sms_log WHERE direction='OUT'").fetchone()[0],
            "cas_actifs": con.execute(
                "SELECT COUNT(*) FROM alertes WHERE statut != 'TRAITE'").fetchone()[0],
            "traites": con.execute(
                "SELECT COUNT(*) FROM alertes WHERE statut = 'TRAITE'").fetchone()[0],
        }
        scores = malimap.scores_par_region(con)
        prealertes = malimap.alertes_predictives(scores)
        derniers = con.execute(
            """SELECT e.prenom, e.age_mois, e.region, e.score_risque,
                      e.date_saisie, ag.nom AS agent
               FROM enfants e LEFT JOIN agents ag ON ag.id = e.agent_id
               ORDER BY e.date_saisie DESC LIMIT 8""").fetchall()
        sms_recents = con.execute(
            "SELECT * FROM sms_log ORDER BY date_sms DESC LIMIT 8").fetchall()
    return templates.TemplateResponse(request, "index.html", { "stats": stats, "scores": scores[:6],
        "prealertes": prealertes, "derniers": derniers,
        "sms_recents": sms_recents, "mode_sms": sms_mod.MODE, "page": "accueil"})


# ── Module A · NutriScan ─────────────────────────────────────────────────────
@app.get("/nutriscan", response_class=HTMLResponse)
def nutriscan_form(request: Request):
    return templates.TemplateResponse(request, "nutriscan.html", { "regions": REGIONS, "resultat": None, "page": "nutriscan"})


@app.post("/nutriscan", response_class=HTMLResponse)
def nutriscan_depister(request: Request,
                       prenom: str = Form(...), sexe: str = Form("M"),
                       age_mois: int = Form(...), region: str = Form(...),
                       poids_kg: float = Form(...), taille_cm: float = Form(...),
                       pb_mm: float = Form(None), oedemes: bool = Form(False)):
    with database.db() as con:
        enfant_id, res = nutriscan.enregistrer_depistage(
            con, prenom=prenom, sexe=sexe, age_mois=age_mois, region=region,
            poids_kg=poids_kg, taille_cm=taille_cm, pb_mm=pb_mm, oedemes=oedemes,
            agent_id=request.session.get("agent_id"))
        database.journaliser(con, request.session.get("agent_id"), "dépistage",
                             f"{prenom} ({age_mois} m, {region}) → {res.niveau}")
        if res.niveau == "ROUGE":
            sms_mod.envoyer_alerte(
                con, {"prenom": prenom, "age_mois": age_mois, "region": region},
                "ROUGE", {"nom": f"CS {region}", "tel": "+221770000000"})
        if res.niveau in ("ROUGE", "ORANGE"):
            mamamenu.intensifier_suivi(con, region, age_mois)
    return templates.TemplateResponse(request, "nutriscan.html", { "regions": REGIONS, "resultat": res,
        "enfant": {"prenom": prenom, "id": enfant_id}, "page": "nutriscan"})


# ── Module B · MamaMenu ──────────────────────────────────────────────────────
@app.get("/mamamenu", response_class=HTMLResponse)
def mamamenu_page(request: Request):
    with database.db() as con:
        abonnees = con.execute(
            "SELECT * FROM abonnees ORDER BY date_inscription DESC LIMIT 20").fetchall()
        recettes = con.execute(
            "SELECT * FROM recettes ORDER BY fer_mg DESC").fetchall()
        sms_log = con.execute(
            "SELECT * FROM sms_log ORDER BY date_sms DESC LIMIT 12").fetchall()
    return templates.TemplateResponse(request, "mamamenu.html", { "abonnees": abonnees, "recettes": recettes,
        "sms_log": sms_log, "mode_sms": sms_mod.MODE, "regions": REGIONS, "page": "mamamenu"})


@app.post("/mamamenu/inscription")
def mamamenu_inscription(request: Request, telephone: str = Form(...),
                         age_enfant_mois: int = Form(...),
                         region: str = Form(...), langue: str = Form("fr")):
    """Inscription détaillée (formulaire agent) : numéro, âge, région, langue.
    Même moteur que le bot SMS — la mère reçoit le SMS de bienvenue."""
    with database.db() as con:
        mamamenu.inscrire_abonnee(con, telephone.strip(), age_enfant_mois,
                                  region, langue)
        tel = telephone.strip()
        database.journaliser(con, request.session.get("agent_id"),
                             "inscription abonnée",
                             f"{tel[:5]}…{tel[-2:]} ({age_enfant_mois} m, {region})")
    return RedirectResponse("/mamamenu", status_code=303)


@app.post("/sms/webhook", response_class=PlainTextResponse)
def sms_webhook(From: str = Form(...), Body: str = Form(...)):
    """Webhook Twilio entrant (format TwiML minimal)."""
    with database.db() as con:
        rep = mamamenu.traiter_sms_entrant(con, From, Body)
    return f'<?xml version="1.0"?><Response><Message>{rep}</Message></Response>'


@app.post("/api/sms/simuler")
def simuler_sms(telephone: str = Form(...), corps: str = Form(...)):
    """Simulateur de téléphone pour la démo (même logique que le webhook)."""
    with database.db() as con:
        rep = mamamenu.traiter_sms_entrant(con, telephone, corps)
    return RedirectResponse("/mamamenu", status_code=303)


@app.post("/api/campagne-hebdo")
def campagne():
    with database.db() as con:
        n = mamamenu.envoyer_campagne_hebdo(con)
    return RedirectResponse(f"/mamamenu?envoyes={n}", status_code=303)


# ── Suivi des cas (NutriScan → résolution) ──────────────────────────────────
STATUTS = ("NOUVEAU", "PRIS_EN_CHARGE", "TRAITE")


@app.get("/cas", response_class=HTMLResponse)
def cas_page(request: Request):
    with database.db() as con:
        cas = con.execute(
            """SELECT a.id, a.score, a.statut, a.date_alerte, a.date_maj,
                      a.traite_par, a.note,
                      e.prenom, e.sexe, e.age_mois, e.region, e.perimetre_brachial
               FROM alertes a JOIN enfants e ON e.id = a.enfant_id
               ORDER BY CASE a.statut WHEN 'NOUVEAU' THEN 0
                                      WHEN 'PRIS_EN_CHARGE' THEN 1 ELSE 2 END,
                        a.date_alerte DESC""").fetchall()
        n = {s: con.execute("SELECT COUNT(*) FROM alertes WHERE statut=?",
                            (s,)).fetchone()[0] for s in STATUTS}
    total = sum(n.values()) or 1
    taux = round(100 * n["TRAITE"] / total)
    return templates.TemplateResponse(request, "cas.html", { "cas": cas, "n": n,
        "taux": taux, "page": "cas"})


@app.post("/cas/{alerte_id}/statut")
def cas_statut(request: Request, alerte_id: int, statut: str = Form(...),
               traite_par: str = Form(None), note: str = Form(None)):
    """Fait avancer (ou rouvre) un cas. La clôture enregistre l'agent du
    centre de santé et une note ; un cas TRAITE sort immédiatement du score
    MaliMap de sa région — les données restent vivantes."""
    if statut not in STATUTS:
        return RedirectResponse("/cas", status_code=303)
    maintenant = dt.datetime.now().isoformat(" ", "seconds")
    with database.db() as con:
        info = con.execute(
            """SELECT e.prenom, e.region FROM alertes a
               JOIN enfants e ON e.id = a.enfant_id WHERE a.id = ?""",
            (alerte_id,)).fetchone()
        qui = f" par {traite_par.strip()}" if (traite_par or "").strip() else ""
        database.journaliser(
            con, request.session.get("agent_id"), "suivi de cas",
            f"{info['prenom']} ({info['region']}) → {statut}{qui}" if info
            else f"cas #{alerte_id} → {statut}")
        if statut == "TRAITE":
            con.execute(
                """UPDATE alertes SET statut=?, date_maj=?,
                       traite_par=?, note=? WHERE id=?""",
                (statut, maintenant, (traite_par or "").strip() or None,
                 (note or "").strip() or None, alerte_id))
        elif statut == "NOUVEAU":  # réouverture : on repart de zéro
            con.execute(
                """UPDATE alertes SET statut=?, date_maj=?,
                       traite_par=NULL, note=NULL WHERE id=?""",
                (statut, maintenant, alerte_id))
        else:
            con.execute(
                "UPDATE alertes SET statut=?, date_maj=? WHERE id=?",
                (statut, maintenant, alerte_id))
    return RedirectResponse("/cas", status_code=303)


# ── Espace admin : activité des agents, rôles, export ───────────────────────
@app.get("/admin/activite", response_class=HTMLResponse)
def admin_activite(request: Request, agent_id: int | None = None):
    with database.db() as con:
        agents = con.execute(
            """SELECT ag.id, ag.nom, ag.identifiant, ag.role, ag.zone, ag.region,
                  (SELECT COUNT(*) FROM enfants e WHERE e.agent_id = ag.id) AS dep,
                  (SELECT COUNT(*) FROM enfants e WHERE e.agent_id = ag.id
                     AND e.score_risque = 'ROUGE') AS rouges,
                  (SELECT MAX(date_action) FROM journal j
                     WHERE j.agent_id = ag.id) AS derniere
               FROM agents ag ORDER BY dep DESC, ag.nom""").fetchall()
        journal = con.execute(
            """SELECT j.action, j.details, j.date_action, ag.nom AS agent
               FROM journal j LEFT JOIN agents ag ON ag.id = j.agent_id
               WHERE (:aid IS NULL OR j.agent_id = :aid)
               ORDER BY j.date_action DESC LIMIT 120""",
            {"aid": agent_id}).fetchall()
    return templates.TemplateResponse(request, "activite.html", { 
        "agents": agents, "journal": journal, "filtre": agent_id,
        "page": "activite"})


@app.post("/admin/agents/{aid}/role")
def admin_role(request: Request, aid: int, role: str = Form(...)):
    """Promotion / rétrogradation. Garde-fou : impossible de se rétrograder
    soi-même (éviter de perdre le dernier accès admin en pleine démo)."""
    if role in ("agent", "admin") and aid != request.session.get("agent_id"):
        with database.db() as con:
            con.execute("UPDATE agents SET role = ? WHERE id = ?", (role, aid))
            nom = con.execute("SELECT nom FROM agents WHERE id = ?",
                              (aid,)).fetchone()
            database.journaliser(con, request.session.get("agent_id"),
                                 "changement de rôle",
                                 f"{nom['nom'] if nom else aid} → {role}")
    return RedirectResponse("/admin/activite", status_code=303)


@app.get("/admin/export/cas.csv")
def admin_export_cas():
    """Export CSV (séparateur ; + BOM → s'ouvre proprement dans Excel FR)
    pour partage avec le Ministère de la Santé / ONG partenaires."""
    with database.db() as con:
        lignes = con.execute(
            """SELECT a.id, e.prenom, e.sexe, e.age_mois, a.region, a.score,
                      a.statut, a.date_alerte, a.date_maj, a.traite_par, a.note
               FROM alertes a JOIN enfants e ON e.id = a.enfant_id
               ORDER BY a.date_alerte DESC""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Cas", "Enfant", "Sexe", "Âge (mois)", "Région", "Gravité",
                "Statut", "Signalé le", "Mis à jour le", "Traité par", "Note"])
    for l in lignes:
        w.writerow([l["id"], l["prenom"], l["sexe"], l["age_mois"], l["region"],
                    l["score"], l["statut"], l["date_alerte"], l["date_maj"],
                    l["traite_par"], l["note"]])
    return Response("\ufeff" + buf.getvalue(),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             'attachment; filename="cas_nutrisenegal.csv"'})


# ── Module C · MaliMap ───────────────────────────────────────────────────────
@app.get("/malimap", response_class=HTMLResponse)
def malimap_page(request: Request):
    with database.db() as con:
        scores = malimap.scores_par_region(con)
        prealertes = malimap.alertes_predictives(scores)
        maj = con.execute("SELECT MAX(maj) FROM contexte_regions").fetchone()[0]
    return templates.TemplateResponse(request, "malimap.html", { "scores": scores, "prealertes": prealertes,
        "maj": maj, "page": "malimap"})


@app.get("/malimap/carte", response_class=HTMLResponse)
def malimap_carte():
    with database.db() as con:
        scores = malimap.scores_par_region(con)
    return malimap.generer_carte(scores)


# ── API JSON & santé ─────────────────────────────────────────────────────────
@app.get("/api/scores")
def api_scores():
    with database.db() as con:
        return malimap.scores_par_region(con)


@app.get("/health")
def health():
    return {"statut": "ok", "service": "NutriSénégal",
            "heure": dt.datetime.now().isoformat(), "sms": sms_mod.MODE}
