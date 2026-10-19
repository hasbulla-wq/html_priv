import json
import os
import re
import secrets
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

DATA_DIR          = "/var/www/iob-fornecedores/data"
LINKS_DIR         = "/var/www/iob-fornecedores/links"
MAX_PAYLOAD_BYTES = 512 * 1024  # 512 KB

VALID_TOKEN = re.compile(r'^[A-Za-z0-9_-]{8,32}$')
EMAIL_RE    = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


@app.route("/api/submit", methods=["POST"])
def submit():
    if request.content_length and request.content_length > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Payload muito grande"}), 413

    try:
        data = request.get_json(force=True, silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "JSON inválido"}), 400

        # Nome do arquivo baseado em timestamp + CNPJ sanitizado
        cnpj = re.sub(r"[^0-9]", "", str(data.get("escopo", {}).get("cnpj", "sem-cnpj")))
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts}_{cnpj}.json"
        filepath = os.path.join(DATA_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({"_meta": {"received_at": ts, "ip": request.remote_addr}, **data}, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "ok", "id": filename}), 201

    except Exception as e:
        app.logger.error("Erro ao salvar resposta: %s", e)
        return jsonify({"error": "Erro interno"}), 500


@app.route("/api/responses", methods=["GET"])
def list_responses():
    """Endpoint interno — listagem de respostas salvas."""
    files = sorted(os.listdir(DATA_DIR), reverse=True)
    return jsonify({"total": len(files), "files": files}), 200


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"}), 200


# ── Links curtos para o fornecedor ──────────────────────────────────────────
# Gera um token opaco (~64 bits de entropia) e guarda {prazo, analista, email}
# em /var/www/iob-fornecedores/links/<token>.json. O fornecedor acessa
# fornecedores.iob.com.br/?t=<token> e o front-end busca os dados aqui.
# Nenhum dado pessoal trafega na URL.

@app.route('/api/link', methods=['POST'])
def create_link():
    try:
        body = request.get_json(silent=True) or {}
        prazo    = (body.get('prazo')    or '').strip()
        analista = (body.get('analista') or '').strip()
        email    = (body.get('email')    or '').strip()

        if not prazo or not re.match(r'^\d{4}-\d{2}-\d{2}$', prazo):
            return jsonify({'error': 'Prazo inválido (esperado YYYY-MM-DD)'}), 400
        if not analista and not email:
            return jsonify({'error': 'Informe analista ou e-mail'}), 400
        if email and not EMAIL_RE.match(email):
            return jsonify({'error': 'E-mail inválido'}), 400
        if len(analista) > 120 or len(email) > 160:
            return jsonify({'error': 'Campo muito longo'}), 400

        os.makedirs(LINKS_DIR, exist_ok=True)

        # Gera token com retry por colisão (praticamente nula com 64 bits)
        for _ in range(5):
            token = secrets.token_urlsafe(8).rstrip('=')
            dest = os.path.join(LINKS_DIR, token + '.json')
            if not os.path.exists(dest):
                break
        else:
            return jsonify({'error': 'Falha ao gerar token'}), 500

        payload = {
            'prazo':    prazo,
            'analista': analista,
            'email':    email,
            'createdAt': datetime.now(timezone.utc).isoformat(),
        }
        with open(dest, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        app.logger.info('Link criado: %s (prazo=%s)', token, prazo)
        return jsonify({'token': token}), 201

    except Exception as e:
        app.logger.error('Erro em create_link: %s', e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/link/<token>')
def get_link(token):
    if not VALID_TOKEN.match(token):
        abort(400)
    path = os.path.join(LINKS_DIR, token + '.json')
    if not os.path.isfile(path):
        abort(404)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({
            'prazo':    data.get('prazo', ''),
            'analista': data.get('analista', ''),
            'email':    data.get('email', ''),
        }), 200
    except Exception as e:
        app.logger.error('Erro ao ler link %s: %s', token, e)
        return jsonify({'error': 'Erro interno'}), 500
