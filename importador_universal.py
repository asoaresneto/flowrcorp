import pandas as pd
import io
import re
import hashlib
from datetime import datetime
from typing import List, Dict, Any
from bson.objectid import ObjectId
import logging

class ImportadorUniversal:
    """
    Processa arquivos financeiros (.ofx, .csv, .xlsx), normaliza os dados
    e verifica duplicidades no banco de dados.
    """

    @staticmethod
    def gerar_hash(data_str: str, valor: float, descricao: str) -> str:
        """
        Gera um hash MD5 baseado em data, valor (absoluto) e descrição normalizada
        para identificar transações únicas e prevenir duplicidade.
        """
        desc_norm = re.sub(r'[^a-zA-Z0-9]', '', str(descricao).lower())
        valor_norm = f"{abs(float(valor)):.2f}"
        s = f"{data_str}|{valor_norm}|{desc_norm}"
        return hashlib.md5(s.encode('utf-8')).hexdigest()

    @classmethod
    def parse_file(cls, file_content_bytes: bytes, filename: str) -> tuple:
        """Lê o arquivo de acordo com a extensão e retorna (transacoes, metadados)."""
        ext = filename.lower().split('.')[-1]
        
        if ext == 'ofx':
            return cls._parse_ofx(file_content_bytes)
        elif ext in ['csv', 'xlsx', 'xls']:
            return cls._parse_tabular(file_content_bytes, ext), {}
        else:
            raise ValueError(f"Extensão de arquivo não suportada: .{ext}")

    @classmethod
    def _parse_ofx(cls, file_content_bytes: bytes) -> tuple:
        content_str = file_content_bytes.decode("utf-8", errors="ignore")
        
        metadados = {}
        acctid_match = re.search(r"<ACCTID>([^<\r\n\t]+)", content_str)
        if acctid_match:
            metadados['acctid'] = acctid_match.group(1).strip()
            
        bankid_match = re.search(r"<BANKID>([^<\r\n\t]+)", content_str)
        if bankid_match:
            metadados['bankid'] = bankid_match.group(1).strip()
            
        if 'acctid' in metadados and 'bankid' in metadados:
            metadados['id_origem_ofx'] = f"{metadados['bankid']}+{metadados['acctid']}"
        elif 'acctid' in metadados:
            metadados['id_origem_ofx'] = metadados['acctid']
            
        blocks = content_str.split("<STMTTRN>")
        transacoes = []
        
        for block in blocks[1:]:
            dtposted_match = re.search(r"<DTPOSTED>([^<\r\n\t]+)", block)
            trnamt_match = re.search(r"<TRNAMT>([^<\r\n\t]+)", block)
            memo_match = re.search(r"<MEMO>([^<\r\n\t]+)", block)
            
            if trnamt_match and memo_match:
                dtposted_raw = dtposted_match.group(1).strip() if dtposted_match else ""
                trnamt = float(trnamt_match.group(1).strip())
                memo = memo_match.group(1).strip()
                
                date_str = datetime.today().strftime("%Y-%m-%d")
                if len(dtposted_raw) >= 8:
                    try:
                        year = dtposted_raw[0:4]
                        month = dtposted_raw[4:6]
                        day = dtposted_raw[6:8]
                        date_str = f"{year}-{month}-{day}"
                    except Exception:
                        pass
                
                transacoes.append({
                    "data": date_str,
                    "descricao": memo,
                    "valor": trnamt,
                    "hash": cls.gerar_hash(date_str, trnamt, memo)
                })
        return transacoes, metadados

    @classmethod
    def _parse_tabular(cls, file_content_bytes: bytes, ext: str) -> List[Dict[str, Any]]:
        if ext == 'csv':
            try:
                # Tenta detectar separador
                df = pd.read_csv(io.BytesIO(file_content_bytes), sep=None, engine='python')
            except Exception:
                try:
                    df = pd.read_csv(io.BytesIO(file_content_bytes), sep=';', encoding='latin-1')
                except Exception as e:
                    raise ValueError(f"Erro ao ler arquivo CSV: {e}")
        else:
            try:
                df = pd.read_excel(io.BytesIO(file_content_bytes))
            except Exception as e:
                raise ValueError(f"Erro ao ler arquivo Excel: {e}")
                
        # Normalizar nomes de colunas
        df.columns = [str(c).strip().lower() for c in df.columns]
        
        col_map = {
            'data': ['data', 'date', 'dt', 'data_lancamento', 'data_competencia'],
            'descricao': ['descricao', 'desc', 'historico', 'memo', 'description'],
            'valor': ['valor', 'value', 'amount', 'vl', 'montante']
        }
        
        mapped_cols = {}
        for canonical, options in col_map.items():
            found = False
            for opt in options:
                if opt in df.columns:
                    mapped_cols[canonical] = opt
                    found = True
                    break
            if not found:
                raise ValueError(f"Coluna obrigatória não encontrada para '{canonical}'. Opções esperadas: {', '.join(options)}")
        
        transacoes = []
        for idx, row in df.iterrows():
            data_raw = row[mapped_cols['data']]
            desc_raw = row[mapped_cols['descricao']]
            valor_raw = row[mapped_cols['valor']]
            
            if pd.isna(data_raw) or pd.isna(valor_raw):
                continue
                
            # Sanitização de Data
            if isinstance(data_raw, datetime):
                data_str = data_raw.strftime("%Y-%m-%d")
            else:
                try:
                    dt = pd.to_datetime(str(data_raw), dayfirst=True)
                    data_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    continue
                    
            # Sanitização de Valor Monetário
            if isinstance(valor_raw, str):
                s = str(valor_raw).replace('R$', '').strip()
                negativo = False
                if s.startswith('-'):
                    negativo = True
                    s = s[1:].strip()
                elif s.startswith('(') and s.endswith(')'):
                    negativo = True
                    s = s[1:-1].strip()
                    
                if ',' in s and '.' in s:
                    if s.rfind(',') > s.rfind('.'):
                        s = s.replace('.', '').replace(',', '.') # Padrão BR
                    else:
                        s = s.replace(',', '') # Padrão US
                elif ',' in s:
                    s = s.replace(',', '.') # Provavelmente BR
                    
                try:
                    valor = float(s)
                    if negativo:
                        valor = -valor
                except Exception:
                    continue
            else:
                try:
                    valor = float(valor_raw)
                except Exception:
                    continue
                    
            desc = str(desc_raw).strip() if pd.notna(desc_raw) else "Lançamento Importado"
            
            transacoes.append({
                "data": data_str,
                "descricao": desc,
                "valor": valor,
                "hash": cls.gerar_hash(data_str, valor, desc)
            })
            
        return transacoes

    @classmethod
    def verificar_duplicidades(cls, tenant_db, transacoes: List[Dict[str, Any]], conta_corrente_id: str) -> List[Dict[str, Any]]:
        """
        Consulta o banco de dados dentro do período das transações e marca
        'status_duplicado' = True para aquelas cujo hash já existir.
        """
        if not transacoes:
            return transacoes
            
        try:
            cc_doc = tenant_db.contas_correntes.find_one({"_id": ObjectId(conta_corrente_id)})
            conta_codigo = cc_doc.get("conta_contabil_codigo") if cc_doc else None
        except Exception:
            conta_codigo = None
            
        # Mesmo que não encontre a conta contábil, faremos a verificação global por data
        datas = [t["data"] for t in transacoes]
        data_inicio = min(datas)
        data_fim = max(datas)
        
        query: Dict[str, Any] = {
            "data": {"$gte": data_inicio, "$lte": data_fim}
        }
        
        if conta_codigo:
            query["$or"] = [
                {"conta_debito": conta_codigo},
                {"conta_credito": conta_codigo},
                {"conta_codigo": conta_codigo}
            ]
            
        existentes_hashes = set()
        db_transacoes = tenant_db.transacoes.find(query)
        for db_t in db_transacoes:
            desc = db_t.get("historico", db_t.get("descricao", ""))
            v = float(db_t.get("valor", 0))
            d = db_t.get("data", "")
            h = cls.gerar_hash(d, v, desc)
            existentes_hashes.add(h)
            
        for t in transacoes:
            t["status_duplicado"] = t["hash"] in existentes_hashes
            
        return transacoes
