# ETAPA 14 - Modelagem MongoDB para Dados Financeiros

## 1. Indexação das Coleções
**Status:** ❌ Ausência Crítica de Índices
- **Evidência:** Uma busca profunda no código (`app.py`, `database.py`, `seed.py`) revela que comandos de `create_index` só existem para `contas_correntes` (CNPJ/apelido) e `bancos_febraban` (Central). A coleção mais gigantesca do sistema — `lancamentos_contabeis` — não possui índices criados nas chaves usadas nos relatórios, como `data`, `conta_codigo` e `centro_custo`.
- **Severidade:** CRÍTICO. Como o `processar_balancete` executa queries `{status: "Ativo", data: {$gte: ..., $lte: ...}}` sem índices, ocorrerá `COLLSCAN` (Collection Scan). A base de dados será varrida linearmente do disco à memória, travando o banco de dados em poucos meses de uso em produção.

## 2. Tipagem de Valores Monetários
**Status:** ❌ Híbrido (Inconsistente)
- **Evidência:** 
  - O `MotorContabil` salva corretamente no banco utilizando `Decimal128(p.valor)`.
  - Contudo, as coleções gerenciais (`contas_pagar`, `contas_receber`, `movimentos_ofx_pendentes`) extraem os dados de formulários e os gravam como um `float` primitivo (Ex: `app.py`, linha 1707: `valor = float(...)`).
- **Severidade:** CRÍTICO. Modelos financeiros NoSQL exigem padronização absoluta de valores utilizando `Decimal128` ou Inteiros de centavos (Ex: `15000` para R$ 150,00). O uso de `float` vai corromper centavos nas provisões, impedindo a conciliação exata com o livro diário.

## 3. Isolamento de Tenant (Empresa/Filial)
**Status:** ✅ Isolamento Lógico-Físico via Banco
- **Evidência:** Não encontramos campos `empresa_id` ou `tenant_id` em todos os documentos, mas isso se dá porque o projeto utiliza uma modelagem Multi-Tenant baseada em banco de dados isolado (`db_grupo_<slug>`). O `app.py` resolve o `tenant_db = db_manager.get_group_db(session.get("grupo_slug"))` em cada rota.
- **Severidade:** BAIXO. A estratégia de isolamento por banco (ao invés de por coluna) é robusta contra vazamento cruzado.

## 4. Deleção de Dados (Físico vs Lógico)
**Status:** ❌ Inconsistente
- **Evidência:** O motor contábil impede exclusão de lançamentos contábeis utilizando estorno. No entanto, outras coleções gerenciais (como o Plano de Contas) parecem permitir edição destrutiva. Além disso, coleções transitórias como `movimentos_ofx_pendentes` não possuem campo `excluido_em` (soft-delete), e ao que parece o desenvolvedor opta por `delete_one` ou deleções na UI.
- **Severidade:** MÉDIO. Recomenda-se padrão global de "Soft-Delete" (Auditoria), impedindo hard-delete na aplicação inteira.
