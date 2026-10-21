import os, json, re, shutil, logging, secrets, mimetypes
from datetime import datetime, timezone
from flask import Flask, jsonify, abort, request, send_file

app = Flask(__name__)
DATA_DIR           = '/var/www/iob-fornecedores/data'
ARCHIVE_DIR        = '/var/www/iob-fornecedores/arquivo-morto'
LINKS_DIR          = '/var/www/iob-fornecedores/links'
ATTACH_DIR         = '/var/www/iob-fornecedores/attachments'
ATTACH_ARCHIVE_DIR = '/var/www/iob-fornecedores/arquivo-morto/attachments'
DEVOLUCAO_DIR      = '/var/www/iob-fornecedores/devolucoes'
MANIFEST           = os.path.join(ARCHIVE_DIR, '.manifest.json')

logging.basicConfig(level=logging.INFO)

VALID_FILENAME   = re.compile(r'^[\w\-]+\.json$')
VALID_TOKEN      = re.compile(r'^[A-Za-z0-9_-]{8,32}$')
VALID_SUBMISSAO  = re.compile(r'^sub_[A-Za-z0-9_-]{10,32}$')
VALID_ATTACH_ID  = re.compile(r'^atch_[A-Za-z0-9_-]{10,32}$')
EMAIL_RE         = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

MIME_POR_EXT = {
    'pdf':  'application/pdf',
    'png':  'image/png',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'zip':  'application/zip',
}


