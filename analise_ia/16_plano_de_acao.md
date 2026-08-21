# ETAPA 16 - Plano de Ação Priorizado e Refatoração

Esta tabela consolida as dívidas técnicas e falhas arquiteturais identificadas na auditoria contábil-financeira (Etapas 10 a 15), ordenadas por prioridade.

| # | Módulo | Problema | Severidade | Risco de Negócio | Esforço Estimado | Sugestão de Correção |
|---|:---|:---|:---:|:---|:---:|:---|
| 1 | **Motor Contábil** | Ausência de Bloqueio de Fechamento Contábil | **CRÍTICO** | Lançamentos em anos passados alteram DREs já enviadas ao fisco (multas e infrações graves). | Baixo | Criar coleção `periodos_fechados` e checar em `registrar_lancamento`. |
| 2 | **Contas a Pagar/Receber** | Tratamento Monetário em `float` (`app.py`) | **CRÍTICO** | Erros de arredondamento em centavos causarão desbalanceamento progressivo com o Livro Diário. | Alto | Trocar `float(valor)` por `Decimal(valor)` e gravar no banco como `Decimal128` nos controladores de UI. |
| 3 | **Modelagem (MongoDB)** | Ausência de Índices em `lancamentos_contabeis` | **CRÍTICO** | Consultas diárias causarão `COLLSCAN`, sobrecarregando a CPU/RAM e paralisando o banco (Out of Memory). | Baixo | Executar `create_index` para `data`, `conta_codigo` e `centro_custo`. |
| 4 | **Conciliação OFX** | Parser ignora a tag bancária unívoca `<FITID>` | **CRÍTICO** | Falsos-positivos na deduplicação de transações idênticas no mesmo dia causarão erro no saldo final. | Médio | Extrair `<FITID>` no parser e adotá-lo como Chave Primária Única na coleção `transacoes`. |
| 5 | **Relatórios Contábeis** | Agregação do Balanço feita em memória local | **CRÍTICO** | Lentidão extrema e OOM Killer ao tentar rodar loop for-in em dezenas de milhares de lançamentos. | Alto | Reescrever métodos para usar o Pipeline de Aggregation (`$group`) no motor C++ do Mongo. |
| 6 | **Motor Contábil** | Não diferencia Caixa de Competência | ALTO | Impossibilidade de extrair a DFC (Demonstrativo de Fluxo de Caixa) real e a DRE correta simultaneamente. | Alto | Adicionar colunas `data_competencia` e `data_caixa` na classe `PartidaContabil`. |
| 7 | **Segurança** | Falta de RBAC (Role-Based Access Control) | ALTO | Operadores podem excluir/estornar lançamentos que deveriam ser restritos ao Contador. | Médio | Adicionar middlewares como `@permissao_requerida("FINANCEIRO_AVANCADO")`. |
| 8 | **Contas a Pagar/Receber** | Não suporta Baixa Parcial | ALTO | Frustra a operação quando o cliente paga menos (ou com juros), deixando resíduo. | Alto | Refatorar modal de liquidação aceitando inputs de juros/multa/desconto/valor recebido. |
| 9 | **Motor Contábil** | `ObjectId` ao invés de Sequencial Fiscal | ALTO | Fere normas da RFB que exigem livro diário numerado de forma contínua, sem buracos. | Médio | Implementar um *sequence generator* atômico no MongoDB para cada `tenant`. |
| 10 | **Segurança** | Hard-delete permitido em dicionários | MÉDIO | Perda de trilha de auditoria sobre quem alterou o Plano de Contas. | Baixo | Implementar parâmetro global genérico de `deletado_em` e `deletado_por`. |

---

## Propostas de Correção em Código para Itens CRÍTICOS

### A. Correção do Item 3: Adição de Índices Críticos (`database.py`)
No método que inicializa o banco do Tenant, o código deve ser alterado para criar índices de cobertura:
```python
# Em db_manager.py ou app.py (onde as coleções do tenant são instanciadas):

# Índices para o Livro Diário (Alta performance para Relatórios)
tenant_db.lancamentos_contabeis.create_index([("data", 1), ("status", 1)])
tenant_db.lancamentos_contabeis.create_index("partidas.conta_codigo")
tenant_db.lancamentos_contabeis.create_index("centro_custo")

# Índices para o Contas a Pagar/Receber (Filtros de Dashboard)
tenant_db.contas_pagar.create_index([("data_vencimento", 1), ("status", 1)])
tenant_db.contas_receber.create_index([("data_vencimento", 1), ("status", 1)])
```

### B. Correção do Item 4: Deduplicação OFX Baseada em FITID (`importador_universal.py`)
O OFX já fornece um ID único global. O hash inventado no sistema deve ser abolido ou ser *fallback*:
```python
# Refatoração do _parse_ofx
@classmethod
def _parse_ofx(cls, file_content_bytes: bytes) -> tuple:
    # ... código existente ...
    for block in blocks[1:]:
        dtposted_match = re.search(r"<DTPOSTED>([^<\r\n\t]+)", block)
        trnamt_match = re.search(r"<TRNAMT>([^<\r\n\t]+)", block)
        memo_match = re.search(r"<MEMO>([^<\r\n\t]+)", block)
        fitid_match = re.search(r"<FITID>([^<\r\n\t]+)", block) # [NEW]
        
        if trnamt_match and memo_match:
            # ... processamento de data, valor e memo ...
            
            fitid = fitid_match.group(1).strip() if fitid_match else None
            hash_gerado = cls.gerar_hash(date_str, trnamt, memo)
            
            # FITID é a única garantia real de transação única
            identificador_unico = fitid if fitid else hash_gerado
            
            transacoes.append({
                "data": date_str,
                "descricao": memo,
                "valor": trnamt,
                "fitid": fitid,
                "hash": identificador_unico # Banco deve checar isso, não apenas o hash da string
            })
    return transacoes, metadados
```

### C. Correção do Item 2: Fim do Uso de Float em Controladores (`app.py`)
Todo input monetário recebido do HTML deve ser tratado como Decimal nativo.
```python
from decimal import Decimal
from bson.decimal128 import Decimal128

@app.route("/contas-pagar/salvar", methods=["POST"])
@login_required
def contas_pagar_salvar():
    # ...
    valor_str = request.form.get("valor", "0").strip()
    
    # Padronização correta:
    valor_limpo = valor_str.replace(".", "").replace(",", ".")
    valor_decimal = Decimal(valor_limpo).quantize(Decimal("0.01"))
    
    novo_lancamento = {
        # ...
        "valor": Decimal128(valor_decimal), # Salva protegido no MongoDB
        # ...
    }
    # Na hora de chamar o Motor Contábil (que já usa Decimal) não haverá crash
```
