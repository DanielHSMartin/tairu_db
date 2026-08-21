#!/usr/bin/env python3
"""Verificacao pre-upload do plugin contra as regras REAIS do plugins.qgis.org.

    python3 tools/qgis_preflight.py ../tairu_db-<versao>.zip
    python3 tools/qgis_preflight.py ../tairu_db-<versao>.zip --update-pins

Por que existe: toda rejeicao anterior veio de INFERIR as regras a partir da
documentacao em vez de ler o codigo do servidor. Este script baixa esse codigo,
avisa quando ele muda e roda o scanner de verdade sobre o zip.

O que ele NAO faz: evitar a revisao manual. Ela e o caminho padrao para todo
mundo (`approved=False` sempre no upload; so publica sozinho quem tem a permissao
`plugins.can_approve` E marca "Publish immediately"). Ver RELEASE.md.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import http.client
import shutil
import sys
import tempfile
import types
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PINS_PATH = os.path.join(HERE, "qgis_upstream_rules.json")

RAW = "https://raw.githubusercontent.com/qgis/QGIS-Plugins-Website/master/qgis-app/plugins"

# Os arquivos que SAO o contrato das regras. views.py/forms.py mudam por motivos
# nao relacionados o tempo todo; estes tres carregam as verificacoes em si.
RULE_SOURCES = {
    "security_scanner.py": f"{RAW}/security_scanner.py",
    "validator.py": f"{RAW}/validator.py",
    "tasks/run_security_scan.py": f"{RAW}/tasks/run_security_scan.py",
}

# validator.PLUGIN_REQUIRED_METADATA (default do servidor).
REQUIRED_METADATA = (
    "name", "description", "version", "qgisMinimumVersion",
    "author", "email", "about", "tracker", "repository",
)

# validator.URL_CHECK_* — o servidor testa estas URLs no upload.
URL_FIELDS = ("tracker", "repository", "homepage")
URL_TIMEOUT = 10
URL_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Grafias Qt5 sem escopo: o relatorio Qt6 le o fonte estaticamente e acusa cada
# uma, inclusive dentro de try/except e de comentarios.
QT6_OLD_SPELLINGS = re.compile(
    r"\b(?:Qt|QBuffer|QIODevice|QIODeviceBase)\."
    r"(?:WriteOnly|ReadOnly|UserRole|ItemIsEditable|WA_[A-Za-z_]+|NoFocus"
    r"|RightDockWidgetArea|LeftDockWidgetArea)\b"
    r"|QImage\.Format_"
    r"|QFrame\.(?:HLine|VLine|Sunken|Raised|Plain|NoFrame|Box|StyledPanel)\b"
    r"|QMessageBox\.(?:Yes|No|Ok|Cancel|Warning|Critical|Information|Question)\b"
    r"|Qgis\.(?:Info|Warning|Critical|Success)\b"
    r"|QgsProcessingAlgorithm\.Flag[A-Za-z]"
    r"|QLineEdit\.(?:Password|Normal)\b"
    r"|QColorDialog\.ShowAlphaChannel\b"
    r"|\bexec_\b"
)

FAILURES = []
NOTES = []


def fail(msg):
    FAILURES.append(msg)
    print(f"  FALHOU  {msg}")


def ok(msg):
    print(f"  ok      {msg}")


def note(msg):
    NOTES.append(msg)
    print(f"  aviso   {msg}")


# IncompleteRead e HTTPException, nao OSError: sem ela no except, uma leitura
# truncada derruba o script inteiro em vez de virar um aviso.
NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)


def fetch(url, attempts=3):
    req = urllib.request.Request(url, headers={"User-Agent": URL_UA})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - URL fixa https
                return r.read()
        except NET_ERRORS:
            if attempt == attempts:
                raise
    raise AssertionError("inalcancavel")


# ---------------------------------------------------------------- etapa 1
def check_upstream_rules(workdir, update_pins):
    """Baixa as fontes de regra e compara com os hashes fixados."""
    print("\n[1] Mudancas nas exigencias do plugins.qgis.org")
    try:
        pins = json.load(open(PINS_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        pins = {"sources": {}}
    new_pins = {"fetched_note": pins.get("fetched_note", ""), "sources": {}}
    drift = []

    for name, url in RULE_SOURCES.items():
        try:
            body = fetch(url)
        except NET_ERRORS as exc:
            note(f"sem rede para {name} ({exc}) — nao da para saber se as regras mudaram")
            return None
        dest = os.path.join(workdir, os.path.basename(name))
        with open(dest, "wb") as fh:
            fh.write(body)
        digest = hashlib.sha256(body).hexdigest()
        new_pins["sources"][name] = {"sha256": digest, "lines": body.count(b"\n") + 1}
        old = pins.get("sources", {}).get(name, {}).get("sha256")
        if old is None:
            note(f"{name}: sem hash fixado ainda (primeira execucao)")
            drift.append(name)
        elif old != digest:
            fail(f"{name} MUDOU no servidor (sha256 {old[:12]} -> {digest[:12]}). "
                 "Releia o arquivo antes de publicar; as regras podem ter mudado.")
            drift.append(name)
        else:
            ok(f"{name} inalterado desde a ultima verificacao")

    if update_pins:
        new_pins["fetched_note"] = "hashes aceitos por --update-pins"
        with open(PINS_PATH, "w", encoding="utf-8") as fh:
            json.dump(new_pins, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"  pins atualizados em {os.path.relpath(PINS_PATH)}")
        FAILURES[:] = [f for f in FAILURES if "MUDOU no servidor" not in f]
    elif drift:
        print("  -> depois de reler, rode de novo com --update-pins para fixar")
    return os.path.join(workdir, "security_scanner.py")


# ---------------------------------------------------------------- etapa 2
def run_real_scanner(scanner_path, zip_path):
    """Roda o PluginSecurityScanner do servidor, sem alterar a logica dele."""
    print("\n[2] Scanner de seguranca do servidor, rodado sobre o zip")
    if not scanner_path or not os.path.isfile(scanner_path):
        fail("security_scanner.py indisponivel — etapa nao executada")
        return

    # O scanner chama as ferramentas pelo NOME no PATH, nao via `python -m`, e
    # cada check vira passed=True quando a ferramenta falta. Sem esta checagem o
    # verde nao significa nada.
    missing = [t for t in ("bandit", "detect-secrets", "flake8") if not shutil.which(t)]
    if missing:
        fail(f"ferramentas ausentes no PATH: {', '.join(missing)} — o scanner "
             "passaria em silencio. pip install " + " ".join(missing))
        return

    sys.path.insert(0, os.path.dirname(scanner_path))
    djt = types.ModuleType("django.utils.translation")
    djt.gettext_lazy = lambda s: s
    sys.modules.setdefault("django", types.ModuleType("django"))
    sys.modules.setdefault("django.utils", types.ModuleType("django.utils"))
    sys.modules["django.utils.translation"] = djt

    class _QS:
        def filter(self, **kw):
            return self

        def values_list(self, *a, **kw):
            return []

    pm = types.ModuleType("plugins.models")
    pm.SecurityRule = type("SecurityRule", (), {"objects": _QS()})
    pl = types.ModuleType("plugins")
    pl.models = pm
    sys.modules["plugins"] = pl
    sys.modules["plugins.models"] = pm

    from security_scanner import PluginSecurityScanner  # noqa: E402

    class Rule:
        def __init__(self, cat, code):
            self.check_category, self.check_code = cat, code

    # Regras bloqueantes documentadas; e o modo sem regra alguma, em que o
    # servidor cai para `bandit -ll` (todos os testes padrao) e flake8 sem filtro.
    modes = {
        "regras configuradas": (
            [Rule("bandit", c) for c in
             "B102 B105 B106 B107 B304 B305 B307 B506 B602 B613".split()]
            + [Rule("file_analysis", c) for c in
               ("FILE_HIDDEN", "FILE_SUSPICIOUS", "FILE_EXECUTABLE")]),
        "sem regras (fallback -ll)": [],
    }
    for label, rules in modes.items():
        rep = PluginSecurityScanner(zip_path, enabled_rules=rules).scan()
        crit = rep["summary"]["critical"]
        total = rep["summary"]["total_issues"]
        bad = [c["name"] for c in rep["checks"] if not c["passed"]]
        if crit:
            fail(f"{label}: {crit} check(s) CRITICOS -> upload fica BLOCKED ({bad})")
        elif total:
            note(f"{label}: {total} achado(s) nao-criticos em {bad} — nao bloqueiam, "
                 "mas contam no relatorio")
        else:
            ok(f"{label}: 5/5 checks limpos")


# ---------------------------------------------------------------- etapa 3
def check_metadata(plugin_dir):
    print("\n[3] metadata.txt")
    path = os.path.join(plugin_dir, "metadata.txt")
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
        for key in cp["general"]:
            cp.get("general", key)          # forca a interpolacao ('%' cru quebra)
        ok("parseia com ConfigParser (nenhum '%' cru)")
    except (configparser.Error, KeyError) as exc:
        fail(f"metadata.txt nao parseia: {exc} — escape '%' como '%%' ou reescreva")
        return {}

    # ConfigParser normaliza as chaves para minusculas (optionxform), entao a
    # busca por 'qgisMinimumVersion' tem de ser case-insensitive.
    values = {k.lower(): cp.get("general", k, fallback="") for k in cp["general"]}
    missing = [f for f in REQUIRED_METADATA if not values.get(f.lower(), "").strip()]
    if missing:
        fail(f"campos obrigatorios ausentes/vazios: {', '.join(missing)}")
    else:
        ok(f"os {len(REQUIRED_METADATA)} campos obrigatorios estao presentes")

    if "supportsqt6" in values:
        fail("supportsQt6 esta DEPRECADO: o servidor avisa e pede para remover")
    else:
        ok("sem supportsQt6 (a flag foi deprecada)")

    # Valores multilinha chegam desindentados pelo ConfigParser, entao a entrada
    # do changelog e uma linha que COMECA com a versao.
    ver = values.get("version", "")
    entries = values.get("changelog", "").splitlines()
    if ver and not any(ln.strip().startswith(ver) for ln in entries):
        note(f"changelog nao tem entrada para a versao {ver}")
    return values


# ---------------------------------------------------------------- etapa 4
def check_urls(values):
    """validator.py testa estas URLs no upload; uma morta reprova o envio."""
    print("\n[4] URLs exigidas pelo validator")
    for field in URL_FIELDS:
        url = values.get(field.lower(), "").strip()
        if not url:
            (note if field == "homepage" else fail)(f"{field}: ausente")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": URL_UA})
        try:
            with urllib.request.urlopen(req, timeout=URL_TIMEOUT) as r:  # nosec B310
                ok(f"{field}: HTTP {r.status} {url}")
        except urllib.error.HTTPError as exc:
            fail(f"{field}: HTTP {exc.code} em {url}")
        except NET_ERRORS as exc:
            note(f"{field}: nao deu para checar ({exc}) — {url}")


# ---------------------------------------------------------------- etapa 5
def check_qt6_spellings(zip_path):
    """Relatorio Qt6: precisa ser ZERO. Ele bloqueou a 2.0.15."""
    print("\n[5] Relatorio de compatibilidade Qt6 (grafia dos enums)")
    hits = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.filelist:
            if not info.filename.endswith(".py"):
                continue
            text = zf.read(info.filename).decode("utf-8", errors="ignore")
            for num, line in enumerate(text.splitlines(), 1):
                if QT6_OLD_SPELLINGS.search(line):
                    hits.append(f"{info.filename}:{num}: {line.strip()[:90]}")
    if hits:
        fail(f"{len(hits)} grafia(s) Qt5 sem escopo no pacote:")
        for h in hits[:20]:
            print(f"            {h}")
    else:
        ok("nenhuma grafia antiga nem exec_ no pacote")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip_path", help="zip gerado por build_release.sh")
    ap.add_argument("--update-pins", action="store_true",
                    help="aceita as regras atuais do servidor como a nova referencia")
    args = ap.parse_args()

    zip_path = os.path.abspath(args.zip_path)
    if not os.path.isfile(zip_path):
        sys.exit(f"zip nao encontrado: {zip_path}")
    plugin_dir = os.path.dirname(HERE)

    print(f"preflight de {os.path.basename(zip_path)}")
    workdir = tempfile.mkdtemp()
    try:
        scanner = check_upstream_rules(workdir, args.update_pins)
        run_real_scanner(scanner, zip_path)
        values = check_metadata(plugin_dir)
        if values:
            check_urls(values)
        check_qt6_spellings(zip_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"REPROVADO: {len(FAILURES)} problema(s) — NAO envie este zip")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"APROVADO no preflight ({len(NOTES)} aviso(s) nao bloqueante(s))")
    print("Lembrete: a revisao manual NAO depende disto. Ver RELEASE.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
