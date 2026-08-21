import csv
import io
import re
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from bson.objectid import ObjectId
from decimal import Decimal
from services.motor_contabil import MotorContabil, PartidaContabil, DesbalanceamentoContabilError

class ImportadorLancamentos:
    def __init__(self, tenant_db):
        self.tenant_db = tenant_db

    def _detectar_encoding(self, conteudo_bytes: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                conteudo_bytes.decode(enc)
                return enc
            except (UnicodeDecodeError, ValueError):
                continue
        return "utf-8"

    def _detectar_delimitador(self, primeira_linha: str) -> str:
        try:
            dialeto = csv.Sniffer().sniff(primeira_linha, delimiters=";,\t")
            return dialeto.delimiter
        except csv.Error:
            return ";" if primeira_linha.count(";") >= primeira_linha.count(",") else ","

    def _parse_valor(self, valor_str: str) -> Optional[float]:
        if not valor_str: return None
        s = valor_str.strip()
        negativo = False
        if s.startswith('-') or s.startswith('('):
            negativo = True
            s = s.strip('-()').strip()
        
        # BR Format "-1932,7" or "1.250,50"
        s = s.replace('R$', '').strip().replace(' ', '')
        
        if ',' in s and '.' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')
            else:
                s = s.replace(',', '')
        elif ',' in s:
            s = s.replace(',', '.')
            
        try:
            val = float(s)
            return -val if negativo else val
        except:
            return None

    def _parse_data(self, data_str: str) -> Optional[str]:
        if not data_str: return None
        data_str = data_str.strip()
        try:
            # Assumes DD/MM/YYYY or similar
            if '/' in data_str:
                parts = data_str.split('/')
                if len(parts) == 3:
                    if len(parts[2]) == 2: # YY
                        parts[2] = "20" + parts[2]
                    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            elif '-' in data_str:
                parts = data_str.split('-')
                if len(parts[0]) == 4:
                    return data_str
                if len(parts) == 3:
                    return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        except:
            pass
        return None

    def _parse_categoria(self, categoria_str: str) -> Tuple[str, str]:
        # "4.2.01.013 - Despesas com Pro-Labore" -> ("4.2.01.013", "Despesas com Pro-Labore")
        if not categoria_str:
            return "", ""
        if " - " in categoria_str:
            parts = categoria_str.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return categoria_str.strip(), categoria_str.strip()

    def processar_lote(self, conteudo_bytes: bytes, gerar_partidas_dobradas: bool) -> Dict[str, Any]:
        erros = []
        importadas = 0
        lidas = 0
        
        try:
            encoding = self._detectar_encoding(conteudo_bytes)
            conteudo_str = conteudo_bytes.decode(encoding)
            
            linhas = conteudo_str.splitlines()
            if not linhas:
                return {"sucesso": False, "mensagem": "Arquivo vazio."}
                
            delimitador = self._detectar_delimitador(linhas[0])
            leitor = csv.DictReader(io.StringIO(conteudo_str), delimiter=delimitador)
            
            # Identify columns flexibly
            if not leitor.fieldnames:
                return {"sucesso": False, "mensagem": "Cabeçalhos não encontrados."}
                
            headers_norm = [unicodedata.normalize("NFKD", h).encode('ascii', 'ignore').decode('utf-8').lower().strip() for h in leitor.fieldnames]
            col_map = {}
            for idx, h in enumerate(headers_norm):
                if 'data' in h: col_map['data'] = leitor.fieldnames[idx]
                elif 'descri' in h or 'historico' in h: col_map['descricao'] = leitor.fieldnames[idx]
                elif 'valor' in h or 'montante' in h: col_map['valor'] = leitor.fieldnames[idx]
                elif 'categoria' in h or 'conta' in h and 'bancaria' not in h: col_map['categoria'] = leitor.fieldnames[idx]
                elif 'bancaria' in h or 'origem' in h: col_map['conta_bancaria'] = leitor.fieldnames[idx]
            
            # Lote collections
            lote_transacoes = []
            lote_lancamentos = []
            lote_pendentes = []
            lote_motor_dados = []
            
            # Contadores
            pendentes_cadastro = 0
            
            # Caching para performance (Bulk Import)
            planos = list(self.tenant_db.plano_contas.find({"classificacao": "Analítica"}))
            mapa_plano = {p["codigo"]: p for p in planos if "codigo" in p}
            
            ccs = list(self.tenant_db.contas_correntes.find())
            mapa_cc = {c["apelido_conta"]: c for c in ccs if "apelido_conta" in c}
            
            for linha in leitor:
                lidas += 1
                try:
                    data_raw = linha.get(col_map.get('data', 'Data'), "")
                    desc_raw = linha.get(col_map.get('descricao', 'Descrição'), "")
                    valor_raw = linha.get(col_map.get('valor', 'Valor'), "")
                    cat_raw = linha.get(col_map.get('categoria', 'Categoria'), "")
                    banco_raw = linha.get(col_map.get('conta_bancaria', 'Conta Bancária'), "")
                    
                    data_iso = self._parse_data(data_raw)
                    if not data_iso:
                        erros.append(f"Linha {lidas}: Data inválida ({data_raw})")
                        continue
                        
                    valor_float = self._parse_valor(valor_raw)
                    if valor_float is None:
                        erros.append(f"Linha {lidas}: Valor inválido ({valor_raw})")
                        continue
                        
                    codigo_conta, nome_conta = self._parse_categoria(cat_raw)
                    descricao = desc_raw.strip() if desc_raw else nome_conta
                    
                    doc_id = ObjectId()
                    
                    # Recuperar o apelido da conta da última coluna
                    apelido_col_name = leitor.fieldnames[-1] if leitor.fieldnames else None
                    apelido_conta = linha.get(apelido_col_name, "").strip() if apelido_col_name else ""
                    
                    # Validação 1: Plano de Contas (Memória)
                    conta_resultado_valida = False
                    if codigo_conta:
                        pc_db = mapa_plano.get(codigo_conta)
                        if pc_db:
                            conta_resultado_valida = True
                            
                    # Validação 2: Contas Correntes (Memória)
                    conta_corrente_valida = False
                    conta_disponibilidades_atual = ""
                    if apelido_conta:
                        cc_db = mapa_cc.get(apelido_conta)
                        if cc_db and "conta_contabil_codigo" in cc_db and cc_db["conta_contabil_codigo"]:
                            conta_disponibilidades_atual = cc_db["conta_contabil_codigo"]
                            conta_corrente_valida = True
                            
                    motivos = []
                    if not conta_resultado_valida:
                        motivos.append("conta_resultado_inexistente")
                    if not conta_corrente_valida:
                        motivos.append("conta_corrente_inexistente")
                        
                    if motivos:
                        # Mandar para quarentena
                        pendentes_cadastro += 1
                        lote_pendentes.append({
                            "_id": doc_id,
                            "arquivo_origem": "Importação Histórico CSV",
                            "data": data_iso,
                            "descricao": descricao,
                            "valor": valor_float,
                            "categoria_informada": cat_raw,
                            "conta_bancaria_informada": apelido_conta or banco_raw,
                            "motivo_pendencia": " e ".join(motivos),
                            "sugerido_codigo_resultado": codigo_conta,
                            "sugerido_nome_resultado": nome_conta,
                            "sugerido_apelido_banco": apelido_conta,
                            "status": "Pendente de Cadastro",
                            "gerar_partidas_dobradas": gerar_partidas_dobradas,
                            "importado_em": datetime.now(timezone.utc).isoformat()
                        })
                        
                        if len(lote_pendentes) >= 1000:
                            self.tenant_db.lancamentos_pendentes_cadastro.insert_many(lote_pendentes)
                            lote_pendentes.clear()
                            
                        continue # Pula a inserção normal
                            
                    transacao = {
                        "_id": doc_id,
                        "data": data_iso,
                        "descricao": descricao,
                        "valor": valor_float,
                        "tipo": "Receita" if valor_float >= 0 else "Despesa",
                        "conta_codigo": codigo_conta,
                        "conta_nome": nome_conta,
                        "origem": "Importação Histórico CSV",
                        "conta_bancaria": banco_raw.strip() if banco_raw else "",
                        "centro_custo": "Geral",
                        "importado_em": datetime.now(timezone.utc).isoformat()
                    }
                    lote_transacoes.append(transacao)
                    
                    if gerar_partidas_dobradas:
                        abs_valor = Decimal(str(abs(valor_float)))
                        if valor_float >= 0:
                            conta_deb = conta_disponibilidades_atual
                            conta_cred = codigo_conta
                        else:
                            conta_deb = codigo_conta
                            conta_cred = conta_disponibilidades_atual
                            
                        lote_lancamentos.append({
                            "data": data_iso,
                            "conta_debito": conta_deb,
                            "conta_credito": conta_cred,
                            "valor": abs(valor_float),
                            "historico": descricao,
                            "documento_id": str(doc_id),
                            "tipo_origem": "importacao_csv"
                        })
                        
                        # Usa o novo Motor Contábil (Lote)
                        partidas = [
                            PartidaContabil(conta_deb, 'D', abs_valor),
                            PartidaContabil(conta_cred, 'C', abs_valor)
                        ]
                        lote_motor_dados.append({
                            "data": data_iso,
                            "historico": descricao,
                            "partidas": partidas,
                            "documento_id": str(doc_id),
                            "origem": "importacao_csv"
                        })
                    
                    # Chunk insert every 1000 for legacy transacoes/lancamentos
                    if len(lote_transacoes) >= 1000:
                        self.tenant_db.transacoes.insert_many(lote_transacoes)
                        if gerar_partidas_dobradas:
                            if lote_lancamentos:
                                self.tenant_db.lancamentos.insert_many(lote_lancamentos)
                            if lote_motor_dados:
                                try:
                                    motor = MotorContabil(self.tenant_db)
                                    motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
                                except Exception as m_err:
                                    erros.append(f"Erro Motor Contábil (Lote): {m_err}")
                            
                        importadas += len(lote_transacoes)
                        lote_transacoes.clear()
                        lote_lancamentos.clear()
                        lote_motor_dados.clear()
                        
                except Exception as e:
                    erros.append(f"Linha {lidas}: {str(e)}")
            
            # Insert remaining
            if lote_transacoes:
                self.tenant_db.transacoes.insert_many(lote_transacoes)
                if gerar_partidas_dobradas:
                    if lote_lancamentos:
                        self.tenant_db.lancamentos.insert_many(lote_lancamentos)
                    if lote_motor_dados:
                        try:
                            motor = MotorContabil(self.tenant_db)
                            motor.registrar_lancamento_lote(lote_motor_dados, validar_contas=False)
                        except Exception as m_err:
                            erros.append(f"Erro Motor Contábil (Lote Final): {m_err}")
                importadas += len(lote_transacoes)
                
            if lote_pendentes:
                self.tenant_db.lancamentos_pendentes_cadastro.insert_many(lote_pendentes)
                
            return {
                "sucesso": True,
                "total_lidas": lidas,
                "total_importadas": importadas,
                "total_pendentes_cadastro": pendentes_cadastro,
                "total_erros": len(erros),
                "erros": erros[:50],
                "mensagem": f"{importadas} lançamentos importados com sucesso. {pendentes_cadastro} pendentes de cadastro."
            }
            
        except Exception as e:
            logging.error(f"Erro no processamento em lote: {e}")
            return {
                "sucesso": False,
                "mensagem": f"Erro crítico: {str(e)}"
            }
