import asyncio
import json
import os
import struct
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    env_path = BASE_DIR / ".env"
    load_dotenv(env_path if env_path.exists() else None)
except ImportError:
    pass


def env_bool(nome: str, padrao: bool = False) -> bool:
    return os.getenv(nome, str(padrao)).strip().lower() in ("1", "true", "sim", "yes")


def para_int(valor, padrao: int = 0) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


IMPRESSORA_IP = os.getenv("IMPRESSORA_IP", "").strip()
if not IMPRESSORA_IP:
    raise RuntimeError(
        "Variável IMPRESSORA_IP não configurada. Crie o arquivo src/.env a partir do .env.example."
    )

IMPRESSORA_PORTA = para_int(os.getenv("IMPRESSORA_PORTA", "631"), 631)
IMPRESSORA_CAMINHO = os.getenv("IMPRESSORA_CAMINHO", "/ipp/print").strip() or "/ipp/print"
if not IMPRESSORA_CAMINHO.startswith("/"):
    IMPRESSORA_CAMINHO = f"/{IMPRESSORA_CAMINHO}"

IMPRESSORA_USAR_HTTPS = env_bool("IMPRESSORA_USAR_HTTPS", False)
IPP_VERIFICAR_SSL = env_bool("IPP_VERIFICAR_SSL", True)

EXPOR_ENDERECO_IMPRESSORA = env_bool("EXPOR_ENDERECO_IMPRESSORA", False)
DEBUG_MODE = env_bool("DEBUG_MODE", False)

ARQUIVO_LOG = (BASE_DIR / os.getenv("ARQUIVO_LOG", "logs/impressoes.txt")).resolve()
ARQUIVO_LOG.parent.mkdir(parents=True, exist_ok=True)

MAX_REGISTROS = max(1, para_int(os.getenv("MAX_REGISTROS", "100"), 100))
INTERVALO_SEGUNDOS = float(os.getenv("INTERVALO_SEGUNDOS", "2"))

ESQUEMA_HTTP = "https" if IMPRESSORA_USAR_HTTPS else "http"
IPP_HTTP_URL = f"{ESQUEMA_HTTP}://{IMPRESSORA_IP}:{IMPRESSORA_PORTA}{IMPRESSORA_CAMINHO}"
IPP_PRINTER_URI = f"ipp://{IMPRESSORA_IP}:{IMPRESSORA_PORTA}{IMPRESSORA_CAMINHO}"
IDENTIFICADOR_PUBLICO = IPP_HTTP_URL if EXPOR_ENDERECO_IMPRESSORA else "Impressora de Rede"

JOB_STATES = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}

historico: deque[dict] = deque(maxlen=MAX_REGISTROS)


def montar_ipp_get_jobs(which_jobs: str = "completed") -> bytes:
    def attr(tag: int, nome: str, valor: str) -> bytes:
        n, v = nome.encode("utf-8"), valor.encode("utf-8")
        return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v

    return (
        b"\x01\x01"
        b"\x00\x0a"
        b"\x00\x00\x00\x01"
        b"\x01"
        + attr(0x47, "attributes-charset", "utf-8")
        + attr(0x48, "attributes-natural-language", "en")
        + attr(0x45, "printer-uri", IPP_PRINTER_URI)
        + attr(0x44, "which-jobs", which_jobs)
        + attr(0x44, "requested-attributes", "all")
        + b"\x03"
    )


def montar_ipp_get_printer_attributes() -> bytes:
    def attr(tag: int, nome: str, valor: str) -> bytes:
        n, v = nome.encode("utf-8"), valor.encode("utf-8")
        return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v

    return (
        b"\x01\x01"
        b"\x00\x0b"
        b"\x00\x00\x00\x02"
        b"\x01"
        + attr(0x47, "attributes-charset", "utf-8")
        + attr(0x48, "attributes-natural-language", "en")
        + attr(0x45, "printer-uri", IPP_PRINTER_URI)
        + attr(0x44, "requested-attributes", "all")
        + b"\x03"
    )


