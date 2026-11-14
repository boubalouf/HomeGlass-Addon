#!/usr/bin/env bash
# Vérification « bout en bout » du serveur réel : Ingress (18099) et API Android (18765).
set -uo pipefail

INGRESS="http://127.0.0.1:18099"
API="http://127.0.0.1:18765"
AUTH="Authorization: Bearer good-token"
DATA=/tmp/hglive
FAILED=0

check() { # nom attendu obtenu
    if [ "$2" == "$3" ]; then
        echo "  OK   $1 → $3"
    else
        echo "  FAIL $1 : attendu $2, obtenu $3"
        FAILED=$((FAILED + 1))
    fi
}

status() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

field() { # chemin python d'extraction, args curl
    local path="$1"; shift
    curl -s "$@" | .venv/bin/python -c "import json,sys;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$path"
}

echo "== Page Ingress (chemin normalisé)"
check "GET /" 200 "$(status "$INGRESS/")"
check "GET / préfixé /api/hassio_ingress/<token>/" 200 "$(status "$INGRESS/api/hassio_ingress/jeton/")"
check "GET /api/hassio_ingress/<token>/rooms/salon" 200 "$(status "$INGRESS/api/hassio_ingress/jeton/rooms/salon")"

echo "== Découverte (sans jeton)"
check "GET /api/ping" 200 "$(status "$API/api/ping")"
echo "  ha_uuid            = $(field "['ha_uuid']" "$API/api/ping")"
echo "  source de ha_uuid  = $(grep -o 'source : .*' "$DATA/server.log" | head -1)"

echo "== Séparation des ports"
check "GET /internal/status sur 18765" 404 "$(status "$API/internal/status")"
check "GET /internal/status sur 18099" 200 "$(status "$INGRESS/internal/status")"
check "GET /api/layout sans jeton" 401 "$(status "$API/api/layout")"
check "GET /api/layout jeton invalide" 401 "$(status -H 'Authorization: Bearer faux' "$API/api/layout")"
check "GET /api/layout jeton valide" 200 "$(status -H "$AUTH" "$API/api/layout")"

echo "== Connexion Home Assistant (faux serveur WS)"
echo "  connected  = $(field "['ha']['connected']" "$INGRESS/internal/status")"
echo "  entities   = $(field "['ha']['entities']" "$INGRESS/internal/status")"
echo "  areas      = $(field "['ha']['areas']" "$INGRESS/internal/status")"
echo "  lastError  = $(field "['ha']['lastError']" "$INGRESS/internal/status")"

echo "== Génération automatique"
curl -s -X POST "$INGRESS/internal/layout/autogenerate" -o "$DATA/layout.json"
check "POST /internal/layout/autogenerate" 200 "$(status -X POST "$INGRESS/internal/layout/autogenerate")"
.venv/bin/python - "$DATA/layout.json" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
print("  pièces      =", [room["id"] for room in document["rooms"]])
print("  accueil     =", [section["id"] for section in document["home"]["sections"]])
print("  exclusions  =", "sensor.salon_wifi_signal" not in json.dumps(document))
PY

echo "== Brouillon, publication, ETag"
check "PUT brouillon" 200 "$(status -X PUT -H 'Content-Type: application/json' --data-binary @"$DATA"/layout.json "$INGRESS/internal/layout/draft")"
check "PUT brouillon invalide" 422 "$(status -X PUT -H 'Content-Type: application/json' -d '{"theme":{"accent":"rouge"}}' "$INGRESS/internal/layout/draft")"
check "POST publish" 200 "$(status -X POST "$INGRESS/internal/layout/publish")"
echo "  révision publiée = $(field "['publishedRevision']" "$INGRESS/internal/status")"

ETAG=$(curl -s -D - -o /dev/null -H "$AUTH" "$API/api/layout" | tr -d '\r' | awk 'tolower($1)=="etag:" {print $2}')
echo "  ETag             = $ETAG"
check "GET /api/layout If-None-Match" 304 "$(status -H "$AUTH" -H "If-None-Match: $ETAG" "$API/api/layout")"

echo "== Ressources"
printf '\x89PNG\r\n\x1a\n' > "$DATA/fond.png"
head -c 128 /dev/zero | tr '\0' '0' >> "$DATA/fond.png"
ASSET=$(curl -s -X POST -H "Content-Type: image/png" -H "X-Filename: fond.png" --data-binary @"$DATA"/fond.png "$INGRESS/internal/assets" | .venv/bin/python -c "import json,sys;print(json.load(sys.stdin)['id'])")
echo "  asset            = $ASSET"
check "POST /internal/assets" 201 "$(status -X POST -H 'Content-Type: image/png' --data-binary @"$DATA"/fond.png "$INGRESS/internal/assets")"
check "GET /api/assets/<id>" 200 "$(status -H "$AUTH" "$API/api/assets/$ASSET")"
check "GET /api/assets/<id> Content-Type" "image/png" "$(curl -s -o /dev/null -w '%{content_type}' -H "$AUTH" "$API/api/assets/$ASSET")"
check "POST /internal/assets (texte)" 400 "$(status -X POST -H 'Content-Type: image/png' --data-binary 'pas une image' "$INGRESS/internal/assets")"

echo "== Profil par appareil"
check "PUT profil appareil" 200 "$(status -X PUT -H 'Content-Type: application/json' -d '{"hiddenRooms":["room_cuisine"],"themeMode":"dark"}' "$INGRESS/internal/devices/dev_tablette/profile")"
curl -s "$INGRESS/api/layout?device_id=dev_tablette" -o "$DATA/tablette.json"
.venv/bin/python - "$DATA/tablette.json" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
print("  pièces tablette =", [room["id"] for room in document["rooms"]])
print("  thème tablette  =", document["theme"]["mode"])
PY

echo
if [ "$FAILED" -eq 0 ]; then
    echo "TOUTES LES VÉRIFICATIONS SONT PASSÉES"
else
    echo "$FAILED vérification(s) en échec"
fi
exit "$FAILED"
