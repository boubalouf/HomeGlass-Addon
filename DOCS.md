# HomeGlass Server — documentation

HomeGlass Server conçoit et publie la mise en page du dashboard **HomeGlass** affiché par
l'application Android. L'application n'embarque aucun mode d'édition : toute la
configuration (pièces, tuiles, scènes, thème, onglets, profils par appareil) vit ici.

> Cette documentation couvre les étapes 1 à 4 : squelette de l'add-on, contrat de mise en
> page v1, connexion à Home Assistant et API de l'application Android. Les sections
> marquées « à venir » décrivent le comportement encore attendu.

## Installation

1. **Paramètres → Modules complémentaires → Boutique → ⋮ → Dépôts**, puis ajouter l'URL
   du dépôt d'add-ons.
2. Installer **HomeGlass Server** et démarrer l'add-on.
3. Le panneau **HomeGlass** apparaît dans la barre latérale (Ingress, port interne 8099).
4. Le port `8765` expose l'API destinée à l'application Android si `api_port` est renseigné.

## Options

```yaml
log_level: info              # trace, debug, info, notice, warning, error, fatal
api_port: 8765               # laisser vide pour désactiver l'API hors Ingress
allow_remote_access: false   # accès à l'API depuis l'extérieur du réseau local
```

`hg_*` : les options sont transmises au serveur par `run.sh` sous forme de variables
d'environnement (`HG_LOG_LEVEL`, `HG_API_PORT`, `HG_ALLOW_REMOTE_ACCESS`), ce qui permet
de lancer le serveur hors add-on pour le développement.

## Architecture

- **FastAPI** (Python 3.12) sert l'interface Ingress et l'API Android dans un seul
  processus uvicorn. Deux sockets sont ouverts : `8099` (Ingress) et `api_port` (API).
- L'interface web n'utilise que des URLs **relatives** : elle fonctionne sous le préfixe
  `/api/hassio_ingress/<token>/` sans réécriture d'URL.
- Les données sont stockées dans `/data` et dans le dossier `addon_config`, tous deux
  inclus dans les sauvegardes Home Assistant.

## Contrat de mise en page v1

Le document est versionné par `schemaVersion` (actuellement `1`) et son JSON Schema est
publié dans [`schema/layout.v1.json`](schema/layout.v1.json), généré depuis les modèles
Pydantic (`homeglass_server/models/layout.py`).

Règles :

- `revision` est un entier incrémenté à chaque publication ; `publishedAt` est en ISO 8601 UTC.
- Les identifiants sont stables (`snake_case` ou `kebab-case`), générés une fois et
  jamais réutilisés.
- Couleurs : `#RRGGBB` ou `#AARRGGBB` (normalisées en majuscules).
- Icônes : noms Material Symbols en `snake_case` (`meeting_room`, `lightbulb`).
- `entityId` au format Home Assistant (`light.salon`).
- Tailles de tuile : `small` (1×1), `wide` (2×1), `large` (2×2).
- Champs inconnus refusés (`extra="forbid"`) : l'éditeur et l'API n'acceptent que ce format.
- Les champs optionnels ont des valeurs par défaut explicites et l'API renvoie **tous** les
  champs. Un document sérialisé peut donc contenir des clés à `null` qui n'apparaissent pas
  dans les exemples.

### Actions

| `type` | Champs requis | Effet |
| --- | --- | --- |
| `toggle` | — | Bascule l'entité. |
| `more_info` | — | Ouvre la feuille de détail. |
| `call_service` | `service` (`domaine.service`), `data`, `target` optionnels | Appel de service Home Assistant. |
| `navigate` | `roomId` | Ouvre une pièce. |
| `none` | — | Désactive l'interaction. |

Si `tap` ou `longPress` est absent, l'application applique le comportement par défaut du
domaine de l'entité.

### Sections

Une section est une union discriminée sur `kind` :

- `kind: "tiles"` → `items` de type tuile (`entityId`, `name`, `icon`, `accent`, `size`,
  `display`, `hidden`, `tap`, `longPress`) ;
