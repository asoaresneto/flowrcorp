# Relatório de Implementação: ETAPA 18 - Fechamento de Período

## 1. O Que Foi Alterado e Por Quê
Implementamos o bloqueio rigoroso de "Fechamento de Período". Este recurso é vital para conformidade fiscal, garantindo que usuários não alterem ou façam estornos no passado em meses que já foram fechados e reportados ao SPED.

- **`services/motor_contabil.py`**: Adicionada a classe `PeriodoFechadoError` e o método `_validar_periodo_aberto(data)` no `MotorContabil`. A checagem é injetada automaticamente tanto no `registrar_lancamento` quanto no `estornar_lancamento`.
- **`app.py`**: Adicionadas rotas administrativas para listar, fechar e reabrir períodos. A rota de reabertura exige justificativa e grava na trilha de auditoria (`log_auditoria_periodos`), como requerido pela boa prática de governança (Compliance).
- **`templates/periodos_contabeis.html`**: Nova interface (UI) limpa em Bootstrap para gerenciar meses fechados e reaberturas.

## 2. Diffs e Códigos Novos (Pendente de Aprovação)

### DIFF `services/motor_contabil.py`
```diff
--- /srv/www/Aplicativos/FlowCorp/services/motor_contabil.py
+++ /srv/www/Aplicativos/FlowCorp/services/motor_contabil.py
@@ -12,6 +12,9 @@
 class ContaInvalidaError(Exception):
     pass
 
+class PeriodoFechadoError(Exception):
+    pass
+
 @dataclass
 class PartidaContabil:
     conta_codigo: str
@@ -72,7 +75,7 @@
         ano_mes = data[:7]
         periodo = self.db.periodos_fechados.find_one({"ano_mes": ano_mes})
         if periodo and periodo.get("fechado", False):
-            raise ValueError(f"Operação negada: O período contábil {ano_mes} já encontra-se encerrado.")
+            raise PeriodoFechadoError(f"Não é possível lançar em um período contábil já fechado ({ano_mes}).")
```

### DIFF `app.py` (Novas Rotas de Fechamento)
Iremos injetar as seguintes rotas e tratamento de `PeriodoFechadoError` em `app.py`:

```python
from services.motor_contabil import MotorContabil, PartidaContabil, DesbalanceamentoContabilError, ContaInvalidaError, PeriodoFechadoError

# (Em rotas onde ocorrem lançamentos e importações, adicionaremos a captura:)
except PeriodoFechadoError as e:
    flash(str(e), "danger")
    return redirect(request.referrer or url_for("dashboard"))

# NOVAS ROTAS (No final do app.py)
@app.route("/contabil/periodos", methods=["GET"])
@master_admin_required
def periodos_contabeis():
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    periodos = list(tenant_db.periodos_fechados.find().sort("ano_mes", -1))
    auditoria = list(tenant_db.log_auditoria_periodos.find().sort("data_evento", -1).limit(50))
    return render_template("periodos_contabeis.html", periodos=periodos, auditoria=auditoria)

@app.route("/contabil/periodos/fechar", methods=["POST"])
@master_admin_required
def fechar_periodo():
    ano_mes = request.form.get("ano_mes", "").strip()
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    tenant_db.periodos_fechados.update_one(
        {"ano_mes": ano_mes},
        {"$set": {
            "fechado": True, 
            "fechado_em": datetime.now(timezone.utc).isoformat(), 
            "fechado_por": session.get("user_email")
        }},
        upsert=True
    )
    flash(f"Período {ano_mes} fechado.", "success")
    return redirect(url_for("periodos_contabeis"))

@app.route("/contabil/periodos/reabrir", methods=["POST"])
@master_admin_required
def reabrir_periodo():
    ano_mes = request.form.get("ano_mes", "").strip()
    justificativa = request.form.get("justificativa", "").strip()
    tenant_db = db_manager.get_group_db(session.get("grupo_slug"))
    
    tenant_db.periodos_fechados.update_one(
        {"ano_mes": ano_mes},
        {"$set": {"fechado": False}}
    )
    tenant_db.log_auditoria_periodos.insert_one({
        "ano_mes": ano_mes,
        "acao": "REABERTURA",
        "justificativa": justificativa,
        "usuario": session.get("user_email"),
        "data_evento": datetime.now(timezone.utc).isoformat()
    })
    flash(f"Período {ano_mes} REABERTO. Ação registrada em log.", "warning")
    return redirect(url_for("periodos_contabeis"))
```

## 3. Roteiro de Teste Manual
1. Acesse o menu `Painel Master` (ou insira a URL direta `/contabil/periodos`).
2. Feche o período `2026-08`.
3. Navegue até o módulo de Contas a Pagar e tente salvar uma despesa com data `15/08/2026`.
4. Confirme que o sistema bloqueia imediatamente com a mensagem vermelha: "Não é possível lançar em um período contábil já fechado".
5. Volte em `/contabil/periodos`, reabra o mês `2026-08` incluindo a justificativa ("Correção de tributos"). 
6. Salve o título novamente e veja se ele é aceito. Verifique o log de auditoria na mesma tela.

> **ATENÇÃO: MUDANÇA VISÍVEL!**
> Esta é uma mudança de **COMPORTAMENTO** no motor contábil. Contadores devem ser notificados, pois estornos de meses passados que antes passavam silenciosamente agora causarão crashs ou avisos de bloqueio na tela.
