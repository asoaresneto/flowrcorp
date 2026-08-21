"""
importador_plano_contas.py
--------------------------
Módulo responsável pelo parsing, validação e persistência (upsert) de um
Plano de Contas a partir de arquivo CSV enviado pelo usuário.

Versão 3 — Auto-estruturante com dois modos de importação:
- Modo "upsert" (Mesclar/Atualizar): preserva todas as contas existentes,
  atualiza dados e insere novas contas.
- Modo "reset" (Substituir Plano Completo): remove contas sem movimentação
  que não constam no novo CSV e preserva automaticamente contas com
  lançamentos vinculados (integridade referencial).

Recursos comuns:
- NÃO bloqueia importação por falta de contas pai intermediárias.
- Cria automaticamente nós intermediários como contas Sintéticas.
- Usa upsert nativo do MongoDB (update_one com upsert=True).
- Detecção automática de encoding (utf-8 / latin-1) e delimitador (; ou ,).
- Mapeamento flexível de colunas (variações de nome).
- Inferência automática de tipo, natureza, classificação e CMV pelo código.
- Relatório estruturado com erros apenas para linhas realmente corrompidas.

Autor: FlowCorp
"""

import csv
import io
import re
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any, BinaryIO


# ---------------------------------------------------------------------------
# Helpers de normalização
# ---------------------------------------------------------------------------

def _remover_acentos(texto: str) -> str:
    """Remove acentos e diacríticos de uma string."""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalizar_header(header: str) -> str:
    """Normaliza um nome de coluna: lowercase, sem acentos, sem espaços extras."""
    h = header.strip().lower()
    h = _remover_acentos(h)
    h = re.sub(r"[^a-z0-9_]", "_", h)
    h = re.sub(r"_+", "_", h).strip("_")
    return h


def _sanitizar_codigo(codigo: str) -> str:
    """
    Sanitiza o código da conta contábil:
    - Remove espaços
    - Mantém apenas dígitos e pontos
    - Remove pontos duplicados e trailing dots
    """
    codigo = codigo.strip()
    codigo = re.sub(r"[^\d.]", "", codigo)
    codigo = re.sub(r"\.{2,}", ".", codigo)
    codigo = codigo.strip(".")
    return codigo


# ---------------------------------------------------------------------------
# Verificação de integridade referencial (lançamentos vinculados)
# ---------------------------------------------------------------------------

def conta_tem_lancamentos(tenant_db, codigo_conta: str) -> bool:
    """
    Verifica se uma conta contábil possui lançamentos ou transações vinculados
    em qualquer coleção do sistema. Se sim, a conta NÃO pode ser excluída.

    Coleções verificadas:
    - lancamentos (conta_debito, conta_credito, categoria_id)
    - lancamentos_contabeis (conta_debito, conta_credito)
    - transacoes (conta_codigo)
    - contas_pagar (conta_codigo)
    - contas_receber (conta_codigo)

    :param tenant_db: Referência ao banco MongoDB do tenant
    :param codigo_conta: Código da conta contábil a verificar
    :return: True se a conta possui lançamentos (bloqueada para exclusão)
    """
    total = 0

    # lancamentos
    try:
        total += tenant_db.lancamentos.count_documents(
            {"$or": [
                {"conta_debito": codigo_conta},
                {"conta_credito": codigo_conta},
                {"categoria_id": codigo_conta},
            ]},
            limit=1
        )
    except Exception:
        pass

    if total > 0:
        return True

    # lancamentos_contabeis
    try:
        total += tenant_db.lancamentos_contabeis.count_documents(
            {"$or": [
                {"conta_debito": codigo_conta},
                {"conta_credito": codigo_conta},
            ]},
            limit=1
        )
    except Exception:
        pass

    if total > 0:
        return True

    # transacoes
    try:
        total += tenant_db.transacoes.count_documents(
            {"conta_codigo": codigo_conta},
            limit=1
        )
    except Exception:
        pass

    if total > 0:
        return True

    # contas_pagar
    try:
        total += tenant_db.contas_pagar.count_documents(
            {"conta_codigo": codigo_conta},
            limit=1
        )
    except Exception:
        pass

    if total > 0:
        return True

    # contas_receber
    try:
        total += tenant_db.contas_receber.count_documents(
            {"conta_codigo": codigo_conta},
            limit=1
        )
    except Exception:
        pass

    return total > 0


