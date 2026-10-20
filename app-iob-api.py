import json
import os
import re
import secrets
import hashlib
import mimetypes
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)

DATA_DIR          = "/var/www/iob-fornecedores/data"
LINKS_DIR         = "/var/www/iob-fornecedores/links"
ATTACH_DIR        = "/var/www/iob-fornecedores/attachments"
MAX_PAYLOAD_BYTES = 512 * 1024  # 512 KB (apenas /api/submit)

MAX_ATTACH_BYTES  = 10 * 1024 * 1024   # 10 MB por anexo

# Tipos aceitos (allowlist). Chave: extensão. Valor: (mime esperado, lista de magic bytes).
ALLOWED_TYPES = {
    'pdf':  ('application/pdf',                                                           [b'%PDF-']),
    'png':  ('image/png',                                                                 [b'\x89PNG\r\n\x1a\n']),
    'jpg':  ('image/jpeg',                                                                [b'\xff\xd8\xff']),
    'jpeg': ('image/jpeg',                                                                [b'\xff\xd8\xff']),
    'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',   [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']),
    'xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',         [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']),
    'pptx': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']),
    'zip':  ('application/zip',                                                           [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']),
}

VALID_TOKEN      = re.compile(r'^[A-Za-z0-9_-]{8,32}$')
VALID_SUBMISSAO  = re.compile(r'^sub_[A-Za-z0-9_-]{10,32}$')
VALID_ATTACH_ID  = re.compile(r'^atch_[A-Za-z0-9_-]{10,32}$')
EMAIL_RE         = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def _extensao(nome):
    """Retorna a extensão em minúsculas sem o ponto, ou '' se não houver."""
    _, ext = os.path.splitext(nome or '')
    return ext.lower().lstrip('.')


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


# ── Anexos do questionário ──────────────────────────────────────────────────
# Cada submissão (identificada por sub_<token>, gerado no front do fornecedor)
# tem sua própria pasta em /var/www/iob-fornecedores/attachments/<sub_id>/.
# Arquivos: <atch_id>__<nome-sanitizado-original>
# Metadados ficam no JSON de resposta final.

@app.route('/api/attachment/<sub_id>', methods=['POST'])
def upload_attachment(sub_id):
    if not VALID_SUBMISSAO.match(sub_id):
        return jsonify({'error': 'ID de submissão inválido'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'Campo "file" ausente'}), 400

    f = request.files['file']
    nome_original = (f.filename or '').strip()
    if not nome_original:
        return jsonify({'error': 'Nome do arquivo vazio'}), 400

    ext = _extensao(nome_original)
    if ext not in ALLOWED_TYPES:
        return jsonify({'error': 'Tipo não permitido (aceitos: ' + ', '.join(sorted(ALLOWED_TYPES.keys())) + ')'}), 415

    # Lê o arquivo todo em memória controlada (ok até 10 MB)
    try:
        f.stream.seek(0, os.SEEK_END)
        tamanho = f.stream.tell()
        f.stream.seek(0)
    except Exception:
        tamanho = 0

    if tamanho == 0:
        return jsonify({'error': 'Arquivo vazio'}), 400
    if tamanho > MAX_ATTACH_BYTES:
        return jsonify({'error': 'Arquivo excede 10 MB'}), 413

    conteudo = f.stream.read(MAX_ATTACH_BYTES + 1)
    if len(conteudo) > MAX_ATTACH_BYTES:
        return jsonify({'error': 'Arquivo excede 10 MB'}), 413

    # Validação por magic bytes — não confia no mimetype do browser
    mime_esperado, assinaturas = ALLOWED_TYPES[ext]
    if not any(conteudo.startswith(sig) for sig in assinaturas):
        return jsonify({'error': 'Conteúdo não corresponde à extensão'}), 415

    # Gera id do anexo + nome final sanitizado
    nome_safe = secure_filename(nome_original) or ('arquivo.' + ext)
    if not nome_safe.lower().endswith('.' + ext):
        nome_safe = nome_safe + '.' + ext

    pasta = os.path.join(ATTACH_DIR, sub_id)
    try:
        os.makedirs(pasta, exist_ok=True)
    except Exception as e:
        app.logger.error('Erro ao criar pasta de anexos %s: %s', pasta, e)
        return jsonify({'error': 'Erro interno'}), 500

    for _ in range(5):
        atch_id = 'atch_' + secrets.token_urlsafe(10).rstrip('=')
        dest = os.path.join(pasta, atch_id + '__' + nome_safe)
        if not os.path.exists(dest):
            break
    else:
        return jsonify({'error': 'Falha ao gerar id'}), 500

    try:
        with open(dest, 'wb') as out:
            out.write(conteudo)
    except Exception as e:
        app.logger.error('Erro ao gravar anexo %s: %s', dest, e)
        return jsonify({'error': 'Erro interno ao gravar'}), 500

    sha = hashlib.sha256(conteudo).hexdigest()
    app.logger.info('Anexo salvo: %s/%s (%d bytes)', sub_id, atch_id, tamanho)

    return jsonify({
        'id':       atch_id,
        'nome':     nome_original,
        'tamanho':  tamanho,
        'mime':     mime_esperado,
        'hash':     'sha256:' + sha,
    }), 201


def _localizar_anexo(sub_id, atch_id):
    """Devolve o caminho completo do anexo, ou None se não existir."""
    if not VALID_SUBMISSAO.match(sub_id) or not VALID_ATTACH_ID.match(atch_id):
        return None
    pasta = os.path.join(ATTACH_DIR, sub_id)
    if not os.path.isdir(pasta):
        return None
    prefixo = atch_id + '__'
    for arq in os.listdir(pasta):
        if arq.startswith(prefixo):
            return os.path.join(pasta, arq)
    return None


@app.route('/api/attachment/<sub_id>/<atch_id>')
@app.route('/api/attachment/<sub_id>/<atch_id>/<path:_nome>')
def download_attachment(sub_id, atch_id, _nome=None):
    """Serve o anexo. O <_nome> é opcional e só serve para o browser mostrar
    o nome original na barra de endereço — a origem é sempre o arquivo em disco."""
    caminho = _localizar_anexo(sub_id, atch_id)
    if not caminho:
        abort(404)
    ext = _extensao(caminho)
    mime = ALLOWED_TYPES.get(ext, (None, None))[0] or mimetypes.guess_type(caminho)[0] or 'application/octet-stream'
    nome_download = os.path.basename(caminho).split('__', 1)[-1]
    resp = send_file(caminho, mimetype=mime, as_attachment=False, download_name=nome_download)
    resp.headers['Content-Disposition']    = 'inline; filename="' + nome_download + '"'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control']          = 'private, no-store'
    return resp


@app.route('/api/attachment/<sub_id>/<atch_id>', methods=['DELETE'])
def delete_attachment(sub_id, atch_id):
    """Permite ao fornecedor remover um anexo que selecionou errado,
    enquanto ainda não finalizou o questionário."""
    caminho = _localizar_anexo(sub_id, atch_id)
    if not caminho:
        abort(404)
    try:
        os.remove(caminho)
        app.logger.info('Anexo removido: %s/%s', sub_id, atch_id)
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        app.logger.error('Erro ao remover anexo %s: %s', caminho, e)
        return jsonify({'error': 'Erro interno'}), 500
