# -*- coding: utf-8 -*-

"""
Mirrors QGIS's native proxy settings (Configurações > Opções > Rede) onto
urllib, so the plugin's stdlib HTTP calls (Firebase REST, DEM/contour
downloads) work behind authenticated corporate proxies.

Qt-based requests (tile prefetch) already honor those settings natively;
this extends the same single source of truth to urllib. No plugin-specific
proxy UI: the user configures the proxy once, in QGIS itself.
"""

import urllib.request
from urllib.parse import quote

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

try:
    from .tairu_core.i18n import tr
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.i18n import tr


def install_qgis_proxy():
    """Install a global urllib opener matching QGIS's proxy settings.

    Idempotent and cheap; called at plugin load and at each network entry
    point so mid-session settings changes are picked up without a restart.
    Returns a human-readable status line when a proxy is in effect (or an
    unsupported type is configured), None for direct/system connections.
    """
    settings = QgsSettings()
    enabled = settings.value('proxy/proxyEnabled', False, type=bool)
    host = settings.value('proxy/proxyHost', '', type=str)
    if not enabled or not host:
        urllib.request.install_opener(None)  # back to system/env defaults
        return None

    proxy_type = settings.value('proxy/proxyType', '', type=str)
    if proxy_type == 'DefaultProxy':
        # "Use system proxy": urllib already resolves the system proxy.
        urllib.request.install_opener(None)
        return None
    if proxy_type not in ('HttpProxy', 'HttpCachingProxy'):
        # ponytail: Socks5/FTP proxies need extra deps urllib lacks
        return tr('Tipo de proxy "{kind}" não suportado para as requisições do '
                  'TairuDB; use um proxy HTTP.').format(kind=proxy_type)

    port = settings.value('proxy/proxyPort', '', type=str)
    user = settings.value('proxy/proxyUser', '', type=str)
    password = settings.value('proxy/proxyPassword', '', type=str)

    # Credentials stored in the QGIS Auth Manager take precedence (this is
    # how QGIS itself resolves proxy auth for Qt requests).
    authcfg = settings.value('proxy/authcfg', '', type=str)
    if authcfg:
        cfg = QgsAuthMethodConfig()
        if QgsApplication.authManager().loadAuthenticationConfig(authcfg, cfg, True):
            user = cfg.config('username') or user
            password = cfg.config('password') or password

    netloc = '{}:{}'.format(host, port) if port else host
    plain_url = 'http://' + netloc
    if user:
        # Credentials embedded in the proxy URL: urllib sends them
        # preemptively, which also covers the HTTPS CONNECT tunnel (a 407
        # challenge inside the tunnel is not retried by urllib).
        cred_url = 'http://{}:{}@{}'.format(
            quote(user, safe=''), quote(password or '', safe=''), netloc)
    else:
        cred_url = plain_url

    handlers = [urllib.request.ProxyHandler({'http': cred_url, 'https': cred_url})]
    if user:
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, plain_url, user, password or '')
        handlers.append(urllib.request.ProxyBasicAuthHandler(password_mgr))
    urllib.request.install_opener(urllib.request.build_opener(*handlers))

    if user:
        return tr('Usando proxy do QGIS: {proxy} (usuário: {user})').format(proxy=netloc, user=user)
    return tr('Usando proxy do QGIS: {proxy}').format(proxy=netloc)
