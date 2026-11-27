# Monitor de Impressões de Rede — Laboratório de Informática (IB – Unicamp)

Sistema leve de monitoramento e registro de impressões em rede desenvolvido para apoiar a gestão e o acompanhamento operacional do Laboratório de Informática do Instituto de Biologia (IB) da Unicamp.

A aplicação consulta diretamente a impressora de rede via protocolo **IPP (Internet Printing Protocol)**, captura os metadados de cada trabalho concluído antes que o buffer interno do equipamento os descarte e exibe os registros em uma interface web local.

---

## Problema Resolvido

Impressoras de rede geralmente mantêm um histórico interno muito curto (frequentemente apenas o último trabalho impresso) e apagam esses dados quando reiniciadas. Além disso, estações com Windows 10/11 configuradas com drivers modernos enviam trabalhos via **IPP (porta 631)**, que não são contabilizados em consultas tradicionais via WSD.

Esta solução resolve ambos os cenários sem exigir servidores dedicados ou bancos de dados complexos:
- Um coletor assíncrono em segundo plano consulta a impressora continuamente via IPP.
- Novos trabalhos são gravados localmente em formato estruturado com rotação automática de registros para evitar consumo excessivo de disco.
- Um painel web permite acompanhar o volume de páginas, identificar usuários e documentos e verificar o status de disponibilidade da impressora.

---

## Funcionalidades

- **Coleta Automática em Segundo Plano:** Consulta periódica via IPP (`Get-Jobs`) compatível com estações Windows e Linux da rede.
- **Persistência Leve e Rotativa:** Armazena os registros em arquivo texto (JSON Lines) com limite configurável (`MAX_REGISTROS`), preservando o histórico entre reinicializações da aplicação ou da impressora.
- **Painel Web Integrado:**
  - Contadores de registros exibidos, total de páginas impressas e fila ativa em tempo real.
  - Filtro instantâneo por nome do documento ou usuário.
  - Indicador visual de status da impressora (`Online` / `Offline`).
  - Atualização automática a cada 5 segundos (com opção de pausa).
- **Configuração Isolada por Ambiente:** Endereços IP, portas e parâmetros operacionais gerenciados via `.env`, evitando exposição de dados da infraestrutura interna no código-fonte.

---

## Tecnologias Utilizadas

- **Backend:** Python 3.10+, FastAPI, Uvicorn, HTTPX (cliente HTTP assíncrono), Python-Dotenv
- **Protocolo de Rede:** IPP 1.1 (RFC 8011) sobre HTTP/HTTPS
- **Frontend:** HTML5, CSS3 e JavaScript puro (servido diretamente pelo FastAPI, sem dependências externas)

---

## Estrutura do Projeto

```text
projetoImpressora/
├── src/
│   ├── logs/                # Diretório onde o histórico local é gravado (ignorado pelo Git)
│   ├── .env                 # Configurações locais da impressora (ignorado pelo Git)
│   ├── .env.example         # Modelo de variáveis de ambiente para configuração
│   ├── .gitignore           # Regras de exclusão de arquivos sensíveis e temporários
│   ├── index.html           # Interface web do monitor
│   └── main.py              # API FastAPI e coletor assíncrono IPP
├── requirements.txt         # Dependências Python do projeto
└── README.md                # Documentação do sistema
