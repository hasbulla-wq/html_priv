#!/usr/bin/env bash
# =============================================================================
# reset-base-avaliacao.sh
#
# Limpa TODA a base da Avaliação de Fornecedor:
#   /var/www/iob-fornecedores/data/        (JSONs de respostas enviadas)
#   /var/www/iob-fornecedores/links/       (tokens dos links curtos)
#   /var/www/iob-fornecedores/rascunhos/   (autosave server-side por token)
#   /var/www/iob-fornecedores/attachments/ (anexos agrupados por sub_id)
#   /var/www/iob-fornecedores/devolucoes/  (comentários de devolução do analista)
#
# Antes de apagar:
#   1. Gera backup .tar.gz com data/hora em /var/backups/iob-fornecedores/
#   2. Mostra o conteúdo que será apagado (contagem de arquivos e tamanho)
#   3. Pede confirmação explícita digitando "LIMPAR" (não basta "y")
#
# Autor:  Privacidade & Segurança da Informação — IOB
# Data:   2026-11-23 (deploy v3.4)
# Uso:    sudo bash reset-base-avaliacao.sh
# =============================================================================

set -euo pipefail

BASE_DIR="/var/www/iob-fornecedores"
BACKUP_DIR="/var/backups/iob-fornecedores"
STAMP="$(date +%Y%m%dT%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/base-avaliacao-${STAMP}.tar.gz"

# Subdiretórios que serão zerados (os próprios diretórios permanecem,
# apenas o conteúdo é removido — preserva dono/grupo/mode do systemd).
SUBDIRS=(data links rascunhos attachments devolucoes)

echo "═══════════════════════════════════════════════════════════════════════"
echo "  RESET DA BASE — Avaliação de Fornecedor IOB"
echo "═══════════════════════════════════════════════════════════════════════"

# ── 1. Checagens iniciais ────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "ERRO: este script precisa de sudo." >&2
  exit 1
fi

if [[ ! -d "$BASE_DIR" ]]; then
  echo "ERRO: $BASE_DIR não existe. Nada para limpar." >&2
  exit 1
fi

# ── 2. Mostra o que será apagado ─────────────────────────────────────────
echo
echo "Diretórios que serão zerados (conteúdo apenas, não os diretórios):"
TOTAL_FILES=0
TOTAL_BYTES=0
for sub in "${SUBDIRS[@]}"; do
  full="${BASE_DIR}/${sub}"
  if [[ -d "$full" ]]; then
    n=$(find "$full" -mindepth 1 -type f 2>/dev/null | wc -l)
    sz=$(du -sb "$full" 2>/dev/null | awk '{print $1}')
    sz_human=$(du -sh "$full" 2>/dev/null | awk '{print $1}')
    printf "  %-45s  %6d arquivos  %10s\n" "$full" "$n" "$sz_human"
    TOTAL_FILES=$((TOTAL_FILES + n))
    TOTAL_BYTES=$((TOTAL_BYTES + ${sz:-0}))
  else
    printf "  %-45s  (não existe)\n" "$full"
  fi
done

echo
echo "TOTAL: ${TOTAL_FILES} arquivos, $(numfmt --to=iec --suffix=B ${TOTAL_BYTES} 2>/dev/null || echo "${TOTAL_BYTES} B")"
echo

if [[ $TOTAL_FILES -eq 0 ]]; then
  echo "Nada a apagar — a base já está vazia. Saindo sem fazer backup."
  exit 0
fi

# ── 3. Confirmação explícita ─────────────────────────────────────────────
echo "ATENÇÃO: esta ação é IRREVERSÍVEL do ponto de vista operacional."
echo "Um backup compactado será gerado em:"
echo "  $BACKUP_FILE"
echo
echo "Para restaurar depois, use:"
echo "  sudo tar -xzf $BACKUP_FILE -C /"
echo
read -r -p "Digite LIMPAR em maiúsculas para prosseguir (qualquer outra coisa cancela): " confirm
if [[ "$confirm" != "LIMPAR" ]]; then
  echo "Cancelado. Nada foi alterado."
  exit 0
fi

# ── 4. Backup ────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo
echo "Gerando backup..."
# Entra em / para o tar guardar os paths absolutos corretos
cd /
tar -czf "$BACKUP_FILE" \
  "${BASE_DIR#/}/data" \
  "${BASE_DIR#/}/links" \
  "${BASE_DIR#/}/rascunhos" \
  "${BASE_DIR#/}/attachments" \
  "${BASE_DIR#/}/devolucoes" \
  2>/dev/null || true

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "ERRO: não foi possível criar o backup. Abortando SEM apagar." >&2
  exit 2
fi

BK_SIZE=$(du -sh "$BACKUP_FILE" | awk '{print $1}')
echo "Backup criado: $BACKUP_FILE ($BK_SIZE)"

# ── 5. Limpeza ───────────────────────────────────────────────────────────
echo
echo "Limpando conteúdo..."
for sub in "${SUBDIRS[@]}"; do
  full="${BASE_DIR}/${sub}"
  if [[ -d "$full" ]]; then
    # Remove tudo DENTRO do diretório, inclusive subpastas ocultas,
    # mas NÃO remove o próprio diretório (preserva dono/permissões/systemd)
    find "$full" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    printf "  %-45s  limpo\n" "$full"
  fi
done

# ── 6. Verificação pós-limpeza ──────────────────────────────────────────
echo
echo "Verificando..."
REMAINING=0
for sub in "${SUBDIRS[@]}"; do
  full="${BASE_DIR}/${sub}"
  if [[ -d "$full" ]]; then
    n=$(find "$full" -mindepth 1 -type f 2>/dev/null | wc -l)
    if [[ $n -gt 0 ]]; then
      printf "  %-45s  AINDA TEM %d arquivos ⚠\n" "$full" "$n"
      REMAINING=$((REMAINING + n))
    else
      printf "  %-45s  vazio ✓\n" "$full"
    fi
  fi
done

echo
if [[ $REMAINING -eq 0 ]]; then
  echo "═══════════════════════════════════════════════════════════════════════"
  echo "  OK — base zerada com sucesso."
  echo "  Backup disponível em: $BACKUP_FILE"
  echo "═══════════════════════════════════════════════════════════════════════"
else
  echo "⚠ Alguns arquivos permanecem (${REMAINING}). Verifique manualmente." >&2
  exit 3
fi
