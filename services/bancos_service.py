import os
import csv
import io
import logging
import requests
from pymongo import UpdateOne

class BancosService:
    @staticmethod
    def processar_arquivo_csv(file_stream_ou_bytes, admin_db):
        try:
            # Lidar com bytes ou string (Stream)
            if hasattr(file_stream_ou_bytes, 'read'):
                content = file_stream_ou_bytes.read()
            else:
                content = file_stream_ou_bytes
                
            if isinstance(content, bytes):
                try:
                    text_content = content.decode('utf-8-sig')
                except UnicodeDecodeError:
                    try:
                        text_content = content.decode('utf-8')
                    except UnicodeDecodeError:
                        text_content = content.decode('latin-1')
            else:
                text_content = content
                
            f = io.StringIO(text_content)
            
            # Detectar delimitador (tenta , depois ;)
            header = f.readline()
            f.seek(0)
            delimiter = ';' if ';' in header and ',' not in header else ','
            
            reader = csv.DictReader(f, delimiter=delimiter)
            rows = list(reader)
        except Exception as e:
            logging.error(f"Erro ao ler CSV de bancos: {e}")
            return {
                "sucesso": False,
                "mensagem": f"Erro ao ler arquivo: {e}"
            }
            
        bancos_to_upsert = []
        for row in rows:
            # Pegando chaves ignorando espaços extras
            row_clean = {k.strip() if k else '': v.strip() if isinstance(v, str) else v for k, v in row.items()}
            
            ispb_raw = row_clean.get("ISPB", "")
            nome_reduzido = row_clean.get("Nome_Reduzido", "")
            codigo_raw = row_clean.get("Número_Código", "")
            nome_extenso = row_clean.get("Nome_Extenso", "")
            participa_raw = row_clean.get("Participa_da_Compe", "").upper()
            
            # Sanitização
            ispb = ispb_raw.strip().zfill(8) if ispb_raw else ""
            if not ispb:
                continue
                
            codigo = None
            if codigo_raw and codigo_raw.lower() not in ['n/a', 'na', '']:
                try:
                    # Pode vir como float (ex: 1.0) dependendo do parsing do csv/pandas
                    codigo = str(int(float(codigo_raw))).zfill(3)
                except ValueError:
                    codigo = None
                    
            participa_compe = participa_raw in ["SIM", "S"]
            nome_formatado = f"{codigo} - {nome_reduzido}" if codigo else nome_reduzido
            
            banco_doc = {
                "ispb": ispb,
                "nome_reduzido": nome_reduzido,
                "nome_extenso": nome_extenso,
                "codigo": codigo,
                "participa_compe": participa_compe,
                "nome_formatado": nome_formatado
            }
            
            bancos_to_upsert.append(
                UpdateOne(
                    {"ispb": ispb},
                    {"$set": banco_doc},
                    upsert=True
                )
            )
            
        if bancos_to_upsert:
            try:
                # Criar índices
                admin_db.bancos_febraban.create_index("ispb", unique=True)
                # Índice esparso para código
                admin_db.bancos_febraban.create_index("codigo", unique=True, sparse=True)
                
                result = admin_db.bancos_febraban.bulk_write(bancos_to_upsert)
                logging.info(f"Importados {len(bancos_to_upsert)} bancos para o MongoDB central.")
                
                return {
                    "sucesso": True,
                    "total_processados": len(bancos_to_upsert),
                    "inseridos": result.upserted_count,
                    "atualizados": result.modified_count,
                    "mensagem": "Tabela de bancos atualizada com sucesso."
                }
            except Exception as e:
                logging.error(f"Erro ao salvar bancos no MongoDB: {e}")
                return {
                    "sucesso": False,
                    "mensagem": f"Erro no banco de dados: {e}"
                }
        return {
            "sucesso": False,
            "mensagem": "Nenhum dado válido encontrado no arquivo."
        }

    @staticmethod
    def carregar_ou_atualizar_bancos(admin_db, csv_path="ParticipantesSTR.csv"):
        if not os.path.exists(csv_path):
            logging.warning(f"Arquivo CSV de bancos nao encontrado: {csv_path}")
            return False
            
        try:
            with open(csv_path, 'rb') as f:
                content = f.read()
            res = BancosService.processar_arquivo_csv(content, admin_db)
            return res.get("sucesso", False)
        except Exception as e:
            logging.error(f"Erro ao carregar CSV local: {e}")
            return False

    @staticmethod
    def sincronizar_automatico(admin_db):
        # 1. Tentar carregar do CSV local
        if os.path.exists("ParticipantesSTR.csv"):
            with open("ParticipantesSTR.csv", 'rb') as f:
                content = f.read()
            res = BancosService.processar_arquivo_csv(content, admin_db)
            if res.get("sucesso"):
                return res
                
        # 2. Fallback para BrasilAPI
        logging.info("Sincronizando bancos via BrasilAPI...")
        try:
            response = requests.get('https://brasilapi.com.br/api/banks/v1', timeout=10)
            if response.status_code == 200:
                brasilapi_data = response.json()
                bancos_to_upsert = []
                
                for b in brasilapi_data:
                    codigo = str(b.get("code")).zfill(3) if b.get("code") else None
                    ispb = str(b.get("ispb")).zfill(8) if b.get("ispb") is not None else None
                    if not ispb: continue
                    
                    nome = b.get("name") or b.get("fullName") or ""
                    nome_extenso = b.get("fullName") or b.get("name") or ""
                    nome_formatado = f"{codigo} - {nome}" if codigo else nome
                    
                    banco_doc = {
                        "ispb": ispb,
                        "nome_reduzido": nome,
                        "nome_extenso": nome_extenso,
                        "codigo": codigo,
                        "participa_compe": bool(codigo), # Assumindo true se tem codigo
                        "nome_formatado": nome_formatado
                    }
                    
                    bancos_to_upsert.append(
                        UpdateOne(
                            {"ispb": ispb},
                            {"$set": banco_doc},
                            upsert=True
                        )
                    )
                
                if bancos_to_upsert:
                    admin_db.bancos_febraban.create_index("ispb", unique=True)
                    admin_db.bancos_febraban.create_index("codigo", unique=True, sparse=True)
                    result = admin_db.bancos_febraban.bulk_write(bancos_to_upsert)
                    
                    return {
                        "sucesso": True,
                        "total_processados": len(bancos_to_upsert),
                        "inseridos": result.upserted_count,
                        "atualizados": result.modified_count,
                        "mensagem": "Tabela de bancos sincronizada com a BrasilAPI."
                    }
        except Exception as e:
            logging.error(f"Erro ao buscar bancos da BrasilAPI: {e}")
            return {"sucesso": False, "mensagem": f"Erro na BrasilAPI: {e}"}
            
        return {"sucesso": False, "mensagem": "Falha na sincronização."}

    @staticmethod
    def get_bancos(admin_db=None):
        bancos = []
        
        # 1. Tentar ler do banco de dados local
        if admin_db is not None:
            try:
                bancos_cursor = list(admin_db.bancos_febraban.find({}))
                if not bancos_cursor:
                    # Tentar importar do CSV
                    if BancosService.carregar_ou_atualizar_bancos(admin_db):
                        bancos_cursor = list(admin_db.bancos_febraban.find({}))
                
                if bancos_cursor:
                    # Converter formato para a API unificada
                    for b in bancos_cursor:
                        if not b.get("codigo"):
                            continue # Ignorar bancos sem compe na listagem unificada
                        bancos.append({
                            "code": b.get("codigo"),
                            "name": b.get("nome_reduzido") or b.get("nome_extenso"),
                            "fullName": b.get("nome_extenso") or b.get("nome_reduzido"),
                            "ispb": b.get("ispb")
                        })
                    
                    # Ordenar por codigo (ignorar quem nao tem, ou tratar vazio)
                    bancos.sort(key=lambda x: (x["code"] is None, x["code"]))
                    return bancos
            except Exception as e:
                logging.error(f"Erro ao buscar bancos do MongoDB: {e}")
        
        # 2. Fallback para BrasilAPI se a coleção estava vazia E o CSV não existia
        logging.info("Utilizando fallback BrasilAPI para lista de bancos.")
        try:
            response = requests.get('https://brasilapi.com.br/api/banks/v1', timeout=5)
            if response.status_code == 200:
                brasilapi_data = response.json()
                for b in brasilapi_data:
                    # BrasilAPI retorna code (int ou null), name, fullName, ispb
                    if not b.get("code"): continue
                    codigo = str(b.get("code")).zfill(3)
                    ispb = str(b.get("ispb")).zfill(8) if b.get("ispb") is not None else None
                    bancos.append({
                        "code": codigo,
                        "name": b.get("name") or b.get("fullName"),
                        "fullName": b.get("fullName") or b.get("name"),
                        "ispb": ispb
                    })
                bancos.sort(key=lambda x: (x["code"] is None, x["code"]))
                return bancos
        except Exception as e:
            logging.error(f"Erro ao buscar bancos da BrasilAPI: {e}")
            
        return bancos
