# -*- coding: utf-8 -*-

"""
Localhost loopback receiver for the browser-based Google/Apple sign-in.

The hosted page (https://{project}.firebaseapp.com/qgis-auth/) signs the user
in with the Firebase JS SDK and delivers {state, refreshToken, idToken} to
http://127.0.0.1:{port}/ — first via fetch() (CORS + Private Network Access
headers answered here), falling back to a top-level form POST, which is never
blocked by mixed-content/PNA policies.

Security posture: bound to 127.0.0.1 only, single-use random state nonce,
5-minute lifetime, torn down on dock unload/sign-out.
"""

import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qgis.PyQt.QtCore import QObject, pyqtSignal

TIMEOUT_SECONDS = 300

_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Tairu Maps</title></head>
<body style="font-family: sans-serif; text-align: center; padding-top: 4em; background:#f4f4f4">
<h2>Login concluído ✔</h2>
<p>Você já pode fechar esta janela e voltar ao QGIS.</p>
</body></html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Tairu Maps</title></head>
<body style="font-family: sans-serif; text-align: center; padding-top: 4em; background:#f4f4f4">
<h2>Falha no login</h2>
<p>{reason}</p>
</body></html>"""


class LoopbackServer(QObject):
    """One-shot credential receiver. Signals are emitted from the server
    thread and delivered queued to the GUI thread."""

    credentialsReceived = pyqtSignal(dict)   # {'refreshToken':…, 'idToken':…}
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = None
        self._thread = None
        self._timer = None
        self.state = None
        self.port = None
        self._used = False

    def start(self):
        """Bind to an ephemeral 127.0.0.1 port. Returns (port, state nonce)."""
        self.stop()
        self.state = secrets.token_urlsafe(16)
        self._used = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence stderr
                pass

            def _cors_headers(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'content-type')
                # Chrome Private Network Access preflight
                self.send_header('Access-Control-Allow-Private-Network', 'true')

            def _respond_html(self, status, html):
                body = html.encode('utf-8')
                self.send_response(status)
                self._cors_headers()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def do_GET(self):
                self._respond_html(200, _ERROR_HTML.format(
                    reason='Aguardando o envio das credenciais pela página de login…'))

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    raw = self.rfile.read(length) if length else b''
                    ctype = (self.headers.get('Content-Type') or '').lower()
                    if 'application/json' in ctype:
                        data = json.loads(raw.decode('utf-8'))
                    else:
                        parsed = urllib.parse.parse_qs(raw.decode('utf-8'))
                        data = {k: v[0] for k, v in parsed.items()}
                except Exception:
                    self._respond_html(400, _ERROR_HTML.format(reason='Requisição inválida.'))
                    return

                if outer._used:
                    self._respond_html(410, _ERROR_HTML.format(reason='Código já utilizado.'))
                    return
                if data.get('state') != outer.state:
                    self._respond_html(403, _ERROR_HTML.format(reason='Código de segurança inválido.'))
                    outer.failed.emit('Código de segurança (state) inválido — tente novamente.')
                    return
                if not data.get('refreshToken'):
                    self._respond_html(400, _ERROR_HTML.format(reason='Credenciais ausentes.'))
                    outer.failed.emit('A página de login não enviou as credenciais.')
                    return

                outer._used = True
                self._respond_html(200, _SUCCESS_HTML)
                outer.credentialsReceived.emit({
                    'refreshToken': data.get('refreshToken'),
                    'idToken': data.get('idToken'),
                    'appCheckToken': data.get('appCheckToken'),
                    'appCheckExpireTimeMillis': data.get('appCheckExpireTimeMillis'),
                })

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self._timer = threading.Timer(TIMEOUT_SECONDS, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        return self.port, self.state

    def _on_timeout(self):
        if not self._used and self._server is not None:
            self.failed.emit('Tempo esgotado aguardando o login no navegador.')
        self.stop()

    def stop(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        server, self._server = self._server, None
        if server is not None:
            # shutdown() blocks until serve_forever exits; safe from any
            # thread except a handler thread (we never call it from one).
            threading.Thread(target=server.shutdown, daemon=True).start()
        self._thread = None
        self.port = None
