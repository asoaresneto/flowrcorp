from decimal import Decimal
from bson.decimal128 import Decimal128
from typing import List, Dict, Any, Optional

class RelatoriosContabeis:
    """
    Agregador contábil para geração do Balancete, DRE e Balanço Patrimonial.
    Utiliza recursão bottom-up (rollup) respeitando a equação fundamental do patrimônio e o regime de competência.
    """
    def __init__(self, tenant_db):
        self.db = tenant_db

    def _get_account_level(self, codigo: str) -> int:
        return len(codigo.split('.'))

    def _get_natureza(self, codigo: str) -> str:
        # 1 Ativo e 4 Custos/Despesas -> Devedora
        # 2 Passivo/PL e 3 Receitas -> Credora
        if codigo.startswith('1') or codigo.startswith('4'):
            return 'D'
        return 'C'

    def processar_balancete(self, data_inicio: Optional[str] = None, data_fim: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Apura o saldo de todas as contas analíticas no período especificado e faz o Bottom-up Rollup para contas sintéticas.
        """
        # 1. Busca plano de contas
        contas = list(self.db.plano_contas.find().sort("codigo", 1))
        
        # 2. Busca lançamentos ativos (não estornados)
        query = {"status": "Ativo"}
        if data_inicio or data_fim:
            date_filter = {}
            if data_inicio:
                date_filter["$gte"] = data_inicio
            if data_fim:
                date_filter["$lte"] = data_fim
            query["data"] = date_filter
            
        lancamentos = list(self.db.lancamentos_contabeis.find(query))
        
        # 3. Calcula saldos das analíticas
        saldos_analiticas = {}
        for l in lancamentos:
            for p in l.get("partidas", []):
                codigo = p["conta_codigo"]
                tipo = p["tipo"]
                valor = p["valor"].to_decimal() if isinstance(p["valor"], Decimal128) else Decimal(str(p["valor"]))
                
                if codigo not in saldos_analiticas:
                    saldos_analiticas[codigo] = Decimal("0.00")
                
                nat = self._get_natureza(codigo)
                # Se a natureza da conta for igual ao tipo do lançamento, aumenta o saldo, senão diminui
                if nat == tipo:
                    saldos_analiticas[codigo] += valor
                else:
                    saldos_analiticas[codigo] -= valor

        # 4. Rollup (Soma dos filhos para os pais)
        for c in contas:
            codigo = c["codigo"]
            c["nivel"] = self._get_account_level(codigo)
            c["natureza"] = self._get_natureza(codigo)
            
            saldo = Decimal("0.00")
            for a_codigo, a_saldo in saldos_analiticas.items():
                if a_codigo == codigo or a_codigo.startswith(codigo + "."):
                    saldo += a_saldo
            
            c["saldo_decimal"] = saldo
            c["saldo"] = float(saldo) # Para compatibilidade com templates legados que usam float format
            
        return contas

    def gerar_dre(self, periodo_inicio: str, periodo_fim: str) -> Dict[str, Any]:
        """
        Gera a Demonstração do Resultado do Exercício (DRE) do período filtrando apenas contas de resultado (3 e 4).
        Retorna também o Lucro/Prejuízo Apurado.
        """
        contas = self.processar_balancete(periodo_inicio, periodo_fim)
        
        contas_dre = []
        receitas_total = Decimal("0.00")
        despesas_total = Decimal("0.00")
        
        for c in contas:
            if c["codigo"].startswith("3") or c["codigo"].startswith("4"):
                if abs(c["saldo_decimal"]) > Decimal("0.001") or c["nivel"] <= 2:
                    contas_dre.append(c)
                if c["classificacao"] == "Analítica":
                    if c["codigo"].startswith("3"):
                        receitas_total += c["saldo_decimal"]
                    else:
                        despesas_total += c["saldo_decimal"]
                        
        lucro_prejuizo = receitas_total - despesas_total
        
        return {
            "contas": contas_dre,
            "lucro_prejuizo": float(lucro_prejuizo),
            "lucro_prejuizo_decimal": lucro_prejuizo
        }

    def gerar_balanco_patrimonial(self, data_fim: str) -> Dict[str, Any]:
        """
        Gera o Balanço Patrimonial (Contas 1 e 2) acumulado até a data informada.
        Integra automaticamente o Lucro/Prejuízo da DRE no Patrimônio Líquido (PL) para garantir o balanço fechado.
        """
        # Balanço consolida todo o histórico até a data final
        contas = self.processar_balancete(None, data_fim)
        
        contas_ativo = []
        contas_passivo = []
        
        total_ativo = Decimal("0.00")
        total_passivo_pl = Decimal("0.00")
        
        # Apuração do Resultado do Exercício (DRE) para integrar no PL
        dre_total = self.gerar_dre("1900-01-01", data_fim)
        lucro_prejuizo = dre_total["lucro_prejuizo_decimal"]
        
        for c in contas:
            if c["codigo"].startswith("1"):
                if abs(c["saldo_decimal"]) > Decimal("0.001") or c["nivel"] <= 2:
                    contas_ativo.append(c)
                if c["codigo"] == "1":
                    total_ativo = c["saldo_decimal"]
                    
            elif c["codigo"].startswith("2"):
                # Ajusta os grupos sintéticos do PL (Ex: 2.3 ou 2 dependendo do plano de contas) para incluir o ARE
                if c["codigo"] == "2" or c["codigo"].startswith("2.3"):
                    c["saldo_decimal"] += lucro_prejuizo
                    c["saldo"] = float(c["saldo_decimal"])
                    
                if abs(c["saldo_decimal"]) > Decimal("0.001") or c["nivel"] <= 2:
                    contas_passivo.append(c)
                    
                if c["codigo"] == "2":
                    total_passivo_pl = c["saldo_decimal"]

        balanco_fecha = (total_ativo == total_passivo_pl)
        
        return {
            "ativo": contas_ativo,
            "passivo": contas_passivo,
            "total_ativo": float(total_ativo),
            "total_passivo_pl": float(total_passivo_pl),
            "balanco_fecha": balanco_fecha,
            "lucro_prejuizo_integrado": float(lucro_prejuizo)
        }