- `kind: "scenes"` → `items` de type scène (`name`, `icon`, `gradient` à deux couleurs,
  `actions`).

`rooms[].sections` utilise exactement la même structure que `home.sections`.

### Thème

| Champ | Valeurs | Défaut |
| --- | --- | --- |
| `mode` | `auto`, `light`, `dark` | `auto` |
| `accent` | couleur hexadécimale | `#FF9500` |
| `tileRadiusDp` | 0 à 60 | `22` |
| `density` | `compact`, `standard`, `comfortable` | `standard` |
| `background.type` | `gradient`, `solid`, `image` | `gradient` |
| `background.assetId` | identifiant d'asset (obligatoire si `image`) | `null` |
| `background.blurDp` | 0 à 100 | `0` |
| `background.dim` | 0 à 1 | `0.0` |

`summaryChips` accepte `lights`, `climate`, `security`, `media`, `energy`.

### Exemple de référence

L'exemple complet du contrat est versionné dans
[`tests/fixtures/example_layout.json`](tests/fixtures/example_layout.json) et sert de test :
il est validé par les modèles Pydantic **et** par le JSON Schema exporté.

## API de l'application Android (port `api_port`, 8765 par défaut)

| Route | Authentification | Rôle |
| --- | --- | --- |
| `GET /api/ping` | aucune | Découverte : version, `ha_uuid`, `schemaVersion`. |
| `POST /api/devices/register` | Bearer | Enregistre l'appareil, renvoie `deviceId`, `revision` et son profil. |
| `GET /api/layout?device_id=` | Bearer | Mise en page résolue, avec `ETag` et `304` si inchangée. |
| `GET /api/assets/{id}` | Bearer | Image (`Content-Type` exact, cache `immutable`, `304` possible). |
| `WS /api/ws` | premier message | Notifications `layout_updated`, `ping` toutes les 30 s. |
| `POST /api/pair/request` | aucune | Demande d'appairage : `pairingId` (le code reste dans Home Assistant). |
| `POST /api/pair/claim` | aucune | Échange `pairingId` + code contre un jeton d'appareil. |
| `GET /api/ha/config` | jeton d'appareil | Version et nom de l'instance, lus par l'add-on. |
| `POST /api/ha/services/{domain}/{service}` | jeton d'appareil | Appel de service relayé à Core. |
| `WS /api/ha/ws` | premier message | Relais du protocole WebSocket de Core pour l'appareil appairé. |

`GET /api/ping` renvoie :

```json
{ "app": "homeglass", "version": "0.1.0", "ha_uuid": "3f2b…", "schemaVersion": 1 }
```

`ha_uuid` identifie l'instance Home Assistant (résolution décrite plus bas) ; il sera aussi
publié dans le TXT mDNS à l'étape 6. La documentation OpenAPI est servie sur `/api/docs`.

### Authentification

Deux types de jetons sont acceptés sur le port API.

**Jeton d'appareil (appairage).** `POST /api/pair/request` crée une demande en attente ; le
code à six chiffres n'est **pas** dans la réponse, il s'affiche dans le panneau
(`GET /internal/pairing`, donc uniquement derrière Ingress, après authentification Home
Assistant). `POST /api/pair/claim` échange le code (usage unique, 5 tentatives, 10 minutes)
contre un jeton `Authorization: Bearer …`, dont le serveur ne stocke que l'empreinte SHA-256.
Ce jeton donne accès à `/api/layout`, `/api/assets`, `WS /api/ws` et à la passerelle
`/api/ha/*` : l'application n'a alors plus aucun jeton Home Assistant. Révocation depuis le
panneau (`DELETE /internal/pairing/devices/{id}`), effective immédiatement.

**Jeton Home Assistant.** Pour les installations sans appairage :

