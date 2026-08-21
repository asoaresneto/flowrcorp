# ETAPA 11 - Contas a Pagar e a Receber

## 1. Controle de Duplicidade
**Status:** ❌ Ausente
- **Evidência:** Na rota `/contas-pagar/salvar` (`app.py`, linha 1686), os dados do formulário são validados superficialmente e imediatamente submetidos ao `insert_one` (linha 1731). Não há checagem prévia no banco se já existe um título para o mesmo `participante_id`, `data_vencimento` e `valor`.
- **Severidade:** ALTO. Aumenta substancialmente o risco de lançamentos duplicados por erro operacional ou duplo-clique.

## 2. Baixa Parcial
**Status:** ❌ Inexistente
- **Evidência:** O método `/contas-pagar/liquidar` (linha 1756) simplesmente atualiza o *status* inteiro para `"Pago"` e realiza a partida dobrada do valor total `bill.get("valor")`. Não existem campos de "valor pago" no request, tampouco geração de títulos residuais.
- **Severidade:** ALTO. Processos reais exigem liquidações parciais rotineiramente.

## 3. Juros, Multas e Descontos
**Status:** ❌ Não Suportado
- **Evidência:** Durante a liquidação (`contas_pagar_liquidar`), não há cálculos ou campos de formulário recebendo os valores de acréscimos moratórios ou decréscimos por antecipação.
- **Severidade:** ALTO. Impede a conciliação correta quando o valor que saiu do banco difere do valor da provisão.

## 4. Vínculo Título -> Lançamento Contábil
**Status:** ✅ Suportado
- **Evidência:** O sistema dispara `registrar_partida_dobrada` passando `documento_id=res.inserted_id` ou `documento_id=bill.get("_id")` (linhas 1744 e 1793). Isso amarra rastreabilidade entre o submódulo financeiro e o diário contábil.
- **Severidade:** BAIXO. Funcionalidade correta.

## 5. Rateio por Centro de Custo
**Status:** ❌ Parcial (Suporta apenas Centro Único)
- **Evidência:** O *request* aceita apenas uma *string* `centro_custo` (linha 1695). Não permite fracionamento matemático do valor (ex: 50% Comercial, 50% TI).
- **Severidade:** MÉDIO.

## 6. Tratamento de Moeda e Arredondamento (Float vs Decimal)
**Status:** ❌ Incorreto e Inseguro
- **Evidência:** Na linha 1707 do `app.py`: `valor = float(valor_str.replace(".", "").replace(",", "."))`. E depois gravado como `float` no banco.
- **Severidade:** CRÍTICO. Sistemas financeiros e contábeis **jamais** devem operar valores em ponto flutuante binário padrão (`float`), devido às distorções da norma IEEE-754. A classe `Decimal` padrão do Python, em conjunto com o `Decimal128` do MongoDB (BSON), é mandatória. Curiosamente, o `MotorContabil` utiliza `Decimal`, porém os submódulos financeiros periféricos usam `float`, criando forte incompatibilidade no ecosistema global.
