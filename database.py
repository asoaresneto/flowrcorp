import logging
from pymongo import MongoClient
from config import Config

# Configuração básica de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class DatabaseManager:
    """
    Gerenciador de conexões MongoDB.
    Estabelece e reutiliza o cliente MongoDB, fornecendo acesso dinâmico
    ao banco central de administração (db_flow_admin) e aos bancos
    isolados de cada Grupo Econômico (db_grupo_...).
    """
    def __init__(self):
        self._client = None

    def _get_client(self):
        """Inicializa e retorna o MongoClient (pool de conexões gerenciado internamente)"""
        if self._client is None:
            try:
                logging.info(f"Conectando ao MongoDB em: {Config.MONGO_URI}")
                # Configura timeouts para evitar que a aplicação trave se o banco estiver indisponível
                self._client = MongoClient(
                    Config.MONGO_URI, 
                    serverSelectionTimeoutMS=3000, 
                    connectTimeoutMS=3000
                )
                # Verifica a conexão executando um comando simples
                self._client.admin.command("ping")
                logging.info("Conexão com o MongoDB estabelecida com sucesso.")
            except Exception as e:
                logging.error(f"Falha crítica na conexão com o MongoDB: {e}")
                self._client = None
        return self._client

    def get_admin_db(self):
        """Retorna a referência ao banco administrativo central (db_flow_admin)"""
        client = self._get_client()
        if client is not None:
            return client[Config.ADMIN_DB_NAME]
        return None

    def get_group_db(self, db_identifier):
        """
        Retorna a referência ao banco isolado de um Grupo Econômico específico.
        Aceita tanto o nome exato do banco (ex: 'db_grupo_holding_xyz') quanto
        o slug do grupo econômico (ex: 'holding_xyz').
        
        :param db_identifier: Nome do banco ou slug do grupo econômico (ex: session.get("grupo_slug"))
        """
        client = self._get_client()
        if client is not None and db_identifier:
            db_identifier = db_identifier.strip()
            # Se for apenas o slug, mapeia para o nome correto do banco
            if not db_identifier.startswith("db_grupo_"):
                db_name = f"db_grupo_{db_identifier.lower().replace(' ', '_')}"
            else:
                db_name = db_identifier
            return client[db_name]
        return None

# Instância única global exportável
db_manager = DatabaseManager()
