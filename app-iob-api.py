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

DATA_DIR           = "/var/www/iob-fornecedores/data"
LINKS_DIR          = "/var/www/iob-fornecedores/links"
ATTACH_DIR         = "/var/www/iob-fornecedores/attachments"
DEVOLUCAO_DIR      = "/var/www/iob-fornecedores/devolucoes"
MAX_PAYLOAD_BYTES  = 512 * 1024   # 512 KB (apenas /api/submit)
MAX_DEVOLUCAO_BYTES = 2 * 1024 * 1024  # 2 MB para devoluções (respostas + comentários)

MAX_ATTACH_BYTES  = 10 * 1024 * 1024   # 10 MB por anexo

# Limite global do Flask: 15 MB (dá margem sobre os 10 MB do anexo).
# Sem isso, uploads multipart grandes podem ser interrompidos antes mesmo de chegar
# na nossa validação de tamanho, provocando "falha de rede" no front.
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024

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

        # Se o fornecedor abriu o questionário por um link curto, enriquece o JSON
        # com os dados do Contato IOB gravados em /links/<token>.json.
        meta_extra = {"received_at": ts, "ip": request.remote_addr}
        token = (data.get("_meta") or {}).get("submissao_token") or data.get("submissao_token")
        link_data = _dados_do_link(token) if token else None
        if link_data:
            meta_extra["submissao_token"] = token
            meta_extra["contato_iob"] = {
                "prazo":       link_data.get("prazo", ""),
                "analista":    link_data.get("analista", ""),
                "email":       link_data.get("email", ""),
                "telefone":    link_data.get("telefone", ""),
                "ticket":      link_data.get("ticket", ""),
                "area":        link_data.get("area", ""),
                "envio":       link_data.get("envio", ""),
                "observacoes": link_data.get("observacoes", ""),
                "fornecedor":  link_data.get("fornecedor", ""),
            }

        # _meta vindo do payload é respeitado, mas o received_at/ip/contato_iob do servidor prevalecem
        meta_merged = {**(data.get("_meta") or {}), **meta_extra}
        payload = {**data, "_meta": meta_merged}

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        # Vincula a resposta ao link, para que o histórico marque o status como "Recebida"
        if token and link_data is not None:
            try:
                link_path = os.path.join(LINKS_DIR, token + '.json')
                link_data['respostaFile'] = filename
                link_data['recebidaEm']   = datetime.now(timezone.utc).isoformat()
                with open(link_path, 'w', encoding='utf-8') as f:
                    json.dump(link_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                app.logger.warning('Falha ao vincular resposta ao link %s: %s', token, e)

        return jsonify({"status": "ok", "id": filename}), 201

    except Exception as e:
        app.logger.error("Erro ao salvar resposta: %s", e)
        return jsonify({"error": "Erro interno"}), 500


@app.route("/api/responses", methods=["GET"])
def list_responses():
    """Endpoint interno — listagem de respostas salvas."""
    files = sorted(os.listdir(DATA_DIR), reverse=True)
    return jsonify({"total": len(files), "files": files}), 200


# ── Parecer do analista ─────────────────────────────────────────────────────
# Atualiza, dentro do JSON de resposta existente, o campo "parecer" com a
# decisão final do analista. É chamado pelo front do analista cada vez que
# o parecer é salvo (decisão ou justificativa alterada). O histórico lê daqui.

VALID_RESPONSE_FILENAME = re.compile(r'^[\w\-]+\.json$')


@app.route('/api/parecer/<filename>', methods=['POST'])
def salvar_parecer(filename):
    if not VALID_RESPONSE_FILENAME.match(filename):
        return jsonify({'error': 'Nome inválido'}), 400
    body = request.get_json(silent=True) or {}
    decisao       = (body.get('decisao') or '').strip()
    justificativa = (body.get('justificativa') or '').strip()
    atualizado_por = (body.get('atualizadoPor') or '').strip() or '(desconhecido)'
    # finalizar=True marca a avaliação como concluída; finalizar=False a reabre
    finalizar = body.get('finalizar', None)

    if decisao and decisao not in ('recomendada', 'recomendada-ressalvas', 'nao-recomendada'):
        return jsonify({'error': 'Decisão inválida'}), 400
    if len(justificativa) > 4000:
        return jsonify({'error': 'Justificativa muito longa'}), 400
    if finalizar is True and not decisao:
        return jsonify({'error': 'Selecione um parecer antes de finalizar a análise'}), 400

    path = os.path.join(DATA_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({'error': 'Arquivo não encontrado'}), 404

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        parecer_atual = data.get('parecer') or {}
        novo_parecer = {
            'decisao':       decisao,
            'justificativa': justificativa,
            'atualizadoEm':  datetime.now(timezone.utc).isoformat(),
            'atualizadoPor': atualizado_por,
            # Preserva a conclusão anterior quando o campo finalizar não vem no payload
            'concluidaEm':   parecer_atual.get('concluidaEm', ''),
            'concluidaPor':  parecer_atual.get('concluidaPor', ''),
        }
        if finalizar is True:
            novo_parecer['concluidaEm']  = datetime.now(timezone.utc).isoformat()
            novo_parecer['concluidaPor'] = atualizado_por
        elif finalizar is False:
            novo_parecer['concluidaEm']  = ''
            novo_parecer['concluidaPor'] = ''
        data['parecer'] = novo_parecer
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        app.logger.info('Parecer salvo em %s: %s (finalizar=%s)', filename, decisao or '(em branco)', finalizar)
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        app.logger.error('Erro ao salvar parecer em %s: %s', filename, e)
        return jsonify({'error': 'Erro interno'}), 500


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

        # Gera token com retry por colisão (praticamente nula com 64 bits)
        for _ in range(5):
            token = secrets.token_urlsafe(8).rstrip('=')
            dest = os.path.join(LINKS_DIR, token + '.json')
            if not os.path.exists(dest):
                break
        else:
            return jsonify({'error': 'Falha ao gerar token'}), 500

        payload = {
            'prazo':         prazo,
            'analista':      analista,
            'email':         email,
            'telefone':      telefone,
            'ticket':        ticket,
            'area':          area,
            'envio':         envio,
            'observacoes':   observacoes,
            'fornecedor':    fornecedor,
            'createdAt':     datetime.now(timezone.utc).isoformat(),
            'respostaFile':  '',  # Preenchido pelo /api/submit quando o fornecedor envia
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
    """Permite ao analista alterar o prazo (e opcionalmente observações) de
    um link já gerado, sem precisar criar um novo. Útil quando o fornecedor
    pede prorrogação ou quando o analista precisa ajustar o contexto."""
    if not VALID_TOKEN.match(token):
        return jsonify({'error': 'Token inválido'}), 400
    path = os.path.join(LINKS_DIR, token + '.json')
    if not os.path.isfile(path):
        return jsonify({'error': 'Link não encontrado'}), 404

    body = request.get_json(silent=True) or {}
    novo_prazo       = (body.get('prazo')       or '').strip()
    novo_observacoes = body.get('observacoes')  # pode ser string vazia
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


def _dados_do_link(token):
    """Lê os dados do analista gravados no link, se o token for válido."""
    if not token or not VALID_TOKEN.match(token):
        return None
    path = os.path.join(LINKS_DIR, token + '.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


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


# ── Devolução ao fornecedor ─────────────────────────────────────────────────
# O analista revisa a avaliação enviada, deixa comentários nas perguntas que
# precisam de ajuste e devolve. O fornecedor, ao reabrir o link, encontra as
# respostas pré-preenchidas e os comentários visíveis para corrigir.

@app.route('/api/devolvida/<sub_id>', methods=['POST'])
def gravar_devolucao(sub_id):
    if not VALID_SUBMISSAO.match(sub_id):
        return jsonify({'error': 'ID de submissão inválido'}), 400
    if request.content_length and request.content_length > MAX_DEVOLUCAO_BYTES:
        return jsonify({'error': 'Payload muito grande'}), 413
    body = request.get_json(silent=True) or {}
    respostas    = body.get('respostas')    or {}
    comentarios  = body.get('comentarios')  or {}
    if not isinstance(respostas, dict) or not isinstance(comentarios, dict):
        return jsonify({'error': 'Formato inválido'}), 400
    try:
        os.makedirs(DEVOLUCAO_DIR, exist_ok=True)
        payload = {
            'submissao_id': sub_id,
            'respostas':    respostas,
            'comentarios':  comentarios,
            'devolvidaEm':  datetime.now(timezone.utc).isoformat(),
        }
        dest = os.path.join(DEVOLUCAO_DIR, sub_id + '.json')
        with open(dest, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        app.logger.info('Devolução gravada: %s (%d comentários)', sub_id, len(comentarios))
        return jsonify({'status': 'ok'}), 201
    except Exception as e:
        app.logger.error('Erro ao gravar devolução %s: %s', sub_id, e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/devolvida/<sub_id>', methods=['GET'])
def ler_devolucao(sub_id):
    if not VALID_SUBMISSAO.match(sub_id):
        return jsonify({'error': 'ID de submissão inválido'}), 400
    path = os.path.join(DEVOLUCAO_DIR, sub_id + '.json')
    if not os.path.isfile(path):
        # Sem devolução pendente — fluxo normal
        return jsonify({'status': 'none'}), 404
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f)), 200
    except Exception as e:
        app.logger.error('Erro ao ler devolução %s: %s', sub_id, e)
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/devolvida/<sub_id>', methods=['DELETE'])
def apagar_devolucao(sub_id):
    """Chamado quando o fornecedor envia a correção — remove a devolução pendente."""
    if not VALID_SUBMISSAO.match(sub_id):
        return jsonify({'error': 'ID de submissão inválido'}), 400
    path = os.path.join(DEVOLUCAO_DIR, sub_id + '.json')
    if not os.path.isfile(path):
        return jsonify({'status': 'ok'}), 200
    try:
        os.remove(path)
        app.logger.info('Devolução removida: %s', sub_id)
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        app.logger.error('Erro ao remover devolução %s: %s', sub_id, e)
        return jsonify({'error': 'Erro interno'}), 500
