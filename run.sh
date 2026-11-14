#!/usr/bin/with-contenv bashio
set -e

export HG_LOG_LEVEL="$(bashio::config 'log_level')"
export HG_ALLOW_REMOTE_ACCESS="$(bashio::config 'allow_remote_access')"

if bashio::config.has_value 'api_port'; then
    export HG_API_PORT="$(bashio::config 'api_port')"
else
    # Port laissé vide dans la configuration : API pour l'app Android désactivée.
    unset HG_API_PORT || true
fi

bashio::log.info "Démarrage de HomeGlass Server (Ingress sur le port 8099)"

exec python -m homeglass_server.serve
