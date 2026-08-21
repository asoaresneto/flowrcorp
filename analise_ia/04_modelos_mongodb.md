# ETAPA 4 - Modelos e Coleções MongoDB

O sistema FlowCorp utiliza o **PyMongo** (sem ORM rígido como MongoEngine), manipulando diretamente dicionários Python que representam os documentos JSON/BSON. A arquitetura de banco de dados é dividida em duas instâncias lógicas: um Banco Central (Admin) e Bancos Isolados (Multi-Tenant) por Grupo Econômico.

Não foram identificados índices declarados explicitamente no código-fonte principal ou no `seed.py` (a indexação pode estar sendo tratada diretamente no servidor do MongoDB ou gerenciada por scripts externos de infraestrutura).

Abaixo estão listadas as coleções identificadas e seus respectivos schemas deduzidos a partir das operações de inserção e sementes:

## Banco Central Administrativo (`db_flow_admin`)

### 1. `assinaturas_grupos`
Gerencia os locatários (tenants) da plataforma.
```python
{
    "_id": ObjectId("..."),
    "nome_grupo": "Nome do Grupo",
    "db_name": "db_grupo_slug",
    "status": "Ativo | Suspenso | Cancelado",
    "valor_mensalidade": <VALOR_MENSALIDADE_EXEMPLO>,
    "max_cnpjs": 5,
    "max_usuarios": 10,
    "data_vencimento": "10",
    "data_limite_acesso": "2026-12-31", # Opcional
    "flag_bpo": True
}
```

### 2. `usuarios_master`
Controla o acesso à plataforma e o vínculo com os locatários.
```python
{
    "_id": ObjectId("..."),
    "nome": "<NOME_EXEMPLO>",
    "email": "<EMAIL_EXEMPLO>",
    "senha": "<HASH_BCRYPT_MASCARADO>",
    "grupo_economico_id": ObjectId("..."),
    "permissao_master": True,
    "role": "superadmin | user",
    "reset_token": "<TOKEN_RECUPERACAO_MASCARADO>", # Opcional
    "reset_token_expires": ISODate("...") # Opcional
}
```

### 3. `bancos_febraban`
Tabela oficial de compensação bancária (STR/BACEN).
```python
{
    "codigo_compensacao": "341",
    "nome_banco": "ITAÚ UNIBANCO S.A.",
    "ispb": "60701190"
}
```

---

## Banco Isolado Multi-Tenant (`db_grupo_...`)

### 4. `plano_contas`
Árvore hierárquica contábil.
```python
{
    "_id": ObjectId("..."),
    "codigo": "1.1.1",
    "nome": "Caixa e Equivalentes",
    "tipo": "Ativo", # Ativo, Passivo, Resultado
    "natureza": "Débito | Crédito",
    "classificacao": "Analítica | Sintética",
    "cmv": False # Indica se a conta compõe Custo de Mercadoria Vendida
}
```

### 5. `participantes`
Fornecedores, Clientes e Funcionários.
```python
{
    "_id": ObjectId("..."),
    "nome_razao": "<NOME_RAZAO_EXEMPLO>",
    "tipo": "Cliente | Fornecedor | Empresa Principal",
    "cnpj_cpf": "<CNPJ_CPF_MASCARADO>",
    "email": "<EMAIL_MASCARADO>",
    "telefone": "<TELEFONE_MASCARADO>"
}
```

### 6. `transacoes`
Movimentações financeiras de fluxo de caixa (Regime de Caixa).
```python
{
    "_id": ObjectId("..."),
    "descricao": "Faturamento Exemplo",
    "valor": <VALOR_EXEMPLO>, # Positivo (Receita) ou Negativo (Despesa)
    "tipo": "Receita | Despesa",
    "data": "YYYY-MM-DD",
    "participante_id": "<OBJECT_ID_STRING>",
    "participante_nome": "<NOME_RAZAO_EXEMPLO>",
    "conta_codigo": "3.1.1",
    "conta_nome": "Venda de Mercadorias",
    "centro_custo": "Comercial",
    "origem": "Sistema | Importação OFX"
}
```

### 7. `lancamentos_contabeis`
Motor de partidas dobradas (Regime de Competência e Histórico).
```python
{
    "_id": ObjectId("..."),
    "data": "YYYY-MM-DD",
    "descricao": "Histórico do Lançamento",
    "conta_debito": "4.1.1",
    "conta_credito": "1.1.1",
    "valor": <VALOR_EXEMPLO_ABSOLUTO>,
    "centro_custo": "Operacional",
    "documento_id": "Opcional_Vinculo_Documento",
    "tipo_origem": "Opcional_Origem_Lancamento"
}
```

### 8. `ofx_arquivos_importados`
Logs de auditoria e rastreabilidade de arquivos OFX bancários.
```python
{
    "_id": ObjectId("..."),
    "nome_arquivo": "extrato_arquivo.ofx",
    "data_importacao": "YYYY-MM-DD HH:MM:SS",
    "total_transacoes": 10,
    "saldo_final": <VALOR_SALDO_EXEMPLO>,
    "data_saldo": "YYYY-MM-DD",
    "data_inicio": "YYYY-MM-DD",
    "data_fim": "YYYY-MM-DD",
    "soma_transacoes": <SOMA_VALORES_EXEMPLO>
}
```

### 9. `alertas_integridade`
Registros de auditoria autônomos (lacunas cronológicas e furos de saldo).
```python
{
    "_id": ObjectId("..."),
    "tipo": "Lacuna Cronológica | Discrepância de Saldo",
    "mensagem": "Texto descritivo apontando a inconsistência detectada."
}
```

### 10. `contas_pagar` e `contas_receber`
Módulos de gestão de títulos futuros (ainda não baixados).
*(Inferido a partir das referências de coleções; schema similar a transações, possuindo controle extra de datas de vencimento e status).*

### 11. `usuarios`
Usuários locais vinculados a este banco isolado, para fins de auditoria interna das ações sem precisar consultar a base master.
```python
{
    "_id": ObjectId("..."),
    "nome": "<NOME_EXEMPLO>",
    "email": "<EMAIL_EXEMPLO>"
}
```
