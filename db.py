"""
db.py — Connexion SQLite + création des tables.
Base unique partagée par les 3 modules (NutriScan, MamaMenu, MaliMap).
WAL activé pour supporter écritures terrain + lectures dashboard simultanées.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "nutrisenegal.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Module A : NutriScan ------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY,
    nom           TEXT NOT NULL,
    telephone     TEXT,
    zone          TEXT,
    region        TEXT,
    identifiant   TEXT,                 -- login (unique, cf. index ci-dessous)
    mdp_hash      TEXT,                 -- PBKDF2 — jamais de mot de passe en clair
    role          TEXT DEFAULT 'agent'  -- agent (terrain) / admin (pilotage)
                  CHECK (role IN ('agent','admin')),
    date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_identifiant
    ON agents(identifiant) WHERE identifiant IS NOT NULL;

-- Journal d'activité : qui a fait quoi, quand (imputabilité).
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER REFERENCES agents(id),
    action      TEXT NOT NULL,          -- connexion / dépistage / cas / ...
    details     TEXT,
    date_action TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_journal_agent ON journal(agent_id, date_action);

CREATE TABLE IF NOT EXISTS enfants (
    id                  INTEGER PRIMARY KEY,
    prenom              TEXT NOT NULL,
    sexe                TEXT CHECK (sexe IN ('M','F')),
    age_mois            INTEGER NOT NULL CHECK (age_mois BETWEEN 0 AND 60),
    region              TEXT NOT NULL,
    poids_kg            REAL NOT NULL,
    taille_cm           REAL NOT NULL,
    perimetre_brachial  REAL,            -- mm (PB / MUAC)
    oedemes             INTEGER DEFAULT 0,
    score_risque        TEXT,            -- VERT / ORANGE / ROUGE
    score_details       TEXT,            -- JSON : indicateurs calculés
    date_saisie         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    agent_id            INTEGER REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_enfants_region ON enfants(region, date_saisie);
CREATE INDEX IF NOT EXISTS idx_enfants_score  ON enfants(score_risque);

-- NutriScan → MaliMap. Cycle de vie : NOUVEAU → PRIS_EN_CHARGE → TRAITE.
-- Seuls les cas non TRAITE pèsent dans le score MaliMap (données vivantes).
CREATE TABLE IF NOT EXISTS alertes (
    id              INTEGER PRIMARY KEY,
    enfant_id       INTEGER REFERENCES enfants(id),
    region          TEXT,
    score           TEXT,
    date_alerte     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    centre_notifie  INTEGER DEFAULT 0,
    statut          TEXT DEFAULT 'NOUVEAU'
                    CHECK (statut IN ('NOUVEAU','PRIS_EN_CHARGE','TRAITE')),
    date_maj        TIMESTAMP,
    traite_par      TEXT,             -- agent du centre de santé qui a clôturé
    note            TEXT              -- note de clôture
);

-- Module B : MamaMenu --------------------------------------------------------
CREATE TABLE IF NOT EXISTS abonnees (
    id               INTEGER PRIMARY KEY,
    telephone        TEXT UNIQUE NOT NULL,
    age_enfant_mois  INTEGER,
    region           TEXT,
    langue           TEXT DEFAULT 'fr',     -- fr / wo
    mode_intensif    INTEGER DEFAULT 0,     -- activé si enfant ORANGE/ROUGE
    date_inscription TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recettes (
    id            INTEGER PRIMARY KEY,
    nom           TEXT NOT NULL,
    age_min_mois  INTEGER,
    age_max_mois  INTEGER,
    kcal          INTEGER,
    proteines_g   REAL,
    fer_mg        REAL,
    vit_a_ug      REAL,
    cout_fcfa     INTEGER,
    saison        TEXT DEFAULT 'toute',     -- toute / hivernage / seche
    regions       TEXT DEFAULT 'toutes',    -- CSV de régions ou 'toutes'
    ingredients   TEXT,
    instructions  TEXT,
    sms_fr        TEXT,
    sms_wo        TEXT
);

-- Journal SMS (sortants + entrants) — sert aussi de simulateur de démo
CREATE TABLE IF NOT EXISTS sms_log (
    id        INTEGER PRIMARY KEY,
    direction TEXT CHECK (direction IN ('IN','OUT')),
    telephone TEXT,
    corps     TEXT,
    statut    TEXT DEFAULT 'simulé',        -- simulé / envoyé / échec
    date_sms  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Module C : MaliMap ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS contexte_regions (
    region                   TEXT PRIMARY KEY,
    pluie_deficit            REAL DEFAULT 0,   -- % vs normale saisonnière
    prix_alimentaires_hausse REAL DEFAULT 0,   -- % vs mois précédent
    densite_medicale         REAL DEFAULT 0,   -- soignants / 10 000 hab
    acces_eau_potable        REAL DEFAULT 0,   -- % ménages
    taux_pauvrete            REAL DEFAULT 0,   -- %
    maj                      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scores_regions (
    region   TEXT,
    score    REAL,
    niveau   TEXT,
    date_maj TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (region, date_maj)
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        _migrer(con)


def _migrer(con):
    """Ajoute les colonnes manquantes sur une base créée avant la v1.1
    (suivi des cas) ou la v1.2 (authentification). Sans effet sinon."""
    cols_a = {r[1] for r in con.execute("PRAGMA table_info(agents)")}
    if "identifiant" not in cols_a:
        con.execute("ALTER TABLE agents ADD COLUMN identifiant TEXT")
    if "mdp_hash" not in cols_a:
        con.execute("ALTER TABLE agents ADD COLUMN mdp_hash TEXT")
    if "date_creation" not in cols_a:
        con.execute("ALTER TABLE agents ADD COLUMN date_creation TIMESTAMP")
    if "role" not in cols_a:
        con.execute("ALTER TABLE agents ADD COLUMN role TEXT DEFAULT 'agent'")
    con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_identifiant
                   ON agents(identifiant) WHERE identifiant IS NOT NULL""")
    con.execute("""CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY, agent_id INTEGER REFERENCES agents(id),
        action TEXT NOT NULL, details TEXT,
        date_action TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_journal_agent
                   ON journal(agent_id, date_action)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(alertes)")}
    if "statut" not in cols:
        con.execute("ALTER TABLE alertes ADD COLUMN statut TEXT DEFAULT 'NOUVEAU'")
    if "date_maj" not in cols:
        con.execute("ALTER TABLE alertes ADD COLUMN date_maj TIMESTAMP")
    if "traite_par" not in cols:
        con.execute("ALTER TABLE alertes ADD COLUMN traite_par TEXT")
    if "note" not in cols:
        con.execute("ALTER TABLE alertes ADD COLUMN note TEXT")


if __name__ == "__main__":
    init_db()
    print(f"Base initialisée : {DB_PATH}")


def journaliser(con, agent_id, action: str, details: str = "") -> None:
    """Trace une action dans le journal d'activité (page admin Activité)."""
    con.execute("INSERT INTO journal (agent_id, action, details) VALUES (?,?,?)",
                (agent_id, action, details[:200]))