def _read_manifest():
    if not os.path.isfile(MANIFEST):
        return {}
    try:
        with open(MANIFEST, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_manifest(data):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _sub_id_do_json(path):
    """Lê um JSON de resposta e devolve o submissao_id gravado em _meta, ou None."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sid = (data.get('_meta') or {}).get('submissao_id') or data.get('submissao_id')
        if sid and VALID_SUBMISSAO.match(str(sid)):
            return sid
    except Exception:
        pass
    return None


def _mover_pasta_anexos(sub_id, origem_base, destino_base):
    """Move origem_base/<sub_id>/ → destino_base/<sub_id>/ se existir. Silencioso em caso de ausência."""
    if not sub_id:
        return
    src = os.path.join(origem_base, sub_id)
    dst = os.path.join(destino_base, sub_id)
    if not os.path.isdir(src):
        return
    try:
        os.makedirs(destino_base, exist_ok=True)
        if os.path.isdir(dst):
            ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            dst = os.path.join(destino_base, f'{ts}_{sub_id}')
        shutil.move(src, dst)
        app.logger.info('Anexos movidos: %s → %s', src, dst)
    except Exception as e:
        app.logger.error('Erro ao mover anexos %s: %s', src, e)


# ── Rotas existentes ────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_file('/var/www/iob-analistas/html/index.html')


@app.route('/api/responses')
def list_responses():
    try:
        files = sorted(
            [f for f in os.listdir(DATA_DIR) if f.endswith('.json')],
            reverse=True
        )
        return jsonify({'total': len(files), 'files': files}), 200
    except Exception as e:
        app.logger.error('Erro ao listar respostas: %s', e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/response/<filename>')
def get_response(filename):
    if not VALID_FILENAME.match(filename):
        abort(400)
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.isfile(filepath):
        abort(404)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f)), 200
    except Exception as e:
        app.logger.error('Erro ao ler arquivo: %s', e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'}), 200


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


# ── Download de anexos pelo analista ────────────────────────────────────────
# O upload acontece no iob-api (porta 5000, domínio fornecedores). Aqui só
# servimos para que o analista baixe pelo próprio domínio (analistas.*) e evite
# problema de CORS/cookie em links abertos a partir da UI de análise.

@app.route('/api/devolvida/<sub_id>', methods=['GET'])
def analista_ler_devolucao(sub_id):
    """Consulta se existe uma devolução pendente para este submissao_id."""
    if not VALID_SUBMISSAO.match(sub_id):
        return jsonify({'error': 'ID inválido'}), 400
    path = os.path.join(DEVOLUCAO_DIR, sub_id + '.json')
    if not os.path.isfile(path):
        return jsonify({'status': 'none'}), 404
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f)), 200
    except Exception as e:
        app.logger.error('Erro ao ler devolução %s: %s', sub_id, e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/attachment/<sub_id>/<atch_id>')
@app.route('/api/attachment/<sub_id>/<atch_id>/<path:_nome>')
def analista_download_attachment(sub_id, atch_id, _nome=None):
    if not VALID_SUBMISSAO.match(sub_id) or not VALID_ATTACH_ID.match(atch_id):
        abort(400)
    pasta = os.path.join(ATTACH_DIR, sub_id)
    if not os.path.isdir(pasta):
        abort(404)
    prefixo = atch_id + '__'
    caminho = None
    for arq in os.listdir(pasta):
        if arq.startswith(prefixo):
            caminho = os.path.join(pasta, arq)
            break
    if not caminho:
        abort(404)
    _, ext = os.path.splitext(caminho)
    ext = ext.lower().lstrip('.')
    mime = MIME_POR_EXT.get(ext) or mimetypes.guess_type(caminho)[0] or 'application/octet-stream'
    nome_download = os.path.basename(caminho).split('__', 1)[-1]
    resp = send_file(caminho, mimetype=mime, as_attachment=False, download_name=nome_download)
    resp.headers['Content-Disposition']    = 'inline; filename="' + nome_download + '"'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control']          = 'private, no-store'
    return resp


# ── Arquivo Morto ────────────────────────────────────────────────────────────

@app.route('/api/archive', methods=['POST'])
def archive_files():
    """Move um ou mais JSONs de DATA_DIR para ARCHIVE_DIR e registra a data."""
    body = request.get_json(silent=True) or {}
    files = body.get('files', [])

    if not files or not isinstance(files, list):
        return jsonify({'error': 'Campo "files" obrigatório (lista)'}), 400

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    manifest = _read_manifest()
    moved, errors = [], []

    for filename in files:
        if not VALID_FILENAME.match(str(filename)):
            errors.append({'file': filename, 'error': 'nome inválido'})
            continue

        src = os.path.join(DATA_DIR, filename)
        dst = os.path.join(ARCHIVE_DIR, filename)

        if not os.path.isfile(src):
            errors.append({'file': filename, 'error': 'arquivo não encontrado'})
            continue

        try:
            # Antes de mover o JSON, descobre o submissao_id dele para mover a pasta de anexos junto
            sub_id = _sub_id_do_json(src)

            # Se já existe no arquivo morto, renomeia para não sobrescrever
            if os.path.isfile(dst):
                ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                dst = os.path.join(ARCHIVE_DIR, f'{ts}_{filename}')

            shutil.move(src, dst)
            archive_name = os.path.basename(dst)
            manifest[archive_name] = datetime.now(timezone.utc).isoformat()
            moved.append(archive_name)
            app.logger.info('Arquivado: %s → %s', src, dst)

            # Move os anexos associados (silencioso se a submissão não tiver anexos)
            _mover_pasta_anexos(sub_id, ATTACH_DIR, ATTACH_ARCHIVE_DIR)
        except Exception as e:
            app.logger.error('Erro ao arquivar %s: %s', filename, e)
            errors.append({'file': filename, 'error': str(e)})

    _write_manifest(manifest)
    return jsonify({'moved': moved, 'errors': errors}), 200


@app.route('/api/restore', methods=['POST'])
def restore_files():
    """Move arquivos do ARCHIVE_DIR de volta para DATA_DIR."""
    body = request.get_json(silent=True) or {}
    files = body.get('files', [])

    if not files or not isinstance(files, list):
        return jsonify({'error': 'Campo "files" obrigatório (lista)'}), 400

    manifest = _read_manifest()
    restored, errors = [], []

    for filename in files:
        if not VALID_FILENAME.match(str(filename)):
            errors.append({'file': filename, 'error': 'nome inválido'})
            continue

        src = os.path.join(ARCHIVE_DIR, filename)
        dst = os.path.join(DATA_DIR, filename)

        if not os.path.isfile(src):
            errors.append({'file': filename, 'error': 'arquivo não encontrado no arquivo morto'})
            continue

        try:
            sub_id = _sub_id_do_json(src)

            if os.path.isfile(dst):
                ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                dst = os.path.join(DATA_DIR, f'{ts}_{filename}')

            shutil.move(src, dst)
            manifest.pop(filename, None)
            restored.append(os.path.basename(dst))
            app.logger.info('Restaurado: %s → %s', src, dst)

            # Restaura os anexos junto
            _mover_pasta_anexos(sub_id, ATTACH_ARCHIVE_DIR, ATTACH_DIR)
        except Exception as e:
            app.logger.error('Erro ao restaurar %s: %s', filename, e)
            errors.append({'file': filename, 'error': str(e)})

    _write_manifest(manifest)
    return jsonify({'restored': restored, 'errors': errors}), 200


@app.route('/api/archived')
def list_archived():
    """Lista os arquivos no arquivo morto com a data de arquivamento."""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        manifest = _read_manifest()
        files = []
        for f in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
            if not f.endswith('.json'):
                continue
            files.append({
                'file':        f,
                'archivedAt':  manifest.get(f),
            })
        return jsonify({'total': len(files), 'files': files}), 200
    except Exception as e:
        app.logger.error('Erro ao listar arquivo morto: %s', e)
        return jsonify({'error': 'Erro interno'}), 500
