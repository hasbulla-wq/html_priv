import os, json, re, shutil, logging
from datetime import datetime, timezone
from flask import Flask, jsonify, abort, request, send_file

app = Flask(__name__)
DATA_DIR    = '/var/www/iob-fornecedores/data'
ARCHIVE_DIR = '/var/www/iob-fornecedores/arquivo-morto'
MANIFEST    = os.path.join(ARCHIVE_DIR, '.manifest.json')

logging.basicConfig(level=logging.INFO)

VALID_FILENAME = re.compile(r'^[\w\-]+\.json$')


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
