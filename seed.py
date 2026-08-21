import sys
import os
import bcrypt
from pymongo import MongoClient
from config import Config
from services.bancos_service import BancosService

def seed_database():
    print("Iniciando semeadura do banco de dados (seed)...")
    
    # Conexão direta para criação do banco
    try:
        client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
    except Exception as e:
        print(f"Erro ao conectar no MongoDB. Certifique-se de que o MongoDB está ativo. Erro: {e}")
        sys.exit(1)

    admin_db = client[Config.ADMIN_DB_NAME]
    
    # Limpa dados existentes para garantir idempotência
    admin_db["usuarios_master"].drop()
    admin_db["assinaturas_grupos"].drop()
    
    print("Coleções antigas removidas do db_flow_admin.")

    # 0. Semente de Bancos Febraban
    if os.path.exists("ParticipantesSTR.csv"):
        print("Semeando coleção bancos_febraban a partir de ParticipantesSTR.csv...")
        BancosService.carregar_ou_atualizar_bancos(admin_db)

    # 1. Criação das Assinaturas de Grupos Econômicos
    grupos_data = [
        {
            "nome_grupo": "Holding XYZ",
            "db_name": "db_grupo_holding_xyz",
            "status": "Ativo",
            "valor_mensalidade": 1500.00,
            "max_cnpjs": 5,
            "max_usuarios": 10,
            "data_vencimento": "10",
            "flag_bpo": True
        },
        {
            "nome_grupo": "Agro ABC",
            "db_name": "db_grupo_agro_abc",
            "status": "Ativo",
            "valor_mensalidade": 950.00,
            "max_cnpjs": 3,
            "max_usuarios": 5,
            "data_vencimento": "15",
            "flag_bpo": True
        },
        {
            "nome_grupo": "Serviços DEF",
            "db_name": "db_grupo_servicos_def",
            "status": "Ativo",
            "valor_mensalidade": 1200.00,
            "max_cnpjs": 8,
            "max_usuarios": 12,
            "data_vencimento": "20",
            "flag_bpo": False
        }
    ]
    
    grupos_result = admin_db["assinaturas_grupos"].insert_many(grupos_data)
    grupos_ids = grupos_result.inserted_ids
    print(f"Inseridos {len(grupos_ids)} Grupos Econômicos.")
    
    # Mapeamento do nome_grupo para seu respectivo ID inserido
    grupo_id_map = {g["nome_grupo"]: id for g, id in zip(grupos_data, grupos_ids)}

    # Senha comum de teste
    senha_plana = "123"
    senha_hash = bcrypt.hashpw(senha_plana.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    # 2. Criação de Usuários Master
    usuarios_data = [
        {
            "nome": "Adriano Administrador",
            "email": "admin@holdingxyz.com.br",
            "senha": senha_hash,
            "grupo_economico_id": grupo_id_map["Holding XYZ"],
            "permissao_master": True,
            "role": "superadmin"
        },
        {
            "nome": "Carlos Produtor",
            "email": "financeiro@agroabc.com.br",
            "senha": senha_hash,
            "grupo_economico_id": grupo_id_map["Agro ABC"],
            "permissao_master": False,
            "role": "user"
        },
        {
            "nome": "Mariana Gerente",
            "email": "gerente@servicosdef.com.br",
            "senha": senha_hash,
            "grupo_economico_id": grupo_id_map["Serviços DEF"],
            "permissao_master": False,
            "role": "user"
        }
    ]
    
    admin_db["usuarios_master"].insert_many(usuarios_data)
    print(f"Inseridos {len(usuarios_data)} Usuários Master.")

    # 3. Criação de dados fictícios para o banco isolado do Grupo Ativo (db_grupo_holding_xyz)
    holding_db = client["db_grupo_holding_xyz"]
    holding_db["transacoes"].drop()
    holding_db["plano_contas"].drop()
    holding_db["participantes"].drop()
    holding_db["contas_pagar"].drop()
    holding_db["contas_receber"].drop()
    holding_db["usuarios"].drop()
    holding_db["usuarios"].insert_many([
        {"nome": "Adriano Administrador", "email": "admin@holdingxyz.com.br"},
        {"nome": "Maria Assistente", "email": "maria@holdingxyz.com.br"},
        {"nome": "Pedro Analista", "email": "pedro@holdingxyz.com.br"}
    ])
    
    # 3.1. Popula Plano de Contas
    plano_data = [
        {"codigo": "1", "nome": "Ativo", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "1.1", "nome": "Ativo Circulante", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "1.1.1", "nome": "Caixa e Equivalentes", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Analítica"},
        {"codigo": "1.1.2", "nome": "Contas a Receber Clientes", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Analítica"},
        {"codigo": "1.1.3", "nome": "Aplicações Financeiras", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Analítica"},
        {"codigo": "1.2", "nome": "Ativo Não Circulante", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "1.2.1", "nome": "Máquinas e Equipamentos", "tipo": "Ativo", "natureza": "Débito", "classificacao": "Analítica"},
        {"codigo": "2", "nome": "Passivo e PL", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Sintética"},
        {"codigo": "2.1", "nome": "Passivo Circulante", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Sintética"},
        {"codigo": "2.1.1", "nome": "Fornecedores Nacionais", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Analítica"},
        {"codigo": "2.1.3", "nome": "Empréstimos Bancários C.P.", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Analítica"},
        {"codigo": "2.2", "nome": "Passivo Não Circulante", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Sintética"},
        {"codigo": "2.2.1", "nome": "Financiamentos L.P.", "tipo": "Passivo", "natureza": "Crédito", "classificacao": "Analítica"},
        {"codigo": "3", "nome": "Receitas", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética"},
        {"codigo": "3.1", "nome": "Receita Operacional Bruta", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Sintética"},
        {"codigo": "3.1.1", "nome": "Venda de Mercadorias", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Analítica"},
        {"codigo": "3.1.2", "nome": "Prestação de Serviços BPO", "tipo": "Resultado", "natureza": "Crédito", "classificacao": "Analítica"},
        {"codigo": "4", "nome": "Custos e Despesas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "4.1", "nome": "Custos de Vendas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "4.1.1", "nome": "Custo de Mercadorias Vendidas (CMV)", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica", "cmv": True},
        {"codigo": "4.2", "nome": "Despesas Administrativas", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Sintética"},
        {"codigo": "4.2.1", "nome": "Serviços de Cloud e Hospedagem", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica"},
        {"codigo": "4.2.2", "nome": "Serviços de Assessoria e BPO", "tipo": "Resultado", "natureza": "Débito", "classificacao": "Analítica"}
    ]
    holding_db["plano_contas"].insert_many(plano_data)
    print("Plano de contas semeado.")

    # 3.2. Popula Participantes
    part_data = [
        {"nome_razao": "Posto Petrobras", "tipo": "Fornecedor", "cnpj_cpf": "11.111.111/0001-11", "email": "contato@petrobras.com", "telefone": "(11) 99999-1111"},
        {"nome_razao": "Supermercados Extra", "tipo": "Fornecedor", "cnpj_cpf": "22.222.222/0002-22", "email": "compras@extra.com", "telefone": "(11) 99999-2222"},
        {"nome_razao": "Amazon Web Services", "tipo": "Fornecedor", "cnpj_cpf": "33.333.333/0003-33", "email": "billing@aws.com", "telefone": "(11) 99999-3333"},
        {"nome_razao": "Cliente Alfa Ltda", "tipo": "Cliente", "cnpj_cpf": "44.444.444/0004-44", "email": "compras@alfa.com", "telefone": "(11) 99999-4444"},
        {"nome_razao": "Cliente Beta S.A.", "tipo": "Cliente", "cnpj_cpf": "55.555.555/0005-55", "email": "financeiro@beta.com", "telefone": "(11) 99999-5555"}
    ]
    holding_db["participantes"].insert_many(part_data)
    print("Parceiros de negócios semeados.")
    
    # Resolve IDs para ligar transações
    aws = holding_db["participantes"].find_one({"nome_razao": "Amazon Web Services"})
    alfa = holding_db["participantes"].find_one({"nome_razao": "Cliente Alfa Ltda"})
    beta = holding_db["participantes"].find_one({"nome_razao": "Cliente Beta S.A."})

    # 3.3. Popula Transações de Histórico (6 meses para o gráfico do Dashboard)
    # Meses: Fevereiro a Julho de 2026
    transacoes_data = [
        # FEV
        {"descricao": "Serviços de Cloud AWS", "valor": -850.00, "tipo": "Despesa", "data": "2026-02-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Alfa", "valor": 8000.00, "tipo": "Receita", "data": "2026-02-15", "participante_id": str(alfa["_id"]), "participante_nome": "Cliente Alfa Ltda", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        # MAR
        {"descricao": "Serviços de Cloud AWS", "valor": -900.00, "tipo": "Despesa", "data": "2026-03-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Alfa", "valor": 9500.00, "tipo": "Receita", "data": "2026-03-15", "participante_id": str(alfa["_id"]), "participante_nome": "Cliente Alfa Ltda", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        {"descricao": "Compra de Equipamento", "valor": -3500.00, "tipo": "Despesa", "data": "2026-03-20", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "1.2.1", "conta_nome": "Máquinas e Equipamentos", "centro_custo": "Administrativo", "origem": "Sistema"},
        # ABR
        {"descricao": "Serviços de Cloud AWS", "valor": -950.00, "tipo": "Despesa", "data": "2026-04-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Beta", "valor": 12000.00, "tipo": "Receita", "data": "2026-04-15", "participante_id": str(beta["_id"]), "participante_nome": "Cliente Beta S.A.", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        # MAI
        {"descricao": "Serviços de Cloud AWS", "valor": -1100.00, "tipo": "Despesa", "data": "2026-05-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Alfa", "valor": 10000.00, "tipo": "Receita", "data": "2026-05-15", "participante_id": str(alfa["_id"]), "participante_nome": "Cliente Alfa Ltda", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        # JUN
        {"descricao": "Serviços de Cloud AWS", "valor": -1200.00, "tipo": "Despesa", "data": "2026-06-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Beta", "valor": 14000.00, "tipo": "Receita", "data": "2026-06-15", "participante_id": str(beta["_id"]), "participante_nome": "Cliente Beta S.A.", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        {"descricao": "Compra de CMV Teste", "valor": -2500.00, "tipo": "Despesa", "data": "2026-06-25", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.1.1", "conta_nome": "Custo de Mercadorias Vendidas (CMV)", "centro_custo": "Operacional", "origem": "Sistema"},
        # JUL
        {"descricao": "Serviços de Cloud AWS", "valor": -1300.00, "tipo": "Despesa", "data": "2026-07-02", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.2.1", "conta_nome": "Serviços de Cloud e Hospedagem", "centro_custo": "Tecnologia", "origem": "Sistema"},
        {"descricao": "Faturamento Alfa", "valor": 15400.00, "tipo": "Receita", "data": "2026-07-05", "participante_id": str(alfa["_id"]), "participante_nome": "Cliente Alfa Ltda", "conta_codigo": "3.1.2", "conta_nome": "Prestação de Serviços BPO", "centro_custo": "Comercial", "origem": "Sistema"},
        {"descricao": "CMV Mês Atual", "valor": -3000.00, "tipo": "Despesa", "data": "2026-07-10", "participante_id": str(aws["_id"]), "participante_nome": "Amazon Web Services", "conta_codigo": "4.1.1", "conta_nome": "Custo de Mercadorias Vendidas (CMV)", "centro_custo": "Operacional", "origem": "Sistema"}
    ]
    holding_db["transacoes"].insert_many(transacoes_data)
    print("Transações de teste de 6 meses inseridas no banco isolado holding_xyz.")
    
    # 3.4. Popula Lançamentos Contábeis (Partidas Dobradas) para as transações de histórico
    holding_db["lancamentos_contabeis"].drop()
    lancamentos_contabeis_data = []
    for t in transacoes_data:
        val = abs(t["valor"])
        if t["tipo"] == "Despesa":
            # D=Despesa/Ativo, C=Caixa
            debito = t["conta_codigo"]
            credito = "1.1.1"
        else:
            # D=Caixa, C=Receita/Passivo
            debito = "1.1.1"
            credito = t["conta_codigo"]
            
        lancamentos_contabeis_data.append({
            "data": t["data"],
            "descricao": f"Semente: {t['descricao']}",
            "conta_debito": debito,
            "conta_credito": credito,
            "valor": val,
            "centro_custo": t["centro_custo"]
        })
    holding_db["lancamentos_contabeis"].insert_many(lancamentos_contabeis_data)
    print("Lançamentos contábeis (partidas dobradas) semeados.")

    # 3.5. Popula Logs de Arquivos OFX com discrepâncias para auditoria de integridade
    holding_db["ofx_arquivos_importados"].drop()
    arquivos_ofx_data = [
        {
            "nome_arquivo": "extrato_abril_2026.ofx",
            "data_importacao": "2026-04-12 18:00:00",
            "total_transacoes": 4,
            "saldo_final": 10500.00,
            "data_saldo": "2026-04-12",
            "data_inicio": "2026-04-01",
            "data_fim": "2026-04-12",
            "soma_transacoes": 2000.00
        },
        {
            "nome_arquivo": "extrato_maio_2026.ofx",
            "data_importacao": "2026-05-15 18:00:00",
            "total_transacoes": 6,
            "saldo_final": 15000.00,
            "data_saldo": "2026-05-15",
            "data_inicio": "2026-04-15", # Lacuna cronológica de 3 dias (12/04 a 15/04)
            "data_fim": "2026-05-15",
            "soma_transacoes": 4500.00 # Saldo inicial calculado = 10500.00 (consistente com final de abril)
        },
        {
            "nome_arquivo": "extrato_junho_2026.ofx",
            "data_importacao": "2026-06-30 18:00:00",
            "total_transacoes": 5,
            "saldo_final": 20000.00,
            "data_saldo": "2026-06-30",
            "data_inicio": "2026-06-01", # Lacuna cronológica (15/05 a 01/06)
            "data_fim": "2026-06-30",
            "soma_transacoes": 3000.00 # Saldo inicial calculado = 17000.00 (furo de saldo de R$ 2000 em relação ao final de maio de 15000!)
        }
    ]
    holding_db["ofx_arquivos_importados"].insert_many(arquivos_ofx_data)
    print("Logs de arquivos OFX semeados com alertas de integridade simulados.")

    # 4. Dados fictícios adicionais para o Grupo BPO Agro ABC
    agro_db = client["db_grupo_agro_abc"]
    agro_db["transacoes"].drop()
    agro_db["transacoes"].insert_many([
        {"descricao": "Venda de Grãos Cooperativa", "valor": 95000.00, "tipo": "Receita", "data": "2026-06-15", "centro_custo": "Geral"},
        {"descricao": "Compra de Fertilizantes", "valor": -35000.00, "tipo": "Despesa", "data": "2026-06-20", "centro_custo": "Geral"}
    ])
    agro_db["usuarios"].drop()
    agro_db["usuarios"].insert_many([
        {"nome": "Carlos Produtor", "email": "financeiro@agroabc.com.br"},
        {"nome": "José Auxiliar", "email": "jose@agroabc.com.br"}
    ])
    agro_db["participantes"].drop()
    agro_db["participantes"].insert_many([
        {"nome_razao": "Cooperativa Grãos", "cnpj_cpf": "12.345.678/0001-90"},
        {"nome_razao": "Fertilizantes Sul", "cnpj_cpf": "98.765.432/0001-21"}
    ])
    print("Banco de dados isolado 'db_grupo_agro_abc' populado com transações, usuários e parceiros.")

    # 5. Dados fictícios adicionais para o Grupo Serviços DEF
    servicos_db = client["db_grupo_servicos_def"]
    servicos_db["usuarios"].drop()
    servicos_db["usuarios"].insert_one({"nome": "Mariana Gerente", "email": "gerente@servicosdef.com.br"})
    servicos_db["participantes"].drop()
    servicos_db["participantes"].insert_many([
        {"nome_razao": "Consultoria Alfa", "cnpj_cpf": "88.888.888/0001-88"},
        {"nome_razao": "Sistemas Beta", "cnpj_cpf": "77.777.777/0001-77"}
    ])
    print("Banco de dados isolado 'db_grupo_servicos_def' populado com usuários e parceiros.")

    print("\nSemeadura concluída com sucesso!")
    print("\nCredenciais de Teste:")
    print("---------------------------------------------------------------------------------")
    print("Grupo Econômico: Holding XYZ (ATIVO)")
    print("  Email: admin@holdingxyz.com.br | Senha: 123")
    print("---------------------------------------------------------------------------------")
    print("Grupo Econômico: Agro ABC (SUSPENSO)")
    print("  Email: financeiro@agroabc.com.br | Senha: 123")
    print("---------------------------------------------------------------------------------")
    print("Grupo Econômico: Serviços DEF (CANCELADO)")
    print("  Email: gerente@servicosdef.com.br | Senha: 123")
    print("---------------------------------------------------------------------------------")

if __name__ == "__main__":
    seed_database()
