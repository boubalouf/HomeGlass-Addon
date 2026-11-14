# HomeGlass Server

Add-on Home Assistant qui produit la mise en page du dashboard **HomeGlass** (application
Android au style « Apple Maison »).

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
   `https://github.com/homeglass/homeglass-addons` (à remplacer par l'URL réelle de
   publication avant de distribuer l'add-on).
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
```

Sans `SUPERVISOR_TOKEN`, le serveur démarre quand même : la connexion à Home Assistant
reste désactivée et `GET /internal/status` le signale. Le faux Home Assistant des tests
(`tests/fake_ha.py`) sert de référence pour le protocole WebSocket.

Construction de l'image (multi-arch, via les images de base Home Assistant) :

```bash
docker build --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20 -t homeglass-server .
```

Le Supervisor utilise `build.yaml` pour choisir l'image de base selon l'architecture
(`amd64`, `aarch64`, `armv7`).

## Structure

```text
config.yaml            métadonnées de l'add-on (Ingress, options, ports)
build.yaml             images de base par architecture
Dockerfile, run.sh     image et point d'entrée (bashio)
homeglass_server/      backend FastAPI + modèles Pydantic
  app.py               assemblage : routes, middleware, cycle de vie
  api/                 routes de l'application, routes de l'éditeur, hub WebSocket
  ha/                  client WebSocket, cache des entités, génération, validation des jetons
  models/layout.py     contrat de mise en page v1
  models/schema_export.py  export du JSON Schema
  store/               brouillon/publication/historique, appareils, ressources
schema/layout.v1.json  schéma versionné, généré depuis les modèles
web/                   interface web servie par l'add-on (page d'accueil à ce stade)
tests/                 pytest (contrat, schéma, client Home Assistant simulé, API, stockage)
```

Le contrat de mise en page est décrit dans [DOCS.md](DOCS.md). Toute évolution cassante
incrémente `schemaVersion` ; les ajouts de champs optionnels ne l'incrémentent pas.