# ---------------------------------------------------------------------------
# Mapeamento flexível de colunas
# ---------------------------------------------------------------------------

_COLUMN_MAP: Dict[str, List[str]] = {
    "codigo":        ["codigo", "cod", "code", "conta", "account", "conta_codigo", "codigo_conta"],
    "nome":          ["nome", "descricao", "name", "description", "desc", "nome_conta", "descricao_conta"],
    "tipo":          ["tipo", "type", "grupo"],
    "natureza":      ["natureza", "nature", "nat", "debito_credito", "dc"],
    "classificacao": ["classificacao", "class", "tipo_conta", "sintetica_analitica", "sa"],
    "cmv":           ["cmv", "custo", "cst"],
    "faz_parte_dre": ["faz_parte_dre", "dre", "parte_dre"],
    "grupo_dre":     ["grupo_dre", "dre_grupo", "grupo_resultado"],
    "faz_parte_dfc": ["faz_parte_dfc", "dfc", "parte_dfc", "fluxo_caixa"],
    "grupo_dfc":     ["grupo_dfc", "dfc_grupo", "atividade_dfc"],
}


def _mapear_colunas(headers_normalizados: List[str]) -> Dict[str, Optional[int]]:
    """Mapeia headers normalizados do CSV para índices de colunas."""
    mapa: Dict[str, Optional[int]] = {campo: None for campo in _COLUMN_MAP}
    for idx, header in enumerate(headers_normalizados):
        for campo_canonico, variantes in _COLUMN_MAP.items():
            if header in variantes and mapa[campo_canonico] is None:
                mapa[campo_canonico] = idx
                break
    return mapa


# ---------------------------------------------------------------------------
# Normalização de valores dos campos
# ---------------------------------------------------------------------------

_TIPO_MAP = {
    "patrimonial": "Patrimonial", "p": "Patrimonial", "pat": "Patrimonial",
    "resultado": "Resultado", "r": "Resultado", "res": "Resultado",
}

_NATUREZA_MAP = {
    "debito": "Débito", "d": "Débito", "deb": "Débito",
    "credito": "Crédito", "c": "Crédito", "cred": "Crédito",
}

_CLASSIFICACAO_MAP = {
    "sintetica": "Sintética", "s": "Sintética", "sint": "Sintética", "agrupadora": "Sintética",
    "analitica": "Analítica", "a": "Analítica", "anal": "Analítica",
}

_CMV_TRUTHY = {"sim", "true", "1", "s", "yes", "verdadeiro", "v", "t"}


def _normalizar_tipo(valor: str) -> Optional[str]:
    return _TIPO_MAP.get(_remover_acentos(valor.strip().lower()))

def _normalizar_natureza(valor: str) -> Optional[str]:
    return _NATUREZA_MAP.get(_remover_acentos(valor.strip().lower()))

def _normalizar_classificacao(valor: str) -> Optional[str]:
    return _CLASSIFICACAO_MAP.get(_remover_acentos(valor.strip().lower()))

def _normalizar_cmv(valor: str) -> bool:
    return _remover_acentos(valor.strip().lower()) in _CMV_TRUTHY

def _normalizar_bool(valor: str) -> bool:
    """Normaliza valor booleano genérico (para faz_parte_dre, faz_parte_dfc)."""
    return _remover_acentos(valor.strip().lower()) in _CMV_TRUTHY


_GRUPO_DRE_MAP = {
    "receita_bruta": "receita_bruta", "receita bruta": "receita_bruta", "rb": "receita_bruta",
    "deducoes": "deducoes", "deducao": "deducoes", "ded": "deducoes",
    "cmv": "cmv", "custo": "cmv", "csv": "cmv", "cpv": "cmv",
    "despesas_operacionais": "despesas_operacionais", "despesa operacional": "despesas_operacionais", "do": "despesas_operacionais",
    "despesas_financeiras": "despesas_financeiras", "despesa financeira": "despesas_financeiras", "df": "despesas_financeiras",
    "outras_receitas_despesas": "outras_receitas_despesas", "outras": "outras_receitas_despesas", "ord": "outras_receitas_despesas",
}