def parse_ipp_grupos(data: bytes, target_tag: int = 0x02) -> tuple[int, list[dict]]:
    if len(data) < 8:
        return -1, []

    status_code = struct.unpack_from(">H", data, 2)[0]
    pos = 8
    grupos = []
    current_group = None
    last_name = None
    collection_depth = 0

    while pos < len(data):
        tag = data[pos]
        pos += 1

        if tag == 0x03:
            break

        if tag < 0x10:
            if tag == target_tag:
                current_group = {}
                grupos.append(current_group)
            else:
                current_group = None
            last_name = None
            collection_depth = 0
            continue

        if pos + 2 > len(data):
            break
        name_len = struct.unpack_from(">H", data, pos)[0]
        pos += 2
        name = data[pos : pos + name_len].decode("utf-8", errors="ignore")
        pos += name_len

        if pos + 2 > len(data):
            break
        val_len = struct.unpack_from(">H", data, pos)[0]
        pos += 2
        raw_val = data[pos : pos + val_len]
        pos += val_len

        if name:
            last_name = name

        if tag == 0x34:
            collection_depth += 1
            continue
        if tag == 0x37:
            collection_depth = max(0, collection_depth - 1)
            continue

        if current_group is None or collection_depth > 0 or not last_name:
            continue

        if tag in (0x21, 0x23) and val_len == 4:
            val = struct.unpack(">i", raw_val)[0]
        elif tag == 0x22 and val_len == 1:
            val = raw_val[0] != 0
        elif tag in (0x35, 0x36) and val_len >= 4:
            lang_len = struct.unpack_from(">H", raw_val, 0)[0]
            off = 2 + lang_len
            if off + 2 <= val_len:
                txt_len = struct.unpack_from(">H", raw_val, off)[0]
                val = raw_val[off + 2 : off + 2 + txt_len].decode("utf-8", errors="replace")
            else:
                val = raw_val.decode("utf-8", errors="replace")
        elif tag >= 0x41:
            val = raw_val.decode("utf-8", errors="replace")
        else:
            continue

        if last_name not in current_group:
            current_group[last_name] = val

    return status_code, grupos


def normalizar_job(raw: dict) -> dict:
    folhas = (
        raw.get("job-media-sheets-completed")
        or raw.get("job-impressions-completed")
        or 0
    )
    impressoes = (
        raw.get("job-impressions-completed")
        or raw.get("job-media-sheets-completed")
        or 0
    )
    tamanho_kb = raw.get("job-k-octets-processed") or raw.get("job-k-octets") or 0
    state_code = raw.get("job-state")

    return {
        "id_trabalho": str(raw.get("job-id", "")),
        "documento": str(raw.get("job-name", "")),
        "usuario": str(raw.get("job-originating-user-name", "")),
        "status": JOB_STATES.get(state_code, str(state_code or "")),
        "paginas_impressas": para_int(folhas),
        "impressoes_totais": para_int(impressoes),
        "tamanho_kb": para_int(tamanho_kb),
        "tempo_conclusao": para_int(raw.get("time-at-completed", 0)),
    }


def assinatura_job(job: dict) -> tuple:
    return (
        job.get("id_trabalho", ""),
        job.get("documento", ""),
        job.get("usuario", ""),
        job.get("paginas_impressas", 0),
        job.get("tempo_conclusao", 0),
    )


