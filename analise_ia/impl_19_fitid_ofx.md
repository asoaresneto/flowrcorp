# Relatório de Implementação: ETAPA 19 - Deduplicação via FITID (OFX)

## 1. O Que Foi Alterado e Por Quê
Na Etapa 12 da auditoria, descobrimos um bug grave no processamento de OFX: o hash (MD5) usado para evitar duplicidade de transações gerava colisões quando dois depósitos idênticos (mesmo valor e dia) ocorriam, deletando um depósito legítimo.

Para resolver, aplicamos a extração da *tag* bancária real (`<FITID>`) do arquivo OFX. 
- O campo `hash` agora usa o `fitid` primordialmente. O MD5 (antigo) ficou como *fallback* unicamente para quando o OFX/CSV vier sem esse campo preenchido (comum em bancos menores ou planilhas legadas).

## 2. Migração e Auditoria de Dados Existentes
O esquema da coleção `transacoes` é dinâmico (no-SQL), então a injeção do campo opcional `fitid` para novos documentos não quebra o sistema. Não recriamos índices únicos de transação para evitar crashes no ambiente de produção legada.

Em contrapartida, foi desenvolvido o script:
`[NEW]` `scripts/verificar_duplicidade_transacoes.py`
Ele percorre todos os *tenants*, agrupa os documentos na coleção `transacoes` usando `aggregate` e reporta os hashes antigos que possuam `count > 1`, mostrando as descrições, valores e datas no terminal para inspeção visual do *Master Admin*, sem apagar nada.

## 3. Como Executar a Auditoria
Em produção, basta executar o script no terminal:
```bash
python scripts/verificar_duplicidade_transacoes.py
```
