# ETAPA 5 - Módulos Financeiros e Contábeis

Os módulos principais que encapsulam as regras de negócio do FlowCorp operam com base na rigorosa validação contábil (partidas dobradas) e na consolidação bottom-up (rollup) de relatórios.

## 1. Motor Contábil (`services/motor_contabil.py`)
Responsável por garantir a imutabilidade, o balanceamento de partidas e o uso correto do plano de contas analítico. Opera utilizando tipos `Decimal` nativos para prevenir erros de precisão flutuante.

```python
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from decimal import Decimal, ROUND_HALF_UP
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId

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
            raise ValueError(f"Tipo de partida inválido: {self.tipo}.")
        if not isinstance(self.valor, Decimal):
            self.valor = Decimal(str(self.valor))
        self.valor = self.valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class MotorContabil:
    def __init__(self, tenant_db):
        self.db = tenant_db

    def _validar_balanceamento(self, partidas: List[PartidaContabil]):
        total_d = Decimal("0.00")
        total_c = Decimal("0.00")
        for p in partidas:
            if p.tipo == 'D': total_d += p.valor
            else: total_c += p.valor
        if total_d != total_c:
            raise DesbalanceamentoContabilError("Lançamento desbalanceado.")

    def _validar_contas_analiticas(self, partidas: List[PartidaContabil]):
        # Valida se as contas existem e se são Analíticas (nível folha)
        pass # Lógica interna omitida

    def registrar_lancamento(self, data, historico, partidas, documento_id=None, origem="Manual", centro_custo="Geral"):
        self._validar_balanceamento(partidas)
        self._validar_contas_analiticas(partidas)
        # Salva utilizando Decimal128 do MongoDB para segurança numérica
        pass # Lógica de insert omitida

    def estornar_lancamento(self, lancamento_id, justificativa):
        # NUNCA deleta (hard delete). Cria um lançamento contábil de inversão 'C' -> 'D' e 'D' -> 'C'
        pass # Lógica interna omitida
```

## 2. Relatórios Contábeis (`services/relatorios_contabeis.py`)
Constrói a Demonstração do Resultado do Exercício (DRE) e o Balanço Patrimonial iterando sobre os lançamentos.

```python
from decimal import Decimal
from bson.decimal128 import Decimal128

class RelatoriosContabeis:
    def __init__(self, tenant_db):
        self.db = tenant_db

    def processar_balancete(self, data_inicio=None, data_fim=None):
        # Agrega saldos das contas analíticas com base nas partidas lançadas (bottom-up)
        # Soma para as contas Sintéticas respeitando a hierarquia do Plano de Contas
        pass # Lógica de rollup omitida

    def gerar_dre(self, periodo_inicio, periodo_fim):
        # Filtra apenas contas de Resultado (Grupo 3 e 4)
        # Calcula Lucro/Prejuízo: Receitas - Despesas
        pass 

    def gerar_balanco_patrimonial(self, data_fim):
        # Filtra contas Patrimoniais (Grupo 1 e 2)
        # Consolida automaticamente o Lucro/Prejuízo da DRE no Patrimônio Líquido (PL)
        # Retorna 'balanco_fecha' comparando Ativo == (Passivo + PL)
        pass
```

## 3. Gestão de Recebíveis e Pagáveis (`app.py` -> Controladores Financeiros)
O sistema possui módulos de Contas a Pagar e Contas a Receber acoplados diretamente às rotas `/contas-pagar/liquidar` e `/contas-receber/liquidar`. A lógica assegura que, ao liquidar um título (status passa de "Pendente" para "Pago"), o sistema consome automaticamente o `MotorContabil` para disparar a partida dobrada do fato (Ex: Debita Caixa, Credita Contas a Receber).

## 4. Integração Bancária e Universal (`importador_universal.py` e `services/bancos_service.py`)
Responsáveis por fazer parsing de arquivos OFX, CSV e Excel, identificar a instituição financeira com base na tabela da Febraban (`bancos_service`) e encaminhar as transações brutas para o processo de quarentena/conciliação (RegEx e match manual) antes de ingressarem no Livro Razão.