_GRUPO_DFC_MAP = {
    "operacional": "operacional", "op": "operacional", "o": "operacional",
    "investimento": "investimento", "inv": "investimento", "i": "investimento",
    "financiamento": "financiamento", "fin": "financiamento", "f": "financiamento",
}


def _normalizar_grupo_dre(valor: str) -> Optional[str]:
    return _GRUPO_DRE_MAP.get(_remover_acentos(valor.strip().lower()))

def _normalizar_grupo_dfc(valor: str) -> Optional[str]:
    return _GRUPO_DFC_MAP.get(_remover_acentos(valor.strip().lower()))


# ---------------------------------------------------------------------------
# Inferência automática de atributos pelo código contábil
# ---------------------------------------------------------------------------

# Nomes descritivos para os grupos raiz (usados ao auto-criar nós intermediários)
_NOMES_RAIZ: Dict[str, str] = {
    "1": "Ativo",
    "2": "Passivo",
    "3": "Receitas",
    "4": "Custos e Despesas",
    "5": "Outras Contas",
}


def _inferir_tipo(codigo: str) -> str:
    """Códigos 1/2 → Patrimonial; 3/4/5+ → Resultado."""
    primeiro = codigo.split(".")[0]
    return "Patrimonial" if primeiro in ("1", "2") else "Resultado"


def _inferir_natureza(codigo: str) -> str:
    """Códigos 1/4/5 → Débito; 2/3 → Crédito."""
    primeiro = codigo.split(".")[0]
    return "Débito" if primeiro in ("1", "4", "5") else "Crédito"


def _inferir_cmv(codigo: str) -> bool:
    """
    Marca como CMV contas que pertencem ao grupo de custos operacionais.
    Heurística: código inicia com '4.1' (custos diretos / CMV).
    """
    return codigo.startswith("4.1.") or codigo == "4.1"


def _inferir_faz_parte_dre(codigo: str) -> bool:
    """Contas do grupo 3 (Receitas) e 4 (Custos/Despesas) fazem parte da DRE."""
    primeiro = codigo.split(".")[0]
    return primeiro in ("3", "4")


def _inferir_grupo_dre(codigo: str, cmv: bool = False) -> Optional[str]:
    """
    Classifica a conta em um grupo da DRE baseado no código contábil.
    Retorna None se a conta não faz parte da DRE.
    """
    if not _inferir_faz_parte_dre(codigo):
        return None
    if codigo.startswith("3.1") or codigo == "3.1":
        return "receita_bruta"
    if codigo.startswith("3.2") or codigo == "3.2":
        return "deducoes"
    if cmv or codigo.startswith("4.1") or codigo == "4.1":
        return "cmv"
    if codigo.startswith("4.2") or codigo == "4.2":
        return "despesas_operacionais"
    if codigo.startswith("4.3") or codigo == "4.3":
        return "despesas_financeiras"
    # Demais contas do grupo 3 ou 4
    return "outras_receitas_despesas"


def _inferir_faz_parte_dfc(codigo: str) -> bool:
    """
    Contas com movimentação financeira fazem parte do DFC.
    Inclui: circulantes operacionais (1.1, 2.1), resultado (3, 4),
    imobilizado/investimentos (1.2), e financiamentos (2.2, 2.3).
    """
    primeiro = codigo.split(".")[0]
    # Contas de resultado sempre afetam o DFC
    if primeiro in ("3", "4"):
        return True
    # Circulantes
    if codigo.startswith("1.1") or codigo.startswith("2.1"):
        return True
    # Não circulantes com movimentação
    if codigo.startswith("1.2") or codigo.startswith("2.2") or codigo.startswith("2.3"):
        return True
    return False


