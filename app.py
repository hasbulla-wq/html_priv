import os, json, re, shutil, logging, secrets, traceback
from datetime import datetime, timezone
from flask import Flask, jsonify, abort, request, send_file

app = Flask(__name__)
DATA_DIR    = '/var/www/iob-fornecedores/data'
ARCHIVE_DIR = '/var/www/iob-fornecedores/arquivo-morto'
LINKS_DIR   = '/var/www/iob-fornecedores/links'
MANIFEST    = os.path.join(ARCHIVE_DIR, '.manifest.json')

logging.basicConfig(level=logging.INFO)

VALID_FILENAME = re.compile(r'^[\w\-]+\.json$')
VALID_TOKEN    = re.compile(r'^[A-Za-z0-9_-]{8,32}$')
EMAIL_RE       = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


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
        tb = traceback.format_exc()
        app.logger.error('Erro em create_link: %s\n%s', e, tb)
        # DEBUG TEMPORÁRIO: devolve detalhe no corpo. Remova após diagnosticar.
        return jsonify({
            'error': 'Erro interno',
            'detail': str(e),
            'type': type(e).__name__,
        }), 500


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
            # Se já existe no arquivo morto, renomeia para não sobrescrever
            if os.path.isfile(dst):
                ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                dst = os.path.join(ARCHIVE_DIR, f'{ts}_{filename}')

            shutil.move(src, dst)
            archive_name = os.path.basename(dst)
            manifest[archive_name] = datetime.now(timezone.utc).isoformat()
            moved.append(archive_name)
            app.logger.info('Arquivado: %s → %s', src, dst)
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
            if os.path.isfile(dst):
                ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                dst = os.path.join(DATA_DIR, f'{ts}_{filename}')

            shutil.move(src, dst)
            manifest.pop(filename, None)
            restored.append(os.path.basename(dst))
            app.logger.info('Restaurado: %s → %s', src, dst)
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
