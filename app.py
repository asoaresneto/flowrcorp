import logging
import re
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import bcrypt
from bson.objectid import ObjectId
from bson.decimal128 import Decimal128
from decimal import Decimal

from config import Config
from database import db_manager
from importador_plano_contas import ImportadorPlanoContas
from importador_lancamentos import ImportadorLancamentos
from importador_universal import ImportadorUniversal
from services.motor_contabil import MotorContabil, PartidaContabil, DesbalanceamentoContabilError, ContaInvalidaError, PeriodoFechadoError
from services.relatorios_contabeis import RelatoriosContabeis
from services.bancos_service import BancosService

# Configuração de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Middleware para tratar Proxy Reverso (Nginx) rodando sob subcaminho (ex: /corp)
class ReverseProxyPrefixMiddleware:
    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        # Lê o cabeçalho X-Script-Name enviado pelo Nginx, se houver.
        # Fallback para o prefixo configurado no config.py / .env
        script_name = environ.get("HTTP_X_SCRIPT_NAME", self.prefix)
        
        if script_name:
            # SCRIPT_NAME diz ao Flask qual é o prefixo base da URL externa
            environ["SCRIPT_NAME"] = script_name
            path_info = environ.get("PATH_INFO", "")
            
            # Se o PATH_INFO enviado pelo Nginx já começar com o prefixo,
            # removemos para que as rotas internas do Flask casem corretamente (ex: /corp/login vira /login)
            if path_info.startswith(script_name):
                environ["PATH_INFO"] = path_info[len(script_name):]
        
        # Reconhece protocolo seguro (HTTPS) encaminhado pelo proxy reverso
        scheme = environ.get("HTTP_X_FORWARDED_PROTO", "")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
            
        return self.wsgi_app(environ, start_response)


# Criação da aplicação Flask com pastas estáticas/templates controladas por pathlib
app = Flask(
    __name__,
    template_folder=Config.TEMPLATE_FOLDER,
    static_folder=Config.STATIC_FOLDER
)
app.config.from_object(Config)

# Aplica o middleware de proxy
app.wsgi_app = ReverseProxyPrefixMiddleware(app.wsgi_app, prefix=Config.SCRIPT_NAME_PREFIX)