def carregar_arquivo():
    if not ARQUIVO_LOG.exists():
        return
    with ARQUIVO_LOG.open("r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            try:
                dado = json.loads(linha)
            except json.JSONDecodeError:
                continue

            if isinstance(dado, dict):
                historico.append(dado)
            elif isinstance(dado, list):
                for item in dado:
                    if isinstance(item, dict):
                        historico.append(item)


def salvar_arquivo():
    conteudo = "\n".join(json.dumps(item, ensure_ascii=False) for item in historico)
    ARQUIVO_LOG.write_text(conteudo + ("\n" if conteudo else ""), encoding="utf-8")


async def consultar_ipp(
    client: httpx.AsyncClient, which_jobs: str = "completed"
) -> tuple[int, list[dict]]:
    payload = montar_ipp_get_jobs(which_jobs)
    response = await client.post(
        IPP_HTTP_URL,
        content=payload,
        headers={
            "Content-Type": "application/ipp",
            "Connection": "close",
        },
    )
    response.raise_for_status()
    status_code, raw_jobs = parse_ipp_grupos(response.content, target_tag=0x02)
    return status_code, [normalizar_job(j) for j in raw_jobs]


async def consultar_contador_paginas(client: httpx.AsyncClient) -> tuple[int, dict]:
    payload = montar_ipp_get_printer_attributes()
    response = await client.post(
        IPP_HTTP_URL,
        content=payload,
        headers={
            "Content-Type": "application/ipp",
            "Connection": "close",
        },
    )
    response.raise_for_status()
    _, grupos = parse_ipp_grupos(response.content, target_tag=0x04)
    attrs = grupos[0] if grupos else {}
    total = para_int(
        attrs.get("printer-impressions-completed")
        or attrs.get("printer-media-sheets-completed")
        or 0
    )
    return total, attrs


async def coletor_impressoes():
    impressora_online: bool | None = None
    ultimo_contador_paginas: int | None = None

    async with httpx.AsyncClient(timeout=5.0, verify=IPP_VERIFICAR_SSL) as client:
        while True:
            try:
                _, jobs = await consultar_ipp(client, which_jobs="completed")
                total_paginas_motor, _ = await consultar_contador_paginas(client)

                if impressora_online is False:
                    print("[OK] Conexão com a impressora restabelecida.")
                impressora_online = True

                recentes = {
                    assinatura_job(item)
                    for item in list(historico)[-30:]
                    if isinstance(item, dict)
                }
                mudou = False
                impressoes_novos_ipp = 0

                jobs_ordenados = sorted(
                    jobs,
                    key=lambda j: (
                        j.get("tempo_conclusao", 0),
                        para_int(j.get("id_trabalho")),
                    ),
                )

                for job in jobs_ordenados:
                    if not job["id_trabalho"]:
                        continue
                    if assinatura_job(job) not in recentes:
                        job["registrado_em"] = datetime.now().isoformat(timespec="seconds")
                        historico.append(job)
                        recentes.add(assinatura_job(job))
                        impressoes_novos_ipp += job.get("impressoes_totais", 0)
                        mudou = True
                        print(
                            f"[NOVO IPP] #{job['id_trabalho']} | {job['documento']} "
                            f"({job['paginas_impressas']} fls)"
                        )

                if total_paginas_motor > 0:
                    if ultimo_contador_paginas is None:
                        ultimo_contador_paginas = total_paginas_motor
                    elif total_paginas_motor > ultimo_contador_paginas:
                        delta = total_paginas_motor - ultimo_contador_paginas
                        sobra = delta - impressoes_novos_ipp

                        if sobra > 0:
                            _, fila_ativa = await consultar_ipp(client, which_jobs="not-completed")
                            if len(fila_ativa) == 0:
                                registro_raw = {
                                    "id_trabalho": f"raw-{total_paginas_motor}",
                                    "documento": "Impressão via Porta TCP/9100 (sem metadados IPP)",
                                    "usuario": "Estação (RAW / IP:Porta)",
                                    "status": "completed",
                                    "paginas_impressas": sobra,
                                    "impressoes_totais": sobra,
                                    "tamanho_kb": 0,
                                    "tempo_conclusao": 0,
                                    "registrado_em": datetime.now().isoformat(timespec="seconds"),
                                }
                                historico.append(registro_raw)
                                mudou = True
                                ultimo_contador_paginas = total_paginas_motor
                                print(f"[NOVO RAW] Impressão não-IPP detectada ({sobra} pág.)")
                        else:
                            ultimo_contador_paginas = total_paginas_motor

                if mudou:
                    salvar_arquivo()
            except asyncio.CancelledError:
                break
            except httpx.HTTPError as e:
                if impressora_online is not False:
                    print(
                        f"[AVISO] Impressora sem resposta ({type(e).__name__}: {e}). "
                        "Tentando novamente em segundo plano..."
                    )
                impressora_online = False
            except Exception as e:
                print(f"[ERRO INTERNO] {type(e).__name__}: {e}")
                traceback.print_exc()

            await asyncio.sleep(INTERVALO_SEGUNDOS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    carregar_arquivo()
    task = asyncio.create_task(coletor_impressoes())
    yield
    task.cancel()


app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if DEBUG_MODE else None,
    redoc_url="/redoc" if DEBUG_MODE else None,
)


@app.get("/", response_class=HTMLResponse)
def pagina_inicial():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/historico-rede")
def obter_historico(limite: int = Query(default=10, ge=1, le=MAX_REGISTROS)):
    trabalhos = list(reversed(historico))[:limite]
    return {
        "impressora": IDENTIFICADOR_PUBLICO,
        "quantidade": len(trabalhos),
        "trabalhos": trabalhos,
    }


@app.get("/fila-rede")
async def obter_fila_ativa():
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=IPP_VERIFICAR_SSL) as client:
            status_ipp, trabalhos = await consultar_ipp(client, which_jobs="not-completed")
        return {
            "online": True,
            "impressora": IDENTIFICADOR_PUBLICO,
            "status_ipp": hex(status_ipp),
            "quantidade_na_fila": len(trabalhos),
            "trabalhos": trabalhos,
        }
    except httpx.HTTPError as e:
        return {
            "online": False,
            "impressora": IDENTIFICADOR_PUBLICO,
            "erro": f"Impressora sem resposta ({type(e).__name__})",
            "quantidade_na_fila": 0,
            "trabalhos": [],
        }
    except Exception as e:
        print(f"[ERRO INTERNO /fila-rede] {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "online": False,
            "impressora": IDENTIFICADOR_PUBLICO,
            "erro": f"Erro interno na API ({type(e).__name__})",
            "quantidade_na_fila": 0,
            "trabalhos": [],
        }


if DEBUG_MODE:

    @app.get("/debug-ipp")
    async def debug_ipp():
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=IPP_VERIFICAR_SSL) as client:
                status_ipp, jobs = await consultar_ipp(client, which_jobs="completed")
                contador_paginas, attrs_impressora = await consultar_contador_paginas(client)
            return {
                "online": True,
                "ipp_status": hex(status_ipp),
                "contador_paginas_motor": contador_paginas,
                "quantidade_jobs_ipp": len(jobs),
                "jobs_ipp": jobs[:5],
                "atributos_contador": {
                    k: v
                    for k, v in attrs_impressora.items()
                    if any(t in k for t in ("impression", "sheet", "page", "count", "state"))
                },
            }
        except httpx.HTTPError as e:
            return {
                "online": False,
                "erro": f"Falha ao conectar na impressora ({type(e).__name__})",
            }
        except Exception as e:
            traceback.print_exc()
            return {
                "online": False,
                "erro": f"Erro interno ({type(e).__name__}: {e})",
            }
