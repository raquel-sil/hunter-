import hmac
import os
import re
import unicodedata

from flask import Flask, Response, render_template_string, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APP_USER = os.getenv("APP_USER", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

APIFY_URL = "https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items"

# Quantas páginas do Google buscar por consulta (cada página traz ~10 resultados)
MAX_PAGINAS = 30

# Sinais de Open to Work (já normalizados: sem acento, minúsculo, sem #)
SINAIS_OPEN_TO_WORK = ("open to work", "opentowork", "buscando oportunidade")


def normalizar(texto):
    """Remove acentos e deixa minúsculo: 'São Paulo' -> 'sao paulo'."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).lower().strip()


def termos_localizacao(localizacao):
    """'São Paulo, Rio de Janeiro' -> ['sao paulo', 'rio de janeiro']"""
    return [normalizar(p) for p in (localizacao or "").split(",") if normalizar(p)]


def texto_do_resultado(item):
    """Título + snippet do resultado, normalizado e sem '#'."""
    bruto = f"{item.get('title', '')} {item.get('description', '')}"
    return normalizar(bruto).replace("#", "")


def e_open_to_work(texto):
    return any(sinal in texto for sinal in SINAIS_OPEN_TO_WORK)


def bate_localizacao_exata(texto, termos):
    """
    Aceita só quando a cidade aparece como localização atual do perfil,
    e não como experiência passada ou formação.
    """
    if not termos:
        return True

    palavras_passado = r"\b(ex|anterior|anteriormente|antes|previamente|trabalhou|formado|formou|estudou|desde)\b"

    for t in termos:
        padrao = (
            r"(?:^|[·|\-–•,]|\bem\b)\s*"                          # antes: início, separador ou "em"
            + re.escape(t)
            + r"(?=\s*(?:,|·|\||-|–|•|\.|$|\bbrasil\b|\bbrazil\b))"  # depois: separador, fim ou país
        )
        for m in re.finditer(padrao, texto):
            contexto_antes = texto[max(0, m.start() - 30):m.start()]
            if re.search(palavras_passado, contexto_antes):
                continue
            return True

    return False


def extrair_nome_de_titulo(titulo_google):
    """Exemplo: 'João Silva - Gerente de RH - Empresa | LinkedIn' -> 'João Silva'"""
    if not titulo_google:
        return "Candidato"
    limpo = re.sub(r"\s*\|\s*LinkedIn.*$", "", titulo_google, flags=re.IGNORECASE)
    limpo = re.sub(r"\s*-\s*LinkedIn.*$", "", limpo, flags=re.IGNORECASE)
    partes = re.split(r"\s*[\-\|–]\s*", limpo)
    return partes[0].strip() if partes and partes[0].strip() else "Candidato"


def buscar_candidatos(cargo, localizacao, limite=20):
    if not APIFY_TOKEN:
        return [], "ERRO CRÍTICO: Token do Apify ausente (APIFY_TOKEN). Verifique seu arquivo .env!"

    termos = termos_localizacao(localizacao)

    # Query: perfis do LinkedIn + cargo + localização + sinal de Open to Work
    clausula_loc = ""
    if termos:
        partes = [p.strip() for p in localizacao.split(",") if p.strip()]
        clausula_loc = " (" + " OR ".join(f'"{p}"' for p in partes) + ")"

    query = (
        f'site:linkedin.com/in/ "{cargo}"{clausula_loc} '
        f'("open to work" OR "#opentowork" OR "buscando oportunidade")'
    )

    # Busca em várias páginas, porque os filtros estritos descartam bastante coisa
    paginas = MAX_PAGINAS

    payload = {
        "queries": query,
        "maxPagesPerQuery": paginas,
        "resultsPerPage": 10,
        "countryCode": "br",
        "languageCode": "pt-BR",
    }

    try:
        res = requests.post(APIFY_URL, params={"token": APIFY_TOKEN}, json=payload, timeout=300)
    except Exception as e:
        return [], f"Falha ao conectar com Apify: {e}"

    if res.status_code not in (200, 201):
        return [], f"Apify retornou erro ({res.status_code}): {res.text}"

    dataset = res.json()
    if not dataset or not isinstance(dataset, list):
        return [], f"Nenhum resultado retornado pelo Apify para '{cargo}'."

    organics = [o for item in dataset for o in (item.get("organicResults") or [])]

    contatos, vistos = [], set()
    for o in organics:
        url_linkedin = o.get("url", "")
        if "linkedin.com/in/" not in url_linkedin or url_linkedin in vistos:
            continue

        texto = texto_do_resultado(o)
        if not e_open_to_work(texto):
            continue
        if not bate_localizacao_exata(texto, termos):
            continue

        vistos.add(url_linkedin)
        contatos.append({
            "nome": extrair_nome_de_titulo(o.get("title", "")),
            "cargo": cargo,
            "localizacao": localizacao,
            "linkedin": url_linkedin,
        })
        if len(contatos) >= limite:
            break

    if not contatos:
        msg = f"Nenhum perfil 'Open to Work' encontrado para '{cargo}'"
        if localizacao:
            msg += f" em '{localizacao}'"
        return [], msg + "."

    return contatos, None


app = Flask(__name__)


def _pedir_login():
    return Response(
        "Acesso restrito.", 401,
        {"WWW-Authenticate": 'Basic realm="Start RH - Apify Candidate Search"'}
    )


@app.before_request
def exigir_login():
    if not APP_USER or not APP_PASSWORD:
        return Response("Servidor sem APP_USER/APP_PASSWORD configurados.", 503)
    auth = request.authorization
    if not auth:
        return _pedir_login()
    user_ok = hmac.compare_digest(auth.username or "", APP_USER)
    pass_ok = hmac.compare_digest(auth.password or "", APP_PASSWORD)
    if not (user_ok and pass_ok):
        return _pedir_login()


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Start RH - Pesquisa Apify (Open to Work)</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex flex-col items-center p-6">
    <div class="max-w-5xl w-full bg-gray-800 rounded-xl shadow-2xl border border-gray-700 p-8 mt-6">

        <div class="border-b border-gray-700 pb-6 mb-6">
            <h1 class="text-2xl font-bold text-amber-500 flex items-center gap-2">
                <i class="fa-solid fa-spider"></i> Busca Apify: Candidatos "Open To Work"
            </h1>
            <p class="text-sm text-gray-400 mt-1">Perfis do LinkedIn com Open to Work na localização informada.</p>
        </div>

        <div class="space-y-4">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">Cargo do Candidato:</label>
                    <input type="text" id="cargoInput" placeholder="Ex: Gerente de RH, Desenvolvedor Python"
                        class="w-full bg-gray-900 border border-gray-700 rounded-lg p-3 text-gray-100 focus:outline-none focus:border-amber-500 transition text-sm">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">Localização do Candidato:</label>
                    <input type="text" id="localizacaoInput" value="São Paulo" placeholder="Ex: São Paulo, Rio de Janeiro"
                        class="w-full bg-gray-900 border border-gray-700 rounded-lg p-3 text-gray-100 focus:outline-none focus:border-amber-500 transition text-sm">
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium text-gray-300 mb-1">Quantidade máxima de candidatos:</label>
                <input type="number" id="limiteInput" value="20" min="1" max="50"
                    class="w-32 bg-gray-900 border border-gray-700 rounded-lg p-2 text-gray-100 focus:outline-none focus:border-amber-500 transition font-mono text-sm">
            </div>

            <button id="btnProcessar" onclick="processarHunting()"
                class="w-full bg-amber-500 hover:bg-amber-600 text-gray-950 font-bold py-3 px-6 rounded-lg transition flex items-center justify-center gap-2 shadow-lg shadow-amber-500/20">
                <i class="fa-solid fa-magnifying-glass"></i> Buscar via Apify
            </button>
        </div>

        <div id="loading" class="hidden my-8 text-center">
            <div class="inline-block animate-spin rounded-full h-10 w-10 border-4 border-amber-500 border-t-transparent"></div>
            <p class="text-gray-400 text-sm mt-3 animate-pulse">Executando busca no Apify...</p>
        </div>

        <div id="resultadoContainer" class="hidden mt-8 border-t border-gray-700 pt-6">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-semibold text-gray-200 flex items-center gap-2">
                    <i class="fa-solid fa-list text-amber-500"></i> Perfis Encontrados:
                </h2>
                <span id="totalBadge" class="bg-amber-500/10 text-amber-400 text-xs px-3 py-1 rounded-full border border-amber-500/20 font-mono"></span>
            </div>
            <div id="logList" class="space-y-3 font-sans text-sm"></div>
        </div>
    </div>

    <script>
        function esc(valor) {
            const d = document.createElement('div');
            d.textContent = valor ?? '';
            return d.innerHTML;
        }

        async function processarHunting() {
            const cargo = document.getElementById('cargoInput').value.trim();
            const localizacao = document.getElementById('localizacaoInput').value.trim();
            const limite = parseInt(document.getElementById('limiteInput').value) || 20;

            if (!cargo) return alert('Por favor, informe o cargo.');

            const btn = document.getElementById('btnProcessar');
            const loading = document.getElementById('loading');
            const resultadoContainer = document.getElementById('resultadoContainer');
            const logList = document.getElementById('logList');
            const totalBadge = document.getElementById('totalBadge');

            btn.disabled = true;
            btn.classList.add('opacity-50', 'cursor-not-allowed');
            loading.classList.remove('hidden');
            resultadoContainer.classList.add('hidden');
            logList.innerHTML = '';

            try {
                const response = await fetch('/api/buscar_candidatos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cargo: cargo, localizacao: localizacao, limite: limite })
                });
                const data = await response.json();

                loading.classList.add('hidden');
                resultadoContainer.classList.remove('hidden');

                if (data.status === 'success') {
                    totalBadge.innerText = `${data.contatos.length} Candidato(s)`;

                    if (data.contatos.length === 0) {
                        logList.innerHTML = `<p class="text-rose-400 p-3 bg-rose-500/10 rounded border border-rose-500/20"><i class="fa-solid fa-triangle-exclamation"></i> <b>Aviso:</b> ${esc(data.erro)}</p>`;
                    } else {
                        let tableHtml = `
                            <div class="overflow-x-auto">
                                <table class="w-full text-left border-collapse border border-gray-700">
                                    <thead>
                                        <tr class="bg-gray-900 text-amber-400 border-b border-gray-700 text-xs uppercase font-mono">
                                            <th class="p-3">Nome</th>
                                            <th class="p-3">Cargo Buscado</th>
                                            <th class="p-3">Localização</th>
                                            <th class="p-3 text-center">LinkedIn</th>
                                        </tr>
                                    </thead>
                                    <tbody class="divide-y divide-gray-700 bg-gray-800/50">`;

                        data.contatos.forEach(c => {
                            tableHtml += `
                                <tr class="hover:bg-gray-800 transition">
                                    <td class="p-3 font-semibold text-gray-100">${esc(c.nome)}</td>
                                    <td class="p-3 text-gray-300">${esc(c.cargo)}</td>
                                    <td class="p-3 text-gray-300">${esc(c.localizacao)}</td>
                                    <td class="p-3 text-center">
                                        <a href="${esc(c.linkedin)}" target="_blank" rel="noopener" class="inline-flex items-center gap-1 bg-blue-600/20 hover:bg-blue-600/40 text-blue-400 border border-blue-500/30 px-3 py-1 rounded-md text-xs transition"><i class="fa-brands fa-linkedin"></i> Ver Perfil</a>
                                    </td>
                                </tr>`;
                        });

                        tableHtml += `</tbody></table></div>`;
                        logList.innerHTML = tableHtml;
                    }
                } else {
                    logList.innerHTML = `<p class="text-rose-500 p-3 bg-rose-500/10 rounded border border-rose-500/20"><i class="fa-solid fa-bomb"></i> Erro no servidor: ${esc(data.message)}</p>`;
                }
            } catch (err) {
                loading.classList.add('hidden');
                resultadoContainer.classList.remove('hidden');
                logList.innerHTML = `<p class="text-rose-500">Erro na requisição: ${esc(err.message)}</p>`;
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        }
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/buscar_candidatos", methods=["POST"])
def api_buscar_candidatos():
    data = request.json or {}
    cargo = (data.get("cargo") or "").strip()
    localizacao = (data.get("localizacao") or "").strip()
    try:
        limite = max(1, min(int(data.get("limite", 20)), 50))
    except (TypeError, ValueError):
        limite = 20

    if not cargo:
        return jsonify({"status": "error", "message": "O campo 'cargo' é obrigatório."}), 400

    contatos, erro = buscar_candidatos(cargo, localizacao, limite=limite)

    return jsonify({
        "status": "success",
        "cargo": cargo,
        "localizacao": localizacao,
        "contatos": contatos,
        "erro": erro,
    })


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "5000"))
    print("\n--- SERVIDOR LOCAL START RH (APIFY CANDIDATE SEARCH) INICIADO ---")
    print(f"Acesse no navegador: http://localhost:{porta}\n")
    app.run(host="127.0.0.1", port=porta, debug=False)
