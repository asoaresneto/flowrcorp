import sys
import os
import logging
from pymongo import MongoClient

# Adiciona o diretório raiz ao path para importar config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def verificar_duplicidades_tenant(db):
    logging.info(f"--- Varrendo tenant: {db.name} ---")
    
    pipeline = [
        # Agrupa pelo hash original (que gerava colisão)
        {
            "$group": {
                "_id": "$hash",
                "count": {"$sum": 1},
                "transacoes_ids": {"$push": "$_id"},
                "descricoes": {"$push": "$descricao"},
                "valores": {"$push": "$valor"},
                "datas": {"$push": "$data"}
            }
        },
        # Filtra apenas os que tem duplicidade (mais de 1)
        {
            "$match": {
                "count": {"$gt": 1}
            }
        }
    ]
    
    duplicidades = list(db.transacoes.aggregate(pipeline))
    if not duplicidades:
        logging.info(f"[{db.name}] Nenhuma duplicidade suspeita por hash encontrada.")
        return 0

    logging.warning(f"[{db.name}] ATENÇÃO: {len(duplicidades)} hashes duplicados encontrados!")
    
    for dup in duplicidades:
        logging.warning(f"  -> Hash: {dup['_id']} (Ocorrências: {dup['count']})")
        logging.warning(f"     Datas: {dup['datas']}")
        logging.warning(f"     Valores: {dup['valores']}")
        logging.warning(f"     Descrições: {dup['descricoes']}")
        logging.warning("-" * 40)
        
    return len(duplicidades)

def run():
    client = MongoClient(Config.MONGO_URI)
    admin_db = client[Config.ADMIN_DB_NAME]
    
    logging.info("Buscando grupos/tenants ativos...")
    grupos = admin_db.assinaturas_grupos.find()
    
    total_duplicidades = 0
    for grupo in grupos:
        db_name = grupo.get("db_name")
        if db_name:
            if not db_name.startswith("db_grupo_"):
                db_name = f"db_grupo_{db_name.lower().replace(' ', '_')}"
                
            tenant_db = client[db_name]
            total_duplicidades += verificar_duplicidades_tenant(tenant_db)
            
    logging.info(f"Processamento concluído. Encontrados {total_duplicidades} hashes duplicados em todos os tenants.")
    logging.info("Nenhum dado foi apagado. Revise os logs para decidir se são falsos positivos (depósitos duplos do mesmo dia) ou duplicações reais.")

if __name__ == "__main__":
    run()
