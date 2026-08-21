from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from decimal import Decimal, ROUND_HALF_UP
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from datetime import datetime, timezone
import logging

class DesbalanceamentoContabilError(Exception):
    pass

class ContaInvalidaError(Exception):
    pass

@dataclass
class PartidaContabil:
    conta_codigo: str
    tipo: str # 'D' ou 'C'
    valor: Decimal
    
    def __post_init__(self):
        if self.tipo not in ('D', 'C'):
            raise ValueError(f"Tipo de partida inválido: {self.tipo}. Deve ser 'D' ou 'C'.")
        # Força o valor para Decimal e garante 2 casas decimais
        if not isinstance(self.valor, Decimal):
            self.valor = Decimal(str(self.valor))
        self.valor = self.valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if self.valor < 0:
            raise ValueError(f"Valor da partida não pode ser negativo. Use 'D' ou 'C'. Valor recebido: {self.valor}")

class MotorContabil:
    """
    Núcleo do sistema de Lançamentos Contábeis do FlowCorp.
    Garante o Método das Partidas Dobradas, previne imprecisão de float e assegura integridade.
    """
    def __init__(self, tenant_db):
        self.db = tenant_db

    def _validar_balanceamento(self, partidas: List[PartidaContabil]):
        total_d = Decimal("0.00")
        total_c = Decimal("0.00")
        
        for p in partidas:
            if p.tipo == 'D':
                total_d += p.valor
            else:
                total_c += p.valor
                
        if total_d != total_c:
            raise DesbalanceamentoContabilError(
                f"Lançamento desbalanceado: Débitos ({total_d}) != Créditos ({total_c}). Diferença: {abs(total_d - total_c)}"
            )

    def _validar_contas_analiticas(self, partidas: List[PartidaContabil]):
        codigos = [p.conta_codigo for p in partidas]
        if not codigos:
            raise ValueError("O lançamento deve conter pelo menos uma partida.")
            
        contas = list(self.db.plano_contas.find({"codigo": {"$in": codigos}}))
        contas_dict = {c['codigo']: c for c in contas}
        
        for p in partidas:
            conta = contas_dict.get(p.conta_codigo)
            if not conta:
                raise ContaInvalidaError(f"Conta contábil não encontrada no plano de contas: {p.conta_codigo}")
            if conta.get("classificacao") != "Analítica":
                raise ContaInvalidaError(f"Não é permitido lançar em conta sintética: {p.conta_codigo} - {conta.get('nome')}")
            
    def registrar_lancamento(self, data: str, historico: str, partidas: List[PartidaContabil], 
                             documento_id: Optional[str] = None, origem: str = "Manual", 
                             centro_custo: str = "Geral", usuario_id: Optional[str] = None) -> str:
        """
        Registra um lançamento contábil no MongoDB após rigorosa validação.
        Suporta partidas 1:1, 1:N, N:1 e N:N.
        """
        self._validar_balanceamento(partidas)
        self._validar_contas_analiticas(partidas)
        
        lancamento_id = ObjectId()
        
        # Converte para o formato de persistência
        partidas_dict = []
        for p in partidas:
            partidas_dict.append({
                "conta_codigo": p.conta_codigo,
                "tipo": p.tipo,
                "valor": Decimal128(p.valor)
            })
            
        doc = {
            "_id": lancamento_id,
            "data": data,
            "historico": historico,
            "partidas": partidas_dict,
            "documento_id": str(documento_id) if documento_id else None,
            "origem": origem,
            "centro_custo": centro_custo,
            "usuario_id": str(usuario_id) if usuario_id else None,
            "status": "Ativo",
            "criado_em": datetime.now(timezone.utc).isoformat()
        }
        
        self.db.lancamentos_contabeis.insert_one(doc)
        return str(lancamento_id)

    def registrar_lancamento_lote(self, lote_dados: List[Dict[str, Any]], validar_contas: bool = False) -> List[str]:
        """
        Registra múltiplos lançamentos contábeis de uma só vez (bulk insert).
        Recebe uma lista de dicionários com as chaves: data, historico, partidas, documento_id, origem, centro_custo, usuario_id.
        Por padrão, assume que a validação de contas já foi feita em massa na camada superior (validar_contas=False).
        """
        if not lote_dados:
            return []
            
        documentos_mongo = []
        ids = []
        
        # Validar as contas de todo o lote de uma vez (opcional)
        if validar_contas:
            todas_partidas = []
            for item in lote_dados:
                todas_partidas.extend(item.get("partidas", []))
            self._validar_contas_analiticas(todas_partidas)
            
        for item in lote_dados:
            partidas = item.get("partidas", [])
            self._validar_balanceamento(partidas)
            
            lancamento_id = ObjectId()
            ids.append(str(lancamento_id))
            
            partidas_dict = []
            for p in partidas:
                partidas_dict.append({
                    "conta_codigo": p.conta_codigo,
                    "tipo": p.tipo,
                    "valor": Decimal128(p.valor)
                })
                
            doc = {
                "_id": lancamento_id,
                "data": item.get("data"),
                "historico": item.get("historico"),
                "partidas": partidas_dict,
                "documento_id": str(item.get("documento_id")) if item.get("documento_id") else None,
                "origem": item.get("origem", "Manual"),
                "centro_custo": item.get("centro_custo", "Geral"),
                "usuario_id": str(item.get("usuario_id")) if item.get("usuario_id") else None,
                "status": "Ativo",
                "criado_em": datetime.now(timezone.utc).isoformat()
            }
            documentos_mongo.append(doc)
            
        if documentos_mongo:
            self.db.lancamentos_contabeis.insert_many(documentos_mongo)
            
        return ids

    def estornar_lancamento(self, lancamento_id: str, justificativa: str, usuario_id: Optional[str] = None) -> str:
        """
        Realiza o estorno de um lançamento revertendo suas partidas de forma rastreável.
        Nenhum registro é apagado (hard delete evitado).
        """
        lancamento_original = self.db.lancamentos_contabeis.find_one({"_id": ObjectId(lancamento_id)})
        if not lancamento_original:
            raise ValueError(f"Lançamento {lancamento_id} não encontrado.")
            
        if lancamento_original.get("status") == "Estornado":
            raise ValueError("Este lançamento já foi estornado.")
            
        # Marca o original como estornado
        self.db.lancamentos_contabeis.update_one(
            {"_id": ObjectId(lancamento_id)},
            {"$set": {"status": "Estornado"}}
        )
        
        # Cria as partidas inversas
        partidas_inversas = []
        for p in lancamento_original.get("partidas", []):
            tipo_inverso = 'C' if p["tipo"] == 'D' else 'D'
            # Converte o Decimal128 do mongo devolta pra Decimal nativo
            valor_decimal = p["valor"].to_decimal() if isinstance(p["valor"], Decimal128) else Decimal(str(p["valor"]))
            partidas_inversas.append(PartidaContabil(
                conta_codigo=p["conta_codigo"],
                tipo=tipo_inverso,
                valor=valor_decimal
            ))
            
        return self.registrar_lancamento(
            data=datetime.today().strftime("%Y-%m-%d"),
            historico=f"Estorno do lançamento {lancamento_id}: {justificativa}",
            partidas=partidas_inversas,
            documento_id=lancamento_id,
            origem="Estorno",
            centro_custo=lancamento_original.get("centro_custo", "Geral"),
            usuario_id=usuario_id
        )
