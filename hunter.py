import hmac
import os
import re
from flask import Flask, Response, render_template_string, request, jsonify
import requests
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env (uso local)
load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

APP_USER = os.getenv("APP_USER", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

HEADERS_APOLLO = {
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "x-api-key": APOLLO_API_KEY,
}

def extrair_nome_e_cargo(titulo_google, origem="LinkedIn"):
    """
    Trata os títulos retornados pelo Google Search para extrair Nome e Cargo.
    Ajustado para lidar com o formato específico do LinkedIn e da Catho.
    """
    if not titulo_google:
        return "Candidato", "Não informado"
    
    # Limpa marcações de fim de página
    titulo_limpo = re.sub(r"\s*\|\s*(LinkedIn|Catho).*$", "", titulo_google, flags=re.IGNORECASE)
    titulo_limpo = re.sub(r"\s*-\s*(LinkedIn|Catho).*$", "", titulo_limpo, flags=re.IGNORECASE)
    
    if origem == "Catho":
        # Formato comum Catho: "Currículo de Gerente de RH em São Paulo, SP"
        titulo_limpo = re.sub(r"^(Currículo de|Perfil de|Candidato:?)\s*", "", titulo_limpo, flags=re.IGNORECASE)
        partes = re.split(r"\s*[\-\|–]\s*", titulo_limpo)
        first_part = partes[0].strip()
        
        if " em " in first_part.lower():
            cargo = re.split(r"\s+em\s+", first_part, flags=re.IGNORECASE)[0].strip()
            return "Candidato (Catho)", cargo
        return "Candidato (Catho)", first_part

    # Formato LinkedIn: "João Silva - Gerente de RH - Empresa"
    partes = re.split(r"\s*[\-\|–]\s*", titulo_limpo)
    nome = partes[0].strip() if len(partes) > 0 else "Candidato"
    cargo = partes[1].strip() if len(partes) > 1 else "Não informado"
    
    return nome, cargo

def enriquecer_contato_apollo(linkedin_url):
    """
    Busca E-mail e Telefone no Apollo via URL do LinkedIn.
    """
    if not APOLLO_API_KEY or not linkedin_url:
        return "Não disponível", "Não disponível"

    url_match = "https://api.apollo.io/v1/people/match"
    payload = {
        "api_key": APOLLO_API_KEY,
        "details_api_key": APOLLO_API_KEY,
        "linkedin_url": linkedin_url
    }

    try:
        res = requests.post(url_match, headers=HEADERS_APOLLO, json=payload, timeout=10)
        if res.status_code == 200:
            person = res.json().get("person") or {}
            email = person.get("email") or "Não disponível"
            
            telefone = "Não disponível"
            phones = person.get("phone_numbers") or []
            if phones and isinstance(phones, list) and len(phones) > 0:
                telefone = phones[0].get("sanitized_number") or phones[0].get("raw_number") or "Não disponível"
            elif person.get("sanitized_phone_number"):
                telefone = person.get("sanitized_phone_number")
                
            return email, telefone
    except Exception:
        pass

    return "Não disponível", "Não disponível"

def enriquecer_por_nome_apollo(nome, cargo):
    """
    Busca E-mail e Telefone no Apollo usando Nome + Cargo (para perfis da Catho).
    """
    if not APOLLO_API_KEY or not nome or "Candidato" in nome:
        return "Não disponível", "Não disponível"

    url_search = "https://api.apollo.io/v1/mixed_people/api_search"
    payload = {
        "api_key": APOLLO_API_KEY,
        "q_keywords": f'"{nome}" "{cargo}"',
        "page": 1,
        "per_page": 1
    }

    try:
        res = requests.post(url_search, headers=HEADERS_APOLLO, json=payload, timeout=10)
        if res.status_code == 200:
            pessoas = res.json().get("people") or []
            if pessoas:
                p = pessoas[0]
                email = p.get("email") or "Não disponível"
                telefone = "Não disponível"
                
                phones = p.get("phone_numbers") or []
                if phones and isinstance(phones, list) and len(phones) > 0:
                    telefone = phones[0].get("sanitized_number") or phones[0].get("raw_number") or "Não disponível"
                elif p.get("sanitized_phone_number"):
                    telefone = p.get("sanitized_phone_number")
                    
                return email, telefone
    except Exception:
        pass

    return "Não disponível", "Não disponível"

def buscar_candidatos_apify(cargos_raw, localizacao, limite=20):
    if not APIFY_TOKEN:
        return [], "ERRO CRÍTICO: Token do Apify ausente (APIFY_TOKEN). Verifique seu arquivo .env!"

    cargos_lista = [c.strip() for c in cargos_raw.split(",") if c.strip()]
    if not cargos_lista:
        return [], "Por favor, informe ao menos um cargo."

    # Gera buscas individuais para LinkedIn e Catho para cada cargo
    queries_lista = []
    for cargo in cargos_lista:
        queries_lista.append(
            f'site:linkedin.com/in/ "{cargo}" "{localizacao}"'
        )
        queries_lista.append(
            f'site:catho.com.br "{cargo}" "{localizacao}"'
        )

    query_final_str = "\n".join(queries_lista)
    
    apify_url = f"https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    
    payload = {
        "queries": query_final_str,
        "maxPagesPerQuery": 1,
        "resultsPerPage": max(10, min(limite, 50))
    }

    try:
        res = requests.post(apify_url, json=payload, timeout=90)
        
        if res.status_code not in (200, 201):
            return [], f"Apify retornou erro ({res.status_code}): {res.text}"

        dataset = res.json()
        if not dataset or not isinstance(dataset, list):
            return [], "Nenhum resultado retornado para os cargos informados."

        candidatos = []
        urls_vistas = set()

        for pagina_busca in dataset:
            organics = pagina_busca.get("organicResults") or []
            
            for item in organics:
                url_perfil = item.get("url", "")
                
                if url_perfil in urls_vistas:
                    continue

                origem = ""
                if "/in/" in url_perfil:
                    origem = "LinkedIn"
                elif "catho.com.br" in url_perfil:
                    origem = "Catho"
                else:
                    continue

                urls_vistas.add(url_perfil)

                titulo_item = item.get("title", "")
                nome, cargo_extraido = extrair_nome_e_cargo(titulo_item, origem=origem)
                
                cargo_final = cargo_extraido if cargo_extraido != "Não informado" else cargos_lista[0]

                # Enriquecimento de contato
                if origem == "LinkedIn":
                    email, telefone = enriquecer_contato_apollo(url_perfil)
                else:
                    email, telefone = enriquecer_por_nome_apollo(nome, cargo_final)

                candidatos.append({
                    "nome": nome,
                    "cargo": cargo_final,
                    "localizacao": localizacao,
                    "origem": origem,
                    "email": email,
                    "telefone": telefone,
                    "link": url_perfil
                })

                if len(candidatos) >= limite:
                    break

            if len(candidatos) >= limite:
                break

        if not candidatos:
            return [], f"Nenhum perfil encontrado para os cargos '{cargos_raw}' na região '{localizacao}'."

        return candidatos, None

    except Exception as e:
        return [], f"Falha ao conectar com Apify: {str(e)}"


app = Flask(__name__)

def _pedir_login():
    return Response(
        "Acesso restrito.", 401, {"WWW-Authenticate": 'Basic realm="Start RH - Candidate Search"'}
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
    <title>Start RH - Busca</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex flex-col items-center p-6">
    <div class="max-w-6xl w-full bg-gray-800 rounded-xl shadow-2xl border border-gray-700 p-8 mt-6">
        
        <div class="flex items-center justify-between border-b border-gray-700 pb-6 mb-6">
            <div>
                <h1 class="text-2xl font-bold text-amber-500">
                    Busca
                </h1>
                <p class="text-sm text-gray-400 mt-1">Pesquise múltiplos cargos no LinkedIn e na Catho em tempo real via Apify.</p>
            </div>
        </div>

        <div class="space-y-4">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">Cargos Desejados (separados por vírgula):</label>
                    <input type="text" id="cargoInput" placeholder="Ex: Gerente de RH, Recrutador, Tech Recruiter" 
                        class="w-full bg-gray-900 border border-gray-700 rounded-lg p-3 text-gray-100 focus:outline-none focus:border-amber-500 transition text-sm">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">Localização do Candidato:</label>
                    <input type="text" id="localizacaoInput" value="São Paulo" placeholder="Ex: São Paulo, Rio de Janeiro, Curitiba" 
                        class="w-full bg-gray-900 border border-gray-700 rounded-lg p-3 text-gray-100 focus:outline-none focus:border-amber-500 transition text-sm">
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium text-gray-300 mb-1">
                    Quantidade máxima de candidatos:
                </label>
                <input type="number" id="limiteInput" value="20" min="1" max="50"
                    class="w-32 bg-gray-900 border border-gray-700 rounded-lg p-2 text-gray-100 focus:outline-none focus:border-amber-500 transition font-mono text-sm">
            </div>

            <button id="btnProcessar" onclick="processarHunting()" 
                class="w-full bg-amber-500 hover:bg-amber-600 text-gray-950 font-bold py-3 px-6 rounded-lg transition flex items-center justify-center gap-2 shadow-lg shadow-amber-500/20">
                <i class="fa-solid fa-magnifying-glass"></i> Buscar
            </button>
        </div>

        <div id="loading" class="hidden my-8 text-center">
            <div class="inline-block animate-spin rounded-full h-10 w-10 border-4 border-amber-500 border-t-transparent"></div>
            <p class="text-gray-400 text-sm mt-3 animate-pulse">Varrendo LinkedIn & Catho e cruzando telefones/e-mails no Apollo...</p>
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
        async function processarHunting() {
            const cargo = document.getElementById('cargoInput').value.trim();
            const localizacao = document.getElementById('localizacaoInput').value.trim();
            const limite = parseInt(document.getElementById('limiteInput').value) || 20;
            
            if (!cargo) return alert('Por favor, informe ao menos um cargo.');

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
                        logList.innerHTML = `<p class="text-rose-400 p-3 bg-rose-500/10 rounded border border-rose-500/20"><i class="fa-solid fa-triangle-exclamation"></i> <b>Aviso:</b> ${data.erro}</p>`;
                    } else {
                        let tableHtml = `
                            <div class="overflow-x-auto">
                                <table class="w-full text-left border-collapse border border-gray-700">
                                    <thead>
                                        <tr class="bg-gray-900 text-amber-400 border-b border-gray-700 text-xs uppercase font-mono">
                                            <th class="p-3">Nome</th>
                                            <th class="p-3">Cargo</th>
                                            <th class="p-3">Localização</th>
                                            <th class="p-3">E-mail</th>
                                            <th class="p-3">Telefone</th>
                                            <th class="p-3 text-center">Fonte</th>
                                        </tr>
                                    </thead>
                                    <tbody class="divide-y divide-gray-700 bg-gray-800/50">`;

                        data.contatos.forEach(c => {
                            const badgeColor = c.origem === 'LinkedIn' 
                                ? 'bg-blue-600/20 text-blue-400 border-blue-500/30' 
                                : 'bg-rose-600/20 text-rose-400 border-rose-500/30';

                            tableHtml += `
                                <tr class="hover:bg-gray-800 transition">
                                    <td class="p-3 font-semibold text-gray-100">${c.nome}</td>
                                    <td class="p-3 text-gray-300">${c.cargo}</td>
                                    <td class="p-3 text-gray-400">${c.localizacao}</td>
                                    <td class="p-3 font-mono text-xs text-amber-300/90">${c.email}</td>
                                    <td class="p-3 font-mono text-xs text-emerald-400">${c.telefone}</td>
                                    <td class="p-3 text-center">
                                        <a href="${c.link}" target="_blank" class="inline-flex items-center gap-1 border px-2 py-1 rounded text-xs transition ${badgeColor}">
                                            ${c.origem} <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
                                        </a>
                                    </td>
                                </tr>`;
                        });

                        tableHtml += `</tbody></table></div>`;
                        logList.innerHTML = tableHtml;
                    }
                } else {
                    logList.innerHTML = `<p class="text-rose-500 p-3 bg-rose-500/10 rounded border border-rose-500/20"><i class="fa-solid fa-bomb"></i> Erro no servidor: ${data.message}</p>`;
                }
            } catch (err) {
                loading.classList.add('hidden');
                resultadoContainer.classList.remove('hidden');
                logList.innerHTML = `<p class="text-rose-500">Erro na requisição: ${err.message}</p>`;
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
    cargo = data.get("cargo", "").strip()
    localizacao = data.get("localizacao", "").strip()
    try:
        limite = max(1, min(int(data.get("limite", 20)), 50))
    except (TypeError, ValueError):
        limite = 20

    if not cargo:
        return jsonify({"status": "error", "message": "O campo 'cargo' é obrigatório."}), 400

    contatos, erro_apify = buscar_candidatos_apify(cargo, localizacao, limite=limite)
    
    return jsonify({
        "status": "success",
        "cargo": cargo,
        "localizacao": localizacao,
        "contatos": contatos,
        "erro": erro_apify
    })

if __name__ == "__main__":
    porta = int(os.getenv("PORT", "5000"))
    print("\n--- SERVIDOR LOCAL START RH INICIADO ---")
    print(f"Acesse no navegador: http://localhost:{porta}\n")
    app.run(host="127.0.0.1", port=porta, debug=False)
