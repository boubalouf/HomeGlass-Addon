# HomeGlass Server

Add-on Home Assistant qui produit la mise en page du dashboard **HomeGlass** (application
Android au style « Apple Maison »).

* **Dépôt principal du projet :** [https://github.com/boubalouf/HomeGlass](https://github.com/boubalouf/HomeGlass)

L'add-on est le seul endroit où la mise en page est éditée : pièces, sections, tuiles,
scènes, thème, onglets et profils par appareil. Il expose ensuite cette mise en page en
JSON à l'application Android, qui ne fait que l'afficher.

## État d'avancement

| Étape | Contenu | État |
| --- | --- | --- |
| 1 | Squelette d'add-on, Ingress, page d'accueil | ✅ |
| 2 | Modèles Pydantic du contrat v1, JSON Schema exporté, tests | ✅ |
| 3 | Connexion à Home Assistant (entités, areas, appareils), génération automatique | ✅ |
| 4 | API Android (`ping`, `register`, `layout`, `assets`, `ws`), authentification, stockage | ✅ |
| 5 | Éditeur web (React) avec aperçu et publication | à venir |
| 6 | Découverte mDNS `_homeglass._tcp` et diffusion temps réel | à venir |
| 7 | Tests complémentaires et documentation finale | à venir |

## Installation

1. Dans Home Assistant : **Paramètres → Modules complémentaires → Boutique**.

2. Menu **⋮ → Dépôts**, ajouter l'URL de ce dépôt :
   `https://github.com/boubalouf/HomeGlass` (ou l'URL du dépôt d'add-ons dédié le cas échéant).
3. Installer **HomeGlass Server**, démarrer, puis ouvrir le panneau **HomeGlass** dans la
   barre latérale.

## Options

| Option | Type | Défaut | Description |
| --- | --- | --- | --- |
| `log_level` | liste | `info` | Verbosité des journaux. |
| `api_port` | port (optionnel) | `8765` | Port de l'API pour l'application Android. Vide = API désactivée. |
| `allow_remote_access` | booléen | `false` | Autorise l'accès à l'API depuis l'extérieur du réseau local. |

## Développement

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Tests
.venv/bin/pytest

# Régénérer le JSON Schema versionné
.venv/bin/python -m homeglass_server.models.schema_export

# Vérifier que le schéma versionné correspond aux modèles
.venv/bin/python -m homeglass_server.models.schema_export --check

# Lancer le serveur hors add-on (les valeurs HG_* remplacent les options de l'add-on)
HG_API_PORT=8765 .venv/bin/python -m homeglass_server.serve
# puis http://localhost:8099/ et http://localhost:8099/api/ping

# Se connecter à un Home Assistant réel depuis un poste de développement :
HG_CORE_WS_URL=ws://homeassistant.local:8123/api/websocket \
HG_AUTH_WS_URL=ws://homeassistant.local:8123/api/websocket \
HG_DATA_DIR=/tmp/homeglass-data \
SUPERVISOR_TOKEN="<jeton d'accès longue durée>" \
  .venv/bin/python -m homeglass_server.serve
