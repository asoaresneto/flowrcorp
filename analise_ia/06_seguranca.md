# ETAPA 6 - Autenticação e Segurança

O sistema não utiliza JWT, baseando-se no gerenciamento seguro de sessões do Flask (cookies assinados na origem), com forte isolamento multi-tenant. A senha dos usuários é protegida via hashing irreversível usando `bcrypt`. Abaixo estão os principais trechos de código que garantem o isolamento de dados e o controle de inatividade.

## 1. Configurações de Segurança e Segredos (`config.py`)
```python
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

class Config:
    # Chave mascarada
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "<CHAVE_SECRETA_MASCARADA>")
    MONGO_URI = os.getenv("MONGO_URI", "<STRING_CONEXAO_MONGO_MASCARADA>")
    ADMIN_DB_NAME = "db_flow_admin"
    
    # Duração da sessão (Controle de Inatividade estrito)
    PERMANENT_SESSION_LIFETIME_MINUTES = 5

    # SMTP mascarado
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
    SMTP_USER = os.getenv("SMTP_USER", "<EMAIL_SMTP_MASCARADO>")
    SMTP_PASS = os.getenv("SMTP_PASS", "<SENHA_SMTP_MASCARADA>")
    SMTP_FROM = os.getenv("SMTP_FROM", "<EMAIL_SMTP_MASCARADO>")
```

## 2. Middlewares e Decorators de Acesso (`app.py`)

### Proteção de Proxy Reverso
```python
class ReverseProxyPrefixMiddleware:
    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        script_name = environ.get("HTTP_X_SCRIPT_NAME", self.prefix)
        if script_name:
            environ["SCRIPT_NAME"] = script_name
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(script_name):
                environ["PATH_INFO"] = path_info[len(script_name):]
        
        scheme = environ.get("HTTP_X_FORWARDED_PROTO", "")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
        return self.wsgi_app(environ, start_response)
```

### Hook Global: Expiração por Inatividade Absoluta (5 minutos)
```python
@app.before_request
def check_session_inactivity():
    if request.endpoint in ["login", "logout", "static"] or not request.endpoint:
        return

    if "user_id" in session:
        last_activity = session.get("last_activity")
        now = datetime.now(timezone.utc).timestamp()
        
        if last_activity:
            elapsed_seconds = now - last_activity
            timeout_limit = Config.PERMANENT_SESSION_LIFETIME_MINUTES * 60
            
            if elapsed_seconds > timeout_limit:
                session.clear()
                return redirect(url_for("login", reason="timeout"))
        
        session["last_activity"] = now
```

### Decorators de Acesso
```python
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

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
            flash("Acesso restrito.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function
```

## 3. Autenticação Isolada e Hashing (Rota `/login` - Omissão Parcial)
```python
# (...)
# 1. Busca o Usuário no Banco Central
user = admin_db.usuarios_master.find_one({"email": email})

# 2. Valida a Senha Hashed (Bcrypt)
stored_hash = user.get("senha", "")
if not bcrypt.checkpw(senha.encode("utf-8"), stored_hash.encode("utf-8")):
    error_msg = "E-mail ou senha incorretos."

# 3. Autenticação bem-sucedida: Define o tenant do cliente na sessão isolando o banco!
session.clear()
session.permanent = True
session["user_id"] = str(user["_id"])
session["grupo_id"] = str(grupo["_id"])
session["db_name"] = grupo.get("db_name")  # NOME DO BANCO ISOLADO NO MONGODB DO CLIENTE (Tenant)
session["grupo_slug"] = grupo.get("db_name")
session["role"] = user.get("role", "user")
session["last_activity"] = datetime.now(timezone.utc).timestamp()
# (...)
```
