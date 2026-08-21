import sys
import os
import logging
from pymongo import MongoClient, ASCENDING, DESCENDING

# Adiciona o diretório raiz ao path para importar config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def criar_indices_para_tenant(db):
    logging.info(f"--- Iniciando criação de índices no tenant: {db.name} ---")
    
    # Função auxiliar para listar índices existentes
    def get_existing_indexes(collection_name):
        try:
            return list(db[collection_name].index_information().keys())
        except Exception:
            return []

    colecoes_e_indices = {
        "lancamentos_contabeis": [
            ([("data", ASCENDING), ("status", ASCENDING)], "idx_data_status"),
            ([("conta_debito", ASCENDING)], "idx_conta_debito"),
            ([("conta_credito", ASCENDING)], "idx_conta_credito"),
            ([("centro_custo", ASCENDING)], "idx_centro_custo"),
        ],
        "contas_pagar": [
            ([("data_vencimento", ASCENDING), ("status", ASCENDING)], "idx_data_venc_status"),
            ([("participante_id", ASCENDING)], "idx_participante_id"),
        ],
        "contas_receber": [
            ([("data_vencimento", ASCENDING), ("status", ASCENDING)], "idx_data_venc_status"),
            ([("participante_id", ASCENDING)], "idx_participante_id"),
        ],
        "transacoes": [
            ([("data", ASCENDING), ("tipo", ASCENDING)], "idx_data_tipo"),
            ([("conta_codigo", ASCENDING)], "idx_conta_codigo"),
        ],
        "movimentos_ofx_pendentes": [
            ([("status", ASCENDING)], "idx_status"),
        ]
    }
    
    for collection_name, indexes in colecoes_e_indices.items():
        existentes = get_existing_indexes(collection_name)
        
        for keys, name in indexes:
            if name in existentes or f"{name}_1" in existentes:
                logging.info(f"[{collection_name}] Índice já existe: {name}")
            else:
                logging.info(f"[{collection_name}] Criando índice: {name} para chaves {keys}")
                try:
                    # background=True evita lock na tabela em produção
                    db[collection_name].create_index(keys, name=name, background=True)
                    logging.info(f"[{collection_name}] Índice {name} criado com sucesso.")
                except Exception as e:
                    logging.error(f"[{collection_name}] Falha ao criar índice {name}: {e}")

def run():
    client = MongoClient(Config.MONGO_URI)
    admin_db = client[Config.ADMIN_DB_NAME]
    
    logging.info("Buscando grupos/tenants ativos...")
    grupos = admin_db.assinaturas_grupos.find()
    count = 0
    
    for grupo in grupos:
        db_name = grupo.get("db_name")
        if db_name:
            if not db_name.startswith("db_grupo_"):
                db_name = f"db_grupo_{db_name.lower().replace(' ', '_')}"
                
            tenant_db = client[db_name]
            criar_indices_para_tenant(tenant_db)
            count += 1
            
    logging.info(f"Processamento concluído. {count} tenants indexados.")

if __name__ == "__main__":
    run()