# Decorador para exigir login nas rotas privadas
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            logging.info("Acesso negado: Usuário não autenticado.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


# Helper de Validação Master
def is_master_admin_session():
    return (
        session.get("role") == "superadmin" or 
        session.get("permissao_master") is True or 
        session.get("impersonator_role") == "superadmin" or
        session.get("impersonator_permissao_master") is True
    )


# Decorador para exigir permissão de Administrador Master (Superadmin)
def master_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        is_super = (
            session.get("role") == "superadmin" or 
            session.get("permissao_master") is True or 
            session.get("impersonator_role") == "superadmin" or
            session.get("impersonator_permissao_master") is True
        )
        if not is_super:
            logging.warning(f"Tentativa de acesso negada a rota master: {session.get('user_email')}")
            flash("Acesso restrito. Esta rota exige permissão de Administrador Master (Superadmin).", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function


# Hook executado antes de cada requisição para controle estrito de inatividade (5 minutos)
@app.before_request
def check_session_inactivity():
    # Ignora verificação para rotas públicas e arquivos estáticos
    if request.endpoint in ["login", "logout", "static"] or not request.endpoint:
        return

    if "user_id" in session:
        last_activity = session.get("last_activity")
        now = datetime.now(timezone.utc).timestamp()
        
        if last_activity:
            elapsed_seconds = now - last_activity
            timeout_limit = Config.PERMANENT_SESSION_LIFETIME_MINUTES * 60
            
            if elapsed_seconds > timeout_limit:
                logging.info(f"Sessão do usuário {session.get('user_email')} expirada por inatividade. Tempo: {elapsed_seconds:.1f}s")
                session.clear()
                # Redireciona com parâmetro de timeout para mostrar mensagem no front
                return redirect(url_for("login", reason="timeout"))
        
        # Atualiza a atividade do usuário para evitar expiração nas requisições ativas
        session["last_activity"] = now


# ROTA: Raiz (Redirecionamento para Login)
@app.route("/")
def index():
    return redirect(url_for("login"))


# ROTA: Cadastro de Novo Grupo Econômico (Onboarding Self-Service)
@app.route("/cadastrar-grupo", methods=["GET", "POST"], strict_slashes=False)
def cadastrar_grupo():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
        
    error_msg = None
    if request.method == "POST":
        nome_grupo = request.form.get("nome_grupo", "").strip()
        nome_admin = request.form.get("nome_admin", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        cnpj = request.form.get("cnpj", "").strip()
        
        if not nome_grupo or not nome_admin or not email or not password or not confirm_password or not cnpj:
            error_msg = "Todos os campos são obrigatórios."
            return render_template("cadastro_grupo.html", error=error_msg)
            
        if password != confirm_password:
            error_msg = "A senha e a confirmação de senha não coincidem."
            return render_template("cadastro_grupo.html", error=error_msg)
            
        if len(password) < 6:
            error_msg = "A senha deve ter no mínimo 6 caracteres."
            return render_template("cadastro_grupo.html", error=error_msg)
            
        try:
            admin_db = db_manager.get_admin_db()
            if admin_db is None:
                raise Exception("Não foi possível conectar ao banco central.")
                
            # 1. Valida se o email já existe
            existing_user = admin_db.usuarios_master.find_one({"email": email})
            if existing_user:
                error_msg = "Este endereço de e-mail já está cadastrado no sistema."
                return render_template("cadastro_grupo.html", error=error_msg)
                
            # 2. Cria o slug/db_name automático
            import unicodedata
            import re
            
            # Lógica simplificada de slugify
            slug_base = unicodedata.normalize('NFKD', nome_grupo).encode('ascii', 'ignore').decode('utf-8')
            slug_base = re.sub(r'[^\w\s-]', '', slug_base).strip().lower()
            slug_base = re.sub(r'[-\s]+', '_', slug_base)
            db_name = f"db_grupo_{slug_base}"
            
            # Evita colisões no db_name
            original_db_name = db_name
            counter = 1
            while admin_db.assinaturas_grupos.find_one({"db_name": db_name}) is not None:
                db_name = f"{original_db_name}_{counter}"
                counter += 1
                
            # 3. Insere o registro na coleção 'assinaturas_grupos'
            grupo_doc = {
                "nome_grupo": nome_grupo,
                "db_name": db_name,
                "status": "Ativo",
                "valor_mensalidade": 0.00,
                "max_cnpjs": 1,
                "max_usuarios": 2,
                "data_vencimento": "10",
                "flag_bpo": False
            }
            grupo_res = admin_db.assinaturas_grupos.insert_one(grupo_doc)
            grupo_id = grupo_res.inserted_id
            
            # 4. Cria o usuário master
            senha_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            user_doc = {
                "nome": nome_admin,
                "email": email,
                "senha": senha_hash,
                "grupo_economico_id": grupo_id,
                "permissao_master": True,
                "role": "superadmin"
            }
            admin_db.usuarios_master.insert_one(user_doc)
            
            # 5. Inicializa o banco isolado do tenant
            tenant_db = db_manager.get_group_db(db_name)
            if tenant_db is not None:
                # 5.1 Popula Plano de Contas padrão
                contas_padrao = [
                    {"codigo": "1", "nome": "Ativo", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "1.1", "nome": "Ativo Circulante", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "1.1.1", "nome": "Caixa e Equivalentes de Caixa", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Analítica", "cmv": False},
                    {"codigo": "1.1.2", "nome": "Contas a Receber de Clientes", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Analítica", "cmv": False},
                    {"codigo": "2", "nome": "Passivo", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "2.1", "nome": "Passivo Circulante", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "2.1.1", "nome": "Fornecedores a Pagar", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                    {"codigo": "2.1.2", "nome": "Obrigações Sociais e Trabalhistas", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                    {"codigo": "3", "nome": "Receitas", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "3.1", "nome": "Receita Bruta de Vendas", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "3.1.1", "nome": "Receita de Serviços Prestados", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                    {"codigo": "4", "nome": "Custos e Despesas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "4.1", "nome": "Custos Operacionais", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "4.1.1", "nome": "Mão de Obra Direta BPO", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": True},
                    {"codigo": "4.1.2", "nome": "Licenças de ERP Repassadas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": True},
                    {"codigo": "4.2", "nome": "Despesas Gerais", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                    {"codigo": "4.2.1", "nome": "Aluguel Comercial", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": False}
                ]
                tenant_db.plano_contas.insert_many(contas_padrao)
                
                # 5.2 Popula Empresa Principal em participantes
                participante_principal = {
                    "nome_razao": nome_grupo,
                    "tipo": "Empresa Principal",
                    "cnpj_cpf": cnpj,
                    "email": email,
                    "telefone": ""
                }
                tenant_db.participantes.insert_one(participante_principal)
                
                # 5.3 Cria usuário de auditoria isolado na coleção usuarios
                tenant_db.usuarios.insert_one({
                    "nome": nome_admin,
                    "email": email
                })
                
            flash("Assinatura criada com sucesso! Faça login para começar a usar a plataforma.", "success")
            return redirect(url_for("login"))
            
        except Exception as e:
            logging.error(f"Erro no auto-cadastro de grupo: {e}")
            error_msg = f"Erro interno ao processar cadastro: {str(e)}"
            return render_template("cadastro_grupo.html", error=error_msg)
            
    return render_template("cadastro_grupo.html")


# ROTA: Login
@app.route("/login", methods=["GET", "POST"])
def login():
    # Se já logado, redireciona ao dashboard
    if "user_id" in session:
        return redirect(url_for("dashboard"))
        
    error_msg = None
    reason = request.args.get("reason")
    
    if reason == "timeout":
        error_msg = "Sua sessão expirou por inatividade (limite de 5 minutos). Faça login novamente."
    elif reason == "unauthorized":
        error_msg = "Acesso restrito. Faça login para acessar esta página."

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("password", "").strip()
        
        if not email or not senha:
            error_msg = "Por favor, preencha todos os campos."
            return render_template("login.html", error=error_msg)

        try:
            admin_db = db_manager.get_admin_db()
            if admin_db is None:
                raise Exception("Não foi possível conectar ao banco central do sistema.")
                
            # 1. Busca o Usuário no Banco Central (db_flow_admin)
            user = admin_db.usuarios_master.find_one({"email": email})
            if not user:
                error_msg = "E-mail ou senha incorretos."
                return render_template("login.html", error=error_msg)
                
            # 2. Valida a Senha Hashed (Bcrypt)
            stored_hash = user.get("senha", "")
            if not bcrypt.checkpw(senha.encode("utf-8"), stored_hash.encode("utf-8")):
                error_msg = "E-mail ou senha incorretos."
                return render_template("login.html", error=error_msg)
                
            # 3. Busca o Grupo Econômico associado e valida o status
            grupo_id = user.get("grupo_economico_id")
            grupo = admin_db.assinaturas_grupos.find_one({"_id": grupo_id})
            
            if not grupo:
                error_msg = "Grupo Econômico não encontrado para este usuário."
                return render_template("login.html", error=error_msg)
                
            status_grupo = grupo.get("status", "Suspenso")
            nome_grupo = grupo.get("nome_grupo", "Não cadastrado")
            
            # Vocabulário comercial: "Grupo Econômico"
            if status_grupo != "Ativo":
                if status_grupo == "Suspenso":
                    error_msg = f"O acesso para o Grupo Econômico '{nome_grupo}' está suspenso por pendências financeiras. Entre em contato com o suporte."
                elif status_grupo == "Cancelado":
                    error_msg = f"O acesso para o Grupo Econômico '{nome_grupo}' está cancelado. Contate o administrador."
                else:
                    error_msg = f"O status do Grupo Econômico '{nome_grupo}' é inválido. Acesso bloqueado."
                return render_template("login.html", error=error_msg)
                
            # Validação da Data Limite de Acesso (data_limite_acesso)
            data_limite_raw = grupo.get("data_limite_acesso")
            if data_limite_raw:
                dt_limite = None
                if isinstance(data_limite_raw, datetime):
                    dt_limite = data_limite_raw.date()
                elif isinstance(data_limite_raw, date):
                    dt_limite = data_limite_raw
                else:
                    try:
                        # Tenta parsear no formato YYYY-MM-DD
                        dt_limite = datetime.strptime(str(data_limite_raw).strip()[:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
                
                if dt_limite:
                    hoje = datetime.now().date()
                    if hoje > dt_limite:
                        data_limite_br = dt_limite.strftime("%d/%m/%Y")
                        error_msg = f"O período de acesso para o Grupo Econômico expirou em {data_limite_br}. Regularize sua assinatura."
                        return render_template("login.html", error=error_msg)
            
            # 4. Login bem-sucedido: configura a sessão Flask (isolamento multi-tenant dinâmico)
            session.clear()
            session.permanent = True
            session["user_id"] = str(user["_id"])
            session["user_name"] = user.get("nome")
            session["user_email"] = user.get("email")
            session["grupo_id"] = str(grupo["_id"])
            session["grupo_nome"] = grupo.get("nome_grupo")
            session["db_name"] = grupo.get("db_name")  # Nome do banco específico do cliente
            session["grupo_slug"] = grupo.get("db_name")  # Identificador para busca de banco dinâmico (slug do grupo)
            session["permissao_master"] = user.get("permissao_master", False)
            session["role"] = user.get("role", "user")
            session["last_activity"] = datetime.now(timezone.utc).timestamp()
            
            logging.info(f"Usuário {email} logado no Grupo Econômico '{nome_grupo}' (Banco: {session['db_name']}).")
            return redirect(url_for("dashboard"))
            
        except Exception as e:
            logging.error(f"Erro ao processar login: {e}")
            error_msg = f"Erro no servidor: {str(e)}"
            
    return render_template("login.html", error=error_msg)


# ROTA: Recuperar Senha (GET/POST)
@app.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha():
    error_msg = None
    success_msg = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if not email:
            error_msg = "Por favor, preencha o e-mail."
            return render_template("recuperar_senha.html", error=error_msg)
            
        try:
            admin_db = db_manager.get_admin_db()
            if admin_db is None:
                raise Exception("Não foi possível conectar ao banco central do sistema.")
                
            user = admin_db.usuarios_master.find_one({"email": email})
            if not user:
                # Segurança: não revela se o e-mail existe ou não
                success_msg = "Se o e-mail estiver cadastrado, você receberá um link para redefinir sua senha."
                return render_template("recuperar_senha.html", success=success_msg)
                
            # Gera token de reset
            import secrets
            token = secrets.token_urlsafe(32)
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            
            admin_db.usuarios_master.update_one(
                {"_id": user["_id"]},
                {"$set": {"reset_token": token, "reset_token_expires": expires}}
            )
            
            reset_link = url_for("resetar_senha", token=token, _external=True)
            logging.info(f"[Recuperação de Senha] Link de reset para {email}: {reset_link}")
            
            # Envio do e-mail via SMTP
            try:
                import smtplib
                from email.mime.text import MIMEText
                
                msg = MIMEText(f"Olá,\n\nVocê solicitou a redefinição de senha para o Flow Empresas. Clique no link abaixo para cadastrar uma nova senha:\n\n{reset_link}\n\nEste link é válido por 1 hora.\n\nSe você não solicitou esta redefinição, desconsidere este e-mail.")
                msg["Subject"] = "Flow Empresas - Recuperação de Senha"
                msg["From"] = Config.SMTP_FROM
                msg["To"] = email
                
                with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=10) as server:
                    server.starttls()
                    server.login(Config.SMTP_USER, Config.SMTP_PASS)
                    server.sendmail(Config.SMTP_FROM, [email], msg.as_string())
                    
                logging.info(f"E-mail de recuperação enviado com sucesso para {email}.")
            except Exception as mail_err:
                logging.error(f"Falha ao enviar e-mail de recuperação para {email}: {mail_err}. O link foi gerado e registrado nos logs.")
                
            success_msg = "Se o e-mail estiver cadastrado, você receberá um link para redefinir sua senha."
            return render_template("recuperar_senha.html", success=success_msg)
            
        except Exception as e:
            logging.error(f"Erro na recuperação de senha: {e}")
            error_msg = f"Erro no servidor: {str(e)}"
            
    return render_template("recuperar_senha.html", error=error_msg)


# ROTA: Resetar Senha (GET/POST)
@app.route("/resetar-senha", methods=["GET", "POST"])
def resetar_senha():
    error_msg = None
    success_msg = None
    token = request.args.get("token", "").strip()
    
    if not token:
        return render_template("resetar_senha.html", valid=False, error="Token de redefinição inválido ou ausente.")
        
    try:
        admin_db = db_manager.get_admin_db()
        if admin_db is None:
            raise Exception("Não foi possível conectar ao banco central do sistema.")
            
        user = admin_db.usuarios_master.find_one({"reset_token": token})
        if not user:
            return render_template("resetar_senha.html", valid=False, error="Token de redefinição inválido ou expirado.")
            
        # Valida expiração
        expires = user.get("reset_token_expires")
        if expires:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires:
                return render_template("resetar_senha.html", valid=False, error="O token de redefinição expirou.")
                
        if request.method == "POST":
            senha = request.form.get("password", "").strip()
            confirmar = request.form.get("confirm_password", "").strip()
            
            if not senha or not confirmar:
                error_msg = "Por favor, preencha todos os campos."
                return render_template("resetar_senha.html", valid=True, error=error_msg)
                
            if senha != confirmar:
                error_msg = "As senhas não coincidem."
                return render_template("resetar_senha.html", valid=True, error=error_msg)
                
            # Hash da nova senha
            import bcrypt
            hashed_pw = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            
            # Atualiza no banco e limpa o token
            admin_db.usuarios_master.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {"senha": hashed_pw},
                    "$unset": {"reset_token": "", "reset_token_expires": ""}
                }
            )
            
            flash("Sua senha foi redefinida com sucesso! Faça login com a nova senha.", "success")
            return redirect(url_for("login"))
            
        return render_template("resetar_senha.html", valid=True)
        
    except Exception as e:
        logging.error(f"Erro na redefinição de senha: {e}")
        return render_template("resetar_senha.html", valid=False, error=f"Erro no servidor: {str(e)}")


# ROTA: Dashboard (Privada e Isolada por Grupo Econômico)
@app.route("/dashboard")
@login_required
def dashboard():
    grupo_slug = session.get("grupo_slug")
    grupo_nome = session.get("grupo_nome")
    
    periodo = request.args.get("periodo", datetime.today().strftime("%Y-%m"))
    cnpj_filtro = request.args.get("cnpj", "").strip()
    centro_custo_filtro = request.args.get("centro_custo", "").strip()
    
    transacoes = []
    conn_error = None
    
    receitas = 0.0
    cmv = 0.0
    despesas_operacionais = 0.0
    
    meses_lista = []
    chart_data = {}
    contas_analiticas = []
    
    centros_custo = ["Geral"]
    participantes_cnpj = []
    
    try:
        tenant_db = db_manager.get_group_db(grupo_slug)
        if tenant_db is None:
            raise Exception("Não foi possível conectar ao banco de dados isolado deste Grupo Econômico.")
            
        # 1. Carrega filtros globais (participantes e centros de custo ativos)
        participantes_cnpj = list(tenant_db.participantes.find({"cnpj_cpf": {"$ne": ""}}).sort("nome_razao", 1))
        
        cc_trans = tenant_db.transacoes.distinct("centro_custo")
        cc_pagar = tenant_db.contas_pagar.distinct("centro_custo")
        cc_receber = tenant_db.contas_receber.distinct("centro_custo")
        unique_cc = set(cc_trans + cc_pagar + cc_receber)
        unique_cc.discard(None)
        unique_cc.discard("")
        centros_custo = sorted(list(unique_cc))
        if "Geral" not in centros_custo:
            centros_custo.insert(0, "Geral")
            
        # 2. Carrega plano de contas analítico para o checkbox do gráfico
        contas_analiticas = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}).sort("codigo", 1))
        cmv_accounts = set(c["codigo"] for c in tenant_db.plano_contas.find({"cmv": True}))
        
        # 3. Constrói Filtro e Calcula KPI do Mês Atual
        match_query = {"data": {"$regex": f"^{periodo}"}}
        if cnpj_filtro:
            part = tenant_db.participantes.find_one({"cnpj_cpf": cnpj_filtro})
            if part:
                match_query["participante_id"] = str(part["_id"])
            else:
                match_query["participante_id"] = "non-existent"
        if centro_custo_filtro and centro_custo_filtro != "Geral":
            match_query["centro_custo"] = centro_custo_filtro
            
        transacoes_cursor = tenant_db.transacoes.find(match_query).sort("data", -1)
        transacoes = list(transacoes_cursor)
        
        for t in transacoes:
            val = abs(t.get("valor", 0.0))
            if t.get("tipo") == "Receita":
                receitas += val
            else:
                if t.get("conta_codigo") in cmv_accounts:
                    cmv += val
                else:
                    despesas_operacionais += val
                    
        # 4. Histórico de 6 Meses para o Gráfico
        base_date = datetime.strptime(periodo, "%Y-%m")
        for i in range(5, -1, -1):
            y = base_date.year
            m = base_date.month - i
            while m <= 0:
                m += 12
                y -= 1
            meses_lista.append(f"{y:04d}-{m:02d}")
            
        start_month = meses_lista[0]
        end_month = meses_lista[-1]
        
        history_query = {
            "data": {
                "$gte": f"{start_month}-01",
                "$lte": f"{end_month}-31"
            }
        }
        if cnpj_filtro:
            part = tenant_db.participantes.find_one({"cnpj_cpf": cnpj_filtro})
            if part:
                history_query["participante_id"] = str(part["_id"])
            else:
                history_query["participante_id"] = "non-existent"
        if centro_custo_filtro and centro_custo_filtro != "Geral":
            history_query["centro_custo"] = centro_custo_filtro
            
        history_trans = list(tenant_db.transacoes.find(history_query))
        
        for c in contas_analiticas:
            chart_data[c["codigo"]] = [0.0] * 6
            
        for t in history_trans:
            code = t.get("conta_codigo")
            t_date = t.get("data", "")
            if code in chart_data and len(t_date) >= 7:
                t_month = t_date[0:7]
                if t_month in meses_lista:
                    idx = meses_lista.index(t_month)
                    chart_data[code][idx] += t.get("valor", 0.0)
                    
    except Exception as e:
        logging.error(f"Erro ao consultar banco do Grupo {grupo_nome}: {e}")
        conn_error = f"Erro ao acessar banco isolado: {str(e)}"
        
    resultado_bruto = receitas - cmv
    receita_liquida = resultado_bruto - despesas_operacionais
    margem_liquida = (receita_liquida / receitas * 100.0) if receitas > 0 else 0.0
    
    # Executa a auditoria para exibir no topo do painel
    alertas_integridade_count = 0
    try:
        if tenant_db is not None:
            executar_auditoria_integridade(tenant_db)
            alertas_integridade_count = tenant_db.alertas_integridade.count_documents({})
    except Exception as e:
        logging.error(f"Erro ao injetar alertas de integridade no dashboard: {e}")

    import json
    return render_template(
        "dashboard.html",
        transacoes=transacoes,
        conn_error=conn_error,
        receitas=receitas,
        cmv=cmv,
        resultado_bruto=resultado_bruto,
        despesas_operacionais=despesas_operacionais,
        receita_liquida=receita_liquida,
        margem_liquida=margem_liquida,
        periodo=periodo,
        cnpj_filtro=cnpj_filtro,
        centro_custo_filtro=centro_custo_filtro,
        centros_custo=centros_custo,
        participantes_cnpj=participantes_cnpj,
        contas_analiticas=contas_analiticas,
        meses_lista=meses_lista,
        chart_data_json=json.dumps(chart_data),
        alertas_integridade_count=alertas_integridade_count
    )


# AUXILIAR: Auditoria de Integridade de Arquivos OFX
def executar_auditoria_integridade(tenant_db):
    alertas = []
    if tenant_db is None:
        return alertas
        
    try:
        # 1. Verifica furos cronológicos superiores a 5 dias nas transações
        trans = list(tenant_db.transacoes.find().sort("data", 1))
        for idx in range(1, len(trans)):
            prev_t = trans[idx - 1]
            curr_t = trans[idx]
            
            try:
                dt_prev = datetime.strptime(prev_t["data"], "%Y-%m-%d")
                dt_curr = datetime.strptime(curr_t["data"], "%Y-%m-%d")
                delta = (dt_curr - dt_prev).days
                if delta > 5:
                    alertas.append({
                        "tipo": "Lacuna Cronológica",
                        "mensagem": f"Lacuna identificada: {delta} dias sem movimentação registrada entre {prev_t['data']} e {curr_t['data']}."
                    })
            except Exception as e:
                logging.error(f"Erro ao verificar gap cronológico entre transações: {e}")
                
        # 2. Verifica discrepâncias de saldo entre arquivos OFX importados
        arquivos = list(tenant_db.ofx_arquivos_importados.find().sort("data_inicio", 1))
        for idx in range(1, len(arquivos)):
            anterior = arquivos[idx - 1]
            atual = arquivos[idx]
            
            try:
                saldo_final_anterior = float(anterior.get("saldo_final", 0.0))
                saldo_final_atual = float(atual.get("saldo_final", 0.0))
                soma_atual = float(atual.get("soma_transacoes", 0.0))
                
                # Saldo inicial calculado
                saldo_inicial_calculado = saldo_final_atual - soma_atual
                
                if abs(saldo_inicial_calculado - saldo_final_anterior) > 0.01:
                    alertas.append({
                        "tipo": "Discrepância de Saldo",
                        "mensagem": f"Furo de saldo identificado entre os arquivos {anterior['nome_arquivo']} e {atual['nome_arquivo']}. Saldo final em {anterior['data_fim']} era R$ {saldo_final_anterior:.2f}, mas o saldo inicial em {atual['data_inicio']} foi calculado como R$ {saldo_inicial_calculado:.2f}."
                    })
            except Exception as e:
                logging.error(f"Erro ao verificar discrepância de saldo de arquivos: {e}")
                
        # Salva na coleção persistente alertas_integridade
        tenant_db.alertas_integridade.drop()
        if alertas:
            tenant_db.alertas_integridade.insert_many(alertas)
            
    except Exception as e:
        logging.error(f"Erro ao executar auditoria de integridade: {e}")
        
    return alertas


# CONTEXT PROCESSOR: Injeta contagem de alertas de integridade de forma global
@app.context_processor
def inject_integrity_alerts():
    grupo_slug = session.get("grupo_slug")
    if not grupo_slug:
        return {"integridade_alertas_count": 0}
    tenant_db = db_manager.get_group_db(grupo_slug)
    if tenant_db is None:
        return {"integridade_alertas_count": 0}
    count = tenant_db.alertas_integridade.count_documents({})
    return {"integridade_alertas_count": count}


# HELPER: Gravação de lançamentos contábeis em partidas dobradas
def registrar_lancamento_contabil(tenant_db, data, descricao, conta_debito, conta_credito, valor, centro_custo="Geral"):
    try:
        valor = abs(float(valor))
        lancamento = {
            "data": data,
            "descricao": descricao,
            "conta_debito": conta_debito,
            "conta_credito": conta_credito,
            "valor": valor,
            "centro_custo": centro_custo or "Geral"
        }
        tenant_db.lancamentos_contabeis.insert_one(lancamento)
        logging.info(f"Lançamento contábil registrado com sucesso: D={conta_debito}, C={conta_credito}, V={valor}")
        return True
    except Exception as e:
        logging.error(f"Erro ao registrar lançamento contábil: {e}")
        return False


# FILTER: Formatação de data no formato brasileiro
@app.template_filter("formata_data")
def formata_data_filter(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    try:
        value_str = str(value).strip()
        # Se já está no formato DD/MM/AAAA (10 chars e barra no 2º ou 3º ind)
        if len(value_str) >= 10 and value_str[2] == '/':
            return value_str[:10]
            
        if len(value_str) >= 10:
            date_part = value_str[:10]
            if '-' in date_part:
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                return dt.strftime("%d/%m/%Y")
    except Exception:
        pass
    return ""


# HELPER: Converte valor em formato monetário brasileiro para float puro
def converter_valor_br_para_float(valor_str):
    if not valor_str:
        return 0.0
    try:
        clean_str = str(valor_str).replace(".", "").replace(",", ".").strip()
        return float(clean_str)
    except Exception:
        return 0.0


# HELPER: Resolve conta contábil ativa no plano de contas do tenant
def obter_conta_contabil_valida(db, codigos_preferidos):
    for cod in codigos_preferidos:
        exists = db.plano_contas.find_one({"codigo": cod})
        if exists:
            return cod
    return codigos_preferidos[0]


# HELPER: Estorno contábil qualificado por origem para evitar furos de razonete/lançamentos
def estornar_lancamentos_origem(db, documento_id, tipo_origem=None):
    try:
        query = {"documento_id": str(documento_id)}
        if tipo_origem:
            query["tipo_origem"] = tipo_origem
            
        # Encontra lançamentos associados ao documento_id (e tipo_origem se fornecido)
        lancamentos = list(db.lancamentos.find(query))
        for l in lancamentos:
            conta_debito = l["conta_debito"]
            conta_credito = l["conta_credito"]
            valor = l["valor"]
            
            # Debito
            nat_deb = "Débito" if (conta_debito.startswith("1") or conta_debito.startswith("4")) else "Crédito"
            dec_saldo_deb = -valor if nat_deb == "Débito" else valor
            db.razonetes.update_one(
                {"conta_codigo": conta_debito},
                {"$inc": {
                    "total_debito": -valor,
                    "saldo_debito": -valor,
                    "saldo": dec_saldo_deb
                }}
            )
            # Credito
            nat_cred = "Débito" if (conta_credito.startswith("1") or conta_credito.startswith("4")) else "Crédito"
            dec_saldo_cred = valor if nat_cred == "Débito" else -valor
            db.razonetes.update_one(
                {"conta_codigo": conta_credito},
                {"$inc": {
                    "total_credito": -valor,
                    "saldo_credito": -valor,
                    "saldo": dec_saldo_cred
                }}
            )
            
        db.lancamentos.delete_many(query)
        db.lancamentos_contabeis.delete_many(query)
        db.transacoes.delete_many(query)
        logging.info(f"Lançamentos estornados com sucesso para documento_id: {documento_id}, tipo_origem: {tipo_origem}")
        return True
    except Exception as e:
        logging.error(f"Erro ao estornar lançamentos contábeis: {e}")
        return False


# HELPER: Lançamento dinâmico em partidas dobradas com tipo_origem
def registrar_partida_dobrada(db, data, conta_debito, conta_credito, valor, historico, documento_id, tipo_origem):
    try:
        valor = abs(float(valor))
        # 1. Estorna qualquer lançamento existente para este documento_id e tipo_origem
        estornar_lancamentos_origem(db, documento_id, tipo_origem)
        
        # 2. Grava na coleção 'lancamentos'
        lancamento = {
            "data": data,
            "conta_debito": conta_debito,
            "conta_credito": conta_credito,
            "valor": valor,
            "historico": historico,
            "documento_id": str(documento_id),
            "tipo_origem": tipo_origem
        }
        db.lancamentos.insert_one(lancamento)
        
        # 3. Grava na coleção legada 'lancamentos_contabeis'
        db.lancamentos_contabeis.insert_one({
            "data": data,
            "descricao": historico,
            "conta_debito": conta_debito,
            "conta_credito": conta_credito,
            "valor": valor,
            "centro_custo": "Geral",
            "documento_id": str(documento_id),
            "tipo_origem": tipo_origem
        })
        
        # 4. Atualiza os saldos na coleção 'razonetes'
        # Conta Débito
        nat_deb = "Débito" if (conta_debito.startswith("1") or conta_debito.startswith("4")) else "Crédito"
        inc_saldo_deb = valor if nat_deb == "Débito" else -valor
        db.razonetes.update_one(
            {"conta_codigo": conta_debito},
            {"$inc": {
                "total_debito": valor,
                "saldo_debito": valor,
                "saldo": inc_saldo_deb
            }},
            upsert=True
        )
        # Conta Crédito
        nat_cred = "Débito" if (conta_credito.startswith("1") or conta_credito.startswith("4")) else "Crédito"
        inc_saldo_cred = -valor if nat_cred == "Débito" else valor
        db.razonetes.update_one(
            {"conta_codigo": conta_credito},
            {"$inc": {
                "total_credito": valor,
                "saldo_credito": valor,
                "saldo": inc_saldo_cred
            }},
            upsert=True
        )
        
        # 5. DRE Integration (Fato Gerador / Competência)
        is_provisao = False
        resultado_conta = None
        if (conta_debito.startswith("3") or conta_debito.startswith("4")) and (conta_credito in ["2.1.1", "2.1.01.001", "1.1.2", "1.1.03.001"]):
            is_provisao = True
            resultado_conta = conta_debito
        elif (conta_credito.startswith("3") or conta_credito.startswith("4")) and (conta_debito in ["2.1.1", "2.1.01.001", "1.1.2", "1.1.03.001"]):
            is_provisao = True
            resultado_conta = conta_credito
            
        if is_provisao and resultado_conta:
            tipo_trans = "Receita" if resultado_conta.startswith("3") else "Despesa"
            valor_trans = valor if tipo_trans == "Receita" else -valor
            conta_doc = db.plano_contas.find_one({"codigo": resultado_conta})
            conta_nome = conta_doc.get("nome", "") if conta_doc else ""
            
            db.transacoes.insert_one({
                "descricao": historico,
                "valor": valor_trans,
                "tipo": tipo_trans,
                "data": data,
                "conta_codigo": resultado_conta,
                "conta_nome": conta_nome,
                "origem": "Provisão Contábil",
                "documento_id": str(documento_id),
                "tipo_origem": tipo_origem
            })
            
        # 6. DFC Integration (Baixa / Regime de Caixa)
        is_baixa = False
        conta_origem = None
        valor_caixa = 0.0
        
        disponibilidade_pattern = ["1.1.1", "1.1.01.002"]
        def is_disponibilidade(code):
            return code.startswith("1.1.1") or code.startswith("1.1.01") or code.startswith("1.1.02")
            
        if is_disponibilidade(conta_debito) and (conta_credito in ["1.1.2", "1.1.03.001"]):
            # Recebimento de Clientes (entrada de caixa)
            is_baixa = True
            valor_caixa = valor
            raw_id = str(documento_id)
            doc_ref = db.contas_receber.find_one({"_id": ObjectId(raw_id)})
            conta_origem = doc_ref.get("conta_codigo") if doc_ref else None
        elif is_disponibilidade(conta_credito) and (conta_debito in ["2.1.1", "2.1.01.001"]):
            # Pagamento a Fornecedores (saída de caixa)
            is_baixa = True
            valor_caixa = -valor
            raw_id = str(documento_id)
            doc_ref = db.contas_pagar.find_one({"_id": ObjectId(raw_id)})
            conta_origem = doc_ref.get("conta_codigo") if doc_ref else None
            
        if is_baixa and conta_origem:
            conta_doc = db.plano_contas.find_one({"codigo": conta_origem})
            conta_nome = conta_doc.get("nome", "") if conta_doc else ""
            tipo_trans = "Receita" if valor_caixa >= 0 else "Despesa"
            
            db.transacoes.insert_one({
                "descricao": historico,
                "valor": valor_caixa,
                "tipo": tipo_trans,
                "data": data,
                "conta_codigo": conta_origem,
                "conta_nome": conta_nome,
                "origem": "Baixa Contábil",
                "documento_id": str(documento_id),
                "tipo_origem": tipo_origem,
                "fluxo_caixa": True
            })
            
        logging.info(f"Partida dobrada registrada com sucesso: D={conta_debito}, C={conta_credito}")
        return True
    except Exception as e:
        logging.error(f"Erro em registrar_partida_dobrada: {e}")
        return False


# ROTA: Logout
@app.route("/logout")
def logout():
    email = session.get("user_email")
    if email:
        logging.info(f"Usuário {email} fez logout voluntário.")
    session.clear()
    
    # Se foi redirecionamento por timeout do JavaScript
    reason = request.args.get("reason")
    if reason == "timeout":
        return redirect(url_for("login", reason="timeout"))
        
    return redirect(url_for("login"))


# AUXILIAR: Ordenação estruturada de códigos do Plano de Contas
def sort_accounts(accounts):
    def sort_key(acc):
        parts = acc.get("codigo", "").split(".")
        key = []
        for p in parts:
            try:
                key.append(int(p))
            except ValueError:
                key.append(p)
        return key
    return sorted(accounts, key=sort_key)


# ROTA: Configurações -> Plano de Contas (Listagem)
@app.route("/plano-contas")
@login_required
def plano_contas():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    contas = []
    if tenant_db is not None:
        try:
            contas_cursor = tenant_db.plano_contas.find()
            contas = sort_accounts(list(contas_cursor))
        except Exception as e:
            logging.error(f"Erro ao consultar plano de contas: {e}")
            flash(f"Erro ao carregar plano de contas: {e}", "danger")
            
    return render_template("plano_contas.html", contas=contas)


# ROTA: Salvar Conta do Plano de Contas
@app.route("/plano-contas/salvar", methods=["POST"])
@login_required
def salvar_plano_contas():
    codigo = request.form.get("codigo", "").strip()
    nome = request.form.get("nome", "").strip()
    tipo = request.form.get("tipo", "").strip()
    natureza = request.form.get("natureza", "").strip()
    classificacao = request.form.get("classificacao", "").strip()
    cmv = request.form.get("cmv") == "true"
    
    if not codigo or not nome or not tipo or not natureza or not classificacao:
        flash("Todos os campos obrigatórios devem ser preenchidos.", "danger")
        return redirect(url_for("plano_contas"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Validação: não permitir duplicar contas com o mesmo código (especialmente analíticas)
            existing = tenant_db.plano_contas.find_one({"codigo": codigo})
            if existing:
                flash(f"Já existe uma conta cadastrada com o código '{codigo}'. Não é possível duplicar códigos contábeis.", "danger")
                return redirect(url_for("plano_contas"))
                
            # Documento gravado na coleção 'plano_contas'
            # Exemplo de documento:
            # {
            #     "codigo": "1.1.1",
            #     "nome": "Caixa Geral",
            #     "tipo": "Patrimonial",
            #     "natureza": "Débito",
            #     "classificacao": "Analítica",
            #     "cmv": false
            # }
            nova_conta = {
                "codigo": codigo,
                "nome": nome,
                "tipo": tipo,
                "natureza": natureza,
                "classificacao": classificacao,
                "cmv": cmv,
                "grupo_dre": request.form.get("grupo_dre", "").strip() or None,
                "grupo_dfc": request.form.get("grupo_dfc", "").strip() or None,
                "faz_parte_dre": codigo.startswith("3") or codigo.startswith("4"),
                "faz_parte_dfc": any(codigo.startswith(p) for p in ("1.1", "1.2", "2.1", "2.2", "2.3", "3", "4")),
            }
            tenant_db.plano_contas.insert_one(nova_conta)
            
            resolvidos, _ = _reprocessar_pendentes_interno(tenant_db)
            msg_extra = f" {resolvidos} lançamento(s) pendente(s) reprocessado(s)." if resolvidos > 0 else ""
            
            flash(f"Conta '{codigo} - {nome}' cadastrada com sucesso no seu Grupo Econômico!{msg_extra}", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar conta contábil: {e}")
            flash(f"Erro ao salvar conta: {e}", "danger")
            
    return redirect(url_for("plano_contas"))


# ROTA: Buscar dados de uma conta contábil (JSON)
@app.route("/api/plano-contas/buscar/<codigo>")
@login_required
def buscar_conta_json(codigo):
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            conta = tenant_db.plano_contas.find_one({"codigo": codigo})
            if conta:
                # Converte o _id de ObjectId para string para JSON
                conta["_id"] = str(conta["_id"])
                return jsonify(conta)
            return jsonify({"error": "Conta contábil não encontrada"}), 404
        except Exception as e:
            logging.error(f"Erro ao buscar conta contábil JSON: {e}")
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Banco de dados indisponível"}), 500


# ROTA: Atualizar cadastro de conta contábil existente
@app.route("/plano-contas/atualizar", methods=["POST"])
@login_required
def atualizar_plano_contas():
    codigo_original = request.form.get("codigo_original", "").strip()
    codigo = request.form.get("codigo", "").strip()
    nome = request.form.get("nome", "").strip()
    tipo = request.form.get("tipo", "").strip()
    natureza = request.form.get("natureza", "").strip()
    classificacao = request.form.get("classificacao", "").strip()
    cmv = request.form.get("cmv") in ["true", "on"]
    
    if not codigo_original or not codigo or not nome or not tipo or not natureza or not classificacao:
        flash("Todos os campos obrigatórios devem ser preenchidos.", "danger")
        return redirect(url_for("plano_contas"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Validação: se o código mudou, não permitir duplicar
            if codigo != codigo_original:
                existing = tenant_db.plano_contas.find_one({"codigo": codigo})
                if existing:
                    flash(f"Já existe uma conta cadastrada com o código '{codigo}'. Não é possível duplicar códigos contábeis.", "danger")
                    return redirect(url_for("plano_contas"))
                    
            tenant_db.plano_contas.update_one(
                {"codigo": codigo_original},
                {"$set": {
                    "codigo": codigo,
                    "nome": nome,
                    "tipo": tipo,
                    "natureza": natureza,
                    "classificacao": classificacao,
                    "cmv": cmv,
                    "grupo_dre": request.form.get("grupo_dre", "").strip() or None,
                    "grupo_dfc": request.form.get("grupo_dfc", "").strip() or None,
                    "faz_parte_dre": codigo.startswith("3") or codigo.startswith("4"),
                    "faz_parte_dfc": any(codigo.startswith(p) for p in ("1.1", "1.2", "2.1", "2.2", "2.3", "3", "4")),
                }}
            )
            
            # Atualiza propagando as referências em outras coleções
            if codigo != codigo_original:
                tenant_db.transacoes.update_many(
                    {"conta_codigo": codigo_original},
                    {"$set": {"conta_codigo": codigo}}
                )
                tenant_db.contas_pagar.update_many(
                    {"conta_codigo": codigo_original},
                    {"$set": {"conta_codigo": codigo}}
                )
                tenant_db.contas_receber.update_many(
                    {"conta_codigo": codigo_original},
                    {"$set": {"conta_codigo": codigo}}
                )
                
            flash(f"Conta contábil '{codigo} - {nome}' atualizada com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao atualizar conta contábil: {e}")
            flash(f"Erro ao atualizar conta contábil: {e}", "danger")
            
    return redirect(url_for("plano_contas"))


# ROTA: Importar Plano de Contas Padrão (Simulado/Mock Seed)
@app.route("/plano-contas/importar-padrao", methods=["POST"])
@login_required
def importar_plano_contas():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Evita duplicidades
            count = tenant_db.plano_contas.count_documents({})
            if count > 0:
                flash("O Plano de Contas já possui estruturas cadastradas. Importação abortada.", "danger")
                return redirect(url_for("plano_contas"))
                
            # Estrutura inicial padrão
            contas_padrao = [
                {"codigo": "1", "nome": "Ativo", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "1.1", "nome": "Ativo Circulante", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "1.1.1", "nome": "Caixa e Equivalentes de Caixa", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Analítica", "cmv": False},
                {"codigo": "1.1.2", "nome": "Contas a Receber de Clientes", "tipo": "Patrimonial", "natureza": "Débito", "classificacao": "Analítica", "cmv": False},
                {"codigo": "2", "nome": "Passivo", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "2.1", "nome": "Passivo Circulante", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "2.1.1", "nome": "Fornecedores a Pagar", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                {"codigo": "2.1.2", "nome": "Obrigações Sociais e Trabalhistas", "tipo": "Patrimonial", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                {"codigo": "3", "nome": "Receitas", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "3.1", "nome": "Receita Bruta de Vendas", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "3.1.1", "nome": "Receita de Serviços Prestados", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Analítica", "cmv": False},
                {"codigo": "4", "nome": "Custos e Despesas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "4.1", "nome": "Custos Operacionais", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "4.1.1", "nome": "Mão de Obra Direta BPO", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": True},
                {"codigo": "4.1.2", "nome": "Licenças de ERP Repassadas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": True},
                {"codigo": "4.2", "nome": "Despesas Gerais", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética", "cmv": False},
                {"codigo": "4.2.1", "nome": "Aluguel Comercial", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": False}
            ]
            
            tenant_db.plano_contas.insert_many(contas_padrao)
            flash("Estrutura do Plano de Contas padrão importada com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao importar plano de contas padrão: {e}")
            flash(f"Erro na importação: {e}", "danger")
            
    return redirect(url_for("plano_contas"))


# ROTA: Importar Plano de Contas via Arquivo CSV (Upload + Upsert)
@app.route("/plano-contas/importar-csv", methods=["POST"])
@login_required
def importar_plano_contas_csv():
    """Recebe um arquivo CSV via upload, processa e persiste o plano de contas."""
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)

    if tenant_db is None:
        return jsonify({"sucesso": False, "mensagem": "Banco de dados indisponível."}), 500

    # Valida presença do arquivo
    if "csv_file" not in request.files:
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo enviado."}), 400

    arquivo = request.files["csv_file"]
    if arquivo.filename == "":
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo selecionado."}), 400

    # Valida extensão
    if not arquivo.filename.lower().endswith(".csv"):
        return jsonify({"sucesso": False, "mensagem": "Formato inválido. Envie um arquivo .csv."}), 400

    # Flag de atualização de contas existentes
    atualizar = request.form.get("atualizar_existentes", "true").lower() in ("true", "1", "sim", "on")

    # Modo de importação: 'upsert' (mesclar) ou 'reset' (substituir com proteção)
    modo_importacao = request.form.get("modo_importacao", "upsert").strip().lower()

    try:
        importador = ImportadorPlanoContas()
        resultado = importador.processar(
            file_stream=arquivo.stream,
            tenant_db=tenant_db,
            atualizar_existentes=atualizar,
            modo_importacao=modo_importacao
        )
        logging.info(
            f"Importação CSV plano de contas [{grupo_slug}] modo={modo_importacao}: "
            f"lidas={resultado['total_lidas']}, inseridas={resultado['total_inseridas']}, "
            f"atualizadas={resultado['total_atualizadas']}, auto_geradas={resultado['total_auto_geradas']}, "
            f"preservadas={resultado['total_preservadas_com_historico']}, "
            f"removidas={resultado['total_removidas_sem_uso']}, erros={resultado['total_erros']}"
        )
        return jsonify(resultado)
    except Exception as e:
        logging.error(f"Erro crítico na importação CSV do plano de contas: {e}")
        return jsonify({"sucesso": False, "mensagem": f"Erro interno: {e}"}), 500


# ROTA: Importar Lançamentos em Lote via CSV
@app.route("/lancamentos/importar-csv", methods=["POST"], strict_slashes=False)
@login_required
def importar_lancamentos_csv():
    """Recebe um arquivo CSV via upload, processa e persiste lançamentos contábeis."""
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)

    if tenant_db is None:
        return jsonify({"sucesso": False, "mensagem": "Banco de dados indisponível."}), 500

    # Valida presença do arquivo
    if "csv_file" not in request.files:
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo enviado."}), 400

    arquivo = request.files["csv_file"]
    if arquivo.filename == "":
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo selecionado."}), 400

    if not arquivo.filename.lower().endswith(".csv"):
        return jsonify({"sucesso": False, "mensagem": "Formato inválido. Envie um arquivo .csv."}), 400

    try:
        gerar_partidas_dobradas = request.form.get("gerar_partidas_dobradas") in ["true", "on", "True", "1"]
        conteudo_bytes = arquivo.read()
        
        importador = ImportadorLancamentos(tenant_db)
        resultado = importador.processar_lote(
            conteudo_bytes=conteudo_bytes,
            gerar_partidas_dobradas=gerar_partidas_dobradas
        )
        logging.info(
            f"Importação CSV Histórico (Lote) [{grupo_slug}]: "
            f"lidas={resultado.get('total_lidas')}, importadas={resultado.get('total_importadas')}, "
            f"erros={resultado.get('total_erros')}"
        )
        return jsonify(resultado)
    except Exception as e:
        logging.error(f"Erro crítico na importação CSV de lançamentos em lote: {e}")
        return jsonify({"sucesso": False, "mensagem": f"Erro interno: {e}"}), 500


# ROTA: Cadastros -> Clientes e Fornecedores (Listagem)
@app.route("/participantes")
@login_required
def participantes():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    participantes_lista = []
    max_cnpjs = 5
    cnpjs_atuais = 0
    if tenant_db is not None:
        try:
            participantes_cursor = tenant_db.participantes.find().sort("nome_razao", 1)
            participantes_lista = list(participantes_cursor)
            
            # Limites Comerciais: max_cnpjs do db_flow_admin
            admin_db = db_manager.get_admin_db()
            if admin_db is not None:
                grupo_doc = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
                if grupo_doc:
                    max_cnpjs = int(grupo_doc.get("max_cnpjs", 5))
            
            cnpjs_atuais = tenant_db.participantes.count_documents({"cnpj_cpf": {"$ne": ""}})
        except Exception as e:
            logging.error(f"Erro ao consultar participantes: {e}")
            flash(f"Erro ao carregar parceiros: {e}", "danger")
            
    return render_template(
        "participantes.html", 
        participantes=participantes_lista, 
        max_cnpjs=max_cnpjs, 
        cnpjs_atuais=cnpjs_atuais
    )


# ROTA: Salvar Cliente ou Fornecedor
@app.route("/participantes/salvar", methods=["POST"])
@login_required
def salvar_participante():
    tipo = request.form.get("tipo", "").strip()
    nome_razao = request.form.get("nome_razao", "").strip()
    cnpj_cpf = request.form.get("cnpj_cpf", "").strip()
    inscricao_estadual = request.form.get("inscricao_estadual", "").strip()
    email_financeiro = request.form.get("email_financeiro", "").strip()
    telefone = request.form.get("telefone", "").strip()
    
    # Dados bancários padrão
    banco = request.form.get("banco", "").strip()
    agencia = request.form.get("agencia", "").strip()
    conta = request.form.get("conta", "").strip()
    
    if not tipo or not nome_razao or not cnpj_cpf or not email_financeiro:
        flash("Os campos Tipo, Razão Social, CNPJ/CPF e E-mail Financeiro são obrigatórios.", "danger")
        return redirect(url_for("participantes"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Trava de Limites Comerciais: max_cnpjs do db_flow_admin
            admin_db = db_manager.get_admin_db()
            if admin_db is not None:
                grupo_doc = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
                if grupo_doc:
                    max_cnpjs = int(grupo_doc.get("max_cnpjs", 5))
                    cnpjs_atuais = tenant_db.participantes.count_documents({"cnpj_cpf": {"$ne": ""}})
                    if cnpjs_atuais >= max_cnpjs:
                        flash("Limite de CNPJs atingido para o plano do seu Grupo Econômico. Faça um upgrade para cadastrar novas empresas.", "danger")
                        return redirect(url_for("participantes"))
            # Documento gravado na coleção 'participantes'
            # Exemplo de documento:
            # {
            #     "tipo": "Cliente",
            #     "nome_razao": "Empresa Alfa Ltda",
            #     "cnpj_cpf": "12.345.678/0001-90",
            #     "inscricao_estadual": "Isento",
            #     "email_financeiro": "financeiro@alfa.com",
            #     "telefone": "(11) 98765-4321",
            #     "dados_bancarios": {
            #         "banco": "Itaú (341)",
            #         "agencia": "1234",
            #         "conta": "56789-0"
            #     }
            # }
            novo_participante = {
                "tipo": tipo,
                "nome_razao": nome_razao,
                "cnpj_cpf": cnpj_cpf,
                "inscricao_estadual": inscricao_estadual,
                "email_financeiro": email_financeiro,
                "telefone": telefone,
                "dados_bancarios": {
                    "banco": banco,
                    "agencia": agency if (agency := agencia) else "",
                    "conta": acc if (acc := conta) else ""
                }
            }
            # Trata dados vazios
            if not banco:
                novo_participante["dados_bancarios"] = {}
                
            tenant_db.participantes.insert_one(novo_participante)
            flash(f"Parceiro de negócios '{nome_razao}' cadastrado com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar participante: {e}")
            flash(f"Erro ao salvar parceiro: {e}", "danger")
            
    return redirect(url_for("participantes"))


# ROTA: Buscar dados de um participante (JSON)
@app.route("/api/participantes/buscar/<id>")
@login_required
def buscar_participante_json(id):
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            partner = tenant_db.participantes.find_one({"_id": ObjectId(id)})
            if partner:
                # Converte o _id de ObjectId para string para JSON
                partner["_id"] = str(partner["_id"])
                return jsonify(partner)
            return jsonify({"error": "Parceiro não encontrado"}), 404
        except Exception as e:
            logging.error(f"Erro ao buscar participante JSON: {e}")
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Banco de dados indisponível"}), 500


# ROTA: Atualizar cadastro de participante existente
@app.route("/participantes/atualizar", methods=["POST"])
@login_required
def atualizar_participante():
    participante_id = request.form.get("participante_id", "").strip()
    tipo = request.form.get("tipo", "").strip()
    nome_razao = request.form.get("nome_razao", "").strip()
    cnpj_cpf = request.form.get("cnpj_cpf", "").strip()
    inscricao_estadual = request.form.get("inscricao_estadual", "").strip()
    email_financeiro = request.form.get("email_financeiro", "").strip()
    telefone = request.form.get("telefone", "").strip()
    
    banco = request.form.get("banco", "").strip()
    agencia = request.form.get("agencia", "").strip()
    conta = request.form.get("conta", "").strip()
    
    if not participante_id or not tipo or not nome_razao or not cnpj_cpf or not email_financeiro:
        flash("Os campos Tipo, Razão Social, CNPJ/CPF e E-mail Financeiro são obrigatórios.", "danger")
        return redirect(url_for("participantes"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Prepara dados bancários
            dados_bancarios = {}
            if banco:
                dados_bancarios = {
                    "banco": banco,
                    "agencia": agencia,
                    "conta": conta
                }
                
            tenant_db.participantes.update_one(
                {"_id": ObjectId(participante_id)},
                {"$set": {
                    "tipo": tipo,
                    "nome_razao": nome_razao,
                    "cnpj_cpf": cnpj_cpf,
                    "inscricao_estadual": inscricao_estadual,
                    "email_financeiro": email_financeiro,
                    "telefone": telefone,
                    "dados_bancarios": dados_bancarios
                }}
            )
            
            # Atualiza também nas transações recentes se for necessário manter consistência de nome do participante
            tenant_db.transacoes.update_many(
                {"participante_id": participante_id},
                {"$set": {"participante_nome": nome_razao}}
            )
            
            flash(f"Parceiro '{nome_razao}' atualizado com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao atualizar participante: {e}")
            flash(f"Erro ao atualizar parceiro: {e}", "danger")
            
    return redirect(url_for("participantes"))


# ROTA: Usuários (Listagem e Cadastro)
@app.route("/usuarios")
@login_required
def listar_usuarios():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    usuarios_lista = []
    if tenant_db is not None:
        try:
            usuarios_lista = list(tenant_db.usuarios.find().sort("nome", 1))
        except Exception as e:
            logging.error(f"Erro ao buscar usuários: {e}")
            flash(f"Erro ao carregar usuários: {e}", "danger")
            
    return render_template("usuarios.html", usuarios=usuarios_lista)


# ROTA: Salvar Novo Usuário (com validação de max_usuarios do banco master)
@app.route("/usuarios/salvar", methods=["POST"])
@login_required
def salvar_usuario():
    nome = request.form.get("nome", "").strip()
    email = request.form.get("email", "").strip()
    
    if not nome or not email:
        flash("Os campos Nome Completo e E-mail são obrigatórios.", "danger")
        return redirect(url_for("listar_usuarios"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Trava de Limites Comerciais: max_usuarios do db_flow_admin
            admin_db = db_manager.get_admin_db()
            if admin_db is not None:
                grupo_doc = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
                if grupo_doc:
                    max_usuarios = int(grupo_doc.get("max_usuarios", 5))
                    usuarios_atuais = tenant_db.usuarios.count_documents({})
                    if usuarios_atuais >= max_usuarios:
                        flash("Limite de Usuários atingido para o plano do seu Grupo Econômico. Faça um upgrade para cadastrar novos usuários.", "danger")
                        return redirect(url_for("listar_usuarios"))
            
            novo_usuario = {
                "nome": nome,
                "email": email
            }
            tenant_db.usuarios.insert_one(novo_usuario)
            flash(f"Usuário '{nome}' cadastrado com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar usuário: {e}")
            flash(f"Erro ao cadastrar usuário: {e}", "danger")
            
    return redirect(url_for("listar_usuarios"))

# ROTA API: Buscar Bancos da BrasilAPI
@app.route("/api/bancos", methods=["GET"], strict_slashes=False)
@login_required
def api_bancos():
    try:
        admin_db = db_manager.get_admin_db()
        bancos = BancosService.get_bancos(admin_db)
        return jsonify(bancos)
    except Exception as e:
        logging.error(f"Erro ao buscar bancos: {e}")
        return jsonify([]), 500

# ROTA POST: Importar CSV de Bancos (Febraban)
@app.route("/config/bancos/importar-csv", methods=["POST"], strict_slashes=False)
@login_required
def config_bancos_importar_csv():
    admin_db = db_manager.get_admin_db()
    if 'arquivo_csv' not in request.files:
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo enviado."}), 400
    
    file = request.files['arquivo_csv']
    if file.filename == '':
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo selecionado."}), 400
        
    try:
        content = file.read()
        resultado = BancosService.processar_arquivo_csv(content, admin_db)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"sucesso": False, "mensagem": f"Erro interno: {str(e)}"}), 500

# ROTA POST: Sincronizar Bancos Automaticamente (Local CSV ou BrasilAPI)
@app.route("/config/bancos/sincronizar", methods=["POST"], strict_slashes=False)
@login_required
def config_bancos_sincronizar():
    admin_db = db_manager.get_admin_db()
    try:
        resultado = BancosService.sincronizar_automatico(admin_db)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"sucesso": False, "mensagem": f"Erro interno: {str(e)}"}), 500


# ROTA API: Cadastro Rápido de Participantes (AJAX)
@app.route("/api/participantes/rapido", methods=["POST"])
@login_required
def api_participantes_rapido():
    nome_razao = request.form.get("nome_razao", "").strip()
    cnpj_cpf = request.form.get("cnpj_cpf", "").strip()
    email_financeiro = request.form.get("email_financeiro", "").strip()
    tipo = request.form.get("tipo", "").strip() # 'Cliente' ou 'Fornecedor'
    
    if not nome_razao or not cnpj_cpf or not email_financeiro or not tipo:
        return {"success": False, "error": "Campos obrigatórios ausentes."}, 400
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Trava de Limites Comerciais: max_cnpjs do db_flow_admin
            admin_db = db_manager.get_admin_db()
            if admin_db is not None:
                grupo_doc = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
                if grupo_doc:
                    max_cnpjs = int(grupo_doc.get("max_cnpjs", 5))
                    cnpjs_atuais = tenant_db.participantes.count_documents({"cnpj_cpf": {"$ne": ""}})
                    if cnpjs_atuais >= max_cnpjs:
                        return {"success": False, "error": "Limite de CNPJs atingido para o plano do seu Grupo Econômico. Faça um upgrade para cadastrar novas empresas."}, 400
                        
            # Documento gravado na coleção 'participantes' em background
            novo_participante = {
                "tipo": tipo,
                "nome_razao": nome_razao,
                "cnpj_cpf": cnpj_cpf,
                "inscricao_estadual": "Isento",
                "email_financeiro": email_financeiro,
                "telefone": "",
                "dados_bancarios": {}
            }
            result = tenant_db.participantes.insert_one(novo_participante)
            return {
                "success": True,
                "id": str(result.inserted_id),
                "nome_razao": nome_razao
            }
        except Exception as e:
            logging.error(f"Erro ao salvar participante rápido via API: {e}")
            return {"success": False, "error": f"Erro interno: {str(e)}"}, 500
            
    return {"success": False, "error": "Banco de dados indisponível."}, 500


# ROTA: Financeiro -> Contas a Pagar (Listagem e Formulário)
@app.route("/contas-pagar")
@login_required
def contas_pagar():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    fornecedores = []
    contas_despesa = []
    lancamentos = []
    
    if tenant_db is not None:
        try:
            # Busca fornecedores ativos (Fornecedor ou Ambos)
            fornecedores = list(tenant_db.participantes.find({"tipo": {"$in": ["Fornecedor", "Ambos"]}}).sort("nome_razao", 1))
            
            # Busca contas analíticas para o lançamento de despesa
            contas_despesa = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}).sort("codigo", 1))
            
            # Busca contas a pagar ordenadas por data de vencimento
            lancamentos = list(tenant_db.contas_pagar.find().sort("data_vencimento", 1))
            
            # Busca centros de custo
            centros_custo = list(tenant_db.centros_custo.find())
        except Exception as e:
            logging.error(f"Erro ao carregar dados de Contas a Pagar: {e}")
            flash(f"Erro ao carregar registros: {e}", "danger")
            
    return render_template(
        "contas_pagar.html",
        fornecedores=fornecedores,
        contas_despesa=contas_despesa,
        lancamentos=lancamentos,
        centros_custo=centros_custo
    )


# ROTA: Contas a Pagar -> Salvar
@app.route("/contas-pagar/salvar", methods=["POST"])
@login_required
def contas_pagar_salvar():
    participante_id = request.form.get("participante_id", "").strip()
    descricao = request.form.get("descricao", "").strip()
    valor_str = request.form.get("valor", "").strip()
    data_emissao = request.form.get("data_emissao", "").strip()
    data_vencimento = request.form.get("data_vencimento", "").strip()
    conta_codigo = request.form.get("conta_codigo", "").strip()
    centro_custo = request.form.get("centro_custo", "Geral").strip() or "Geral"
    
    if not participante_id or not descricao or not valor_str or not data_emissao or not data_vencimento or not conta_codigo:
        flash("Todos os campos do lançamento são obrigatórios.", "danger")
        return redirect(url_for("contas_pagar"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Converte valor para Decimal
            valor_limpo = valor_str.replace(".", "").replace(",", ".")
            valor = Decimal128(Decimal(valor_limpo).quantize(Decimal("0.01")))
            
            # Resolve os nomes dos objetos vinculados para cachear no documento do lançamento (evita joins lentos)
            fornecedor = tenant_db.participantes.find_one({"_id": ObjectId(participante_id)})
            conta_contabil = tenant_db.plano_contas.find_one({"codigo": conta_codigo})
            
            if not fornecedor or not conta_contabil:
                flash("Fornecedor ou Conta Contábil inválidos.", "danger")
                return redirect(url_for("contas_pagar"))
                
            # Documento gravado na coleção 'contas_pagar'
            novo_lancamento = {
                "participante_id": participante_id,
                "participante_nome": fornecedor.get("nome_razao"),
                "descricao": descricao,
                "valor": valor,
                "data_emissao": data_emissao,
                "data_vencimento": data_vencimento,
                "conta_codigo": conta_codigo,
                "conta_nome": conta_contabil.get("nome"),
                "status": "Pendente",
                "data_pagamento": "",
                "centro_custo": centro_custo
            }
            res = tenant_db.contas_pagar.insert_one(novo_lancamento)
            
            # Registra Partidas Dobradas: Provisão de Contas a Pagar
            # Débito: Despesa (conta_codigo)
            # Crédito: Fornecedores a Pagar (resolvida dinamicamente)
            conta_forn = obter_conta_contabil_valida(tenant_db, ["2.1.01.001", "2.1.1"])
            registrar_partida_dobrada(
                db=tenant_db,
                data=data_emissao,
                conta_debito=conta_codigo,
                conta_credito=conta_forn,
                valor=valor,
                historico=f"Provisão Contas a Pagar: {fornecedor.get('nome_razao')} - {descricao}",
                documento_id=res.inserted_id,
                tipo_origem="contas_pagar_provisao"
            )
            flash("Conta a pagar lançada com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar conta a pagar: {e}")
            flash(f"Erro ao salvar lançamento: {e}", "danger")
            
    return redirect(url_for("contas_pagar"))


# ROTA: Contas a Pagar -> Liquidar (Baixa por Caixa)
@app.route("/contas-pagar/liquidar", methods=["POST"])
@login_required
def contas_pagar_liquidar():
    lancamento_id = request.form.get("lancamento_id", "").strip()
    data_pagamento = request.form.get("data_pagamento", "").strip()
    
    if not lancamento_id or not data_pagamento:
        flash("Código do lançamento e data de pagamento são obrigatórios.", "danger")
        return redirect(url_for("contas_pagar"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            bill = tenant_db.contas_pagar.find_one({"_id": ObjectId(lancamento_id)})
            if bill:
                tenant_db.contas_pagar.update_one(
                    {"_id": ObjectId(lancamento_id)},
                    {"$set": {
                        "status": "Pago",
                        "data_pagamento": data_pagamento
                    }}
                )
                # Gera Partidas Dobradas: Baixa de Contas a Pagar
                # Débito: Fornecedores a Pagar (resolvida dinamicamente)
                # Crédito: Caixa/Banco (resolvida dinamicamente)
                conta_forn = obter_conta_contabil_valida(tenant_db, ["2.1.01.001", "2.1.1"])
                conta_banc = obter_conta_contabil_valida(tenant_db, ["1.1.01.002", "1.1.1"])
                descricao_contabil = f"Pagamento Fornecedor: {bill.get('participante_nome')} - {bill.get('descricao')}"
                registrar_partida_dobrada(
                    db=tenant_db,
                    data=data_pagamento,
                    conta_debito=conta_forn,
                    conta_credito=conta_banc,
                    valor=bill.get("valor", 0.0),
                    historico=descricao_contabil,
                    documento_id=bill.get("_id"),
                    tipo_origem="contas_pagar_baixa"
                )
                flash("Baixa efetuada com sucesso!", "success")
            else:
                flash("Conta a pagar não encontrada.", "danger")
        except Exception as e:
            logging.error(f"Erro ao liquidar conta a pagar: {e}")
            flash(f"Erro ao efetuar baixa: {e}", "danger")
            
    return redirect(url_for("contas_pagar"))


# ROTA: Financeiro -> Contas a Receber (Listagem e Formulário)
@app.route("/contas-receber")
@login_required
def contas_receber():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    clientes = []
    contas_receita = []
    lancamentos = []
    
    if tenant_db is not None:
        try:
            # Busca clientes ativos (Cliente ou Ambos)
            clientes = list(tenant_db.participantes.find({"tipo": {"$in": ["Cliente", "Ambos"]}}).sort("nome_razao", 1))
            
            # Busca contas analíticas para lançamento (filtrando apenas Resultado e natureza Crédito - Grupo de Receitas)
            contas_receita = list(tenant_db.plano_contas.find({
                "classificacao": "Analítica",
                "tipo": "Resultado",
                "natureza": "Crédito"
            }).sort("codigo", 1))
            
            # Busca contas a receber ordenadas por data de vencimento
            lancamentos = list(tenant_db.contas_receber.find().sort("data_vencimento", 1))
            
            # Busca centros de custo
            centros_custo = list(tenant_db.centros_custo.find())
        except Exception as e:
            logging.error(f"Erro ao carregar dados de Contas a Receber: {e}")
            flash(f"Erro ao carregar registros: {e}", "danger")
            
    return render_template(
        "contas_receber.html",
        clientes=clientes,
        contas_receita=contas_receita,
        lancamentos=lancamentos,
        centros_custo=centros_custo
    )


# ROTA: Contas a Receber -> Salvar
@app.route("/contas-receber/salvar", methods=["POST"])
@login_required
def contas_receber_salvar():
    participante_id = request.form.get("participante_id", "").strip()
    descricao = request.form.get("descricao", "").strip()
    valor_str = request.form.get("valor", "").strip()
    data_emissao = request.form.get("data_emissao", "").strip()
    data_vencimento = request.form.get("data_vencimento", "").strip()
    conta_codigo = request.form.get("conta_codigo", "").strip()
    centro_custo = request.form.get("centro_custo", "Geral").strip() or "Geral"
    
    if not participante_id or not descricao or not valor_str or not data_emissao or not data_vencimento or not conta_codigo:
        flash("Todos os campos do lançamento são obrigatórios.", "danger")
        return redirect(url_for("contas_receber"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Converte valor para Decimal
            valor_limpo = valor_str.replace(".", "").replace(",", ".")
            valor = Decimal128(Decimal(valor_limpo).quantize(Decimal("0.01")))
            
            # Resolve os nomes dos objetos vinculados
            cliente = tenant_db.participantes.find_one({"_id": ObjectId(participante_id)})
            conta_contabil = tenant_db.plano_contas.find_one({"codigo": conta_codigo})
            
            if not cliente or not conta_contabil:
                flash("Cliente ou Conta Contábil inválidos.", "danger")
                return redirect(url_for("contas_receber"))
                
            # Documento gravado na coleção 'contas_receber'
            novo_lancamento = {
                "participante_id": participante_id,
                "participante_nome": cliente.get("nome_razao"),
                "descricao": descricao,
                "valor": valor,
                "data_emissao": data_emissao,
                "data_vencimento": data_vencimento,
                "conta_codigo": conta_codigo,
                "conta_nome": conta_contabil.get("nome"),
                "status": "Pendente",
                "data_recebimento": "",
                "centro_custo": centro_custo
            }
            res = tenant_db.contas_receber.insert_one(novo_lancamento)
            
            # Registra Partidas Dobradas: Provisão de Contas a Receber
            # Débito: Clientes a Receber (resolvida dinamicamente)
            # Crédito: Receita (conta_codigo)
            conta_cli = obter_conta_contabil_valida(tenant_db, ["1.1.03.001", "1.1.2"])
            registrar_partida_dobrada(
                db=tenant_db,
                data=data_emissao,
                conta_debito=conta_cli,
                conta_credito=conta_codigo,
                valor=valor,
                historico=f"Provisão Contas a Receber: {cliente.get('nome_razao')} - {descricao}",
                documento_id=res.inserted_id,
                tipo_origem="contas_receber_provisao"
            )
            flash("Conta a receber lançada com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar conta a receber: {e}")
            flash(f"Erro ao salvar lançamento: {e}", "danger")
            
    return redirect(url_for("contas_receber"))


# ROTA: Contas a Receber -> Liquidar (Baixa por Caixa)
@app.route("/contas-receber/liquidar", methods=["POST"])
@login_required
def contas_receber_liquidar():
    lancamento_id = request.form.get("lancamento_id", "").strip()
    data_recebimento = request.form.get("data_recebimento", "").strip()
    
    if not lancamento_id or not data_recebimento:
        flash("Código do lançamento e data de recebimento são obrigatórios.", "danger")
        return redirect(url_for("contas_receber"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            bill = tenant_db.contas_receber.find_one({"_id": ObjectId(lancamento_id)})
            if bill:
                tenant_db.contas_receber.update_one(
                    {"_id": ObjectId(lancamento_id)},
                    {"$set": {
                        "status": "Recebido",
                        "data_recebimento": data_recebimento
                    }}
                )
                # Gera Partidas Dobradas: Baixa de Contas a Receber
                # Débito: Caixa/Banco (resolvida dinamicamente)
                # Crédito: Clientes a Receber (resolvida dinamicamente)
                conta_banc = obter_conta_contabil_valida(tenant_db, ["1.1.01.002", "1.1.1"])
                conta_cli = obter_conta_contabil_valida(tenant_db, ["1.1.03.001", "1.1.2"])
                descricao_contabil = f"Recebimento Cliente: {bill.get('participante_nome')} - {bill.get('descricao')}"
                registrar_partida_dobrada(
                    db=tenant_db,
                    data=data_recebimento,
                    conta_debito=conta_banc,
                    conta_credito=conta_cli,
                    valor=bill.get("valor", 0.0),
                    historico=descricao_contabil,
                    documento_id=bill.get("_id"),
                    tipo_origem="contas_receber_baixa"
                )
                flash("Baixa por recebimento efetuada com sucesso!", "success")
            else:
                flash("Conta a receber não encontrada.", "danger")
        except Exception as e:
            logging.error(f"Erro ao liquidar conta a receber: {e}")
            flash(f"Erro ao efetuar baixa: {e}", "danger")
            
    return redirect(url_for("contas_receber"))


# HELPER: Faz o parser seguro de data a partir de múltiplos formatos brasileiros e internacionais
def parse_date_safely(date_str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


# ROTA: Contas a Pagar/Receber -> Atualizar (Edição Unificada)
@app.route("/financeiro/atualizar/<tipo_fluxo>/<id_titulo>", methods=["POST"])
@login_required
def financeiro_atualizar(tipo_fluxo, id_titulo):
    if tipo_fluxo not in ["pagar", "receber"]:
        flash("Fluxo financeiro inválido.", "danger")
        return redirect(url_for("dashboard"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # 1. Limpa o passado contábil (estorna todos os lançamentos atrelados ao documento_id)
            estornar_lancamentos_origem(tenant_db, id_titulo)
            
            # 2. Recebe e sanitiza os dados do formulário
            participante_id = request.form.get("participante_id", "").strip()
            descricao = request.form.get("descricao", "").strip()
            valor_str = request.form.get("valor", "").strip()
            data_emissao_str = request.form.get("data_emissao", "").strip()
            data_vencimento_str = request.form.get("data_vencimento", "").strip()
            conta_codigo = request.form.get("conta_codigo", "").strip()
            centro_custo = request.form.get("centro_custo", "Geral").strip() or "Geral"
            status = request.form.get("status", "Pendente").strip()
            
            if not participante_id or not descricao or not valor_str or not data_emissao_str or not data_vencimento_str or not conta_codigo:
                flash("Todos os campos do lançamento são obrigatórios.", "danger")
                return redirect(url_for("contas_pagar" if tipo_fluxo == "pagar" else "contas_receber"))
            
            # Sanitiza valor monetário brasileiro
            valor = converter_valor_br_para_float(valor_str)
            
            # Converte as strings de data para objetos datetime para validação
            dt_emissao = parse_date_safely(data_emissao_str) or datetime.today()
            dt_vencimento = parse_date_safely(data_vencimento_str) or datetime.today()
            
            data_emissao_formatted = dt_emissao.strftime("%Y-%m-%d")
            data_vencimento_formatted = dt_vencimento.strftime("%Y-%m-%d")
                
            # Dados específicos de liquidação
            data_baixa_str = ""
            data_baixa_formatted = ""
            if status in ["Pago", "Recebido"]:
                if tipo_fluxo == "pagar":
                    data_baixa_str = request.form.get("data_pagamento", "").strip()
                else:
                    data_baixa_str = request.form.get("data_recebimento", "").strip()
                
                dt_baixa = parse_date_safely(data_baixa_str)
                if dt_baixa:
                    data_baixa_formatted = dt_baixa.strftime("%Y-%m-%d")
                    
            # 3. Busca parceiro e conta contábil
            parceiro = tenant_db.participantes.find_one({"_id": ObjectId(participante_id)})
            parceiro_nome = parceiro.get("nome_razao") if parceiro else ""
            
            conta_contabil = tenant_db.plano_contas.find_one({"codigo": conta_codigo})
            conta_nome = conta_contabil.get("nome") if conta_contabil else ""
            
            # 4. Atualiza o documento na coleção correspondente
            if tipo_fluxo == "pagar":
                update_doc = {
                    "participante_id": participante_id,
                    "participante_nome": parceiro_nome,
                    "descricao": descricao,
                    "valor": valor,
                    "data_emissao": data_emissao_formatted,
                    "data_vencimento": data_vencimento_formatted,
                    "conta_codigo": conta_codigo,
                    "conta_nome": conta_nome,
                    "status": status,
                    "data_pagamento": data_baixa_formatted,
                    "centro_custo": centro_custo
                }
                tenant_db.contas_pagar.update_one(
                    {"_id": ObjectId(id_titulo)},
                    {"$set": update_doc}
                )
                
                # 5. Reconstrói a nova Provisão (Competência) via Partidas Dobradas
                # D: Custo/Despesa (conta_codigo)
                # C: Passivo Circulante (Fornecedores)
                conta_forn = obter_conta_contabil_valida(tenant_db, ["2.1.01.001", "2.1.1"])
                registrar_partida_dobrada(
                    db=tenant_db,
                    data=data_emissao_formatted,
                    conta_debito=conta_codigo,
                    conta_credito=conta_forn,
                    valor=valor,
                    historico=f"Provisão Contas a Pagar (Editado): {parceiro_nome} - {descricao}",
                    documento_id=id_titulo,
                    tipo_origem="contas_pagar_provisao"
                )
                
                # 6. Se estiver Pago, reconstrói a nova Liquidação (Caixa)
                # D: Passivo Circulante (Fornecedores)
                # C: Disponibilidades (Caixa/Banco)
                if status == "Pago" and data_baixa_formatted:
                    conta_banc = obter_conta_contabil_valida(tenant_db, ["1.1.01.002", "1.1.1"])
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_baixa_formatted,
                        conta_debito=conta_forn,
                        conta_credito=conta_banc,
                        valor=valor,
                        historico=f"Pagamento Fornecedor (Editado): {parceiro_nome} - {descricao}",
                        documento_id=id_titulo,
                        tipo_origem="contas_pagar_baixa"
                    )
                flash("Título a pagar editado e refletido contábilmente com sucesso!", "success")
                return redirect(url_for("contas_pagar"))
                
            else: # tipo_fluxo == "receber"
                update_doc = {
                    "participante_id": participante_id,
                    "participante_nome": parceiro_nome,
                    "descricao": descricao,
                    "valor": valor,
                    "data_emissao": data_emissao_formatted,
                    "data_vencimento": data_vencimento_formatted,
                    "conta_codigo": conta_codigo,
                    "conta_nome": conta_nome,
                    "status": status,
                    "data_recebimento": data_baixa_formatted,
                    "centro_custo": centro_custo
                }
                tenant_db.contas_receber.update_one(
                    {"_id": ObjectId(id_titulo)},
                    {"$set": update_doc}
                )
                
                # 5. Reconstrói a nova Provisão (Competência) via Partidas Dobradas
                # D: Ativo Circulante (Clientes)
                # C: Receita (conta_codigo)
                conta_cli = obter_conta_contabil_valida(tenant_db, ["1.1.03.001", "1.1.2"])
                registrar_partida_dobrada(
                    db=tenant_db,
                    data=data_emissao_formatted,
                    conta_debito=conta_cli,
                    conta_credito=conta_codigo,
                    valor=valor,
                    historico=f"Provisão Contas a Receber (Editado): {parceiro_nome} - {descricao}",
                    documento_id=id_titulo,
                    tipo_origem="contas_receber_provisao"
                )
                
                # 6. Se estiver Recebido, reconstrói a nova Liquidação (Caixa)
                # D: Disponibilidades (Caixa/Banco)
                # C: Ativo Circulante (Clientes)
                if status == "Recebido" and data_baixa_formatted:
                    conta_banc = obter_conta_contabil_valida(tenant_db, ["1.1.01.002", "1.1.1"])
                    conta_cli = obter_conta_contabil_valida(tenant_db, ["1.1.03.001", "1.1.2"])
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_baixa_formatted,
                        conta_debito=conta_banc,
                        conta_credito=conta_cli,
                        valor=valor,
                        historico=f"Recebimento Cliente (Editado): {parceiro_nome} - {descricao}",
                        documento_id=id_titulo,
                        tipo_origem="contas_receber_baixa"
                    )
                flash("Título a receber editado e refletido contábilmente com sucesso!", "success")
                return redirect(url_for("contas_receber"))
                
        except Exception as e:
            logging.error(f"Erro ao atualizar título financeiro: {e}")
            flash(f"Erro ao salvar edição do título: {e}", "danger")
            
    return redirect(url_for("dashboard"))


# ROTA: Diário -> Lançamento Contábil -> Atualizar (Edição Lançamento Manual)
@app.route("/contabilidade/lancamento/atualizar/<id_lancamento>", methods=["POST"])
@login_required
def contabilidade_lancamento_atualizar(id_lancamento):
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # 1. Busca o estado anterior no banco de dados (coleção 'lancamentos')
            antigo = tenant_db.lancamentos.find_one({"_id": ObjectId(id_lancamento)})
            is_legacy = False
            if not antigo:
                antigo = tenant_db.lancamentos_contabeis.find_one({"_id": ObjectId(id_lancamento)})
                is_legacy = True
                
            if not antigo:
                flash("Lançamento contábil não encontrado.", "danger")
                return redirect(url_for("contabil_lancamentos"))
                
            # 2. Deduz o valor antigo do 'saldo_debito' da conta_debito antiga e do 'saldo_credito' da conta_credito antiga no 'razonetes'
            conta_debito_antiga = antigo.get("conta_debito")
            conta_credito_antiga = antigo.get("conta_credito")
            valor_antigo = antigo.get("valor", 0.0)
            
            # Debito antigo
            nat_deb_ant = "Débito" if (conta_debito_antiga.startswith("1") or conta_debito_antiga.startswith("4")) else "Crédito"
            dec_saldo_deb = -valor_antigo if nat_deb_ant == "Débito" else valor_antigo
            tenant_db.razonetes.update_one(
                {"conta_codigo": conta_debito_antiga},
                {"$inc": {
                    "total_debito": -valor_antigo,
                    "saldo_debito": -valor_antigo,
                    "saldo": dec_saldo_deb
                }}
            )
            
            # Credito antigo
            nat_cred_ant = "Débito" if (conta_credito_antiga.startswith("1") or conta_credito_antiga.startswith("4")) else "Crédito"
            dec_saldo_cred = valor_antigo if nat_cred_ant == "Débito" else -valor_antigo
            tenant_db.razonetes.update_one(
                {"conta_codigo": conta_credito_antiga},
                {"$inc": {
                    "total_credito": -valor_antigo,
                    "saldo_credito": -valor_antigo,
                    "saldo": dec_saldo_cred
                }}
            )
            
            # 3. Trata e sanitiza dados vindos do formulário
            descricao = request.form.get("descricao", "").strip()
            valor_str = request.form.get("valor", "").strip()
            data_str = request.form.get("data", "").strip()
            conta_debito_nova = request.form.get("conta_debito", "").strip()
            conta_credito_nova = request.form.get("conta_credito", "").strip()
            centro_custo = request.form.get("centro_custo", "Geral").strip() or "Geral"
            
            valor_novo = converter_valor_br_para_float(valor_str)
            dt_nova = parse_date_safely(data_str) or datetime.today()
            data_nova_formatted = dt_nova.strftime("%Y-%m-%d")
            
            # 4. Atualiza o documento no MongoDB
            if not is_legacy:
                tenant_db.lancamentos.update_one(
                    {"_id": ObjectId(id_lancamento)},
                    {"$set": {
                        "data": data_nova_formatted,
                        "conta_debito": conta_debito_nova,
                        "conta_credito": conta_credito_nova,
                        "valor": valor_novo,
                        "historico": descricao
                    }}
                )
                tenant_db.lancamentos_contabeis.update_one(
                    {"documento_id": str(id_lancamento)},
                    {"$set": {
                        "data": data_nova_formatted,
                        "descricao": descricao,
                        "conta_debito": conta_debito_nova,
                        "conta_credito": conta_credito_nova,
                        "valor": valor_novo,
                        "centro_custo": centro_custo
                    }}
                )
            else:
                tenant_db.lancamentos_contabeis.update_one(
                    {"_id": ObjectId(id_lancamento)},
                    {"$set": {
                        "data": data_nova_formatted,
                        "descricao": descricao,
                        "conta_debito": conta_debito_nova,
                        "conta_credito": conta_credito_nova,
                        "valor": valor_novo,
                        "centro_custo": centro_custo
                    }}
                )
                
            # 5. Somar o novo valor no 'saldo_debito' da nova conta_debito e no 'saldo_credito' da nova conta_credito no 'razonetes'
            nat_deb_nov = "Débito" if (conta_debito_nova.startswith("1") or conta_debito_nova.startswith("4")) else "Crédito"
            inc_saldo_deb = valor_novo if nat_deb_nov == "Débito" else -valor_novo
            tenant_db.razonetes.update_one(
                {"conta_codigo": conta_debito_nova},
                {"$inc": {
                    "total_debito": valor_novo,
                    "saldo_debito": valor_novo,
                    "saldo": inc_saldo_deb
                }},
                upsert=True
            )
            
            nat_cred_nov = "Débito" if (conta_credito_nova.startswith("1") or conta_credito_nova.startswith("4")) else "Crédito"
            inc_saldo_cred = -valor_novo if nat_cred_nov == "Débito" else valor_novo
            tenant_db.razonetes.update_one(
                {"conta_codigo": conta_credito_nova},
                {"$inc": {
                    "total_credito": valor_novo,
                    "saldo_credito": valor_novo,
                    "saldo": inc_saldo_cred
                }},
                upsert=True
            )
            
            flash("Lançamento contábil manual atualizado e razonete recalculado com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao atualizar lançamento contábil manual: {e}")
            flash(f"Erro ao salvar edição do lançamento: {e}", "danger")
            
    return redirect(url_for("contabil_lancamentos"))


# AUXILIAR: Parser de conteúdo de arquivo extrato OFX (Tags clássicas)
def parse_ofx_content(content_str):
    # Separa os blocos de transações delimados por <STMTTRN>
    blocks = content_str.split("<STMTTRN>")
    transactions = []
    
    for block in blocks[1:]:
        trntype_match = re.search(r"<TRNTYPE>([^<\r\n\t]+)", block)
        dtposted_match = re.search(r"<DTPOSTED>([^<\r\n\t]+)", block)
        trnamt_match = re.search(r"<TRNAMT>([^<\r\n\t]+)", block)
        memo_match = re.search(r"<MEMO>([^<\r\n\t]+)", block)
        fitid_match = re.search(r"<FITID>([^<\r\n\t]+)", block)
        
        if trnamt_match and memo_match:
            trntype = trntype_match.group(1).strip() if trntype_match else "DEBIT"
            dtposted_raw = dtposted_match.group(1).strip() if dtposted_match else ""
            trnamt = float(trnamt_match.group(1).strip())
            memo = memo_match.group(1).strip()
            fitid = fitid_match.group(1).strip() if fitid_match else ""
            
            # Converte data DTPOSTED para YYYY-MM-DD
            date_str = datetime.today().strftime("%Y-%m-%d")
            if len(dtposted_raw) >= 8:
                try:
                    year = dtposted_raw[0:4]
                    month = dtposted_raw[4:6]
                    day = dtposted_raw[6:8]
                    date_str = f"{year}-{month}-{day}"
                except:
                    pass
                    
            transactions.append({
                "tipo": trntype,
                "data": date_str,
                "valor": trnamt,
                "memo": memo,
                "fitid": fitid
            })
            
    # Extrai o saldo final e a data do saldo para auditoria de integridade
    balamt_match = re.search(r"<BALAMT>([^<\r\n\t]+)", content_str)
    dtasof_match = re.search(r"<DTASOF>([^<\r\n\t]+)", content_str)
    
    balamt = 0.0
    if balamt_match:
        try:
            balamt = float(balamt_match.group(1).strip())
        except:
            pass
            
    dtasof_str = ""
    if dtasof_match:
        dtasof_raw = dtasof_match.group(1).strip()
        if len(dtasof_raw) >= 8:
            try:
                dtasof_str = f"{dtasof_raw[0:4]}-{dtasof_raw[4:6]}-{dtasof_raw[6:8]}"
            except:
                pass
                
    return {
        "transacoes": transactions,
        "saldo_final": balamt,
        "data_saldo": dtasof_str
    }


# ROTA: Configurações -> Filiais (Listagem e Cadastro)
@app.route("/config/filiais", methods=["GET", "POST"], strict_slashes=False)
@login_required
def config_filiais():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    admin_db = db_manager.get_admin_db()
    
    filiais = []
    max_cnpjs = 1
    
    if tenant_db is not None and admin_db is not None:
        try:
            grupo = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
            if grupo:
                max_cnpjs = grupo.get("max_cnpjs", 1)
            
            if request.method == "POST":
                razao_social = request.form.get("razao_social", "").strip()
                cnpj = request.form.get("cnpj", "").strip()
                ie = request.form.get("ie", "").strip()
                telefone = request.form.get("telefone", "").strip()
                
                if not razao_social or not cnpj:
                    flash("Razão Social e CNPJ são campos obrigatórios.", "danger")
                else:
                    # Trava comercial
                    current_count = tenant_db.filiais.count_documents({})
                    if current_count >= max_cnpjs:
                        flash(f"Limite comercial atingido. Seu plano permite cadastrar no máximo {max_cnpjs} filial(is).", "danger")
                    else:
                        # Evita CNPJ duplicado
                        existing = tenant_db.filiais.find_one({"cnpj": cnpj})
                        if existing:
                            flash("Filial com este CNPJ já cadastrada.", "danger")
                        else:
                            tenant_db.filiais.insert_one({
                                "razao_social": razao_social,
                                "cnpj": cnpj,
                                "ie": ie,
                                "telefone": telefone
                            })
                            # Também insere nos participantes como Empresa Principal se não existir
                            part_existing = tenant_db.participantes.find_one({"cnpj_cpf": cnpj})
                            if not part_existing:
                                tenant_db.participantes.insert_one({
                                    "nome_razao": razao_social,
                                    "tipo": "Empresa Principal",
                                    "cnpj_cpf": cnpj,
                                    "email": "",
                                    "telefone": telefone
                                })
                            flash("Filial cadastrada com sucesso!", "success")
                            return redirect(url_for("config_filiais"))
            
            filiais = list(tenant_db.filiais.find())
        except Exception as e:
            logging.error(f"Erro ao carregar ou salvar filiais: {e}")
            flash(f"Erro ao carregar filiais: {e}", "danger")
            
    return render_template(
        "filiais.html",
        filiais=filiais,
        max_cnpjs=max_cnpjs
    )


# ROTA: Configurações -> Centros de Custo (Listagem e Cadastro)
@app.route("/config/centros-custo", methods=["GET", "POST"], strict_slashes=False)
@login_required
def config_centros_custo():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    centros = []
    
    if tenant_db is not None:
        try:
            if request.method == "POST":
                codigo = request.form.get("codigo", "").strip()
                nome = request.form.get("nome", "").strip()
                departamento = request.form.get("departamento", "").strip()
                
                if not codigo or not nome:
                    flash("Código e Nome do Centro são campos obrigatórios.", "danger")
                else:
                    existing = tenant_db.centros_custo.find_one({"codigo": codigo})
                    if existing:
                        flash("Centro de Custo com este código já cadastrado.", "danger")
                    else:
                        tenant_db.centros_custo.insert_one({
                            "codigo": codigo,
                            "nome": nome,
                            "departamento": departamento
                        })
                        flash("Centro de Custo cadastrado com sucesso!", "success")
                        return redirect(url_for("config_centros_custo"))
            
            centros = list(tenant_db.centros_custo.find())
        except Exception as e:
            logging.error(f"Erro ao carregar ou salvar centros de custo: {e}")
            flash(f"Erro ao carregar centros de custo: {e}", "danger")
            
    return render_template(
        "centros_custo.html",
        centros=centros
    )


# ROTA: Configurações -> Contas Correntes Bancárias (Listagem e Cadastro)
@app.route("/config/contas-correntes", methods=["GET", "POST"], strict_slashes=False)
@login_required
def config_contas_correntes():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    contas = []
    contas_contabeis = []
    
    if tenant_db is not None:
        try:
            # Garante índices únicos para regras de conciliação
            tenant_db.contas_correntes.create_index([("cnpj", 1), ("apelido_conta", 1)], unique=True)
            tenant_db.contas_correntes.create_index("id_origem_ofx", unique=True, sparse=True)
            
            # Busca contas analíticas do grupo 1.1.1 ou 1.1.01
            contas_contabeis = list(tenant_db.plano_contas.find({
                "classificacao": "Analítica",
                "$or": [
                    {"codigo": {"$regex": "^1.1.1"}},
                    {"codigo": {"$regex": "^1.1.01"}}
                ]
            }).sort("codigo", 1))
            
            if request.method == "POST":
                banco_codigo = request.form.get("banco_codigo", "").strip()
                banco_ispb = request.form.get("banco_ispb", "").strip()
                banco_nome = request.form.get("banco_nome", "").strip()
                banco_codigo_zfilled = banco_codigo.zfill(3) if banco_codigo else ""
                
                # Para fallback/compatibilidade ainda mantemos o campo antigo 'banco'
                banco_legacy = f"{banco_codigo_zfilled} - {banco_nome}" if banco_codigo else request.form.get("banco", "").strip()
                
                agencia = request.form.get("agencia", "").strip()
                conta_numero = request.form.get("conta_numero", "").strip()
                conta_dv = request.form.get("conta_dv", "").strip()
                apelido_conta = request.form.get("apelido_conta", "").strip()
                cnpj = request.form.get("cnpj", "").strip()
                id_origem_ofx = request.form.get("id_origem_ofx", "").strip()
                conta_contabil_codigo = request.form.get("conta_contabil_codigo", "").strip()
                tipo_conta = request.form.get("tipo_conta", "Corrente").strip()
                ativo = request.form.get("ativo") == "on"
                
                if not banco_legacy or not agencia or not conta_numero or not conta_dv or not apelido_conta or not cnpj or not conta_contabil_codigo:
                    flash("Preencha todos os campos obrigatórios.", "danger")
                else:
                    try:
                        tenant_db.contas_correntes.insert_one({
                            "banco": banco_legacy,
                            "banco_codigo": banco_codigo_zfilled,
                            "banco_ispb": banco_ispb,
                            "banco_nome": banco_nome,
                            "agencia": agencia,
                            "conta_numero": conta_numero,
                            "conta_dv": conta_dv,
                            "apelido_conta": apelido_conta,
                            "cnpj": cnpj,
                            "id_origem_ofx": id_origem_ofx if id_origem_ofx else None,
                            "conta_contabil_codigo": conta_contabil_codigo,
                            "tipo_conta": tipo_conta,
                            "ativo": ativo
                        })
                        
                        resolvidos, _ = _reprocessar_pendentes_interno(tenant_db)
                        msg_extra = f" {resolvidos} lançamento(s) pendente(s) reprocessado(s)." if resolvidos > 0 else ""
                        
                        flash(f"Conta Corrente cadastrada com sucesso!{msg_extra}", "success")
                        return redirect(url_for("config_contas_correntes"))
                    except Exception as e:
                        flash(f"Erro ao salvar: {e}. Verifique se Apelido ou ID OFX já existem.", "danger")
            
            raw_contas = list(tenant_db.contas_correntes.find())
            cc_dict = {c["codigo"]: f"{c['codigo']} - {c['nome']}" for c in contas_contabeis}
            
            for c in raw_contas:
                c["conta_contabil_nome"] = cc_dict.get(c.get("conta_contabil_codigo"), c.get("conta_contabil_codigo"))
                contas.append(c)
                
        except Exception as e:
            logging.error(f"Erro ao carregar ou salvar contas correntes: {e}")
            flash(f"Erro ao carregar contas correntes: {e}", "danger")
            
    return render_template(
        "contas_correntes.html",
        contas=contas,
        contas_contabeis=contas_contabeis
    )

@app.route("/config/contas-correntes/atualizar", methods=["POST"], strict_slashes=False)
@login_required
def config_contas_correntes_atualizar():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            conta_id = request.form.get("conta_id", "").strip()
            banco_codigo = request.form.get("banco_codigo", "").strip()
            banco_ispb = request.form.get("banco_ispb", "").strip()
            banco_nome = request.form.get("banco_nome", "").strip()
            banco_codigo_zfilled = banco_codigo.zfill(3) if banco_codigo else ""
            
            banco_legacy = f"{banco_codigo_zfilled} - {banco_nome}" if banco_codigo else request.form.get("banco", "").strip()
            
            agencia = request.form.get("agencia", "").strip()
            conta_numero = request.form.get("conta_numero", "").strip()
            conta_dv = request.form.get("conta_dv", "").strip()
            apelido_conta = request.form.get("apelido_conta", "").strip()
            cnpj = request.form.get("cnpj", "").strip()
            id_origem_ofx = request.form.get("id_origem_ofx", "").strip()
            conta_contabil_codigo = request.form.get("conta_contabil_codigo", "").strip()
            tipo_conta = request.form.get("tipo_conta", "Corrente").strip()
            ativo = request.form.get("ativo") == "on"
            
            if not conta_id:
                flash("ID da conta não fornecido.", "danger")
                return redirect(url_for("config_contas_correntes"))

            tenant_db.contas_correntes.update_one(
                {"_id": ObjectId(conta_id)},
                {"$set": {
                    "banco": banco_legacy,
                    "banco_codigo": banco_codigo_zfilled,
                    "banco_ispb": banco_ispb,
                    "banco_nome": banco_nome,
                    "agencia": agencia,
                    "conta_numero": conta_numero,
                    "conta_dv": conta_dv,
                    "apelido_conta": apelido_conta,
                    "cnpj": cnpj,
                    "id_origem_ofx": id_origem_ofx if id_origem_ofx else None,
                    "conta_contabil_codigo": conta_contabil_codigo,
                    "tipo_conta": tipo_conta,
                    "ativo": ativo
                }}
            )
            
            resolvidos, _ = _reprocessar_pendentes_interno(tenant_db)
            msg_extra = f" {resolvidos} lançamento(s) pendente(s) reprocessado(s)." if resolvidos > 0 else ""
            
            flash(f"Conta Corrente atualizada com sucesso!{msg_extra}", "success")
        except Exception as e:
            logging.error(f"Erro ao atualizar conta corrente: {e}")
            flash(f"Erro ao atualizar: {e}", "danger")
            
    return redirect(url_for("config_contas_correntes"))


# ROTA: Configurações -> Regras de RegEx (Listagem e Cadastro)
@app.route("/config/regex")
@login_required
def config_regex():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    regras = []
    participantes = []
    contas = []
    
    if tenant_db is not None:
        try:
            regras = list(tenant_db.regras_regex.find())
            participantes = list(tenant_db.participantes.find().sort("nome_razao", 1))
            contas = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}).sort("codigo", 1))
        except Exception as e:
            logging.error(f"Erro ao carregar dados de Regras de Regex: {e}")
            flash(f"Erro ao carregar configurações: {e}", "danger")
            
    return render_template(
        "config_regex.html",
        regras=regras,
        participantes=participantes,
        contas=contas
    )


# ROTA: Regras de RegEx -> Salvar
@app.route("/config/regex/salvar", methods=["POST"])
@login_required
def config_regex_salvar():
    pattern = request.form.get("pattern", "").strip()
    tipo = request.form.get("tipo", "").strip()
    participante_id = request.form.get("participante_id", "").strip()
    conta_codigo = request.form.get("conta_codigo", "").strip()
    
    if not pattern or not tipo or not participante_id or not conta_codigo:
        flash("Todos os campos do formulário de regra são obrigatórios.", "danger")
        return redirect(url_for("config_regex"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # Valida expressão regular para evitar falhas em tempo de execução
            try:
                re.compile(pattern)
            except re.error:
                flash(f"A expressão regular '{pattern}' é inválida. Corrija a sintaxe de regex.", "danger")
                return redirect(url_for("config_regex"))
                
            participante = tenant_db.participantes.find_one({"_id": ObjectId(participante_id)})
            conta = tenant_db.plano_contas.find_one({"codigo": conta_codigo})
            
            if not participante or not conta:
                flash("Participante ou Conta Contábil inválidos.", "danger")
                return redirect(url_for("config_regex"))
                
            # Documento gravado na coleção 'regras_regex'
            nova_regra = {
                "pattern": pattern,
                "tipo": tipo,
                "participante_id": participante_id,
                "participante_nome": participante.get("nome_razao"),
                "conta_codigo": conta_codigo,
                "conta_nome": conta.get("nome")
            }
            tenant_db.regras_regex.insert_one(nova_regra)
            flash("Regra de RegEx cadastrada com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar regra regex: {e}")
            flash(f"Erro ao salvar regra: {e}", "danger")
            
    return redirect(url_for("config_regex"))


# ROTA: Importações -> Importar OFX (Histórico e Upload)
@app.route("/importar-ofx")
@login_required
def importar_ofx():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    arquivos = []
    contas_correntes = []
    if tenant_db is not None:
        try:
            arquivos = list(tenant_db.ofx_arquivos_importados.find().sort("data_importacao", -1))
            contas_correntes = list(tenant_db.contas_correntes.find())
        except Exception as e:
            logging.error(f"Erro ao carregar dados da página de importação OFX: {e}")
            flash(f"Erro ao carregar histórico: {e}", "danger")
            
    return render_template("importar_ofx.html", arquivos=arquivos, contas_correntes=contas_correntes)


# ROTA: Importar OFX -> Processar Upload
@app.route("/importar-ofx/upload", methods=["POST"])
@login_required
def importar_ofx_upload():
    if "ofx_file" not in request.files:
        flash("Nenhum arquivo enviado.", "danger")
        return redirect(url_for("importar_ofx"))
        
    file = request.files["ofx_file"]
    if file.filename == "":
        flash("Selecione um arquivo .ofx válido.", "danger")
        return redirect(url_for("importar_ofx"))
        
    conta_corrente_id = request.form.get("conta_corrente_id", "").strip()
    if not conta_corrente_id:
        flash("Selecione a Conta Corrente de Origem.", "danger")
        return redirect(url_for("importar_ofx"))
        
    if file and file.filename.lower().endswith(".ofx"):
        grupo_slug = session.get("grupo_slug")
        tenant_db = db_manager.get_group_db(grupo_slug)
        
        if tenant_db is not None:
            try:
                # Busca a conta corrente
                cc_doc = tenant_db.contas_correntes.find_one({"_id": ObjectId(conta_corrente_id)})
                if not cc_doc:
                    flash("Conta Corrente selecionada não encontrada.", "danger")
                    return redirect(url_for("importar_ofx"))
                
                conta_contabil_codigo = cc_doc.get("conta_contabil_codigo")
                
                content = file.read().decode("utf-8", errors="ignore")
                ofx_data = parse_ofx_content(content)
                movimentos = ofx_data["transacoes"]
                saldo_final = ofx_data["saldo_final"]
                data_saldo = ofx_data["data_saldo"]
                
                if not movimentos:
                    flash("Nenhuma transação contábil válida encontrada no arquivo OFX.", "danger")
                    return redirect(url_for("importar_ofx"))
                
                # Resolve intervalo de datas do arquivo para auditoria
                datas = [m["data"] for m in movimentos]
                data_inicio = min(datas)
                data_fim = max(datas)
                soma_transacoes = sum(m["valor"] for m in movimentos)
                
                # Grava o log de importação do arquivo
                arquivo_meta = {
                    "nome_arquivo": file.filename,
                    "data_importacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_transacoes": len(movimentos),
                    "saldo_final": saldo_final,
                    "data_saldo": data_saldo or data_fim,
                    "data_inicio": data_inicio,
                    "data_fim": data_fim,
                    "soma_transacoes": soma_transacoes,
                    "conta_corrente_id": conta_corrente_id,
                    "conta_contabil_codigo": conta_contabil_codigo
                }
                tenant_db.ofx_arquivos_importados.insert_one(arquivo_meta)
                
                # Grava os movimentos individuais na coleção 'movimentos_ofx_pendentes'
                for mov in movimentos:
                    mov_doc = {
                        "data": mov["data"],
                        "descricao_original": mov["memo"],
                        "valor": mov["valor"],
                        "fitid": mov["fitid"],
                        "id_arquivo": file.filename,
                        "status": "Pendente",
                        "conta_corrente_id": conta_corrente_id,
                        "conta_contabil_codigo": conta_contabil_codigo
                    }
                    tenant_db.movimentos_ofx_pendentes.insert_one(mov_doc)
                    
                flash(f"Extrato OFX processado! {len(movimentos)} lançamentos importados com sucesso.", "success")
            except Exception as e:
                logging.error(f"Erro ao processar arquivo OFX: {e}")
                flash(f"Falha ao ler arquivo extrato: {str(e)}", "danger")
    else:
        flash("Extensão inválida. Por favor, envie apenas arquivos no formato .ofx.", "danger")
        
    return redirect(url_for("importar_ofx"))


# ROTA: Importações -> Classificar Movimentos (Painel e Conciliação)
@app.route("/classificar-movimentos")
@login_required
def classificar_movimentos():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    movimentos_pendentes = []
    regras = []
    clientes = []
    fornecedores = []
    contas_receita = []
    contas_despesa = []
    
    total_valor = 0.0
    total_itens = 0
    
    if tenant_db is not None:
        try:
            # Carrega movimentos OFX pendentes
            movimentos_pendentes = list(tenant_db.movimentos_ofx_pendentes.find({"status": "Pendente"}))
            total_valor = sum(m.get("valor", 0.0) for m in movimentos_pendentes)
            total_itens = len(movimentos_pendentes)
            
            # Carrega regras de RegEx
            regras = list(tenant_db.regras_regex.find())
            
            # Carrega parceiros e plano de contas para as seleções manuais
            clientes = list(tenant_db.participantes.find({"tipo": {"$in": ["Cliente", "Ambos"]}}).sort("nome_razao", 1))
            fornecedores = list(tenant_db.participantes.find({"tipo": {"$in": ["Fornecedor", "Ambos"]}}).sort("nome_razao", 1))
            
            contas_receita = list(tenant_db.plano_contas.find({
                "classificacao": "Analítica",
                "tipo": "Resultado",
                "natureza": "Crédito"
            }).sort("codigo", 1))
            
            contas_despesa = list(tenant_db.plano_contas.find({
                "classificacao": "Analítica",
                "tipo": "Resultado",
                "natureza": "Débito"
            }).sort("codigo", 1))
            
            # Motor de Combinação (Regex Matcher)
            for mov in movimentos_pendentes:
                desc = mov.get("descricao_original", "")
                mov["sugestao"] = None
                
                # Executa re.search contra todas as regras salvas
                for r in regras:
                    pattern = r.get("pattern", "")
                    try:
                        if re.search(pattern, desc, re.IGNORECASE):
                            mov["sugestao"] = {
                                "participante_id": r.get("participante_id"),
                                "participante_nome": r.get("participante_nome"),
                                "conta_codigo": r.get("conta_codigo"),
                                "conta_nome": r.get("conta_nome"),
                                "regra_id": str(r.get("_id"))
                            }
                            break # Usa a primeira regra correspondente
                    except Exception as e:
                        logging.warning(f"Sintaxe de RegEx inválida na regra {r.get('_id')}: {pattern}. Erro: {e}")
                        
            # Busca centros de custo
            centros_custo = list(tenant_db.centros_custo.find())
        except Exception as e:
            logging.error(f"Erro ao carregar classificação de movimentos: {e}")
            flash(f"Erro ao processar movimentos: {e}", "danger")
            
    return render_template(
        "classificar_movimentos.html",
        movimentos=movimentos_pendentes,
        total_valor=total_valor,
        total_itens=total_itens,
        clientes=clientes,
        fornecedores=fornecedores,
        contas_receita=contas_receita,
        contas_despesa=contas_despesa,
        centros_custo=centros_custo
    )


# ROTA: Classificar Movimentos -> Conciliar Lançamento
@app.route("/movimentos/conciliar", methods=["POST"])
@login_required
def movimentos_conciliar():
    movimento_id = request.form.get("movimento_id", "").strip()
    participante_id = request.form.get("participante_id", "").strip()
    conta_codigo = request.form.get("conta_codigo", "").strip()
    
    criar_regra = request.form.get("criar_regra") == "true"
    regra_pattern = request.form.get("regra_pattern", "").strip()
    centro_custo = request.form.get("centro_custo", "Geral").strip() or "Geral"
    
    if not movimento_id or not participante_id or not conta_codigo:
        flash("Preencha o parceiro e a conta contábil para conciliar o movimento.", "danger")
        return redirect(url_for("classificar_movimentos"))
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is not None:
        try:
            # 1. Busca o movimento pendente correspondente
            mov = tenant_db.movimentos_ofx_pendentes.find_one({"_id": ObjectId(movimento_id)})
            if not mov:
                flash("Movimento de extrato não encontrado.", "danger")
                return redirect(url_for("classificar_movimentos"))
                
            participante = tenant_db.participantes.find_one({"_id": ObjectId(participante_id)})
            conta = tenant_db.plano_contas.find_one({"codigo": conta_codigo})
            
            if not participante or not conta:
                flash("Parceiro de negócios ou Conta Contábil inválidos.", "danger")
                return redirect(url_for("classificar_movimentos"))
                
            # 2. Motor de Conciliação e Baixa Automática no Contas a Pagar/Receber
            valor_mov = mov.get("valor", 0.0)
            has_matched_bill = False
            
            # Resolvendo conta_banc a partir da Conta Corrente selecionada no upload do OFX
            conta_banc_ofx = mov.get("conta_contabil_codigo")
            conta_banc = conta_banc_ofx if conta_banc_ofx else obter_conta_contabil_valida(tenant_db, ["1.1.01.002", "1.1.1"])
            
            # Converte data da transação do extrato para objeto datetime
            data_extrato = mov.get("data")
            if isinstance(data_extrato, datetime):
                dt_pagamento = data_extrato
            else:
                try:
                    dt_pagamento = datetime.strptime(str(data_extrato)[:10], "%Y-%m-%d")
                except Exception:
                    dt_pagamento = datetime.today()
            
            data_pagamento_str = dt_pagamento.strftime("%Y-%m-%d")
            
            if valor_mov < 0:
                # É um débito (Despesa). Procura conta a pagar Pendente correspondente
                bill = tenant_db.contas_pagar.find_one({
                    "status": "Pendente",
                    "participante_id": participante_id,
                    "valor": abs(valor_mov)
                })
                if bill:
                    tenant_db.contas_pagar.update_one(
                        {"_id": bill["_id"]},
                        {"$set": {
                            "status": "Pago",
                            "data_pagamento": dt_pagamento
                        }}
                    )
                    has_matched_bill = True
                    # Herança de Centro de Custo da competência
                    if bill.get("centro_custo"):
                        centro_custo = bill.get("centro_custo")
                    
                    # Lançamento Contábil de Baixa via Partidas Dobradas
                    conta_forn = obter_conta_contabil_valida(tenant_db, ["2.1.01.001", "2.1.1"])
                    descricao_contabil = f"Liquidação OFX Fornecedor: {participante.get('nome_razao')} - {mov.get('descricao_original')}"
                    
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_pagamento_str,
                        conta_debito=conta_forn,
                        conta_credito=conta_banc,
                        valor=abs(valor_mov),
                        historico=descricao_contabil,
                        documento_id=bill["_id"],
                        tipo_origem="contas_pagar_baixa"
                    )
                    logging.info(f"Auto-conciliado: Baixa de Contas a Pagar ID {bill['_id']} efetuada.")
            else:
                # É um crédito (Receita). Procura conta a receber Pendente correspondente
                bill = tenant_db.contas_receber.find_one({
                    "status": "Pendente",
                    "participante_id": participante_id,
                    "valor": valor_mov
                })
                if bill:
                    tenant_db.contas_receber.update_one(
                        {"_id": bill["_id"]},
                        {"$set": {
                            "status": "Recebido",
                            "data_recebimento": dt_pagamento
                        }}
                    )
                    has_matched_bill = True
                    # Herança de Centro de Custo da competência
                    if bill.get("centro_custo"):
                        centro_custo = bill.get("centro_custo")
                        
                    # Lançamento Contábil de Baixa via Partidas Dobradas
                    conta_clie = obter_conta_contabil_valida(tenant_db, ["1.1.03.001", "1.1.2"])
                    descricao_contabil = f"Liquidação OFX Cliente: {participante.get('nome_razao')} - {mov.get('descricao_original')}"
                    
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_pagamento_str,
                        conta_debito=conta_banc,
                        conta_credito=conta_clie,
                        valor=valor_mov,
                        historico=descricao_contabil,
                        documento_id=bill["_id"],
                        tipo_origem="contas_receber_baixa"
                    )
                    logging.info(f"Auto-conciliado: Baixa de Contas a Receber ID {bill['_id']} efetuada.")
            
            # Se não houve match com duplicata/competência, faz lançamento direto
            if not has_matched_bill:
                if valor_mov < 0:
                    # Lançamento Direto de Despesa via Partidas Dobradas
                    descricao_contabil = f"Lançamento Direto Despesa: {participante.get('nome_razao')} - {mov.get('descricao_original')}"
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_pagamento_str,
                        conta_debito=conta_codigo,
                        conta_credito=conta_banc,
                        valor=abs(valor_mov),
                        historico=descricao_contabil,
                        documento_id=mov["_id"],
                        tipo_origem="conciliacao_ofx"
                    )
                else:
                    # Lançamento Direto de Receita via Partidas Dobradas
                    descricao_contabil = f"Lançamento Direto Receita: {participante.get('nome_razao')} - {mov.get('descricao_original')}"
                    registrar_partida_dobrada(
                        db=tenant_db,
                        data=data_pagamento_str,
                        conta_debito=conta_banc,
                        conta_credito=conta_codigo,
                        valor=valor_mov,
                        historico=descricao_contabil,
                        documento_id=mov["_id"],
                        tipo_origem="conciliacao_ofx"
                    )
                    
            # 3. Persiste o movimento na coleção real de transações (Dashboard)
            nova_transacao = {
                "descricao": mov.get("descricao_original"),
                "valor": valor_mov,
                "tipo": "Receita" if valor_mov >= 0 else "Despesa",
                "data": mov.get("data"),
                "participante_id": participante_id,
                "participante_nome": participante.get("nome_razao"),
                "conta_codigo": conta_codigo,
                "conta_nome": conta.get("nome"),
                "origem": "Conciliação OFX",
                "centro_custo": centro_custo
            }
            tenant_db.transacoes.insert_one(nova_transacao)
            
            # 4. Remove o item dos movimentos pendentes
            tenant_db.movimentos_ofx_pendentes.delete_one({"_id": ObjectId(movimento_id)})
            
            # 5. Se o usuário solicitou criar uma regra de RegEx na conciliação manual
            if criar_regra:
                # Fallback: Se não especificou o regex no input, gera um regex baseado no MEMO
                if not regra_pattern:
                    clean_memo = re.sub(r'\s+', '.*', re.escape(mov.get("descricao_original")))
                    regra_pattern = f".*{clean_memo}.*"
                    
                # Valida regex
                try:
                    re.compile(regra_pattern)
                    # Grava a regra de automação
                    nova_regra = {
                        "pattern": regra_pattern,
                        "tipo": "Receita" if valor_mov >= 0 else "Despesa",
                        "participante_id": participante_id,
                        "participante_nome": participante.get("nome_razao"),
                        "conta_codigo": conta_codigo,
                        "conta_nome": conta.get("nome")
                    }
                    tenant_db.regras_regex.insert_one(nova_regra)
                    flash("Conciliação efetuada e regra regex salva!", "success")
                except re.error:
                    flash("Conciliação efetuada, mas a regra regex falhou por sintaxe inválida.", "danger")
            else:
                flash("Movimento de extrato conciliado com sucesso!", "success")
                
        except Exception as e:
            logging.error(f"Erro ao conciliar movimento: {e}")
            flash(f"Falha ao executar conciliação: {e}", "danger")
            
    return redirect(url_for("classificar_movimentos"))


# Helper para DRE: determina nível de indentação de contas
def get_account_indentation_level(code):
    parts = code.split(".")
    return min(len(parts), 4)

# Helper para DFC: segmenta atividades (usa metadata do plano de contas com fallback por código)
def classify_dfc_activity(conta_codigo, conta_doc=None):
    # Se temos o documento da conta com grupo_dfc preenchido, usa ele
    if conta_doc and conta_doc.get("grupo_dfc"):
        gd = conta_doc["grupo_dfc"]
        if gd == "investimento":
            return "Investimento"
        elif gd == "financiamento":
            return "Financiamento"
        return "Operacional"
    # Fallback por código
    if not conta_codigo:
        return "Operacional"
    if conta_codigo.startswith("1.2") or conta_codigo.startswith("1.1.3"):
        return "Investimento"
    if conta_codigo.startswith("2.2") or conta_codigo.startswith("2.1.3") or conta_codigo.startswith("2.3"):
        return "Financiamento"
    return "Operacional"

# ROTA: Financeiro -> DRE Contábil
@app.route("/financeiro/dre")
@login_required
def financeiro_dre():
    grupo_slug = session.get("grupo_slug")
    grupo_nome = session.get("grupo_nome")
    
    periodo = request.args.get("periodo", datetime.today().strftime("%Y-%m"))
    
    contas_dre = []
    lucro_prejuizo = 0.0
    
    retroceder_periodo = ""
    avancar_periodo = ""
    
    try:
        tenant_db = db_manager.get_group_db(grupo_slug)
        if tenant_db is not None:
            # Resolução da Navegação Temporal
            base_date = datetime.strptime(periodo, "%Y-%m")
            m_prev = base_date.month - 1
            y_prev = base_date.year
            if m_prev == 0:
                m_prev = 12
                y_prev -= 1
            retroceder_periodo = f"{y_prev:04d}-{m_prev:02d}"
            
            m_next = base_date.month + 1
            y_next = base_date.year
            if m_next == 13:
                m_next = 1
                y_next += 1
            avancar_periodo = f"{y_next:04d}-{m_next:02d}"
            
            data_inicio = f"{periodo}-01"
            data_fim = f"{periodo}-31"
            
            relatorios = RelatoriosContabeis(tenant_db)
            resultado_dre = relatorios.gerar_dre(data_inicio, data_fim)
            
            contas_dre = resultado_dre["contas"]
            lucro_prejuizo = resultado_dre["lucro_prejuizo"]
            
    except Exception as e:
        logging.error(f"Erro ao gerar DRE para Grupo {grupo_nome}: {e}")
        flash(f"Erro ao processar DRE: {e}", "danger")
        
    return render_template(
        "dre.html",
        contas=contas_dre,
        lucro_prejuizo=lucro_prejuizo,
        periodo=periodo,
        retroceder_periodo=retroceder_periodo,
        avancar_periodo=avancar_periodo
    )

# ROTA: Financeiro -> Balanço Patrimonial
@app.route("/financeiro/balanco")
@login_required
def financeiro_balanco():
    grupo_slug = session.get("grupo_slug")
    grupo_nome = session.get("grupo_nome")
    
    periodo = request.args.get("periodo", datetime.today().strftime("%Y-%m"))
    
    ativo = []
    passivo = []
    total_ativo = 0.0
    total_passivo_pl = 0.0
    balanco_fecha = False
    
    retroceder_periodo = ""
    avancar_periodo = ""
    
    try:
        tenant_db = db_manager.get_group_db(grupo_slug)
        if tenant_db is not None:
            # Resolução da Navegação Temporal
            base_date = datetime.strptime(periodo, "%Y-%m")
            m_prev = base_date.month - 1
            y_prev = base_date.year
            if m_prev == 0:
                m_prev = 12
                y_prev -= 1
            retroceder_periodo = f"{y_prev:04d}-{m_prev:02d}"
            
            m_next = base_date.month + 1
            y_next = base_date.year
            if m_next == 13:
                m_next = 1
                y_next += 1
            avancar_periodo = f"{y_next:04d}-{m_next:02d}"

            data_fim = f"{periodo}-31"
            
            relatorios = RelatoriosContabeis(tenant_db)
            resultado_bp = relatorios.gerar_balanco_patrimonial(data_fim)
            
            ativo = resultado_bp["ativo"]
            passivo = resultado_bp["passivo"]
            total_ativo = resultado_bp["total_ativo"]
            total_passivo_pl = resultado_bp["total_passivo_pl"]
            balanco_fecha = resultado_bp["balanco_fecha"]
            
    except Exception as e:
        logging.error(f"Erro ao gerar Balanço Patrimonial: {e}")
        flash(f"Erro ao processar Balanço Patrimonial: {e}", "danger")
        
    return render_template(
        "balanco.html",
        ativo=ativo,
        passivo=passivo,
        total_ativo=total_ativo,
        total_passivo_pl=total_passivo_pl,
        balanco_fecha=balanco_fecha,
        periodo=periodo,
        retroceder_periodo=retroceder_periodo,
        avancar_periodo=avancar_periodo
    )


# ROTA: Financeiro -> DRE Anual Comparativo
@app.route("/financeiro/dre-anual")
@login_required
def financeiro_dre_anual():
    grupo_slug = session.get("grupo_slug")
    grupo_nome = session.get("grupo_nome")
    
    ano = int(request.args.get("ano", datetime.today().year))
    cnpj_filtro = request.args.get("cnpj", "").strip()
    centro_custo_filtro = request.args.get("centro_custo", "").strip()
    
    contas_com_saldo = []
    participantes_cnpj = []
    centros_custo = ["Geral"]
    
    try:
        tenant_db = db_manager.get_group_db(grupo_slug)
        if tenant_db is not None:
            participantes_cnpj = list(tenant_db.participantes.find({"cnpj_cpf": {"$ne": ""}}).sort("nome_razao", 1))
            
            cc_trans = tenant_db.transacoes.distinct("centro_custo")
            cc_pagar = tenant_db.contas_pagar.distinct("centro_custo")
            cc_receber = tenant_db.contas_receber.distinct("centro_custo")
            unique_cc = set(cc_trans + cc_pagar + cc_receber)
            unique_cc.discard(None)
            unique_cc.discard("")
            centros_custo = sorted(list(unique_cc))
            if "Geral" not in centros_custo:
                centros_custo.insert(0, "Geral")
                
            # Transações filtradas por ano
            match_query = {"data": {"$regex": f"^{ano}"}}
            if cnpj_filtro:
                part = tenant_db.participantes.find_one({"cnpj_cpf": cnpj_filtro})
                if part:
                    match_query["participante_id"] = str(part["_id"])
                else:
                    match_query["participante_id"] = "non-existent"
            if centro_custo_filtro and centro_custo_filtro != "Geral":
                match_query["centro_custo"] = centro_custo_filtro
                
            trans = list(tenant_db.transacoes.find(match_query))
            
            # Agrega saldos de contas analíticas por mês (0 a 11)
            analitica_monthly = {}
            for t in trans:
                code = t.get("conta_codigo")
                val = t.get("valor", 0.0)
                t_date = t.get("data", "")
                if len(t_date) >= 7:
                    try:
                        m_idx = int(t_date[5:7]) - 1
                        if code not in analitica_monthly:
                            analitica_monthly[code] = [0.0] * 12
                        analitica_monthly[code][m_idx] += val
                    except:
                        pass
                        
            # Rollup comparativo 12 meses
            contas = list(tenant_db.plano_contas.find().sort("codigo", 1))
            for c in contas:
                code = c["codigo"]
                c["nivel"] = get_account_indentation_level(code)
                
                monthly_balances = [0.0] * 12
                for a_code, a_vals in analitica_monthly.items():
                    if a_code == code or a_code.startswith(code + "."):
                        for m in range(12):
                            monthly_balances[m] += a_vals[m]
                            
                accumulated = sum(monthly_balances)
                c["mensal"] = monthly_balances
                c["acumulado"] = accumulated
                
                # Ocultação de Acumulado Zero
                if abs(accumulated) > 0.001:
                    contas_com_saldo.append(c)
                    
    except Exception as e:
        logging.error(f"Erro ao gerar DRE Anual para Grupo {grupo_nome}: {e}")
        flash(f"Erro ao processar DRE Anual: {e}", "danger")
        
    return render_template(
        "dre_anual.html",
        contas=contas_com_saldo,
        ano=ano,
        cnpj_filtro=cnpj_filtro,
        centro_custo_filtro=centro_custo_filtro,
        centros_custo=centros_custo,
        participantes_cnpj=participantes_cnpj
    )


# ROTA: Financeiro -> DFC (Fluxo de Caixa Operacional/Investimento/Financiamento)
@app.route("/financeiro/dfc")
@login_required
def financeiro_dfc():
    grupo_slug = session.get("grupo_slug")
    grupo_nome = session.get("grupo_nome")
    
    periodo = request.args.get("periodo", datetime.today().strftime("%Y-%m"))
    cnpj_filtro = request.args.get("cnpj", "").strip()
    centro_custo_filtro = request.args.get("centro_custo", "").strip()
    
    participantes_cnpj = []
    centros_custo = ["Geral"]
    
    dfc_operacional = []
    dfc_investimento = []
    dfc_financiamento = []
    
    total_operacional = 0.0
    total_investimento = 0.0
    total_financiamento = 0.0
    saldo_liquido = 0.0
    
    try:
        tenant_db = db_manager.get_group_db(grupo_slug)
        if tenant_db is not None:
            participantes_cnpj = list(tenant_db.participantes.find({"cnpj_cpf": {"$ne": ""}}).sort("nome_razao", 1))
            
            cc_trans = tenant_db.transacoes.distinct("centro_custo")
            cc_pagar = tenant_db.contas_pagar.distinct("centro_custo")
            cc_receber = tenant_db.contas_receber.distinct("centro_custo")
            unique_cc = set(cc_trans + cc_pagar + cc_receber)
            unique_cc.discard(None)
            unique_cc.discard("")
            centros_custo = sorted(list(unique_cc))
            if "Geral" not in centros_custo:
                centros_custo.insert(0, "Geral")
                
            # Transações do período (Regime de Caixa - desconsidera Provisões contábeis)
            match_query = {"data": {"$regex": f"^{periodo}"}, "origem": {"$ne": "Provisão Contábil"}}
            if cnpj_filtro:
                part = tenant_db.participantes.find_one({"cnpj_cpf": cnpj_filtro})
                if part:
                    match_query["participante_id"] = str(part["_id"])
                else:
                    match_query["participante_id"] = "non-existent"
            if centro_custo_filtro and centro_custo_filtro != "Geral":
                match_query["centro_custo"] = centro_custo_filtro
                
            trans = list(tenant_db.transacoes.find(match_query).sort("data", 1))
            
            for t in trans:
                code = t.get("conta_codigo")
                val = t.get("valor", 0.0)
                
                item = {
                    "data": t.get("data"),
                    "descricao": t.get("descricao"),
                    "participante_nome": t.get("participante_nome", "Geral"),
                    "conta_nome": f"{code} - {t.get('conta_name', t.get('conta_nome', ''))}",
                    "valor": val
                }
                
                act_type = classify_dfc_activity(code)
                if act_type == "Investimento":
                    dfc_investimento.append(item)
                    total_investimento += val
                elif act_type == "Financiamento":
                    dfc_financiamento.append(item)
                    total_financiamento += val
                else:
                    dfc_operacional.append(item)
                    total_operacional += val
                    
            saldo_liquido = total_operacional + total_investimento + total_financiamento
            
    except Exception as e:
        logging.error(f"Erro ao gerar DFC para Grupo {grupo_nome}: {e}")
        flash(f"Erro ao processar DFC: {e}", "danger")
        
    return render_template(
        "dfc.html",
        periodo=periodo,
        cnpj_filtro=cnpj_filtro,
        centro_custo_filtro=centro_custo_filtro,
        centros_custo=centros_custo,
        participantes_cnpj=participantes_cnpj,
        dfc_operacional=dfc_operacional,
        dfc_investimento=dfc_investimento,
        dfc_financiamento=dfc_financiamento,
        total_operacional=total_operacional,
        total_investimento=total_investimento,
        total_financiamento=total_financiamento,
        saldo_liquido=saldo_liquido
    )


# ROTA: Contábil -> Lançamentos (Livro Diário)
@app.route("/contabil/lancamentos")
@login_required
def contabil_lancamentos():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    lancamentos = []
    if tenant_db is not None:
        try:
            lancamentos = list(tenant_db.lancamentos_contabeis.find().sort("data", -1))
        except Exception as e:
            logging.error(f"Erro ao buscar lancamentos contabeis: {e}")
            flash(f"Erro ao carregar lançamentos: {e}", "danger")
            
    return render_template("lancamentos.html", lancamentos=lancamentos)


# ROTA: Contábil -> Livro Razão
@app.route("/contabil/razao")
@login_required
def contabil_razao():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    periodo = request.args.get("periodo", "").strip()
    centro_custo = request.args.get("centro_custo", "Geral").strip() or "Geral"
    
    contas = []
    razao_dados = {}
    centros_custo = ["Geral"]
    
    if tenant_db is not None:
        try:
            contas = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}).sort("codigo", 1))
            
            # Popula filtros de centro de custo
            cc_trans = tenant_db.transacoes.distinct("centro_custo")
            cc_pagar = tenant_db.contas_pagar.distinct("centro_custo")
            cc_receber = tenant_db.contas_receber.distinct("centro_custo")
            unique_cc = set(cc_trans + cc_pagar + cc_receber)
            unique_cc.discard(None)
            unique_cc.discard("")
            centros_custo = sorted(list(unique_cc))
            if "Geral" not in centros_custo:
                centros_custo.insert(0, "Geral")
                
            query = {}
            if periodo:
                query["data"] = {"$regex": f"^{periodo}"}
            if centro_custo and centro_custo != "Geral":
                query["centro_custo"] = centro_custo
                
            postings = list(tenant_db.lancamentos_contabeis.find(query).sort("data", 1))
            
            for c in contas:
                codigo = c["codigo"]
                nome = c["nome"]
                
                # Natureza da conta contábil
                if codigo.startswith("1") or codigo.startswith("4"):
                    nat_conta = "Débito"
                else:
                    nat_conta = "Crédito"
                    
                saldo = 0.0
                lancamentos_conta = []
                
                for p in postings:
                    is_debito = (p["conta_debito"] == codigo)
                    is_credito = (p["conta_credito"] == codigo)
                    
                    if not is_debito and not is_credito:
                        continue
                        
                    v = p["valor"]
                    tipo_mov = ""
                    
                    if is_debito:
                        tipo_mov = "D"
                        if nat_conta == "Débito":
                            saldo += v
                        else:
                            saldo -= v
                    elif is_credito:
                        tipo_mov = "C"
                        if nat_conta == "Débito":
                            saldo -= v
                        else:
                            saldo += v
                            
                    lancamentos_conta.append({
                        "data": p["data"],
                        "descricao": p["descricao"],
                        "tipo_mov": tipo_mov,
                        "valor": v,
                        "saldo": saldo
                    })
                    
                if lancamentos_conta:
                    razao_dados[codigo] = {
                        "nome": nome,
                        "natureza": nat_conta,
                        "lancamentos": lancamentos_conta,
                        "saldo_final": saldo
                    }
        except Exception as e:
            logging.error(f"Erro ao buscar razao contábil: {e}")
            flash(f"Erro ao carregar livro razão: {e}", "danger")
            
    return render_template(
        "razao.html",
        razao_dados=razao_dados,
        periodo=periodo,
        centro_custo_filtro=centro_custo,
        centros_custo=centros_custo
    )


# ROTA: Contábil -> Razonetes
@app.route("/contabil/razonetes")
@login_required
def contabil_razonetes():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    periodo = request.args.get("periodo", "").strip()
    centro_custo = request.args.get("centro_custo", "Geral").strip() or "Geral"
    
    contas = []
    razonetes_dados = {}
    centros_custo = ["Geral"]
    
    if tenant_db is not None:
        try:
            contas = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}).sort("codigo", 1))
            
            # Popula filtros de centro de custo
            cc_trans = tenant_db.transacoes.distinct("centro_custo")
            cc_pagar = tenant_db.contas_pagar.distinct("centro_custo")
            cc_receber = tenant_db.contas_receber.distinct("centro_custo")
            unique_cc = set(cc_trans + cc_pagar + cc_receber)
            unique_cc.discard(None)
            unique_cc.discard("")
            centros_custo = sorted(list(unique_cc))
            if "Geral" not in centros_custo:
                centros_custo.insert(0, "Geral")
                
            query = {}
            if periodo:
                query["data"] = {"$regex": f"^{periodo}"}
            if centro_custo and centro_custo != "Geral":
                query["centro_custo"] = centro_custo
                
            postings = list(tenant_db.lancamentos_contabeis.find(query).sort("data", 1))
            
            for c in contas:
                codigo = c["codigo"]
                nome = c["nome"]
                
                if codigo.startswith("1") or codigo.startswith("4"):
                    nat_conta = "Débito"
                else:
                    nat_conta = "Crédito"
                    
                debitos = []
                creditos = []
                total_debito = 0.0
                total_credito = 0.0
                
                for p in postings:
                    v = p["valor"]
                    if p["conta_debito"] == codigo:
                        debitos.append({"data": p["data"], "valor": v, "descricao": p["descricao"]})
                        total_debito += v
                    elif p["conta_credito"] == codigo:
                        creditos.append({"data": p["data"], "valor": v, "descricao": p["descricao"]})
                        total_credito += v
                        
                if debitos or creditos:
                    if nat_conta == "Débito":
                        saldo = total_debito - total_credito
                    else:
                        saldo = total_credito - total_debito
                        
                    razonetes_dados[codigo] = {
                        "nome": nome,
                        "natureza": nat_conta,
                        "debitos": debitos,
                        "creditos": creditos,
                        "total_debito": total_debito,
                        "total_credito": total_credito,
                        "saldo": saldo
                    }
        except Exception as e:
            logging.error(f"Erro ao buscar razonetes: {e}")
            flash(f"Erro ao carregar razonetes: {e}", "danger")
            
    return render_template(
        "razonetes.html",
        razonetes_dados=razonetes_dados,
        periodo=periodo,
        centro_custo_filtro=centro_custo,
        centros_custo=centros_custo
    )


# ROTA: Importações -> Auditoria de Integridade OFX
@app.route("/importacoes/integridade")
@login_required
def importacoes_integridade():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    alertas = []
    arquivos = []
    
    if tenant_db is not None:
        try:
            alertas = executar_auditoria_integridade(tenant_db)
            arquivos = list(tenant_db.ofx_arquivos_importados.find().sort("data_inicio", -1))
        except Exception as e:
            logging.error(f"Erro na integridade de dados: {e}")
            flash(f"Erro ao executar auditoria: {e}", "danger")
            
    return render_template("integridade.html", alertas=alertas, arquivos=arquivos)



@app.template_filter('real')
def real_filter(val):
    if val is None:
        return "R$ 0,00"
    try:
        val = float(str(val))
    except (ValueError, TypeError):
        return "R$ 0,00"
    
    negativo = val < 0
    val = abs(val)
    s = f"{val:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    if negativo:
        return f"-R$ {s}"
    return f"R$ {s}"


@app.template_filter('num_br')
def num_br_filter(val):
    if val is None:
        return "0,00"
    try:
        val = float(val)
    except (ValueError, TypeError):
        return "0,00"
    
    negativo = val < 0
    val = abs(val)
    s = f"{val:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    if negativo:
        return f"-{s}"
    return s


# ==========================================
# PAINEL MASTER ADMIN & MOTOR IMPERSONATION
# ==========================================

@app.route("/admin/dashboard", strict_slashes=False)
@app.route("/admin/dashboard/", strict_slashes=False)
@app.route("/admin-master", strict_slashes=False)
@app.route("/admin-master/", strict_slashes=False)
@app.route("/gestao-master", strict_slashes=False)
@app.route("/gestao-master/", strict_slashes=False)
@login_required
def admin_dashboard():
    # Permissão flexível
    if not (session.get("permissao_master") is True or session.get("role") == "superadmin" or session.get("impersonator_role") == "superadmin" or session.get("impersonator_permissao_master") is True):
        flash("Acesso exclusivo para Gestores da Plataforma Master.", "danger")
        return redirect(url_for("dashboard"))
        
    try:
        admin_db = db_manager.get_admin_db()
        if admin_db is None:
            raise Exception("Não foi possível conectar ao banco central.")
            
        grupos = list(admin_db.assinaturas_grupos.find().sort("nome_grupo", 1))
        
        total_mrr = 0.0
        ativos_count = 0
        total_bpo_count = 0
        total_users_plataforma = 0
        total_disk_utilizados_mb = 0.0
        grupos_lista = []
        
        for g in grupos:
            valor_mens = float(g.get("valor_mensalidade", 0.0))
            status = g.get("status", "Inativo")
            is_bpo = g.get("flag_bpo", False)
            
            if status == "Ativo":
                total_mrr += valor_mens
                ativos_count += 1
            if is_bpo:
                total_bpo_count += 1
                
            # Consulta em tempo real no banco isolado
            cnpjs_utilizados = 0
            usuarios_utilizados = 0
            total_lancamentos = 0
            data_size_mb = 0.0
            storage_size_mb = 0.0
            
            usuarios_detalhes = []
            filiais_detalhes = []
            
            grupo_db_name = g.get("db_name")
            if grupo_db_name:
                try:
                    tenant_db = db_manager.get_group_db(grupo_db_name)
                    if tenant_db is not None:
                        collections = tenant_db.list_collection_names()
                        
                        # Contagem e detalhes de usuários
                        if "usuarios" in collections:
                            usuarios_utilizados = tenant_db.usuarios.count_documents({})
                            usuarios_detalhes = list(tenant_db.usuarios.find({}, {"nome": 1, "email": 1, "role": 1, "_id": 0}))
                            
                        # Contagem e detalhes de filiais
                        if "filiais" in collections:
                            cnpjs_utilizados = tenant_db.filiais.count_documents({})
                            filiais_detalhes = list(tenant_db.filiais.find({}, {"nome": 1, "cnpj": 1, "cnpj_cpf": 1, "_id": 0}))
                        else:
                            cnpjs_utilizados = tenant_db.participantes.count_documents({"cnpj_cpf": {"$ne": ""}})
                            filiais_detalhes = list(tenant_db.participantes.find({"cnpj_cpf": {"$ne": ""}}, {"nome_razao": 1, "cnpj_cpf": 1, "_id": 0}))
                            
                        for fd in filiais_detalhes:
                            if "nome_razao" in fd and "nome" not in fd:
                                fd["nome"] = fd["nome_razao"]
                            if "cnpj_cpf" in fd and "cnpj" not in fd:
                                fd["cnpj"] = fd["cnpj_cpf"]
                                
                        # Lançamentos totais
                        if "lancamentos" in collections:
                            total_lancamentos += tenant_db.lancamentos.count_documents({})
                        if "lancamentos_contabeis" in collections:
                            total_lancamentos += tenant_db.lancamentos_contabeis.count_documents({})
                        if "transacoes" in collections:
                            total_lancamentos += tenant_db.transacoes.count_documents({})
                            
                        # dbstats
                        try:
                            stats = tenant_db.command("dbstats")
                            data_size_mb = round(stats.get("dataSize", 0) / (1024 * 1024), 2)
                            storage_size_mb = round(stats.get("storageSize", 0) / (1024 * 1024), 2)
                        except Exception as stats_err:
                            logging.warning(f"Erro ao ler dbstats para {grupo_db_name}: {stats_err}")
                except Exception as db_err:
                    logging.warning(f"Erro ao ler banco isolado {grupo_db_name} para contagem no painel admin: {db_err}")
            
            total_disk_utilizados_mb += data_size_mb
            total_users_plataforma += usuarios_utilizados
            
            # Badge de alerta de expiração
            data_limite_raw = g.get("data_limite_acesso", "")
            dias_restantes = None
            alerta_limite = "normal"
            data_limite_br = ""
            
            if data_limite_raw:
                dt_limite = None
                if isinstance(data_limite_raw, datetime):
                    dt_limite = data_limite_raw.date()
                elif isinstance(data_limite_raw, date):
                    dt_limite = data_limite_raw
                else:
                    try:
                        dt_limite = datetime.strptime(str(data_limite_raw).strip()[:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
                
                if dt_limite:
                    data_limite_br = dt_limite.strftime("%d/%m/%Y")
                    hoje = datetime.now().date()
                    diff = (dt_limite - hoje).days
                    dias_restantes = diff
                    if diff < 0:
                        alerta_limite = "expirado"
                    elif diff <= 5:
                        alerta_limite = "proximo"
            
            grupos_lista.append({
                "id": str(g["_id"]),
                "nome_grupo": g.get("nome_grupo"),
                "db_name": g.get("db_name"),
                "status": status,
                "valor_mensalidade": valor_mens,
                "data_vencimento": g.get("data_vencimento", ""),
                "data_limite_acesso": data_limite_raw,
                "data_limite_acesso_br": data_limite_br,
                "dias_restantes": dias_restantes,
                "alerta_limite": alerta_limite,
                "max_cnpjs": g.get("max_cnpjs", 5),
                "max_usuarios": g.get("max_usuarios", 5),
                "cnpjs_utilizados": cnpjs_utilizados,
                "usuarios_utilizados": usuarios_utilizados,
                "total_lancamentos": total_lancamentos,
                "data_size_mb": data_size_mb,
                "storage_size_mb": storage_size_mb,
                "flag_bpo": is_bpo,
                "usuarios_detalhes": usuarios_detalhes,
                "filiais_detalhes": filiais_detalhes
            })
            
        # Formata disco total
        if total_disk_utilizados_mb >= 1024:
            total_disk_str = f"{round(total_disk_utilizados_mb / 1024, 2)} GB"
        else:
            total_disk_str = f"{total_disk_utilizados_mb} MB"
            
        return render_template(
            "admin_dashboard.html",
            grupos=grupos_lista,
            total_mrr=total_mrr,
            ativos_count=ativos_count,
            total_bpo_count=total_bpo_count,
            total_users_plataforma=total_users_plataforma,
            total_disk_str=total_disk_str
        )
    except Exception as e:
        logging.error(f"Erro no painel admin dashboard: {e}")
        flash(f"Erro ao carregar o dashboard administrativo: {e}", "danger")
        return redirect(url_for("dashboard"))


# ROTA: Painel Master -> Atualizar Comercial e Técnico Completo
@app.route("/admin/grupo/atualizar", methods=["POST"], strict_slashes=False)
@app.route("/admin/grupo/atualizar/", methods=["POST"], strict_slashes=False)
@app.route("/admin/grupo/atualizar-completo", methods=["POST"], strict_slashes=False)
@app.route("/admin/grupo/atualizar-completo/", methods=["POST"], strict_slashes=False)
@app.route("/gestao-master/grupo/atualizar", methods=["POST"], strict_slashes=False)
@app.route("/gestao-master/grupo/atualizar/", methods=["POST"], strict_slashes=False)
@login_required
def admin_grupo_atualizar_completo():
    if not (session.get("permissao_master") is True or session.get("role") == "superadmin" or session.get("impersonator_role") == "superadmin" or session.get("impersonator_permissao_master") is True):
        flash("Acesso exclusivo para Gestores da Plataforma Master.", "danger")
        return redirect(url_for("dashboard"))
        
    grupo_id = request.form.get("grupo_id")
    status = request.form.get("status", "Ativo").strip()
    
    try:
        max_cnpjs = int(request.form.get("max_cnpjs", 5))
    except (ValueError, TypeError):
        max_cnpjs = 5
        
    try:
        max_usuarios = int(request.form.get("max_usuarios", 5))
    except (ValueError, TypeError):
        max_usuarios = 5
        
    valor_mensalidade = converter_valor_br_para_float(request.form.get("valor_mensalidade"))
    data_vencimento = request.form.get("data_vencimento", "10").strip()
    flag_bpo = request.form.get("flag_bpo") in ["true", "on", "True"]
    
    data_limite_acesso_raw = request.form.get("data_limite_acesso", "").strip()
    data_limite_acesso = ""
    if data_limite_acesso_raw:
        dt_limite = parse_date_safely(data_limite_acesso_raw)
        if dt_limite:
            data_limite_acesso = dt_limite.strftime("%Y-%m-%d")
            
    admin_db = db_manager.get_admin_db()
    if admin_db is not None:
        try:
            admin_db.assinaturas_grupos.update_one(
                {"_id": ObjectId(grupo_id)},
                {"$set": {
                    "max_cnpjs": max_cnpjs,
                    "max_usuarios": max_usuarios,
                    "valor_mensalidade": valor_mensalidade,
                    "data_vencimento": data_vencimento,
                    "status": status,
                    "flag_bpo": flag_bpo,
                    "data_limite_acesso": data_limite_acesso
                }}
            )
            flash("Assinatura, limites e parâmetros comerciais atualizados com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar reestruturacao master completa: {e}")
            flash(f"Erro ao salvar alterações: {e}", "danger")
            
    return redirect(url_for("admin_dashboard"))


# ROTA: Painel Master -> Portal de Gestão SaaS (/gestao-master)
@app.route("/gestao-master-deprecated", methods=["GET", "POST"], strict_slashes=False)
@login_required
def gestao_master():
    if not is_master_admin_session():
        flash("Acesso exclusivo para Gestores da Plataforma Master.", "danger")
        return redirect(url_for("dashboard"))

    admin_db = db_manager.get_admin_db()
    grupos = []
    if admin_db is not None:
        try:
            grupos = list(admin_db.assinaturas_grupos.find().sort("nome_grupo", 1))
        except Exception as e:
            logging.error(f"Erro ao carregar assinaturas no gestao-master: {e}")
            flash(f"Erro ao carregar assinaturas: {e}", "danger")
            
    grupos_lista = []
    for g in grupos:
        grupo_slug = g.get("db_name")
        cnpjs_utilizados = 0
        usuarios_utilizados = 0
        
        # Consulta contagem em tempo real nos bancos isolados dos tenants
        if grupo_slug:
            try:
                tenant_db = db_manager.get_group_db(grupo_slug)
                if tenant_db is not None:
                    cnpjs_utilizados = tenant_db.participantes.count_documents({"cnpj_cpf": {"$ne": ""}})
                    usuarios_utilizados = tenant_db.usuarios.count_documents({})
            except Exception as e:
                logging.warning(f"Erro ao ler tenant_db {grupo_slug}: {e}")

        grupos_lista.append({
            "id": str(g["_id"]),
            "nome_grupo": g.get("nome_grupo"),
            "db_name": g.get("db_name"),
            "status": g.get("status", "Inativo"),
            "valor_mensalidade": float(g.get("valor_mensalidade", 0.0)),
            "max_cnpjs": g.get("max_cnpjs", 5),
            "max_usuarios": g.get("max_usuarios", 5),
            "cnpjs_utilizados": cnpjs_utilizados,
            "usuarios_utilizados": usuarios_utilizados,
            "data_vencimento": g.get("data_vencimento", "10"),
            "flag_bpo": g.get("flag_bpo", False)
        })

    total_mrr = sum(g["valor_mensalidade"] for g in grupos_lista if g["status"] == "Ativo")
    ativos_count = sum(1 for g in grupos_lista if g["status"] == "Ativo")
    suspensos_count = sum(1 for g in grupos_lista if g["status"] == "Suspenso")

    return render_template(
        "gestao_master.html",
        grupos=grupos_lista,
        total_mrr=total_mrr,
        ativos_count=ativos_count,
        suspensos_count=suspensos_count
    )


# ROTA: Painel Master -> Atualizar Plano/Limites e Bloqueio (/gestao-master/grupo/atualizar)
@app.route("/gestao-master/grupo/atualizar-deprecated", methods=["POST"], strict_slashes=False)
@app.route("/gestao-master/grupo/atualizar-deprecated/", methods=["POST"], strict_slashes=False)
@login_required
def gestao_master_grupo_atualizar():
    if not is_master_admin_session():
        return redirect(url_for("dashboard"))

    grupo_id = request.form.get("grupo_id")
    status = request.form.get("status") # 'Ativo', 'Suspenso', 'Cancelado'
    try:
        max_cnpjs = int(request.form.get("max_cnpjs", 1))
    except (ValueError, TypeError):
        max_cnpjs = 1
        
    try:
        max_usuarios = int(request.form.get("max_usuarios", 2))
    except (ValueError, TypeError):
        max_usuarios = 2
        
    valor_mensalidade = converter_valor_br_para_float(request.form.get("valor_mensalidade"))
    data_vencimento = request.form.get("data_vencimento", "10")
    flag_bpo = request.form.get("flag_bpo") in ["true", "on", "True"]

    admin_db = db_manager.get_admin_db()
    if admin_db is not None:
        try:
            admin_db.assinaturas_grupos.update_one(
                {"_id": ObjectId(grupo_id)},
                {"$set": {
                    "status": status,
                    "max_cnpjs": max_cnpjs,
                    "max_usuarios": max_usuarios,
                    "valor_mensalidade": valor_mensalidade,
                    "data_vencimento": data_vencimento,
                    "flag_bpo": flag_bpo
                }}
            )
            flash("Assinatura e limites atualizados com sucesso!", "success")
        except Exception as e:
            logging.error(f"Erro ao salvar reestruturacao master: {e}")
            flash(f"Erro ao salvar alterações: {e}", "danger")

    return redirect(url_for("gestao_master"))


@app.route("/admin/bpo-central", strict_slashes=False)
@app.route("/admin/bpo-central/", strict_slashes=False)
@login_required
@master_admin_required
def admin_bpo_central():
    try:
        admin_db = db_manager.get_admin_db()
        if admin_db is None:
            raise Exception("Não foi possível conectar ao banco central.")
            
        # Filtra grupos que têm flag_bpo como True
        grupos = list(admin_db.assinaturas_grupos.find({"flag_bpo": True}).sort("nome_grupo", 1))
        
        grupos_lista = []
        for g in grupos:
            grupos_lista.append({
                "id": str(g["_id"]),
                "nome_grupo": g.get("nome_grupo"),
                "db_name": g.get("db_name"),
                "status": g.get("status", "Inativo")
            })
            
        return render_template("bpo_central.html", grupos=grupos_lista)
    except Exception as e:
        logging.error(f"Erro na central BPO: {e}")
        flash(f"Erro ao carregar a central de BPO: {e}", "danger")
        return redirect(url_for("dashboard"))


@app.route("/admin/bpo/operar/<grupo_slug>", methods=["GET", "POST"], strict_slashes=False)
@app.route("/admin/bpo/operar/<grupo_slug>/", methods=["GET", "POST"], strict_slashes=False)
@login_required
@master_admin_required
def admin_bpo_operar(grupo_slug):
    try:
        admin_db = db_manager.get_admin_db()
        if admin_db is None:
            raise Exception("Não foi possível conectar ao banco central.")
            
        grupo = admin_db.assinaturas_grupos.find_one({"db_name": grupo_slug})
        if not grupo:
            flash("Grupo Econômico não encontrado.", "danger")
            return redirect(url_for("admin_bpo_central"))
            
        # 1. Guarda dados originais do Master Admin se não estiver já impersonando
        if "impersonator_user_id" not in session:
            session["impersonator_user_id"] = session.get("user_id")
            session["impersonator_user_name"] = session.get("user_name")
            session["impersonator_user_email"] = session.get("user_email")
            session["impersonator_grupo_id"] = session.get("grupo_id")
            session["impersonator_grupo_nome"] = session.get("grupo_nome")
            session["impersonator_db_name"] = session.get("db_name")
            session["impersonator_grupo_slug"] = session.get("grupo_slug")
            session["impersonator_permissao_master"] = session.get("permissao_master", True)
            session["impersonator_role"] = session.get("role", "superadmin")
            
        # 2. Sobrescreve as variáveis da sessão operacional com o grupo selecionado
        session["grupo_id"] = str(grupo["_id"])
        session["grupo_nome"] = grupo.get("nome_grupo")
        session["db_name"] = grupo.get("db_name")
        session["grupo_slug"] = grupo.get("db_name")
        
        logging.info(f"Master Admin operando Grupo Econômico: {grupo.get('nome_grupo')} (Impersonation)")
        flash(f"Operando financeiramente o grupo: {grupo.get('nome_grupo')}.", "warning")
        return redirect(url_for("dashboard"))
        
    except Exception as e:
        logging.error(f"Erro ao iniciar operação BPO para {grupo_slug}: {e}")
        flash(f"Erro ao operar cliente BPO: {str(e)}", "danger")
        return redirect(url_for("admin_bpo_central"))


@app.route("/admin/bpo/sair", strict_slashes=False)
@app.route("/admin/bpo/sair/", strict_slashes=False)
@login_required
def admin_bpo_sair():
    if "impersonator_user_id" in session:
        # Restaura os dados originais do Master Admin
        session["user_id"] = session.pop("impersonator_user_id")
        session["user_name"] = session.pop("impersonator_user_name")
        session["user_email"] = session.pop("impersonator_user_email")
        session["grupo_id"] = session.pop("impersonator_grupo_id")
        session["grupo_nome"] = session.pop("impersonator_grupo_nome")
        session["db_name"] = session.pop("impersonator_db_name")
        session["grupo_slug"] = session.pop("impersonator_grupo_slug")
        session["permissao_master"] = session.pop("impersonator_permissao_master", True)
        session["role"] = session.pop("impersonator_role", "superadmin")
        
        logging.info("Impersonation encerrada. Sessão Master Admin restaurada.")
        flash("Sessão administrativa restabelecida.", "success")
        return redirect(url_for("admin_bpo_central"))
        
    return redirect(url_for("dashboard"))



# ===============================================================================
# ROTA: Importação Universal (OFX, CSV, Excel) - Novo Fluxo Interativo
# ===============================================================================
@app.route("/importacao-universal", methods=["GET"], strict_slashes=False)
@login_required
def importacao_universal():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    contas_correntes = []
    contas_resultado = []
    if tenant_db is not None:
        try:
            contas_correntes = list(tenant_db.contas_correntes.find())
            # Traz contas analíticas de resultado (3 e 4) para o select de categorias
            contas_resultado = list(tenant_db.plano_contas.find({
                "classificacao": "Analítica",
                "$or": [{"codigo": {"$regex": "^3"}}, {"codigo": {"$regex": "^4"}}]
            }).sort("codigo", 1))
        except Exception as e:
            logging.error(f"Erro ao carregar dados da página de importação universal: {e}")
            
    return render_template("importar_universal.html", contas_correntes=contas_correntes, contas_resultado=contas_resultado)

@app.route("/importacao-universal/preview", methods=["POST"], strict_slashes=False)
@login_required
def importacao_universal_preview():
    if "file" not in request.files:
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo enviado."})
        
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"sucesso": False, "mensagem": "Nenhum arquivo selecionado."})
        
    conta_corrente_id = request.form.get("conta_corrente_id", "").strip()
        
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is None:
        return jsonify({"sucesso": False, "mensagem": "Erro de conexão com o banco de dados."})
        
    try:
        content = file.read()
        transacoes_parsed, metadados = ImportadorUniversal.parse_file(content, file.filename)
        
        if not conta_corrente_id and 'id_origem_ofx' in metadados:
            cc_db = tenant_db.contas_correntes.find_one({"id_origem_ofx": metadados['id_origem_ofx']})
            if cc_db:
                conta_corrente_id = str(cc_db['_id'])
                
        if not conta_corrente_id:
            return jsonify({"sucesso": False, "mensagem": "Selecione a Conta Corrente de Origem (não detectada automaticamente)."})
            
        transacoes_verificadas = ImportadorUniversal.verificar_duplicidades(tenant_db, transacoes_parsed, conta_corrente_id)
        
        # Filtra registros com falhas de parse ou sem data
        transacoes_validas = [t for t in transacoes_verificadas if t.get("data") and t.get("valor") is not None]
        
        return jsonify({
            "sucesso": True, 
            "transacoes": transacoes_validas,
            "total_lidas": len(transacoes_parsed),
            "conta_corrente_id": conta_corrente_id
        })
    except Exception as e:
        logging.error(f"Erro no preview de importação: {e}")
        return jsonify({"sucesso": False, "mensagem": str(e)})

@app.route("/importacao-universal/commit", methods=["POST"], strict_slashes=False)
@login_required
def importacao_universal_commit():
    """Recebe transações selecionadas/editadas e efetiva no banco de dados"""
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if tenant_db is None:
        return jsonify({"sucesso": False, "mensagem": "Erro de conexão com banco de dados."})
        
    try:
        dados = request.get_json()
        conta_corrente_id = dados.get("conta_corrente_id")
        transacoes = dados.get("transacoes", [])
        
        if not conta_corrente_id or not transacoes:
            return jsonify({"sucesso": False, "mensagem": "Dados insuficientes para importação."})
            
        cc_doc = None
        if conta_corrente_id:
            cc_doc = tenant_db.contas_correntes.find_one({"_id": ObjectId(conta_corrente_id)})
            
        conta_bancaria_codigo = cc_doc.get("conta_contabil_codigo") if cc_doc else None
            
        importados = 0
        pendentes_cadastro = 0
        agora = datetime.now(timezone.utc).isoformat()
        
        # Caching para Bulk Insert
        planos = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}))
        mapa_plano = {p["codigo"]: p for p in planos if "codigo" in p}
        
        # Lote collections
        lote_transacoes = []
        lote_pendentes = []
        lote_motor_dados = []
        
        for t in transacoes:
            valor = float(t.get("valor", 0))
            if valor == 0:
                continue
                
            data = t.get("data")
            descricao = t.get("descricao")
            categoria = t.get("categoria") # Conta de resultado escolhida
            
            # Validação (Memória)
            conta_resultado_valida = False
            if categoria:
                pc_db = mapa_plano.get(categoria)
                if pc_db:
                    conta_resultado_valida = True
                    
            conta_corrente_valida = True if conta_bancaria_codigo else False
            
            motivos = []
            if not conta_resultado_valida:
                motivos.append("conta_resultado_inexistente")
            if not conta_corrente_valida:
                motivos.append("conta_corrente_inexistente")
                
            doc_id = ObjectId()
            
            if motivos:
                pendentes_cadastro += 1
                lote_pendentes.append({
                    "_id": doc_id,
                    "arquivo_origem": "Importação Universal",
                    "data": data,
                    "descricao": descricao,
                    "valor": valor,
                    "categoria_informada": categoria,
                    "conta_bancaria_informada": cc_doc.get("apelido_conta") if cc_doc else "",
                    "motivo_pendencia": " e ".join(motivos),
                    "sugerido_codigo_resultado": categoria,
                    "sugerido_nome_resultado": "",
                    "sugerido_apelido_banco": cc_doc.get("apelido_conta") if cc_doc else "",
                    "status": "Pendente de Cadastro",
                    "gerar_partidas_dobradas": True,
                    "importado_em": agora
                })
                
                # Chunk insert
                if len(lote_pendentes) >= 1000:
                    tenant_db.lancamentos_pendentes_cadastro.insert_many(lote_pendentes)
                    lote_pendentes.clear()
                    
                continue
            
            # Registra a partida dobrada.
            if valor > 0:
                conta_debito = conta_bancaria_codigo
                conta_credito = categoria
            else:
                conta_debito = categoria
                conta_credito = conta_bancaria_codigo
                
            abs_valor = Decimal(str(abs(valor)))
            partidas = [
                PartidaContabil(conta_debito, 'D', abs_valor),
                PartidaContabil(conta_credito, 'C', abs_valor)
            ]
            
            lote_motor_dados.append({
                "data": data,
                "historico": descricao,
                "partidas": partidas,
                "documento_id": str(doc_id),
                "origem": "importacao_universal"
            })
            
            # Inserir também na tabela de transações antigas para manter compatibilidade
            lote_transacoes.append({
                "_id": doc_id,
                "data": data,
                "descricao": descricao,
                "valor": valor,
                "tipo": "Receita" if valor >= 0 else "Despesa",
                "conta_codigo": categoria,
                "origem": "Importação Universal",
                "importado_em": agora
            })
            
            importados += 1
            
            # Chunk insert transações e motor
            if len(lote_transacoes) >= 1000:
                tenant_db.transacoes.insert_many(lote_transacoes)
                if lote_motor_dados:
                    motor = MotorContabil(tenant_db)
                    motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
                    
                lote_transacoes.clear()
                lote_motor_dados.clear()
                
        # Insert remaining
        if lote_pendentes:
            tenant_db.lancamentos_pendentes_cadastro.insert_many(lote_pendentes)
            
        if lote_transacoes:
            tenant_db.transacoes.insert_many(lote_transacoes)
            if lote_motor_dados:
                motor = MotorContabil(tenant_db)
                motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
            
        return jsonify({
            "sucesso": True, 
            "importados": importados, 
            "pendentes": pendentes_cadastro,
            "mensagem": f"{importados} lançamentos importados. {pendentes_cadastro} pendentes de cadastro."
        })
    except Exception as e:
        logging.error(f"Erro no commit da importação: {e}")
        return jsonify({"sucesso": False, "mensagem": str(e)})

@app.route("/api/importacao/pendencias", methods=["GET"])
@login_required
def importacao_pendencias():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if not tenant_db:
        return jsonify({"sucesso": False, "mensagem": "Erro de conexão com o banco de dados."})
        
    try:
        pipeline_resultado = [
            {"$match": {"status": "Pendente de Cadastro", "sugerido_codigo_resultado": {"$ne": ""}, "sugerido_codigo_resultado": {"$ne": None}}},
            {"$group": {
                "_id": "$sugerido_codigo_resultado",
                "nome_sugerido": {"$first": "$sugerido_nome_resultado"},
                "total_ocorrencias": {"$sum": 1},
                "valor_acumulado": {"$sum": "$valor"}
            }},
            {"$project": {
                "codigo": "$_id",
                "nome_sugerido": 1,
                "total_ocorrencias": 1,
                "valor_acumulado": 1,
                "_id": 0
            }}
        ]
        
        pipeline_correntes = [
            {"$match": {"status": "Pendente de Cadastro", "sugerido_apelido_banco": {"$ne": ""}, "sugerido_apelido_banco": {"$ne": None}}},
            {"$group": {
                "_id": "$sugerido_apelido_banco",
                "total_ocorrencias": {"$sum": 1},
                "valor_acumulado": {"$sum": "$valor"}
            }},
            {"$project": {
                "apelido_banco": "$_id",
                "total_ocorrencias": 1,
                "valor_acumulado": 1,
                "_id": 0
            }}
        ]
        
        contas_resultado = list(tenant_db.lancamentos_pendentes_cadastro.aggregate(pipeline_resultado))
        contas_correntes = list(tenant_db.lancamentos_pendentes_cadastro.aggregate(pipeline_correntes))
        
        return jsonify({
            "sucesso": True,
            "contas_resultado_faltantes": contas_resultado,
            "contas_correntes_faltantes": contas_correntes
        })
    except Exception as e:
        logging.error(f"Erro ao buscar pendencias agrupadas: {e}")
        return jsonify({"sucesso": False, "mensagem": str(e)})
@app.route("/api/lancamentos-pendentes/reprocessar", methods=["POST"], strict_slashes=False)
@login_required
def reprocessar_pendentes():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if not tenant_db:
        return jsonify({"sucesso": False, "mensagem": "Erro de conexão com o banco de dados."})
        
def _reprocessar_pendentes_interno(tenant_db):
    pendentes = list(tenant_db.lancamentos_pendentes_cadastro.find({"status": "Pendente de Cadastro"}))
    if not pendentes:
        return 0, 0
        
    # Caching
    planos = list(tenant_db.plano_contas.find({"classificacao": "Analítica"}))
    mapa_plano = {p["codigo"]: p for p in planos if "codigo" in p}
    
    ccs = list(tenant_db.contas_correntes.find())
    mapa_cc = {c["apelido_conta"]: c for c in ccs if "apelido_conta" in c}
    
    resolvidos = 0
    agora = datetime.now(timezone.utc).isoformat()
    
    lote_transacoes = []
    lote_lancamentos = []
    lote_motor_dados = []
    ids_resolvidos = []
    
    for p in pendentes:
        doc_id = p["_id"]
        codigo_conta = p.get("sugerido_codigo_resultado") or p.get("categoria_informada")
        apelido_banco = p.get("sugerido_apelido_banco") or p.get("conta_bancaria_informada")
        valor = p.get("valor", 0.0)
        
        # Validação 1: Plano de Contas
        conta_resultado_valida = False
        if codigo_conta:
            pc_db = mapa_plano.get(codigo_conta)
            if pc_db:
                conta_resultado_valida = True
                
        # Validação 2: Contas Correntes
        conta_corrente_valida = False
        conta_disponibilidades_atual = ""
        if apelido_banco:
            cc_db = mapa_cc.get(apelido_banco)
            if cc_db and cc_db.get("conta_contabil_codigo"):
                conta_disponibilidades_atual = cc_db["conta_contabil_codigo"]
                conta_corrente_valida = True
                
        if conta_resultado_valida and conta_corrente_valida:
            # Pode reprocessar!
            
            # Inserir transação legada
            lote_transacoes.append({
                "_id": doc_id,
                "data": p.get("data"),
                "descricao": p.get("descricao"),
                "valor": valor,
                "tipo": "Receita" if valor >= 0 else "Despesa",
                "conta_codigo": codigo_conta,
                "origem": p.get("arquivo_origem", "Reprocessamento"),
                "conta_bancaria": apelido_banco,
                "centro_custo": "Geral",
                "importado_em": p.get("importado_em")
            })
            
            if p.get("gerar_partidas_dobradas"):
                abs_valor = Decimal(str(abs(valor)))
                if valor > 0:
                    conta_debito = conta_disponibilidades_atual
                    conta_credito = codigo_conta
                else:
                    conta_debito = codigo_conta
                    conta_credito = conta_disponibilidades_atual
                    
                # Lançamento legado
                lote_lancamentos.append({
                    "data": p.get("data"),
                    "conta_debito": conta_debito,
                    "conta_credito": conta_credito,
                    "valor": abs(valor),
                    "historico": p.get("descricao"),
                    "documento_id": str(doc_id),
                    "tipo_origem": "reprocessamento"
                })
                
                # Motor Contábil
                partidas = [
                    PartidaContabil(conta_debito, 'D', abs_valor),
                    PartidaContabil(conta_credito, 'C', abs_valor)
                ]
                lote_motor_dados.append({
                    "data": p.get("data"),
                    "historico": p.get("descricao"),
                    "partidas": partidas,
                    "documento_id": str(doc_id),
                    "origem": "reprocessamento"
                })
            
            ids_resolvidos.append(doc_id)
            resolvidos += 1
            
            # Chunk insert
            if len(lote_transacoes) >= 1000:
                tenant_db.transacoes.insert_many(lote_transacoes)
                if lote_lancamentos:
                    tenant_db.lancamentos.insert_many(lote_lancamentos)
                if lote_motor_dados:
                    motor = MotorContabil(tenant_db)
                    motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
                
                tenant_db.lancamentos_pendentes_cadastro.delete_many({"_id": {"$in": ids_resolvidos}})
                
                lote_transacoes.clear()
                lote_lancamentos.clear()
                lote_motor_dados.clear()
                ids_resolvidos.clear()
                
    # Insert remaining
    if lote_transacoes:
        tenant_db.transacoes.insert_many(lote_transacoes)
        if lote_lancamentos:
            tenant_db.lancamentos.insert_many(lote_lancamentos)
        if lote_motor_dados:
            motor = MotorContabil(tenant_db)
            motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
            
        tenant_db.lancamentos_pendentes_cadastro.delete_many({"_id": {"$in": ids_resolvidos}})
            
    return resolvidos, len(pendentes)

@app.route("/api/lancamentos-pendentes/reprocessar", methods=["POST"], strict_slashes=False)
@login_required
def reprocessar_pendentes():
    grupo_slug = session.get("grupo_slug")
    tenant_db = db_manager.get_group_db(grupo_slug)
    
    if not tenant_db:
        return jsonify({"sucesso": False, "mensagem": "Erro de conexão com o banco de dados."})
        
    try:
        resolvidos, total = _reprocessar_pendentes_interno(tenant_db)
        if total == 0:
            return jsonify({"sucesso": True, "resolvidos": 0, "mensagem": "Nenhum lançamento pendente de reprocessamento."})
        
        return jsonify({
            "sucesso": True, 
            "resolvidos": resolvidos, 
            "total_verificados": total,
            "mensagem": f"{resolvidos} lançamentos resolvidos e reprocessados com sucesso!"
        })
        
    except Exception as e:
        logging.error(f"Erro no reprocessamento: {e}")
        return jsonify({"sucesso": False, "mensagem": str(e)})



# NOVAS ROTAS (No final do app.py)
@app.route("/contabil/periodos", methods=["GET"])
@master_admin_required
def periodos_contabeis():
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    periodos = list(tenant_db.periodos_fechados.find().sort("ano_mes", -1))
    auditoria = list(tenant_db.log_auditoria_periodos.find().sort("data_evento", -1).limit(50))
    return render_template("periodos_contabeis.html", periodos=periodos, auditoria=auditoria)

@app.route("/contabil/periodos/fechar", methods=["POST"])
@master_admin_required
def fechar_periodo():
    ano_mes = request.form.get("ano_mes", "").strip()
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    tenant_db.periodos_fechados.update_one(
        {"ano_mes": ano_mes},
        {"$set": {
            "fechado": True, 
            "fechado_em": datetime.now(timezone.utc).isoformat(), 
            "fechado_por": session.get("user_email")
        }},
        upsert=True
    )
    flash(f"Período {ano_mes} fechado.", "success")
    return redirect(url_for("periodos_contabeis"))

@app.route("/contabil/periodos/reabrir", methods=["POST"])
@master_admin_required
def reabrir_periodo():
    ano_mes = request.form.get("ano_mes", "").strip()
    justificativa = request.form.get("justificativa", "").strip()
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    
    tenant_db.periodos_fechados.update_one(
        {"ano_mes": ano_mes},
        {"$set": {"fechado": False}}
    )
    tenant_db.log_auditoria_periodos.insert_one({
        "ano_mes": ano_mes,
        "acao": "REABERTURA",
        "justificativa": justificativa,
        "usuario": session.get("user_email"),
        "data_evento": datetime.now(timezone.utc).isoformat()
    })
    flash(f"Período {ano_mes} REABERTO. Ação registrada em log.", "warning")
    return redirect(url_for("periodos_contabeis"))

if __name__ == "__main__":
    # Roda localmente na porta configurada
    print(f"Iniciando Flow Empresas na porta {Config.PORT}...")
    app.run(host="0.0.0.0", port=Config.PORT, debug=Config.DEBUG)
