import asyncio
import json
import os
import struct
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


IMPRESSORA_IP = os.getenv("IMPRESSORA_IP", "").strip()
if not IMPRESSORA_IP:
    raise RuntimeError("Variável IMPRESSORA_IP não configurada. Crie o arquivo src/.env a partir do .env.example.")

IMPRESSORA_PORTA = int(os.getenv("IMPRESSORA_PORTA", "631"))
IMPRESSORA_CAMINHO = os.getenv("IMPRESSORA_CAMINHO", "/ipp/print").strip()
IMPRESSORA_USAR_HTTPS = env_bool("IMPRESSORA_USAR_HTTPS", False)
IPP_VERIFICAR_SSL = env_bool("IPP_VERIFICAR_SSL", True)

EXPOR_ENDERECO_IMPRESSORA = env_bool("EXPOR_ENDERECO_IMPRESSORA", False)
DEBUG_MODE = env_bool("DEBUG_MODE", False)

ARQUIVO_LOG = (BASE_DIR / os.getenv("ARQUIVO_LOG", "logs/impressoes.txt")).resolve()
ARQUIVO_LOG.parent.mkdir(parents=True, exist_ok=True)

MAX_REGISTROS = int(os.getenv("MAX_REGISTROS", "100"))
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
},


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


def parse_ipp_jobs(data: bytes) -> tuple[int, list[dict]]:
    if len(data) < 8:
        return -1, []

    status_code = struct.unpack_from(">H", data, 2)[0]
    pos = 8
    jobs = []
    current_job = None
    last_name = None
    collection_depth = 0

    while pos < len(data):
        tag = data[pos]
        pos += 1

        if tag == 0x03:
            break

        if tag < 0x10:
            if tag == 0x02:
                current_job = {}
                jobs.append(current_job)
            else:
                current_job = None
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

        if current_job is None or collection_depth > 0 or not last_name:
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

        if last_name not in current_job:
            current_job[last_name] = val

    return status_code, jobs


def normalizar_job(raw: dict) -> dict:
    paginas = (
        raw.get("job-media-sheets-completed")
        or raw.get("job-impressions-completed")
        or 0
    )
    tamanho_kb = raw.get("job-k-octets-processed") or raw.get("job-k-octets") or 0
    state_code = raw.get("job-state")

    return {
        "id_trabalho": str(raw.get("job-id", "")),
        "documento": str(raw.get("job-name", "")),
        "usuario": str(raw.get("job-originating-user-name", "")),
        "status": JOB_STATES.get(state_code, str(state_code or "")),
        "paginas_impressas": int(paginas),
        "tamanho_kb": int(tamanho_kb),
    }


def assinatura_job(job: dict) -> tuple:
    return (
        job.get("id_trabalho", ""),
        job.get("documento", ""),
        job.get("usuario", ""),
        job.get("paginas_impressas", 0),
    )


def carregar_arquivo():
    if not ARQUIVO_LOG.exists():
        return
    with ARQUIVO_LOG.open("r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if linha:
                try:
                    historico.append(json.loads(linha))
                except json.JSONDecodeError:
                    continue


def salvar_arquivo():
    conteudo = "\n".join(json.dumps(item, ensure_ascii=False) for item in historico)
    ARQUIVO_LOG.write_text(conteudo + ("\n" if conteudo else ""), encoding="utf-8")


async def consultar_ipp(client: httpx.AsyncClient, which_jobs: str = "completed") -> tuple[int, list[dict]]:
    payload = montar_ipp_get_jobs(which_jobs)
    response = await client.post(
        IPP_HTTP_URL,
        content=payload,
        headers={"Content-Type": "application/ipp"},
    )
    status_code, raw_jobs = parse_ipp_jobs(response.content)
    return status_code, [normalizar_job(j) for j in raw_jobs]


async def coletor_impressoes():
    impressora_online: bool | None = None

    async with httpx.AsyncClient(timeout=5.0, verify=IPP_VERIFICAR_SSL) as client:
        while True:
            try:
                _, jobs = await consultar_ipp(client, which_jobs="completed")
                if impressora_online is False:
                    print("[OK] Conexão com a impressora restabelecida.")
                impressora_online = True

                recentes = {assinatura_job(item) for item in list(historico)[-20:]}
                mudou = False

                for job in jobs:
                    if not job["id_trabalho"]:
                        continue
                    if assinatura_job(job) not in recentes:
                        job["registrado_em"] = datetime.now().isoformat(timespec="seconds")
                        historico.append(job)
                        recentes.add(assinatura_job(job))
                        mudou = True

                if mudou:
                    salvar_arquivo()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if impressora_online is not False:
                    print(f"[AVISO] Impressora sem resposta ({type(e).__name__}). Tentando novamente em segundo plano...")
                impressora_online = False

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
    except Exception as e:
        return {
            "online": False,
            "impressora": IDENTIFICADOR_PUBLICO,
            "erro": f"Impressora sem resposta ({type(e).__name__})",
            "quantidade_na_fila": 0,
            "trabalhos": [],
        }


if DEBUG_MODE:
    @app.get("/debug-ipp")
    async def debug_ipp():
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=IPP_VERIFICAR_SSL) as client:
                payload = montar_ipp_get_jobs("completed")
                r = await client.post(IPP_HTTP_URL, content=payload, headers={"Content-Type": "application/ipp"})
                status_ipp, raw_jobs = parse_ipp_jobs(r.content)
            return {
                "online": True,
                "http_status": r.status_code,
                "ipp_status": hex(status_ipp),
                "quantidade": len(raw_jobs),
                "amostra_bruta": raw_jobs[:5],
            }
        except Exception as e:
            return {
                "online": False,
                "erro": f"Falha ao conectar na impressora ({type(e).__name__})",
            }