- Le jeton présenté est un **jeton d'accès longue durée** de Home Assistant. L'add-on
  l'accepte en ouvrant une connexion WebSocket vers Core avec ce jeton
  (`ws://homeassistant:8123/api/websocket`), puis en demandant `auth/current_user` :
  même validation que Home Assistant, aucune copie de secret ni de mot de passe.
- Résultat mis en cache **5 minutes** par empreinte SHA-256 du jeton (jamais le jeton).
- `401` si le jeton est absent, invalide ou expiré ; `403` si une action exige un
  administrateur (réservé aux futures routes d'administration de l'application).
- Si Core est injoignable, le journal affiche
  `Validation impossible : Home Assistant est injoignable sur …` et la requête repart en
  `401` : mieux vaut refuser que laisser passer.
- **Via Ingress**, la session Home Assistant fait foi : aucun jeton n'est redemandé, et
  l'identité provient des en-têtes `X-Remote-User-*` posés par Core.
- **Limitation de débit** : 120 requêtes par minute et par (IP + jeton) sur le port API
  (`retry-after` renseigné au-delà). Le port Ingress n'est pas limité.

### WebSocket

```jsonc
// application → add-on
{ "type": "auth", "token": "<jeton HA>" }
// add-on → application
{ "type": "auth_ok", "user": { "id": "…", "name": "…", "isAdmin": true, "source": "token" } }
{ "type": "layout_updated", "revision": 7 }
{ "type": "ping" }          // toutes les 30 s ; l'application peut répondre { "type": "pong" }
```

