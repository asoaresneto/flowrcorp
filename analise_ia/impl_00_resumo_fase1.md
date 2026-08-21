# Resumo de Deploy - Fase 1 (Etapas 17 a 19)

As três etapas da Fase 1 foram implementadas com máxima segurança (cirúrgica), blindando as bases para correções futuras de maior risco.

## 1. Arquivos Modificados / Novos
- `[MODIFY]` `database.py` (Injeção de índices no Onboarding de Tenants)
- `[MODIFY]` `services/motor_contabil.py` (Exceção `PeriodoFechadoError` e bloqueio lógico)
- `[MODIFY]` `app.py` (Novas rotas de Fechamento e Captura Global de Erros)
- `[MODIFY]` `importador_universal.py` (Implementação do FITID do OFX no lugar de MD5, já feita)
- `[NEW]` `templates/periodos_contabeis.html` (Interface do Painel Master para fechamento)
- `[NEW]` `scripts/criar_indices.py` (Script idempotente de migração MongoDB)
- `[NEW]` `scripts/verificar_duplicidade_transacoes.py` (Script de auditoria de duplicidade)

## 2. Ordem Recomendada de Deploy (Go-Live)
Recomendamos os seguintes passos para levar à produção (Master Admin):
1. Fazer o `git pull` (ou `push`/deploy do repositório) no ambiente produtivo.
2. Reiniciar o Gunicorn/Supervisor: `sudo systemctl restart flowcorp.service`
3. **[Mudança Invisível]** Rodar o script de índices em *background*:
   `python scripts/criar_indices.py`
4. **[Mudança Invisível]** Rodar o script de auditoria de duplicidades:
   `python scripts/verificar_duplicidade_transacoes.py` e extrair o log se necessário.
5. **[Mudança Visível]** Avisar a equipe contábil sobre o bloqueio de "Fechamento de Período", que agora os impedirá de mexer no passado inadvertidamente.

## 3. Checklist de Testes Manuais Pós-Deploy
- [ ] Criar um novo Grupo Econômico pela interface principal e verificar (pelo Mongo Shell) se os índices nasceram instanciados junto ao banco.
- [ ] Fechar a competência atual pelo painel (`/contabil/periodos`).
- [ ] Tentar lançar uma conta a pagar/receber no período fechado e garantir a recepção da mensagem vermelha de erro (ao invés de crash - 500).
- [ ] Reabrir a competência com justificativa "Teste de Validação", salvar um lançamento e conferir o histórico no *Log de Auditoria*.
- [ ] Fazer upload de um extrato `.ofx` e conferir se não ocorrem perdas de transações (se for homologação).
