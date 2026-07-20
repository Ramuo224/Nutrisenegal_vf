"""
test_nutrisenegal.py — Suite de tests bout-en-bout (rôles inclus).

Prérequis :
  1. python seed_demo.py     (comptes : awa/demo2026, admin/admin2026)
  2. python -m uvicorn main:app
  3. python test_nutrisenegal.py
"""
import httpx

BASE = "http://127.0.0.1:8000"
agent = httpx.Client(base_url=BASE, follow_redirects=True, timeout=20)
admin = httpx.Client(base_url=BASE, follow_redirects=True, timeout=20)


def test_health_public():
    r = httpx.get(f"{BASE}/health")
    assert r.status_code == 200 and r.json()["statut"] == "ok"
    print("✅ /health accessible sans connexion")


def test_protection_sans_session():
    r = httpx.get(f"{BASE}/", follow_redirects=True)
    assert "/connexion" in str(r.url)
    print("✅ Dashboard protégé : redirection vers /connexion")


def test_connexion_agent():
    r = agent.post("/connexion", data={"identifiant": "awa",
                                       "mdp": "demo2026", "suivant": "/"})
    assert r.status_code == 200 and "Dépistage" in r.text
    assert "/nutriscan" in str(r.url)
    print("✅ Agent connecté → atterrit sur NutriScan (pas le dashboard)")


def test_mauvais_mdp():
    r = httpx.post(f"{BASE}/connexion",
                   data={"identifiant": "awa", "mdp": "mauvais", "suivant": "/"})
    assert r.status_code == 401 and "incorrect" in r.text
    print("✅ Mauvais mot de passe refusé (401)")


def test_agent_bloque_hors_perimetre():
    for chemin in ("/", "/cas", "/malimap", "/admin/activite"):
        r = agent.get(chemin)
        assert "/nutriscan" in str(r.url), chemin
    print("✅ Agent bloqué sur dashboard, cas, carte et admin → renvoyé NutriScan")


def test_depistage_signe():
    r = agent.post("/nutriscan", data={
        "prenom": "Aminata", "age_mois": 18, "sexe": "F",
        "poids_kg": 6.0, "taille_cm": 78, "pb_mm": 110, "region": "Matam"})
    assert r.status_code == 200 and "ROUGE" in r.text
    print("✅ Dépistage ROUGE signé par l'agent connecté")


def test_sms_agent():
    r = agent.post("/api/sms/simuler",
                   data={"telephone": "+221777000001", "corps": "9 DIOURBEL"})
    assert r.status_code == 200
    print("✅ Bot SMS accessible au rôle agent")


def test_connexion_admin():
    r = admin.post("/connexion", data={"identifiant": "admin",
                                       "mdp": "admin2026", "suivant": "/"})
    assert r.status_code == 200 and "Tableau de bord" in r.text
    print("✅ Admin connecté → dashboard complet")


def test_admin_activite_et_journal():
    r = admin.get("/admin/activite")
    assert r.status_code == 200 and "Journal d'activité" in r.text
    assert "dépistage" in r.text
    print("✅ Page Activité : journal des actions par agent visible")


def test_scores_admin():
    r = admin.get("/api/scores")
    scores = r.json()
    assert len(scores) == 14
    print(f"✅ MaliMap (admin) : 14 régions — n°1 : {scores[0]['region']}")


def test_export_csv():
    r = admin.get("/admin/export/cas.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "Gravité" in r.text
    print("✅ Export CSV des cas (Ministère / ONG)")


if __name__ == "__main__":
    tests = [test_health_public, test_protection_sans_session,
             test_connexion_agent, test_mauvais_mdp,
             test_agent_bloque_hors_perimetre, test_depistage_signe,
             test_sms_agent, test_connexion_admin,
             test_admin_activite_et_journal, test_scores_admin,
             test_export_csv]
    ok = 0
    for t in tests:
        try:
            t(); ok += 1
        except Exception as e:
            print(f"❌ {t.__name__} ÉCHOUÉ : {e}")
    print("=" * 46)
    print(f"Résultat : {ok}/{len(tests)} tests passés")
