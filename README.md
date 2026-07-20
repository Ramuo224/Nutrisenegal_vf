# NutriSénégal 🇸🇳 — Dépistage · Prévention · Cartographie

Plateforme numérique de lutte contre la malnutrition infantile au Sénégal.
Trois modules, **une seule boucle de données** : le terrain (NutriScan), la
famille (MamaMenu) et les décideurs (MaliMap).

## Démarrage en 3 commandes

```bash
pip install -r requirements.txt
python seed_demo.py            # données de démonstration réalistes (90 dépistages)
uvicorn main:app --reload      # → http://localhost:8000
```

Aucune clé d'API requise : sans identifiants Twilio, la passerelle SMS passe
automatiquement en **mode simulation** et les messages s'affichent en direct
dans le dashboard (idéal pour la démo jury). Pour des SMS réels :

```bash
export TWILIO_SID=ACxxxx TWILIO_TOKEN=xxxx TWILIO_NUM=+1xxxx
```

## Le flux complet (à montrer au jury)

0. **Connexion** (`/connexion`) — commencer en **agent** (`awa / demo2026`) :
   la sidebar ne montre que NutriScan et MamaMenu. Se déconnecter, revenir en
   **admin** (`admin / admin2026`) : tout apparaît, dont l'*Activité des
   agents* où l'on voit le dépistage qu'Awa vient de faire. Données de santé
   oblige, rien n'est accessible sans compte.
1. **NutriScan** (`/nutriscan`) — saisir : Awa, 14 mois, Matam, 5,2 kg, 72 cm,
   PB 108 mm → verdict **ROUGE** (protocole OMS : œdèmes → PB → poids-pour-âge),
   alerte SMS instantanée au centre de santé.
2. **MamaMenu** (`/mamamenu`) — le cas ROUGE déclenche le **mode intensif** pour
   les mères de la même zone. Deux portes d'entrée :
   - **Formulaire agent** : numéro de la mère, âge de l'enfant, région, langue
     → SMS de bienvenue + 1ʳᵉ recette, en français ou en wolof ;
   - **Simulateur de téléphone** : envoyer `9 DIOURBEL` → inscription condensée ;
     `WO` → bascule en wolof.
3. **Suivi des cas** (`/cas`) — chaque alerte vit son cycle :
   **NOUVEAU → PRIS EN CHARGE → TRAITÉ**. La clôture est **tracée** : nom de
   l'agent du centre de santé + note (ex. *« PB remonté à 128 mm, sortie
   guérie »*) ; rouvrir un cas efface la clôture. Marquer un cas traité le
   retire *en direct* du score MaliMap de sa région → les données ne sont
   jamais statiques, la carte récompense l'action de terrain.
4. **MaliMap** (`/malimap`) — seuls les **cas actifs** pèsent dans le score,
   croisés avec la pluviométrie Open-Meteo (rafraîchie toutes les 3 h) et les
   prix alimentaires → **pré-alertes prédictives** avant l'explosion des cas.

> Démo qui marque : ouvrir `/malimap`, noter le score de Matam, traiter un cas
> sur `/cas`, recharger la carte — le score a baissé.

## Connexion & protection des données

Les dépistages sont des **données de santé nominatives** : toute la plateforme
est derrière une **page de connexion** (`/connexion`), et l'accès est cloisonné
par **rôle** :

| | Agent (terrain) | Admin (coordination) |
|---|:---:|:---:|
| NutriScan (dépistage) | ✔ | ✔ |
| MamaMenu (prévention SMS) | ✔ | ✔ |
| Tableau de bord | — | ✔ |
| Suivi des cas | — | ✔ |
| MaliMap (carte) | — | ✔ |
| Activité des agents (journal) | — | ✔ |
| Export CSV / gestion des rôles | — | ✔ |

Un agent qui tente d'ouvrir une page hors périmètre est renvoyé sur NutriScan.
L'inscription publique (`/inscription`) ne crée que des comptes **agent** ;
les rôles admin s'attribuent depuis la page *Activité des agents*.

**Journal d'activité** (`/admin/activite`) : chaque connexion, dépistage
(avec verdict), inscription d'abonnée et clôture de cas est tracée — l'admin
voit qui a fait quoi, quand, avec stats par agent et filtre.

Comptes de démonstration (créés par `seed_demo.py`) :

| Identifiant | Mot de passe | Rôle | Zone |
|---|---|---|---|
| `awa` | `demo2026` | agent | Touba (Diourbel) |
| `moussa` | `demo2026` | agent | Ourossogui (Matam) |
| `admin` | `admin2026` | **admin** | Coordination nationale |

Sécurité mise en œuvre :
- Mots de passe **hachés PBKDF2-HMAC-SHA256, 600 000 itérations** (reco OWASP),
  sel aléatoire — jamais stockés en clair (100 % bibliothèque standard Python) ;
- Sessions par **cookies signés** (HttpOnly, SameSite=Lax, expiration 8 h) ;
- Comparaisons en **temps constant** — pas d'énumération d'identifiants ;
- Chaque dépistage NutriScan est **signé par l'agent connecté** (imputabilité) ;
- Restent publics : `/health` (supervision) et `/sms/webhook` (Twilio).
- Clé de session : variable d'env `NUTRISENEGAL_SECRET` en production
  (sinon fichier `.secret_key` auto-généré).

Pour la production : passer `https_only=True` (cookies), servir derrière HTTPS,
et ajouter des jetons CSRF — noté dans la feuille de route.

## Moteur de scoring (NutriScan)

Classification par le **pire indicateur** (principe de précaution) :

| Étape | Indicateur | Seuils |
|---|---|---|
| 1 | Œdèmes bilatéraux | présents → ROUGE (kwashiorkor) |
| 2 | Périmètre brachial (6–59 mois) | < 115 mm → ROUGE · < 125 mm → ORANGE |
| 3 | Poids-pour-âge (% médiane OMS, interpolée par sexe) | < 60 % → ROUGE · < 75 % → ORANGE |
| 4 | Filet IMC pédiatrique | < 11,5 → ROUGE · < 13 → ORANGE |

Chaque indicateur calculé est conservé en JSON dans la fiche (traçabilité).

## Score régional (MaliMap)

`0–100 = pluie(30) + prix(20) + cas terrain(30) + pauvreté(10) − densité médicale(5) − accès eau(5)`

Niveaux : FAIBLE < 20 ≤ MODÉRÉ < 40 ≤ ÉLEVÉ < 60 ≤ CRITIQUE.
Pré-alerte prédictive si déficit pluie ≥ 25 % **et** prix +8 %.

**Choroplèthe** : déposer un GeoJSON des 14 régions dans
`data/sen_adm1.geojson` (ex. geoBoundaries SEN ADM1) — la carte passe
automatiquement en mode surfaces colorées ; sinon, symboles proportionnels.

## Architecture

```
FastAPI (main.py)
├── nutriscan.py   moteur OMS + enregistrement + alertes
├── mamamenu.py    recommandation (densité nutritionnelle/coût) + bot SMS fr/wo
├── malimap.py     score composite + carte Folium
├── scheduler.py   Open-Meteo + prix, toutes les 3 h (repli hors ligne)
├── sms.py         Twilio ↔ simulation transparente
├── db.py          SQLite WAL, 8 tables, index
└── data/          17 recettes annotées · 14 régions · prix de repli
```

Déployable sur un Raspberry Pi ou un VPS à 5 $/mois. API JSON : `/api/scores`, `/health`.