def _inferir_grupo_dfc(codigo: str) -> Optional[str]:
    """
    Classifica a conta em uma atividade do DFC:
    operacional, investimento ou financiamento.
    """
    if not _inferir_faz_parte_dfc(codigo):
        return None
    # Investimento: Imobilizado (1.2.3), Intangível (1.2.4), Investimentos (1.2.2), ANC geral (1.2)
    if codigo.startswith("1.2"):
        return "investimento"
    # Financiamento: Empréstimos LP (2.2), PL (2.3), Financiamentos CP (2.1.4, 2.1.3)
    if codigo.startswith("2.2") or codigo.startswith("2.3"):
        return "financiamento"
    if codigo.startswith("2.1.3") or codigo.startswith("2.1.4"):
        return "financiamento"
    # Padrão: operacional (receitas, despesas, circulantes operacionais)
    return "operacional"


def _gerar_nome_intermediario(codigo: str) -> str:
    """
    Gera um nome descritivo para um nó intermediário auto-criado.
    Ex: '4' → 'Custos e Despesas', '4.1' → 'Grupo 4.1', '4.1.01' → 'Subgrupo 4.1.01'
    """
    if codigo in _NOMES_RAIZ:
        return _NOMES_RAIZ[codigo]

    nivel = len(codigo.split("."))
    if nivel == 2:
        return f"Grupo {codigo}"
    return f"Subgrupo {codigo}"


# ---------------------------------------------------------------------------
# Detecção de encoding e delimitador
# ---------------------------------------------------------------------------

def _detectar_encoding(conteudo_bytes: bytes) -> str:
    """Tenta decodificar como utf-8; se falhar, usa latin-1."""
    try:
        conteudo_bytes.decode("utf-8")
        return "utf-8"
    except (UnicodeDecodeError, ValueError):
        return "latin-1"


def _detectar_delimitador(primeira_linha: str) -> str:
    """Detecta o delimitador usando csv.Sniffer; fallback para ';'."""
    try:
        dialeto = csv.Sniffer().sniff(primeira_linha, delimiters=";,\t")
        return dialeto.delimiter
    except csv.Error:
        return ";" if primeira_linha.count(";") >= primeira_linha.count(",") else ","


# ---------------------------------------------------------------------------
# Geração de nós intermediários ausentes
# ---------------------------------------------------------------------------

def _gerar_ancestrais(codigo: str) -> List[str]:
    """
    Dado um código como '4.1.01.001', retorna todos os ancestrais:
    ['4', '4.1', '4.1.01']
    """
    partes = codigo.split(".")
    ancestrais = []
    for i in range(1, len(partes)):
        ancestrais.append(".".join(partes[:i]))
    return ancestrais


def _criar_conta_sintetica(codigo: str) -> Dict[str, Any]:
    """Cria um documento de conta Sintética auto-gerada para um nó intermediário."""
    cmv = _inferir_cmv(codigo)
    return {
        "codigo": codigo,
        "nome": _gerar_nome_intermediario(codigo),
        "tipo": _inferir_tipo(codigo),
        "natureza": _inferir_natureza(codigo),
        "classificacao": "Sintética",
        "cmv": cmv,
        "faz_parte_dre": _inferir_faz_parte_dre(codigo),
        "grupo_dre": _inferir_grupo_dre(codigo, cmv),
        "faz_parte_dfc": _inferir_faz_parte_dfc(codigo),
        "grupo_dfc": _inferir_grupo_dfc(codigo),
        "nivel": len(codigo.split(".")),
        "codigo_pai": ".".join(codigo.split(".")[:-1]) or None,
        "_auto_gerada": True,
    }


# ---------------------------------------------------------------------------
# Classe principal do importador (v2 — Auto-estruturante)
# ---------------------------------------------------------------------------

