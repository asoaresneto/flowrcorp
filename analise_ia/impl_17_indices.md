# Relatório de Implementação: ETAPA 17 - Índices no MongoDB

## 1. O Que Foi Alterado e Por Quê
Identificamos na auditoria que as principais coleções do sistema não possuíam índices, o que faria o MongoDB realizar *Collection Scans* (varrer a base toda) em cada relatório gerado, esgotando a memória do servidor a longo prazo.

Para mitigar isso de forma segura:
- Foi criado um script idempotente `scripts/criar_indices.py` que varre todos os grupos econômicos (tenants) e aplica os índices estruturais em *background* sem "travar" a aplicação em produção.
- Foi desenhado um ajuste na classe `DatabaseManager` (`database.py`) para injetar os índices faltantes automaticamente toda vez que um *tenant* for criado/carregado, protegendo clientes futuros.
- Nenhum índice `unique` foi forçado nesta etapa para evitar bloqueio decorrente de sujeira/legado na base.

## 2. Arquivos Tocados
- `[NEW]` `scripts/criar_indices.py`
- `[MODIFY]` `database.py` (Pendente de confirmação)

## 3. Como Executar e Testar Manualmente
Em produção, basta executar o script no terminal dentro do diretório do projeto:

```bash
cd /srv/www/Aplicativos/FlowCorp
python scripts/criar_indices.py
```

**Tempo Estimado:** A execução será instantânea (poucos segundos) se as coleções não possuírem milhões de registros. Como usamos `background=True`, a performance do banco não será degradada durante a criação.

Para confirmar se funcionou via *Mongo Shell*:
```javascript
use db_grupo_nome_do_cliente
db.lancamentos_contabeis.getIndexes()
```

## 4. Plano de Rollback
Como índices comuns (não-únicos) não alteram ou deletam dados, não há risco de corrupção. Caso um índice cause lentidão inesperada (raro), ele pode ser removido pelo painel do MongoDB Atlas ou executando:
```javascript
db.lancamentos_contabeis.dropIndex("idx_data_status")
```
