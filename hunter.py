import hmac
import os
import re
from flask import Flask, Response, render_template_string, request, jsonify
import requests
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env
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

def extrair_nome_de_titulo(titulo_google):
    """
    Limpa o título retornado pelo Google Search para extrair o Nome da pessoa.
    Exemplo: "João Silva - Gerente de RH - Empresa | LinkedIn" -> "João Silva"
    """
    if not titulo_google:
        return "Candidato"
    
    # Remove marcas padrão do LinkedIn do final
    titulo_limpo = re.sub(r"\s*\|\s*LinkedIn.*$", "", titulo_google, flags=re.IGNORECASE)
    titulo_limpo = re.sub(r"\s*-\s*LinkedIn.*$", "", titulo_limpo, flags=re.IGNORECASE)
    
    # Pega a primeira parte antes do hífen ou travessão (normalmente onde fica o nome)
    partes = re.split(r"\s*[\-\|–]\s*", titulo_limpo)
    if partes:
        return partes[0].strip()
    return titulo_limpo.strip()

def enriquecer_contato_apollo(linkedin_url):
    """
    Usa o Apollo para buscar o e-mail e telefone a partir da URL do LinkedIn.
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
            
            # Busca de telefone
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

def buscar_candidatos_apify(cargo, localizacao, limite=20):
    if not APIFY_TOKEN:
        return [], "ERRO CRÍTICO: Token do Apify ausente (APIFY_TOKEN). Verifique seu arquivo .env!"

    # Query X-Ray direcionada para perfis do LinkedIn Open To Work
    query_search = f'site:linkedin.com/in/ "{cargo}" "{localizacao}" ("open to work" OR "#opentowork" OR "buscando oportunidade")'
    
    # Endpoint síncrono do Google Search Scraper no Apify
    apify_url = f"https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    
    payload = {
        "queries": query_search,
        "maxPagesPerQuery": 1,
        "resultsPerPage": min(limite, 50)
    }

    try:
        res = requests.post(apify_url, json=payload, timeout=60)
        
        if res.status_code not in (200, 201):
            return [], f"Apify retornou erro ({res.status_code}): {res.text}"

        dataset = res.json()
        if not dataset or not isinstance(dataset, list):
            return [], f"Nenhum resultado retornado pelo Apify para '{cargo}' em '{localizacao}'."

        organics = dataset[0].get("organicResults") or []
        
        if not organics:
            return [], f"Nenhum perfil 'Open to Work' encontrado no LinkedIn para '{cargo}' em '{localizacao}'."

        candidatos = []
        for item in organics:
            url_linkedin = item.get("url", "")
            
            # Garante que é um link de perfil pessoal do LinkedIn
            if "/in/" not in url_linkedin:
                continue

            titulo_item = item.get("title", "")
            nome = extrair_nome_de_titulo(titulo_item)
            
            # Tenta enriquecer com E-mail e Telefone via Apollo usando a URL do LinkedIn
            email, telefone = enriquecer_contato_apollo(url_linkedin)

            candidatos.append({
                "nome": nome,
                "cargo": cargo,
                "localizacao": localizacao,
                "email": email,
                "telefone": telefone,
                "linkedin": url_linkedin
            })

            if len(candidatos) >= limite:
                break

        return candidatos, None

    except Exception as e:
        return [], f"Falha ao conectar com Apify: {str(e)}"


app = Flask(__name__)

def _pedir_login():
    return Response(
        "Acesso restrito.", 401, {"WWW-Authenticate": 'Basic realm="Start RH - Apify Candidate Search"'}
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
        
        <div class="flex items-center justify-between border-b border-gray-700 pb-6 mb-6">
            <div>
                <h1 class="text-2xl font-bold text-amber-500 flex items-center gap-2">
                    <i class="fa-solid fa-spider"></i> Busca Apify: Candidatos "Open To Work"
                </h1>
                <p class="text-sm text-gray-400 mt-1">Varredura em tempo real via Apify + Enriquecimento de Contato (E-mail e Telefone).</p>
            </div>
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
                <i class="fa-solid fa-magnifying-glass"></i> Buscar via Apify
            </button>
        </div>

        <div id="loading" class="hidden my-8 text-center">
            <div class="inline-block animate-spin rounded-full h-10 w-10 border-4 border-amber-500 border-t-transparent"></div>
            <p class="text-gray-400 text-sm mt-3 animate-pulse">Executando Actor do Apify e enriquecendo dados de contato...</p>
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
                        logList.innerHTML = `<p class="text-rose-400 p-3 bg-rose-500/10 rounded border border-rose-500/20"><i class="fa-solid fa-triangle-exclamation"></i> <b>Aviso:</b> ${data.erro}</p>`;
                    } else {
                        let tableHtml = `
                            <div class="overflow-x-auto">
                                <table class="w-full text-left border-collapse border border-gray-700">
                                    <thead>
                                        <tr class="bg-gray-900 text-amber-400 border-b border-gray-700 text-xs uppercase font-mono">
                                            <th class="p-3">Nome</th>
                                            <th class="p-3">Cargo Buscado</th>
                                            <th class="p-3">E-mail</th>
                                            <th class="p-3">Telefone</th>
                                            <th class="p-3 text-center">LinkedIn</th>
                                        </tr>
                                    </thead>
                                    <tbody class="divide-y divide-gray-700 bg-gray-800/50">`;

                        data.contatos.forEach(c => {
                            tableHtml += `
                                <tr class="hover:bg-gray-800 transition">
                                    <td class="p-3 font-semibold text-gray-100">${c.nome}</td>
                                    <td class="p-3 text-gray-300">${c.cargo}</td>
                                    <td class="p-3 font-mono text-xs text-amber-300/90">${c.email}</td>
                                    <td class="p-3 font-mono text-xs text-emerald-400">${c.telefone}</td>
                                    <td class="p-3 text-center">
                                        ${c.linkedin 
                                            ? `<a href="${c.linkedin}" target="_blank" class="inline-flex items-center gap-1 bg-blue-600/20 hover:bg-blue-600/40 text-blue-400 border border-blue-500/30 px-3 py-1 rounded-md text-xs transition"><i class="fa-brands fa-linkedin"></i> Ver Perfil</a>` 
                                            : '<span class="text-gray-500 text-xs">N/A</span>'}
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
    print("\n--- SERVIDOR LOCAL START RH (APIFY CANDIDATE SEARCH) INICIADO ---")
    print(f"Acesse no navegador: http://localhost:{porta}\n")
    app.run(host="127.0.0.1", port=porta, debug=False)