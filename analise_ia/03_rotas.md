# ETAPA 3 - Ponto de Entrada e Rotas

## Arquivo de Inicialização Principal: `app.py`
O sistema utiliza o `app.py` como ponto de entrada principal do Flask. Abaixo está a estrutura de inicialização (setup da aplicação, configuração, middlewares e proteção de rotas). *O corpo das funções e lógica interna das rotas foram omitidos para foco na estrutura e segurança.*

```python
import logging
import re
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import bcrypt
from bson.objectid import ObjectId

from config import Config
from database import db_manager
from importador_plano_contas import ImportadorPlanoContas
from importador_lancamentos import ImportadorLancamentos
from importador_universal import ImportadorUniversal
from services.motor_contabil import MotorContabil, PartidaContabil, DesbalanceamentoContabilError, ContaInvalidaError
from services.relatorios_contabeis import RelatoriosContabeis
from services.bancos_service import BancosService

# Configuração de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Criação da aplicação Flask com pastas estáticas/templates controladas por pathlib
app = Flask(
    __name__,
    template_folder=Config.TEMPLATE_FOLDER,
    static_folder=Config.STATIC_FOLDER
)
app.config.from_object(Config)

# Middlewares e Decorators de Segurança
# (Omitidos: ReverseProxyPrefixMiddleware, login_required, master_admin_required, check_session_inactivity)
```

## Mapeamento de Rotas da API e Sistema

Abaixo estão os mapeamentos de endpoints HTTP e suas respectivas funções de controle, responsáveis por interligar os módulos do ERP:

### Públicas / Autenticação
- `GET /` -> `index()`
- `GET/POST /cadastrar-grupo` -> `cadastrar_grupo()`
- `GET/POST /login` -> `login()`
- `GET/POST /recuperar-senha` -> `recuperar_senha()`
- `GET/POST /resetar-senha` -> `resetar_senha()`
- `GET /logout` -> `logout()` *(Requer Login)*

### Dashboard e Configurações (Requerem Login)
- `GET /dashboard` -> `dashboard()`
- `GET /usuarios` -> `usuarios()`
- `POST /usuarios/salvar` -> `salvar_usuario()` (Inferido)
- `GET/POST /config/filiais` -> `filiais()`
- `GET/POST /config/centros-custo` -> `centros_custo()`
- `GET/POST /config/contas-correntes` -> `contas_correntes()`
- `POST /config/contas-correntes/atualizar` -> `contas_correntes_atualizar()`
- `GET /config/regex` -> `config_regex()`
- `POST /config/regex/salvar` -> `salvar_regex()`

### Plano de Contas e Contabilidade (Requerem Login)
- `GET /plano-contas` -> `plano_contas()`
- `POST /plano-contas/salvar` -> `salvar_plano_contas()`
- `GET /api/plano-contas/buscar/<codigo>` -> `buscar_plano_conta()`
- `POST /plano-contas/atualizar` -> `atualizar_plano_conta()`
- `POST /plano-contas/importar-padrao` -> `importar_padrao()`
- `POST /plano-contas/importar-csv` -> `importar_csv_plano()`
- `GET /contabil/lancamentos` -> `contabil_lancamentos()`
- `POST /contabilidade/lancamento/atualizar/<id_lancamento>` -> `atualizar_lancamento_contabil()`
- `GET /contabil/razao` -> `contabil_razao()`
- `GET /contabil/razonetes` -> `contabil_razonetes()`

### Financeiro e Participantes (Requerem Login)
- `GET /participantes` -> `participantes()`
- `POST /participantes/salvar` -> `salvar_participante()`
- `GET /api/participantes/buscar/<id>` -> `buscar_participante()`
- `POST /participantes/atualizar` -> `atualizar_participante()`
- `POST /api/participantes/rapido` -> `salvar_participante_rapido()`
- `GET /contas-pagar` -> `contas_pagar()`
- `POST /contas-pagar/salvar` -> `salvar_conta_pagar()`
- `POST /contas-pagar/liquidar` -> `liquidar_conta_pagar()`
- `GET /contas-receber` -> `contas_receber()`
- `POST /contas-receber/salvar` -> `salvar_conta_receber()`
- `POST /contas-receber/liquidar` -> `liquidar_conta_receber()`
- `POST /financeiro/atualizar/<tipo_fluxo>/<id_titulo>` -> `atualizar_titulo()`

### Relatórios Contábeis (Requerem Login)
- `GET /financeiro/dre` -> `dre()`
- `GET /financeiro/balanco` -> `balanco()`
- `GET /financeiro/dre-anual` -> `dre_anual()`
- `GET /financeiro/dfc` -> `dfc()`

### Importação e Conciliação (Requerem Login)
- `POST /lancamentos/importar-csv` -> `importar_csv_lancamentos()`
- `GET /api/bancos` -> `listar_bancos()`
- `POST /config/bancos/importar-csv` -> `importar_csv_bancos()`
- `POST /config/bancos/sincronizar` -> `sincronizar_bancos()`
- `GET /importar-ofx` -> `importar_ofx()`
- `POST /importar-ofx/upload` -> `upload_ofx()`
- `GET /classificar-movimentos` -> `classificar_movimentos()`
- `POST /movimentos/conciliar` -> `conciliar_movimentos()`
- `GET /importacao-universal` -> `importacao_universal()`
- `POST /importacao-universal/preview` -> `importacao_universal_preview()`
- `POST /importacao-universal/commit` -> `importacao_universal_commit()`
- `GET /api/importacao/pendencias` -> `buscar_pendencias_importacao()`
- `POST /api/lancamentos-pendentes/reprocessar` -> `reprocessar_pendencias()`

### BPO e Painel Master (Requerem Permissão Especial)
- `GET /admin/dashboard` (ou `/admin-master`, `/gestao-master`) -> `gestao_master()`
- `POST /admin/grupo/atualizar` -> `atualizar_grupo()`
- `POST /admin/grupo/atualizar-completo` -> `atualizar_grupo_completo()`
- `GET /admin/bpo-central` -> `bpo_central()`
- `GET/POST /admin/bpo/operar/<grupo_slug>` -> `bpo_operar()`
- `GET /admin/bpo/sair` -> `bpo_sair()`
