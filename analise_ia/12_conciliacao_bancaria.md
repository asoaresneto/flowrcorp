# ETAPA 12 - Conciliação Bancária e Importação OFX

## 1. Deduplicação de Extratos e Transações
**Status:** ❌ Inseguro e Não-Normatizado
- **Evidência:** No arquivo `importador_universal.py` (linha 17), o método `gerar_hash` cria um MD5 usando `(data|valor|descrição)` para evitar duplicidades. No entanto, o `_parse_ofx` (linha 40) **ignora completamente a tag `<FITID>`** do padrão OFX. A tag `FITID` é a única garantia universal dos bancos de que uma transação bancária é única.
- **Severidade:** CRÍTICO. Clientes que realizam duas transferências de mesmo valor no mesmo dia (ex: dois pagamentos de R$ 50,00 para "PADARIA") terão a segunda transação descartada incorretamente como "duplicidade" devido ao colapso do Hash, causando furos na conciliação.

## 2. Classificação Automática (Regras RegEx)
**Status:** ✅ Parcialmente Implementado
- **Evidência:** A rota `/classificar-movimentos` (`app.py`, linha 2795) consulta a coleção `regras_regex` e aplica o match sobre as pendências. 
- **Severidade:** ALTO. A dependência de Regex inserida pelos usuários pode gerar graves "falsos positivos". Se um usuário cria a regra `.*PIX.*` apontada para uma Despesa, qualquer recebimento que contenha PIX também será erroneamente classificado, pois não foi encontrada validação cruzada do sinal do valor (Crédito vs Débito) atrelada à regra.

## 3. Conciliação de Saldo (Extrato vs Razão)
**Status:** ❌ Inexistente
- **Evidência:** O parser de OFX não captura a tag `<BALAMT>` (Saldo Final no Banco) nem a tag `<DTASOF>` (Data do Saldo). Portanto, o sistema é incapaz de alertar o usuário se o Saldo Final acumulado nas Partidas Dobradas contábeis (conta `1.1.X Bancos`) bate com o saldo real reportado pelo banco naquele dia.
- **Severidade:** CRÍTICO. Sistemas contábeis e ERPs baseiam sua saúde financeira na "prova dos nove" da conta bancária. Sem conciliar o saldo absoluto, o módulo de importação é cego e apenas empilha lançamentos.

## 4. Tratamento de Movimentos não Classificados (Quarentena)
**Status:** ✅ Correto
- **Evidência:** Movimentos que falham em todas as regras RegEx (ou cujas contas não existam) são enfileirados na coleção `movimentos_ofx_pendentes` e apresentados na tela `classificar_movimentos.html` para preenchimento manual (Escolha de Parceiro + Conta Débito/Crédito). Eles não são descartados silenciosamente nem jogados diretamente no livro razão.
- **Severidade:** BAIXO. Funciona adequadamente como "Buffer/Quarentena".
