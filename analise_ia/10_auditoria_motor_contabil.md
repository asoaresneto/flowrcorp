# ETAPA 10 - Auditoria do Motor Contábil (`services/motor_contabil.py`)

## 1. Partidas Dobradas
**Status:** ✅ Suportado
O sistema assegura a igualdade entre Débitos e Créditos.
- **Evidência:** Método `_validar_balanceamento` (linhas 39-52) realiza o somatório utilizando `Decimal` e aciona `DesbalanceamentoContabilError` se `total_d != total_c`.
- **Severidade:** BAIXO (A validação existe e está correta).

## 2. Imutabilidade e Estornos
**Status:** ✅ Suportado Parcialmente
O motor contábil prevê um método rigoroso de estorno ao invés de exclusão física, porém altera o estado do documento original.
- **Evidência:** O método `estornar_lancamento` (linha 159) cria partidas inversas ('C'->'D' e 'D'->'C') e chama `registrar_lancamento`. No entanto, ele realiza um *update* (`$set: {"status": "Estornado"}`) no lançamento original (linha 172). Em auditoria estrita, o original nunca deveria sofrer um `$set`, apenas ser logicamente anulado pelo contra-lançamento.
- **Severidade:** MÉDIO (O estorno ocorre corretamente em partidas dobradas, mas altera metadados do documento original).

## 3. Competência vs. Caixa
**Status:** ❌ Não Diferenciado
O motor registra apenas um campo genérico de `data`.
- **Evidência:** O documento montado em `registrar_lancamento` (linha 90) possui apenas `"data": data`. Não há distinção entre a data do fato gerador (competência) e a data da movimentação financeira (caixa).
- **Severidade:** CRÍTICO. Sistemas contábeis precisam separar `data_competencia` e `data_emissao` (ou `data_caixa`) para apurar DRE por competência e DFC por caixa simultaneamente, sem ambiguidade.

## 4. Encadeamento e Numeração Sequencial
**Status:** ❌ Inexistente
- **Evidência:** Utiliza-se apenas o `_id` (`ObjectId()` nativo do MongoDB) gerado na linha 79. Não há implementação de uma sequência numérica fiscal/contábil (ex: Lançamento nº 0001, 0002) sem quebras.
- **Severidade:** ALTO. A legislação (SPED/Livro Diário) exige numeração sequencial contínua para rastreabilidade, que não é garantida pelo `ObjectId`.

## 5. Trilha de Auditoria
**Status:** ✅ Suportado
- **Evidência:** Cada registro de lançamento salva `origem`, `usuario_id`, e `criado_em` (linha 96-100).
- **Severidade:** BAIXO. A trilha básica de criação existe.

## 6. Fechamento de Período (Locking)
**Status:** ❌ Ausente
O motor contábil não realiza qualquer verificação se o mês/ano correspondente à `data` do lançamento já passou por fechamento.
- **Evidência:** Não há rotinas em `registrar_lancamento` ou no construtor que validem bloqueios temporais (ex: consultar coleção de `periodos_fechados`).
- **Severidade:** CRÍTICO. Isso permite que um usuário lance uma despesa em 2024 quando a contabilidade de 2024 já foi fechada e os tributos recolhidos, corrompendo a integridade fiscal.

## 7. Validação de Plano de Contas
**Status:** ✅ Suportado
- **Evidência:** O método `_validar_contas_analiticas` (linha 54) verifica se a conta existe e bloqueia se a `classificacao` for "Sintética". Lança `ContaInvalidaError`.
- **Severidade:** BAIXO (Regra corretamente implementada).
