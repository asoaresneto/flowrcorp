import os
from pathlib import Path
from dotenv import load_dotenv

# Caminho base do projeto
BASE_DIR = Path(__file__).resolve().parent

# Carrega as variáveis do arquivo .env
load_dotenv(BASE_DIR / ".env")

class Config:
    """Classe de configuração do Flask"""
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "fallback-secret-key-for-dev-only")
    
    # Configurações do MongoDB
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    ADMIN_DB_NAME = "db_flow_admin"
    
    # Servidor Flask
    FLASK_ENV = os.getenv("FLASK_ENV", "development")
    PORT = int(os.getenv("FLASK_PORT", 5000))
    DEBUG = FLASK_ENV == "development"
    
    # Prefixo de rota para Nginx Proxy Reverso (ex: /corp)
    SCRIPT_NAME_PREFIX = os.getenv("SCRIPT_NAME_PREFIX", "").strip()
    
    # Duração da sessão (Controle de Inatividade - 5 minutos)
    PERMANENT_SESSION_LIFETIME_MINUTES = 5

    # Pastas de templates e arquivos estáticos usando pathlib
    TEMPLATE_FOLDER = str(BASE_DIR / "templates")
    STATIC_FOLDER = str(BASE_DIR / "static")
    
    # SMTP para Recuperação de Senha
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER", "adriano@asncontabilidade.cnt.br")
    SMTP_PASS = os.getenv("SMTP_PASS", "KQtuywyDLC0w")
    SMTP_FROM = os.getenv("SMTP_FROM", "financeiro@asncontabilidade.cnt.br")
