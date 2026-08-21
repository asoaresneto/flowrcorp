# ETAPA 9 - Deploy e Ambiente

## Contêineres e Scripts de Deploy
Nesta varredura do repositório, **não foram encontrados** arquivos de containerização ou orquestração, tais como:
- `Dockerfile`
- `docker-compose.yml`
- Arquivos/Scripts de CI/CD (ex: `.github/workflows/`, `.gitlab-ci.yml`, ou scripts bash de automação).

## Arquitetura de Publicação Inferida
Embora o repositório não possua os manifestos de infraestrutura como código (IaC), através do arquivo principal `app.py` é possível inferir como a aplicação é executada em produção:

1. **Proxy Reverso (Nginx/Apache):** 
   A aplicação utiliza um middleware customizado (`ReverseProxyPrefixMiddleware`) para lidar com o cabeçalho `X-Script-Name`. Isso indica claramente que o Flask não é exposto diretamente à internet, mas sim executado atrás de um Proxy Reverso (muito provavelmente Nginx), que roteia as requisições (ex: de `/corp` para a raiz `/` do Gunicorn/uWSGI).

2. **Gerenciador de Processos (WSGI):**
   Como é padrão para aplicações Flask em produção, espera-se que a aplicação seja iniciada por um servidor WSGI (como `Gunicorn` ou `uWSGI`), operando na porta 5000 (conforme padrão lido em `config.py`), e o Nginx faça o *upstream* das chamadas.

3. **Banco de Dados (MongoDB):**
   A variável `MONGO_URI` aponta de forma singular para a instância de banco, gerida possivelmente na mesma rede local ou na cloud (MongoDB Atlas). Não há orquestração do banco documentada no projeto, sugerindo que o banco seja configurado e mantido em um ambiente *PaaS* externo ou via instalação manual (bare-metal/VM).

> [!WARNING]
> **Dívida Técnica de Infraestrutura:** 
> A ausência de um `Dockerfile` e do `docker-compose.yml` dificulta a reprodução local idêntica à produção por parte de novos desenvolvedores, bem como impede automações de deploy (*Continuous Deployment*).
