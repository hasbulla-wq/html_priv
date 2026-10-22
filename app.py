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
        prazo       = (body.get('prazo')       or '').strip()
        analista    = (body.get('analista')    or '').strip()
        email       = (body.get('email')       or '').strip()
        telefone    = (body.get('telefone')    or '').strip()
        ticket      = (body.get('ticket')      or '').strip()
        area        = (body.get('area')        or '').strip()
        envio       = (body.get('envio')       or '').strip()
        observacoes = (body.get('observacoes') or '').strip()
        fornecedor  = (body.get('fornecedor')  or '').strip()

        if not prazo or not re.match(r'^\d{4}-\d{2}-\d{2}$', prazo):
            return jsonify({'error': 'Prazo inválido (esperado YYYY-MM-DD)'}), 400
        if not analista and not email:
            return jsonify({'error': 'Informe analista ou e-mail'}), 400
        if email and not EMAIL_RE.match(email):
            return jsonify({'error': 'E-mail inválido'}), 400
        if len(analista) > 120 or len(email) > 160 or len(telefone) > 40:
            return jsonify({'error': 'Campo muito longo'}), 400
        if len(ticket) > 80 or len(area) > 160 or len(observacoes) > 2000 or len(fornecedor) > 160:
            return jsonify({'error': 'Campo muito longo'}), 400
        if envio and not re.match(r'^\d{4}-\d{2}-\d{2}$', envio):
            return jsonify({'error': 'Data de envio inválida'}), 400

        os.makedirs(LINKS_DIR, exist_ok=True)

        for _ in range(5):
            token = secrets.token_urlsafe(8).rstrip('=')
            dest = os.path.join(LINKS_DIR, token + '.json')
            if not os.path.exists(dest):
                break
        else:
            return jsonify({'error': 'Falha ao gerar token'}), 500

        payload = {
            'prazo':        prazo,
            'analista':     analista,
            'email':        email,
            'telefone':     telefone,
            'ticket':       ticket,
            'area':         area,
            'envio':        envio,
            'observacoes':  observacoes,
            'fornecedor':   fornecedor,
            'createdAt':    datetime.now(timezone.utc).isoformat(),
            'respostaFile': '',
        }
        with open(dest, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        app.logger.info('Link criado: %s (prazo=%s, ticket=%s, fornecedor=%s)', token, prazo, ticket, fornecedor)
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
            'prazo':       data.get('prazo', ''),
            'analista':    data.get('analista', ''),
            'email':       data.get('email', ''),
            'telefone':    data.get('telefone', ''),
            'ticket':      data.get('ticket', ''),
            'area':        data.get('area', ''),
            'envio':       data.get('envio', ''),
            'observacoes': data.get('observacoes', ''),
            'fornecedor':  data.get('fornecedor', ''),
        }), 200
    except Exception as e:
        app.logger.error('Erro ao ler link %s: %s', token, e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/link/<token>', methods=['PATCH'])
def patch_link(token):
    """Permite ao analista alterar o prazo e/ou observações do link já gerado."""
    if not VALID_TOKEN.match(token):
        return jsonify({'error': 'Token inválido'}), 400
    path = os.path.join(LINKS_DIR, token + '.json')
    if not os.path.isfile(path):
        return jsonify({'error': 'Link não encontrado'}), 404

    body = request.get_json(silent=True) or {}
    novo_prazo       = (body.get('prazo')       or '').strip()
    novo_observacoes = body.get('observacoes')
    atualizado_por   = (body.get('atualizadoPor') or '').strip() or '(desconhecido)'

    if novo_prazo and not re.match(r'^\d{4}-\d{2}-\d{2}$', novo_prazo):
        return jsonify({'error': 'Prazo inválido'}), 400
    if novo_observacoes is not None and len(str(novo_observacoes)) > 2000:
        return jsonify({'error': 'Observações muito longas'}), 400

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if novo_prazo:
            data['prazo'] = novo_prazo
        if novo_observacoes is not None:
            data['observacoes'] = str(novo_observacoes).strip()
        data['atualizadoEm']  = datetime.now(timezone.utc).isoformat()
        data['atualizadoPor'] = atualizado_por
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        app.logger.info('Link atualizado: %s (novo prazo=%s)', token, novo_prazo or '(mantido)')
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        app.logger.error('Erro em patch_link %s: %s', token, e)
        return jsonify({'error': 'Erro interno'}), 500


# ── Download de anexos pelo analista ────────────────────────────────────────
# O upload acontece no iob-api (porta 5000, domínio fornecedores). Aqui só
# servimos para que o analista baixe pelo próprio domínio (analistas.*) e evite
# problema de CORS/cookie em links abertos a partir da UI de análise.

# ── Histórico de avaliações ─────────────────────────────────────────────────
# Varre DATA_DIR, extrai metadados leves de cada JSON (ticket, data de envio,
# analista responsável, parecer) e devolve lista ordenada por data desc.

def _extrair_meta_avaliacao(path):
    """Lê um JSON de resposta e devolve apenas os metadados exibidos no histórico.
    Prioriza _meta.contato_iob (injetado no /api/submit com base no token do link);
    cai no respostas.campos como fallback para avaliações legadas."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None

    meta     = data.get('_meta') or {}
    contato  = meta.get('contato_iob') or {}
    campos   = (data.get('respostas') or {}).get('campos') or {}
    parecer  = data.get('parecer') or {}

    return {
        'filename':     os.path.basename(path),
        'submissao_id': meta.get('submissao_id') or data.get('submissao_id') or '',
        'submissao_token': meta.get('submissao_token') or '',
        'enviado_em':   meta.get('received_at') or meta.get('exportadoEm') or '',
        'ticket':       contato.get('ticket')     or campos.get('ticket.numero') or '',
        'area':         contato.get('area')       or campos.get('ticket.area')   or '',
        'fornecedor':   contato.get('fornecedor') or '',
        'analista':     contato.get('analista')   or campos.get('analista.nome')  or '',
        'analista_email': contato.get('email')    or campos.get('analista.email') or '',
        'analista_tel':   contato.get('telefone') or campos.get('analista.tel')   or '',
        'prazo_resposta': contato.get('prazo')    or campos.get('prazo.resposta') or '',
        'envio':          contato.get('envio')    or campos.get('prazo.envio')    or '',
        'observacoes':    contato.get('observacoes') or campos.get('observacoes') or '',
        'parecer_decisao':      parecer.get('decisao') or '',
        'parecer_justificativa':parecer.get('justificativa') or '',
    }


def _item_do_link(token, link_data):
    """Transforma /links/<token>.json em uma entrada do histórico."""
    return {
        'token':          token,
        'filename':       link_data.get('respostaFile') or '',
        'submissao_id':   '',
        'submissao_token': token,
        'ticket':         link_data.get('ticket')     or '',
        'area':           link_data.get('area')       or '',
        'fornecedor':     link_data.get('fornecedor') or '',
        'analista':       link_data.get('analista')   or '',
        'analista_email': link_data.get('email')      or '',
        'analista_tel':   link_data.get('telefone')   or '',
        'prazo_resposta': link_data.get('prazo')      or '',
        'envio':          link_data.get('envio')      or '',
        'observacoes':    link_data.get('observacoes') or '',
        'enviado_em':     link_data.get('recebidaEm') or '',
        'criado_em':      link_data.get('createdAt')  or '',
        'parecer_decisao': '',
        'parecer_justificativa': '',
    }


@app.route('/api/historico', methods=['GET'])
def listar_historico():
    """Histórico unificado: todos os links já gerados + merge com as respostas.
    Status possíveis:
      aguardando → link gerado, fornecedor ainda não enviou
      recebida   → fornecedor enviou, analista ainda não emitiu parecer
      concluida  → analista emitiu parecer (recomendada / ressalvas / não recomendada)
    """
    try:
        itens = []
        tokens_vistos = set()

        # 1) Varre todos os links já gerados
        if os.path.isdir(LINKS_DIR):
            for arq in sorted(os.listdir(LINKS_DIR), reverse=True):
                if not arq.endswith('.json'):
                    continue
                token = arq[:-5]
                try:
                    with open(os.path.join(LINKS_DIR, arq), 'r', encoding='utf-8') as f:
                        link_data = json.load(f)
                except Exception:
                    continue

                item = _item_do_link(token, link_data)
                resposta_file = link_data.get('respostaFile') or ''

                # Se o fornecedor já respondeu, enriquece com os dados do JSON de resposta
                if resposta_file:
                    resp_path = os.path.join(DATA_DIR, resposta_file)
                    if os.path.isfile(resp_path):
                        meta_resposta = _extrair_meta_avaliacao(resp_path)
                        if meta_resposta:
                            # dados da resposta prevalecem sobre dados do link
                            for k, v in meta_resposta.items():
                                if v:
                                    item[k] = v
                            item['filename'] = resposta_file

                # Define status
                if item.get('parecer_decisao'):
                    item['status'] = 'concluida'
                elif item.get('enviado_em'):
                    item['status'] = 'recebida'
                else:
                    item['status'] = 'aguardando'

                itens.append(item)
                tokens_vistos.add(token)

        # 2) Varre arquivos de resposta que não tenham token vinculado
        #    (fluxos legados ou envios sem link)
        if os.path.isdir(DATA_DIR):
            for arq in sorted(os.listdir(DATA_DIR), reverse=True):
                if not arq.endswith('.json'):
                    continue
                path = os.path.join(DATA_DIR, arq)
                meta = _extrair_meta_avaliacao(path)
                if not meta:
                    continue
                token = meta.get('submissao_token') or ''
                if token and token in tokens_vistos:
                    continue  # já processado no laço anterior
                meta['token']  = token
                meta['status'] = 'concluida' if meta.get('parecer_decisao') else 'recebida'
                itens.append(meta)

        # Ordena por data mais recente (envio > criação)
        def _chave(i):
            return i.get('enviado_em') or i.get('criado_em') or ''
        itens.sort(key=_chave, reverse=True)

        return jsonify({'total': len(itens), 'itens': itens}), 200
    except Exception as e:
        app.logger.error('Erro ao montar histórico: %s', e)
        return jsonify({'error': 'Erro interno'}), 500


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