class ImportadorPlanoContas:
    """
    Processa um CSV de Plano de Contas, cria automaticamente nós intermediários
    ausentes e persiste via upsert no MongoDB. Nunca bloqueia por falta de
    contas pai — gera-as como Sintéticas automaticamente.
    """

    def __init__(self):
        self.erros: List[Dict[str, Any]] = []
        self.total_lidas: int = 0
        self.total_inseridas: int = 0
        self.total_atualizadas: int = 0
        self.total_auto_geradas: int = 0
        self.total_preservadas_com_historico: int = 0
        self.total_removidas_sem_uso: int = 0

    def _adicionar_erro(self, linha: int, mensagem: str) -> None:
        self.erros.append({"linha": linha, "erro": mensagem})
        logging.warning(f"Importação CSV - Linha {linha}: {mensagem}")

    def _ler_arquivo(self, file_stream: BinaryIO) -> Tuple[str, str]:
        """Lê conteúdo binário, detecta encoding e delimitador."""
        conteudo_bytes = file_stream.read()
        encoding = _detectar_encoding(conteudo_bytes)
        conteudo_texto = conteudo_bytes.decode(encoding)

        # Remove BOM se presente
        if conteudo_texto.startswith("\ufeff"):
            conteudo_texto = conteudo_texto[1:]

        linhas = conteudo_texto.strip().splitlines()
        if not linhas:
            raise ValueError("O arquivo CSV está vazio.")

        delimitador = _detectar_delimitador(linhas[0])
        return conteudo_texto, delimitador

    def _parse_linhas(self, conteudo_texto: str, delimitador: str) -> Dict[str, Dict[str, Any]]:
        """
        Faz o parse do CSV e retorna um dicionário {codigo: documento}.
        Registra erros apenas para linhas realmente corrompidas (sem código ou sem nome).
        """
        reader = csv.reader(io.StringIO(conteudo_texto), delimiter=delimitador)

        # Headers
        try:
            headers_raw = next(reader)
        except StopIteration:
            raise ValueError("O arquivo CSV não possui cabeçalho.")

        headers_normalizados = [_normalizar_header(h) for h in headers_raw]
        mapa_colunas = _mapear_colunas(headers_normalizados)

        # Validação de colunas obrigatórias
        if mapa_colunas["codigo"] is None:
            raise ValueError(
                f"Coluna 'codigo' não encontrada no CSV. "
                f"Colunas detectadas: {', '.join(headers_raw)}"
            )
        if mapa_colunas["nome"] is None:
            raise ValueError(
                f"Coluna 'nome' ou 'descricao' não encontrada no CSV. "
                f"Colunas detectadas: {', '.join(headers_raw)}"
            )

        contas: Dict[str, Dict[str, Any]] = {}  # codigo -> documento
        codigos_vistos: Dict[str, int] = {}

        for num_linha, row in enumerate(reader, start=2):
            self.total_lidas += 1

            # Ignora linhas completamente vazias
            if not any(cell.strip() for cell in row):
                continue

            # Extrai valores
            def _get(campo: str) -> str:
                idx = mapa_colunas.get(campo)
                if idx is not None and idx < len(row):
                    return row[idx].strip()
                return ""

            codigo_raw = _get("codigo")
            nome = _get("nome")
            codigo = _sanitizar_codigo(codigo_raw)

            # Validação: código obrigatório
            if not codigo:
                self._adicionar_erro(num_linha, "Código da conta está vazio ou inválido.")
                continue

            # Validação: nome obrigatório
            if not nome:
                self._adicionar_erro(num_linha, f"Nome/Descrição está vazio para a conta '{codigo}'.")
                continue

            # Duplicata no arquivo: mantém a primeira ocorrência
            if codigo in codigos_vistos:
                self._adicionar_erro(
                    num_linha,
                    f"Código '{codigo}' duplicado (já na linha {codigos_vistos[codigo]}). Ignorado."
                )
                continue

            codigos_vistos[codigo] = num_linha

            # Normaliza campos opcionais do CSV
            tipo_raw = _get("tipo")
            natureza_raw = _get("natureza")
            classificacao_raw = _get("classificacao")
            cmv_raw = _get("cmv")
            faz_parte_dre_raw = _get("faz_parte_dre")
            grupo_dre_raw = _get("grupo_dre")
            faz_parte_dfc_raw = _get("faz_parte_dfc")
            grupo_dfc_raw = _get("grupo_dfc")

            tipo = _normalizar_tipo(tipo_raw) if tipo_raw else None
            natureza = _normalizar_natureza(natureza_raw) if natureza_raw else None
            classificacao = _normalizar_classificacao(classificacao_raw) if classificacao_raw else None
            cmv = _normalizar_cmv(cmv_raw) if cmv_raw else None

            # Inferências automáticas pelo código
            if tipo is None:
                tipo = _inferir_tipo(codigo)
            if natureza is None:
                natureza = _inferir_natureza(codigo)
            if cmv is None:
                cmv = _inferir_cmv(codigo)

            # DRE metadata
            faz_parte_dre = _normalizar_bool(faz_parte_dre_raw) if faz_parte_dre_raw else _inferir_faz_parte_dre(codigo)
            grupo_dre = _normalizar_grupo_dre(grupo_dre_raw) if grupo_dre_raw else _inferir_grupo_dre(codigo, cmv)

            # DFC metadata
            faz_parte_dfc = _normalizar_bool(faz_parte_dfc_raw) if faz_parte_dfc_raw else _inferir_faz_parte_dfc(codigo)
            grupo_dfc = _normalizar_grupo_dfc(grupo_dfc_raw) if grupo_dfc_raw else _inferir_grupo_dfc(codigo)

            nivel = len(codigo.split("."))
            codigo_pai = ".".join(codigo.split(".")[:-1]) or None

            # Classificação: inferência inteligente
            # Se o CSV informou, usa; senão, usa nível >= 3 como heurística
            if classificacao is None:
                # Será resolvido depois no passo de inferência global
                pass

            contas[codigo] = {
                "codigo": codigo,
                "nome": nome,
                "tipo": tipo,
                "natureza": natureza,
                "classificacao": classificacao,
                "cmv": cmv,
                "faz_parte_dre": faz_parte_dre,
                "grupo_dre": grupo_dre,
                "faz_parte_dfc": faz_parte_dfc,
                "grupo_dfc": grupo_dfc,
                "nivel": nivel,
                "codigo_pai": codigo_pai,
                "_linha_csv": num_linha,
            }

        return contas

    def _gerar_intermediarios_ausentes(
        self,
        contas: Dict[str, Dict[str, Any]],
        codigos_existentes_db: set
    ) -> Dict[str, Dict[str, Any]]:
        """
        Para cada conta no dicionário, verifica se todos os seus ancestrais
        existem (no CSV ou no banco). Se não existirem, cria-os automaticamente
        como contas Sintéticas com nomes descritivos.
        """
        intermediarios: Dict[str, Dict[str, Any]] = {}

        # Coleta todos os códigos presentes (CSV + banco)
        codigos_presentes = set(contas.keys()) | codigos_existentes_db

        for codigo in list(contas.keys()):
            ancestrais = _gerar_ancestrais(codigo)
            for ancestral in ancestrais:
                if ancestral not in codigos_presentes and ancestral not in intermediarios:
                    intermediarios[ancestral] = _criar_conta_sintetica(ancestral)
                    self.total_auto_geradas += 1
                    logging.info(
                        f"Auto-gerada conta Sintética intermediária: "
                        f"'{ancestral}' — {intermediarios[ancestral]['nome']}"
                    )

        return intermediarios

    def _inferir_classificacao_global(self, contas: Dict[str, Dict[str, Any]]) -> None:
        """
        Infere classificação para contas que não a tiveram definida no CSV:
        - Se alguma outra conta tem este código como pai → Sintética
        - Se nível >= 3 ou se é folha (ninguém a referencia como pai) → Analítica
        - Demais → Sintética (nível 1 e 2 são tipicamente agrupadores)
        """
        # Coleta todos os código_pai referenciados
        codigos_que_sao_pais = set()
        for c in contas.values():
            pai = c.get("codigo_pai")
            if pai:
                codigos_que_sao_pais.add(pai)

        for c in contas.values():
            if c["classificacao"] is not None:
                continue

            codigo = c["codigo"]
            nivel = c.get("nivel", len(codigo.split(".")))

            if codigo in codigos_que_sao_pais:
                # É pai de alguém → Sintética
                c["classificacao"] = "Sintética"
            elif nivel >= 3:
                # Nível profundo e ninguém o referencia → Analítica
                c["classificacao"] = "Analítica"
            else:
                # Nível 1 ou 2 sem filhos explícitos — trata como Sintética por segurança
                c["classificacao"] = "Sintética"

    def _persistir_upsert(
        self,
        contas: Dict[str, Dict[str, Any]],
        tenant_db
    ) -> None:
        """
        Persiste todas as contas via upsert nativo do MongoDB.
        Ordena por nível (pais primeiro) para garantir consistência.
        """
        colecao = tenant_db.plano_contas
        agora = datetime.now(timezone.utc).isoformat()

        # Ordena por nível para inserir pais antes de filhos
        contas_ordenadas = sorted(contas.values(), key=lambda c: c.get("nivel", 1))

        for conta in contas_ordenadas:
            # Remove metadados internos antes de gravar
            conta.pop("_linha_csv", None)
            auto_gerada = conta.pop("_auto_gerada", False)

            codigo = conta["codigo"]

            # Documento para upsert
            doc_set = {
                "codigo": conta["codigo"],
                "nome": conta["nome"],
                "tipo": conta["tipo"],
                "natureza": conta["natureza"],
                "classificacao": conta["classificacao"],
                "cmv": conta["cmv"],
                "faz_parte_dre": conta.get("faz_parte_dre", False),
                "grupo_dre": conta.get("grupo_dre"),
                "faz_parte_dfc": conta.get("faz_parte_dfc", False),
                "grupo_dfc": conta.get("grupo_dfc"),
                "nivel": conta.get("nivel"),
                "codigo_pai": conta.get("codigo_pai"),
                "ativo": True,
                "atualizado_em": agora,
            }

            # Campos que só são definidos na inserção (não sobrescreve se já existir)
            doc_set_on_insert = {
                "criado_em": agora,
            }

            result = colecao.update_one(
                {"codigo": codigo},
                {
                    "$set": doc_set,
                    "$setOnInsert": doc_set_on_insert,
                },
                upsert=True
            )

            if result.upserted_id:
                self.total_inseridas += 1
            elif result.modified_count > 0:
                self.total_atualizadas += 1

    def _limpar_contas_obsoletas(
        self,
        codigos_novo_plano: set,
        tenant_db
    ) -> None:
        """
        Modo Reset: remove contas que não fazem parte do novo plano importado,
        MAS preserva automaticamente aquelas que possuem lançamentos vinculados.

        Contas preservadas com histórico são marcadas como ativo=False e
        registradas no relatório de feedback.
        """
        colecao = tenant_db.plano_contas
        agora = datetime.now(timezone.utc).isoformat()

        # Busca todas as contas atualmente cadastradas no banco
        contas_atuais = list(colecao.find({}, {"codigo": 1, "nome": 1, "_id": 1}))

        for conta_doc in contas_atuais:
            codigo = conta_doc.get("codigo", "")

            # Se a conta consta no novo plano, será atualizada via upsert — pula
            if codigo in codigos_novo_plano:
                continue

            # Verifica integridade referencial
            if conta_tem_lancamentos(tenant_db, codigo):
                # Preserva a conta, mas marca como inativa
                colecao.update_one(
                    {"_id": conta_doc["_id"]},
                    {"$set": {"ativo": False, "atualizado_em": agora}}
                )
                self.total_preservadas_com_historico += 1
                nome = conta_doc.get("nome", "")
                logging.info(
                    f"Conta '{codigo} - {nome}' preservada (possui lançamentos vinculados). "
                    f"Marcada como inativa."
                )
            else:
                # Sem lançamentos — pode ser removida com segurança
                colecao.delete_one({"_id": conta_doc["_id"]})
                self.total_removidas_sem_uso += 1
                logging.info(f"Conta '{codigo}' removida (sem lançamentos vinculados).")

    def processar(
        self,
        file_stream: BinaryIO,
        tenant_db,
        atualizar_existentes: bool = True,
        modo_importacao: str = "upsert"
    ) -> Dict[str, Any]:
        """
        Método principal: processa o arquivo CSV completo.

        Fluxo:
        1. Lê e detecta encoding/delimitador
        2. Faz parse das linhas do CSV
        3. (Modo reset) Remove contas obsoletas com proteção referencial
        4. Gera automaticamente nós intermediários ausentes
        5. Infere classificação (Sintética/Analítica)
        6. Persiste via upsert no MongoDB

        :param file_stream: Stream binário do arquivo CSV
        :param tenant_db: Referência ao banco MongoDB do tenant
        :param atualizar_existentes: Se True, atualiza contas existentes
        :param modo_importacao: 'upsert' (mesclar) ou 'reset' (substituir com proteção)
        :return: Dicionário com resumo da operação
        """
        # Normaliza o modo
        modo_importacao = modo_importacao.strip().lower() if modo_importacao else "upsert"
        if modo_importacao not in ("upsert", "reset"):
            modo_importacao = "upsert"

        try:
            # 1. Leitura e detecção
            conteudo_texto, delimitador = self._ler_arquivo(file_stream)

            # 2. Parse das linhas
            contas = self._parse_linhas(conteudo_texto, delimitador)

            if not contas and not self.erros:
                return self._resultado(
                    sucesso=False,
                    mensagem="Nenhuma conta válida encontrada no arquivo CSV.",
                    modo=modo_importacao,
                )

            # 3. Buscar códigos já existentes no banco
            codigos_existentes_db: set = set()
            if tenant_db is not None:
                cursor = tenant_db.plano_contas.find({}, {"codigo": 1, "_id": 0})
                codigos_existentes_db = {doc["codigo"] for doc in cursor}

            # 4. Gerar nós intermediários ausentes (auto-estruturante)
            intermediarios = self._gerar_intermediarios_ausentes(contas, codigos_existentes_db)

            # Merge: intermediários + contas do CSV (CSV tem prioridade)
            todas_contas: Dict[str, Dict[str, Any]] = {}
            todas_contas.update(intermediarios)
            todas_contas.update(contas)  # CSV sobrescreve intermediário se houver conflito

            # 5. Inferir classificação global
            self._inferir_classificacao_global(todas_contas)

            # 6. Modo reset: limpar contas obsoletas ANTES do upsert
            if modo_importacao == "reset" and tenant_db is not None:
                codigos_novo_plano = set(todas_contas.keys())
                self._limpar_contas_obsoletas(codigos_novo_plano, tenant_db)

            # 7. Persistência via upsert
            if tenant_db is not None:
                self._persistir_upsert(todas_contas, tenant_db)

            # Mensagem de resumo
            partes_msg = ["Importação concluída com sucesso."]
            if self.total_auto_geradas > 0:
                partes_msg.append(
                    f"{self.total_auto_geradas} conta(s) intermediária(s) criada(s) automaticamente."
                )
            if modo_importacao == "reset":
                if self.total_removidas_sem_uso > 0:
                    partes_msg.append(
                        f"{self.total_removidas_sem_uso} conta(s) antiga(s) removida(s) (sem lançamentos)."
                    )
                if self.total_preservadas_com_historico > 0:
                    partes_msg.append(
                        f"{self.total_preservadas_com_historico} conta(s) preservada(s) por possuírem lançamentos vinculados."
                    )

            return self._resultado(
                sucesso=True,
                mensagem=" ".join(partes_msg),
                modo=modo_importacao,
            )

        except ValueError as ve:
            logging.error(f"Erro de validação na importação CSV: {ve}")
            return self._resultado(
                sucesso=False,
                mensagem=str(ve),
                modo=modo_importacao,
                erro_extra={"linha": 0, "erro": str(ve)},
            )

        except Exception as e:
            logging.error(f"Erro inesperado na importação CSV do plano de contas: {e}")
            return self._resultado(
                sucesso=False,
                mensagem=f"Erro inesperado: {e}",
                modo=modo_importacao,
                erro_extra={"linha": 0, "erro": str(e)},
            )

    def _resultado(
        self,
        sucesso: bool,
        mensagem: str,
        modo: str,
        erro_extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Monta o dicionário de resultado padronizado."""
        erros = list(self.erros)
        if erro_extra:
            erros.append(erro_extra)

        return {
            "sucesso": sucesso,
            "mensagem": mensagem,
            "modo": modo,
            "total_lidas": self.total_lidas,
            "total_inseridas": self.total_inseridas,
            "total_atualizadas": self.total_atualizadas,
            "total_auto_geradas": self.total_auto_geradas,
            "total_preservadas_com_historico": self.total_preservadas_com_historico,
            "total_removidas_sem_uso": self.total_removidas_sem_uso,
            "total_erros": len(erros),
            "erros": erros,
        }
