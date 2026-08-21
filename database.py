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
        self._indexed_dbs = set()

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

    def _ensure_tenant_indexes(self, tenant_db):
        """Garante a criação de índices estruturais críticos para o Tenant DB apenas uma vez por instância."""
        if tenant_db.name not in self._indexed_dbs:
            try:
                # Índices para o Livro Diário (Alta performance para Relatórios)
                tenant_db.lancamentos_contabeis.create_index([("data", 1), ("status", 1)])
                tenant_db.lancamentos_contabeis.create_index("partidas.conta_codigo")
                tenant_db.lancamentos_contabeis.create_index("centro_custo")
                tenant_db.lancamentos_contabeis.create_index("conta_debito")
                tenant_db.lancamentos_contabeis.create_index("conta_credito")
                
                # Índices para o Contas a Pagar/Receber (Filtros de Dashboard)
                tenant_db.contas_pagar.create_index([("data_vencimento", 1), ("status", 1)])
                tenant_db.contas_pagar.create_index("participante_id")
                tenant_db.contas_receber.create_index([("data_vencimento", 1), ("status", 1)])
                tenant_db.contas_receber.create_index("participante_id")
                
                # Índices para Importações e Transações
                tenant_db.transacoes.create_index([("data", 1), ("tipo", 1)])
                tenant_db.transacoes.create_index("conta_codigo")
                tenant_db.movimentos_ofx_pendentes.create_index("status")
                
                # Índices para Regras e Conciliação
                tenant_db.contas_correntes.create_index([("cnpj", 1), ("apelido_conta", 1)], unique=True)
                tenant_db.contas_correntes.create_index("id_origem_ofx", unique=True, sparse=True)
                
                self._indexed_dbs.add(tenant_db.name)
                logging.info(f"Índices garantidos para o tenant: {tenant_db.name}")
            except Exception as e:
                logging.warning(f"Erro ao criar índices para {tenant_db.name}: {e}")

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
                
            db = client[db_name]
            self._ensure_tenant_indexes(db)
            return db
        return None

# Instância única global exportável
db_manager = DatabaseManager()