Une publication notifie immédiatement tous les appareils connectés. Les jetons ci-dessus
(jeton d'appareil compris) sont acceptés.

### Passerelle Home Assistant (`WS /api/ha/ws`)

Même protocole que le WebSocket de Core, mais authentifié par le jeton d'appareil :
`auth_required` → `auth` → `auth_ok`, puis les messages de l'application
(`get_states`, `subscribe_events`, registres, `call_service`…) sont relayés vers Core, où
l'add-on s'authentifie avec `SUPERVISOR_TOKEN`. Chaque connexion application ouvre sa propre
connexion amont ; les identifiants de message restent ceux du client.

## API de l'éditeur (`/internal`, Ingress uniquement)

Ces routes ne sont accessibles que depuis le port Ingress : sur le port 8765, elles
répondent `404` (elles n'existent pas pour l'application).

| Route | Rôle |
| --- | --- |
| `GET /internal/status` | État de la connexion à Home Assistant, révision publiée, compteurs. |
| `GET /internal/entities` | Entités du cache, avec `excludedReason` pour celles hors génération. |
| `GET /internal/areas` | Pièces Home Assistant et nombre d'entités par pièce. |
| `POST /internal/layout/autogenerate` | Génère une mise en page ; `{"publish": true}` la publie. |
| `GET`/`PUT /internal/layout/draft` | Lit/écrit le brouillon (validé contre le contrat). |
| `POST /internal/layout/publish` | Publie le brouillon, ou `{"layout": …}`. |
| `GET /internal/layout/history` | Versions conservées (20 par défaut). |
| `GET /internal/layout/versions/{n}` | Contenu d'une version. |
| `POST /internal/layout/restore/{n}` | Republie une version en tant que nouvelle révision. |
| `GET /internal/devices` | Appareils connus et profil par défaut. |
| `PUT /internal/devices/{id}/profile` | Surcharges d'un appareil (`default` = profil par défaut). |
| `POST /internal/assets` | Envoi d'une image en corps brut (10 Mo max) ; `X-Filename` optionnel. |
| `GET /internal/assets` | Ressources stockées. |

Les réponses de lecture/écriture de mise en page sont enveloppées : `{"layout": …}`,
sauf `POST /internal/layout/autogenerate` sans `publish`, qui renvoie directement le document
layout v1.

### Profils par appareil

```json
{ "hiddenRooms": ["room_garage"], "hiddenTabs": ["auto"], "themeMode": "dark" }
```

Un profil `default` s'applique à tous les appareils ; les valeurs d'un appareil écrasent
celles du profil par défaut. La résolution se fait côté serveur : l'application reçoit
toujours un document complet, identique en structure.

## Schéma JSON

Le schéma est **généré depuis les modèles** : il ne doit jamais être édité à la main.

```bash
python -m homeglass_server.models.schema_export          # régénère schema/layout.v1.json
python -m homeglass_server.models.schema_export --check  # échoue si le fichier est obsolète
```

Un test (`tests/test_schema.py`) échoue dès que le fichier versionné diverge des modèles.

## Connexion à Home Assistant

- Un client WebSocket unique se connecte à `ws://supervisor/core/websocket` avec
  `SUPERVISOR_TOKEN` (`homeassistant_api: true` dans `config.yaml`).
- À chaque (re)connexion : `config/area_registry/list`, `config/device_registry/list`,
  `config/entity_registry/list`, `get_states`, `get_config` ; puis abonnement à
  `state_changed`, `area_registry_updated`, `device_registry_updated` et
  `entity_registry_updated`.
- Le cache est reconstruit à chaque connexion : Home Assistant reste la source de vérité,
  rien n'est dupliqué sur disque.
- Reconnexion automatique avec backoff exponentiel (1 s → 60 s, remis à zéro après une
  session authentifiée). L'état est exposé par `GET /internal/status` : `ha.connected`,
  `ha.attempts`, `ha.lastError`, `ha.connectedSince`, `ha.version`.
- Le nom de la maison (`get_config.location_name`) devient le titre de l'accueil.

### Identifiant d'instance (`ha_uuid`)

Résolu au démarrage, dans cet ordre : variable `HG_HA_UUID` ; clé `uuid` ou `instance_id` de
`GET {HG_CORE_API_URL}/config` ; clé `uuid` de `GET /supervisor/supervisor/info` ; à défaut,
un UUID v4 persisté dans `/data/ha_uuid`. Le journal indique la source retenue.

## Génération automatique de mise en page

`POST /internal/layout/autogenerate` — et la première requête `GET /api/layout` s'il
n'existe aucune version publiée — produit un document layout v1 valide :

- **pièces** : une par area Home Assistant, plus une pièce « Autres » pour les entités sans
  pièce (une entité sans `area_id` hérite de celle de son appareil) ;
- **sections** : par groupe logique de domaines — Éclairage, Climat (climatisation,
  ventilation, humidificateur, chauffe-eau), Volets, Multimédia (lecteurs, caméras),
  Sécurité (serrures, alarmes et capteurs d'ouverture/mouvement/fumée), Capteurs,
  Commandes ;
- **exclusions** : entités `diagnostic`/`config`, désactivées ou masquées dans Home
  Assistant, et domaines techniques (automatisations, mises à jour, outils système) ;
- **affichage** : `slider` pour une lampe graduable ou un volet positionnable, `value` pour
  un capteur, un nombre ou un thermostat, `button` sinon ; tuiles `wide` pour les médias,
  les caméras et le climat ;
- **scènes** : section « Scènes » sur l'accueil, dégradés parcourus dans l'ordre ;
- **accueil** : section « Favoris » vide, à remplir dans l'éditeur ;
- **identifiants** : dérivés des identifiants Home Assistant (`tile_light_salon`), donc
  stables d'une génération à l'autre.

## Stockage et publication

```text
/data/layout/draft.json           brouillon
/data/layout/published.json       version servie à l'application
/data/layout/history/000007.json  20 dernières versions publiées
/data/devices.json                appareils et profils
/data/assets/…                    images (index.json + fichiers)
/data/ha_uuid                     identifiant d'instance de secours
```

- Écriture **atomique** : fichier temporaire, `fsync`, `os.replace` ; un arrêt brutal ne
  laisse jamais un JSON tronqué.
- Chaque publication valide le contrat, incrémente `revision`, horodate `publishedAt`,
  écrit l'historique puis la version courante.
- `migrate()` est le point d'entrée des évolutions de schéma : un document dont le
  `schemaVersion` dépasse celui de l'add-on est refusé avec un message explicite.

## Sécurité

- **Séparation des ports** : `8099` (Ingress) sert l'éditeur, `api_port` sert l'application.
  Le mode est déterminé par le port d'écoute réel (`scope["server"]` d'uvicorn), jamais par
  un en-tête : ajouter `X-Ingress-Path` à la main sur le port 8765 ne donne aucun accès.
- **`/internal/*`** répond `404` sur le port API : l'éditeur n'est joignable que par la
  session Home Assistant déjà authentifiée.
- **Validation des jetons** auprès de Core, cache 5 minutes indexé par SHA-256.
- **Appairage** : le code n'est jamais renvoyé à l'appelant, seulement affiché côté Ingress ;
  usage unique, durée de vie 10 minutes, 5 tentatives puis annulation, jetons stockés hachés
  (SHA-256) et révocables un par un.
- **Écritures** : validation Pydantic stricte (champs inconnus refusés), brouillon séparé de
  la version publiée, historique borné, restauration explicite.
- **Ressources** : 10 Mo maximum, type déduit des octets d'en-tête (PNG, JPEG, GIF, WebP),
  SVG refusé, identifiant = condensat du contenu (aucune traversée de chemin possible).
- **Débit** : 120 requêtes par minute et par (IP + jeton) sur le port API.
- `allow_remote_access: false` restreint l'API hors Ingress au réseau local.

## Dépannage

- **Le panneau répond `{"detail":"Not Found"}`** : les lignes de journal
  `GET <chemin> — client <ip>:<port> | X-Ingress-Path <valeur> | brut '<chemin reçu>'`
  donnent le chemin exact envoyé par le proxy. L'add-on retire lui-même les préfixes
  `/api/hassio_ingress/<token>` et `/ingress/<token>` et sert la page d'accueil sur tout
  chemin hors `/api`, `/internal` et `/assets`.
- **`Invalid HTTP request received`** : uvicorn a reçu autre chose que du HTTP/1.1 sur son
  port. La ligne suivante, `Requête HTTP invalide de <ip>:<port> — <interprétation> |
  premiers octets : …`, identifie le fautif : ClientHello TLS (URL en `https://`), préface
  HTTP/2 en clair, flux binaire ou requête malformée.
- **Journal d'accès** : une ligne `uvicorn.access` par requête, sur les deux ports (8099 et
  8765). `log_level: debug` ajoute les connexions ouvertes puis fermées sans données
  (contrôles de santé TCP).
