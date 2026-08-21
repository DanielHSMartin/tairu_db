# Publicação do plugin no plugins.qgis.org

## A revisão manual é o caminho normal — não é uma reprovação

Isto foi lido no código do servidor, não inferido da documentação
(`qgis-app/plugins/views.py`, `tasks/run_security_scan.py` do repositório
[qgis/QGIS-Plugins-Website](https://github.com/qgis/QGIS-Plugins-Website)):

1. Todo upload nasce com `approved = False` e `validation_status = validating`.
   O comentário no código é literal: *"Always start unapproved"*.
2. A varredura roda em segundo plano. **Só Bandit e Secrets Detection são
   críticos**; se qualquer um acusa, a versão vira `blocked` e nem pode ser
   aprovada. Flake8 e File Permissions são `info`, Suspicious Files é `warning` —
   contam no relatório e não bloqueiam.
3. Sem achado crítico a versão vira `validated` e **fica esperando um aprovador
   voluntário**. Passar em tudo leva a este estado, não à publicação.
4. A publicação automática exige as duas coisas juntas: o autor ter a permissão
   `plugins.can_approve` (usuário *trusted*) **e** marcar "Publish immediately"
   no formulário de envio. O padrão do código é `auto_approve = False` para todo
   mundo.

Ou seja: nenhuma correção no zip encurta a fila. As versões 2.0.1 a 2.0.14
passaram todas por revisão manual e foram aprovadas.

### O que realmente pode travar a fila em silêncio

`_notify_staff_for_review()` **retorna sem fazer nada** quando
`plugin.is_email_confirmed` é falso. Sem o e-mail de contato confirmado, a versão
fica `validated` para sempre e **nenhum aprovador é sequer avisado**. A aprovação
também é barrada explicitamente por esse mesmo motivo. Então, ao publicar:

- confirme o e-mail de contato do plugin (o link é enviado quando a validação
  passa; há um "Resend confirmation" na página do plugin);
- se a espera incomodar de forma recorrente, peça o status de *trusted user* —
  é o único caminho para a publicação imediata.

O relatório **Qt6 é separado** da varredura de segurança e não aparece nesse
fluxo de aprovação, mas ele barrou a 2.0.15 **no primeiro envio**. A correção —
grafia com escopo em todo o pacote, sem shim PyQt5/PyQt6 — foi reenviada com o
mesmo número e **a 2.0.15 está aprovada e publicada desde 2026-08-18**
(confirmado baixando o zip publicado: nenhuma grafia antiga, nenhum `exec_`).
Ou seja: o relatório precisa ficar em zero, e ficar em zero resolve. Não trate a
2.0.15 como reprovada ao numerar a próxima versão.

## Procedimento de verificação antes de enviar

```bash
# 1. versão e changelog em metadata.txt (não bumpar se a anterior não foi aprovada)
# 2. construir — roda os gates locais e recusa o zip se algum falhar
bash QGIS/tairu_db/build_release.sh

# 3. verificar contra as regras REAIS do servidor
python3 QGIS/tairu_db/tools/qgis_preflight.py QGIS/tairu_db-<versão>.zip
```

O `qgis_preflight.py` faz cinco coisas:

| Etapa | O que verifica |
|---|---|
| 1 | Baixa `security_scanner.py`, `validator.py` e `tasks/run_security_scan.py` e compara com os hashes fixados em `tools/qgis_upstream_rules.json`. **Se mudaram, ele reprova**: as exigências podem ter mudado e o arquivo precisa ser relido antes de publicar. Depois de ler, `--update-pins` fixa a nova referência. |
| 2 | Roda o `PluginSecurityScanner` **do próprio servidor** sobre o zip, nos dois modos (com as regras configuradas, e sem regra alguma — em que o servidor cai para `bandit -ll` e flake8 sem filtro). Antes disso confere que `bandit`, `detect-secrets` e `flake8` estão no PATH: o scanner os chama pelo nome e **cada check vira "passou" em silêncio quando a ferramenta falta**. |
| 3 | `metadata.txt`: os 9 campos obrigatórios do `validator.py`, interpolação do ConfigParser (`%` cru quebra o upload) e a flag `supportsQt6`, hoje **deprecada** — o servidor avisa e pede para remover. |
| 4 | `tracker`, `repository` e `homepage` respondendo: o `validator.py` testa essas URLs no envio. |
| 5 | Grafias Qt5 sem escopo e `exec_` no pacote — o relatório Qt6, que precisa ser zero. |

Não substitui julgamento: o conjunto de regras ativo é configuração do servidor
(`enabled_bandit_rules` etc. vêm do admin), então um preflight limpo significa
"nenhum problema conhecido", nunca "vai passar".

## Envio

O upload é externo e usa credenciais do autor — nunca automatize:

```bash
python3 QGIS/tairu_db/plugin_upload.py -u USUARIO -w SENHA QGIS/tairu_db-<versão>.zip
```

ou pela interface web. Depois, confira na página da versão o resultado da
varredura e do relatório Qt6.
