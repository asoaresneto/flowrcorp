# ETAPA 7 - Frontend (Telas e UI)

A interface de usuário (UI) do FlowCorp é renderizada pelo backend em **HTML5 + Jinja2** integrado ao Flask, utilizando **Bootstrap 5**, **Bootstrap Icons** e a biblioteca **Chart.js** para renderização de gráficos financeiros do lado do cliente. Não se trata de uma SPA (Single Page Application) baseada em React ou Vue, mas sim uma estrutura clássica SSR (Server-Side Rendering) turbinada com JavaScript nativo para interatividade.

## Principais Arquivos de Templates (Telas Financeiras e Contábeis)
- `dashboard.html`: Dashboard interativo (Gráficos, Evolução de Caixa, KPIs).
- `lancamentos.html`: Livro Diário com tabela analítica de partidas dobradas e modal de importação CSV.
- `dre.html` / `dre_anual.html`: Relatórios de Demonstração do Resultado do Exercício.
- `balanco.html`: Relatório de Balanço Patrimonial (Ativo = Passivo + PL).
- `razonetes.html`: Visão por Conta T (Razão Contábil Analítico).
- `contas_pagar.html` / `contas_receber.html`: Telas de controle e liquidação de títulos financeiros.
- `importar_ofx.html`: Tela de upload de arquivos bancários e leitura de metadados.
- `classificar_movimentos.html`: Tela de quarentena/conciliação de regras RegEx para os movimentos bancários brutos.

Abaixo, os códigos-fonte extraídos das duas telas mais representativas do sistema. *(Os blocos foram levemente resumidos mantendo os laços Jinja2 e a estilização estrutural)*.

---

### Exemplo 1: `templates/dashboard.html` (Painel Gerencial)
*(Esta tela monta os indicadores e renderiza dinamicamente o gráfico Chart.js com base nos dados fornecidos pelo backend)*

```html
{% extends "base.html" %}

{% block content %}
<div class="dashboard-content container-fluid p-0 animate-fade-in">
    <!-- Grid de 6 Cards Gerenciais de KPIs -->
    <div class="row g-3 mb-4">
        <div class="col-xl-2 col-md-4 col-sm-6">
            <div class="card dv-kpi h-100 p-3">
                <div class="d-flex justify-content-between align-items-start mb-2">
                    <span class="text-muted small fw-bold">Receita Período Atual</span>
                    <i class="bi bi-graph-up text-success"></i>
                </div>
                <div class="dv-kpi-value text-success">{{ receitas | real }}</div>
                <div class="text-muted small mt-1">Realizado no mês</div>
            </div>
        </div>
        <!-- (Outros 5 cards: CMV, Resultado Bruto, Despesas, Rec. Líquida, Margem %) omitidos para brevidade -->
    </div>

    <!-- Painel Centralizado do Gráfico e Filtros Integrados -->
    <div class="card dv-card p-4 mb-4">
        <div class="border-bottom border-secondary pb-3 mb-4">
            <form method="GET" action="{{ url_for('dashboard') }}" class="row g-3 align-items-end">
                <div class="col-md-3 col-sm-6">
                    <label>Competência / Período</label>
                    <input type="month" name="periodo" value="{{ periodo }}" class="form-control" onchange="this.form.submit()">
                </div>
                <!-- Filtros adicionais (CNPJ, Centro de Custo) omitidos para brevidade -->
            </form>
        </div>

        <div class="row g-3">
            <div class="col-lg-9 col-md-8">
                <h5 class="card-title fw-bold text-white mb-3">Evolução Financeira (Últimos 6 Meses)</h5>
                <div style="position: relative; height: 350px; width: 100%;">
                    <!-- Renderização do Chart.js -->
                    <canvas id="evolutionChart"></canvas>
                </div>
            </div>
            <!-- Controle Lateral de Checkboxes para habilitar/desabilitar Linhas do Gráfico -->
            <div class="col-lg-3 col-md-4 border-start border-secondary ps-md-4">
                <h5 class="card-title fw-bold text-white mb-2">Contas no Gráfico</h5>
                <div class="sidebar-chart-accounts-list">
                    {% for c in contas_analiticas %}
                    <div class="form-check mb-2">
                        <input type="checkbox" id="chk-{{ c.codigo }}" value="{{ c.codigo }}" class="form-check-input chart-dataset-toggle" onchange="updateChartDatasets()" {% if loop.index <= 3 %}checked{% endif %}>
                        <label for="chk-{{ c.codigo }}" class="form-check-label text-muted small">
                            <span class="fw-bold text-white">{{ c.codigo }}</span> - {{ c.nome }}
                        </label>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Scripts de Chart.js -->
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const rawChartData = {{ chart_data_json | safe }};
const chartLabels = {{ meses_lista | safe }};
// Inicialização do gráfico de evolução e atualização dinâmica baseada nos checkboxes ativos.
</script>
{% endblock %}
```

---

### Exemplo 2: `templates/lancamentos.html` (Contabilidade / Diário)
*(Esta tela lista todas as partidas dobradas registradas, agrupando as pernas de débito e crédito)*

```html
{% extends "base.html" %}

{% block content %}
<div class="dashboard-content container-fluid p-0 animate-fade-in">
    <!-- Cabeçalho -->
    <div class="d-flex align-items-center justify-content-between mb-4">
        <div class="d-flex align-items-center gap-2">
            <i class="bi bi-pencil-square text-primary fs-4"></i>
            <h2 class="h4 text-white mb-0">Contabilidade - Lançamentos Contábeis (Diário)</h2>
        </div>
    </div>

    <!-- Tabela de Lançamentos -->
    <div class="card dv-card p-4">
        <div class="border-bottom border-secondary pb-2 mb-3">
            <h5 class="card-title fw-bold text-white mb-1">Partidas Dobradas do Período</h5>
            <p class="text-muted small mb-0">Listagem de todos os fatos geradores registrados no banco de dados.</p>
        </div>

        <div class="table-responsive">
            <table class="table table-hover align-middle m-0 text-white">
                <thead>
                    <tr class="text-muted small">
                        <th>Data</th>
                        <th>Histórico / Descrição</th>
                        <th>Conta Débito (D)</th>
                        <th>Conta Crédito (C)</th>
                        <th class="text-end">Valor</th>
                        <th>Centro Custo</th>
                    </tr>
                </thead>
                <tbody>
                    <!-- Laço Dinâmico do Jinja2 -->
                    {% for l in lancamentos %}
                    <tr>
                        <td style="font-family: monospace;">{{ l.data | formata_data }}</td>
                        <td class="fw-bold">{{ l.descricao }}</td>
                        <td>
                            <span class="badge bg-success-subtle text-success">{{ l.conta_debito }}</span>
                        </td>
                        <td>
                            <span class="badge bg-warning-subtle text-warning">{{ l.conta_credito }}</span>
                        </td>
                        <td class="text-end fw-bold text-white">
                            {{ l.valor | real }}
                        </td>
                        <td>
                            <span class="badge bg-dark text-muted">{{ l.centro_custo or 'Geral' }}</span>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="6" class="text-center text-muted py-4">Nenhum lançamento contábil registrado no livro diário.</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}
```
