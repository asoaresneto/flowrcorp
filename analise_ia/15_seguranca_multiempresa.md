# ETAPA 15 - Controle de Acesso e Segurança (Multiempresa / Multiusuário)

## 1. Controle de Permissões por Perfil
**Status:** ❌ Granularidade Insuficiente
- **Evidência:** O sistema implementa dois níveis básicos globais: `@login_required` (Acesso Comum) e `@master_admin_required` (Superadmin/Dev). O código valida `session.get("role") == "superadmin"` ou a permissão master. Não encontramos a implementação de uma malha granular de perfis (RBAC - Role-Based Access Control) dentro de um cliente (ex: Operador, Analista, Contador e Visualizador).
- **Severidade:** ALTO. Num ERP real, o auxiliar do financeiro não pode ter permissão para alterar o Plano de Contas ou desfazer lançamentos passados. Hoje, quem tem login, parece ter acesso de gravação indiscriminado dentro do escopo do seu grupo.

## 2. Risco de IDOR (Insecure Direct Object Reference)
**Status:** ✅ Blindado
- **Evidência:** Como a aplicação resolve o banco de dados do cliente puxando a variável criptografada de dentro do Cookie (ex: `grupo_slug = session.get("grupo_slug")`), um usuário mal intencionado não consegue alterar um parâmetro na URL `?empresa_id=2` para ver dados de terceiros. A barreira está na sessão HTTP assinada pela `SECRET_KEY` do Flask, o que isola de forma segura as rotas (Tenant Isolation).
- **Severidade:** BAIXO. Ótima arquitetura para SaaS Multitenant.

## 3. Confirmação de Ações Críticas
**Status:** ❌ Inexistente
- **Evidência:** Ações destrutivas ou altamente sensíveis (como estornar um lançamento ou "Liquidar" título) não exigem a digitação de uma "Senha de Confirmação" nem validam uma sub-permissão financeira específica antes da execução, bastando o clique do botão no frontend (método POST liberado pelo `@login_required`).
- **Severidade:** ALTO. Aumenta muito as chances de fraude interna ou erro operacional humano irreparável.

## 4. Hashing de Senhas
**Status:** ✅ Correto (Forte)
- **Evidência:** O sistema utiliza `bcrypt`, que gera um hash seguro irreversível com salt automático (ex: `bcrypt.checkpw(senha.encode(), stored_hash)`), blindando as senhas mestre e dos inquilinos em caso de vazamento do banco central (`db_flow_admin`).
- **Severidade:** BAIXO (Muito seguro, dentro do padrão de mercado).