- **`GET /internal/status` donne `ha.connected: false`** : le champ `ha.lastError` indique
  la cause (`authentification refusée` → `SUPERVISOR_TOKEN` absent ou add-on sans
  `homeassistant_api` ; `connexion refusée` → Core pas encore démarré ; `nom d'hôte
  introuvable` → URL incorrecte hors add-on). L'add-on réessaie indéfiniment, 1 s puis 2,
  4… jusqu'à 60 s.
- **`401` sur `GET /api/layout`** : jeton Bearer absent, expiré, ou Core injoignable pour le
  valider (ligne `Validation impossible` dans le journal). Un jeton d'accès longue durée se
  crée dans Home Assistant : profil → **Sécurité** → *Jetons d'accès longue durée*.
- **`404` sur `/internal/…` depuis le port 8765** : c'est voulu ; l'éditeur passe par
  Ingress. Vérifier le port utilisé, pas les en-têtes.
- **`ha_uuid` vide** : la résolution n'a pas encore eu lieu (add-on à peine démarré) ou a
  échoué ; l'identifiant local persisté prend le relais, `GET /internal/status` et le journal
  indiquent la source.
- **Port 8765 déjà utilisé** : l'add-on journalise un avertissement et continue de servir
  Ingress. Changer `api_port` si nécessaire.
- **mDNS indisponible** (réseau Docker sans host network) : ce sera signalé dans les logs
  et dans l'interface à l'étape 6 ; l'application pourra toujours utiliser
  `http://<hôte HA>:8765/api/ping`.
