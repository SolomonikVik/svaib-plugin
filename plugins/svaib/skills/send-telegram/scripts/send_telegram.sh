#!/bin/bash
# Отправка сообщения в Telegram через Bot API (plain, parse_mode=HTML).
# Использование: ./send_telegram.sh [--to alias[,alias...]] [--] "текст сообщения"
#
# Без --to адресат берётся из TELEGRAM_CHAT_ID — прежнее поведение, реестр не нужен.
# С --to алиасы разрешаются реестром (recipients.py) в ИМЕНА ключей окружения,
# а значения подставляются здесь: логика поиска .env живёт только в shell и не
# раздваивается на python.
#
# Хардёнинг (3 дефекта, аудит Codex 2026-06-15):
#   1. Нарезка по СТРОКАМ до конвертации — чанки не рвут <b>…</b> / HTML-entity.
#   2. Проверяет ok в ответе Telegram — ненулевой exit при недоставке любого чанка.
#   3. .env: сначала уже заданные env-переменные, затем .env в pwd, затем в git-root.
#
# Коды возврата:
#   0 — доставлено всем
#   1 — есть подтверждённые отказы доставки (повтор безопасен)
#   2 — есть НЕИЗВЕСТНЫЕ исходы: обрыв, 5xx, неразборное тело. Повторять нельзя,
#       сообщение могло дойти. Двойка важнее единицы.
#   3 — НЕ ОТПРАВЛЕНО НИКОМУ: ошибка вызова или предусловия (нет токена, пустой
#       текст, неизвестный алиас, незаданный ключ, чужой бот). Разведено с 1 и 2
#       намеренно: headless-потребителю нужно отличать «почини конфиг» от
#       «проверь чат глазами до любого повтора».
set -uo pipefail

MAX_LENGTH=4096
CHANNEL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_channel_common.sh
source "$CHANNEL_SCRIPT_DIR/_channel_common.sh"

# --- 0. Диагностика маршрута ------------------------------------------------
# `--check` ничего не отправляет: печатает, чем канал будет слать и что для
# этого настроено. Нужен ровно там, где раньше приходилось гадать: сообщение
# ушло не туда или не ушло вовсе, а конфигурация видна только чтением .env.
if [ "${1:-}" = "--check" ]; then
  channel_report_route
  exit 0
fi

# --- 1. Разбор аргументов ---------------------------------------------------
channel_parse_args "$@" || exit 3
INPUT="$CHANNEL_TEXT"
case "$INPUT" in
  *[![:space:]]*) ;;
  *) echo "ERROR: empty or whitespace-only message" >&2; exit 3 ;;
esac

# --- 2. Креды и адресаты ----------------------------------------------------
channel_load_env
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not set (checked env, ./.env, git-root/.env)" >&2
  exit 3
fi

channel_resolve_recipients "$CHANNEL_TO" || exit $?

# --- 3. Конвертация markdown → HTML (построчно) -----------------------------
# Python3 уже используется каналом; конвертация не зависит от GNU/BSD sed.
# Теги замыкаются внутри строки, поэтому не пересекают границу чанка.
# Байтовый паритет с gateway держит tests/test_plain_render.py.
convert() {
  python3 -c 'from __future__ import annotations

import html
import re
import sys

_BOLD = re.compile(r"\*\*([^*]*)\*\*")
_ITALIC = re.compile(r"__([^_]*)__")
_STRIKE = re.compile(r"~~((?:[^~]|~(?!~))*)~~")
_LINK_START = re.compile(r"(!?)\[([^\[\]]+)\]\(")
_EMOJI_TARGET = re.compile(r"tg://emoji\?id=([0-9]+)")
_UNCHECKED = re.compile(r"^- \[ \] ")
_RULE = re.compile(r"^---$")

def render_plain(markdown: str) -> str:
    """Текст, который уйдёт в `sendMessage` с `parse_mode=HTML`.

    Хвостовые переводы строк срезаются: в канале конвертация идёт через
    подстановку команды, а она их съедает. Сохранив их, шлюз отправил бы не те
    байты, что скрипт, — и подмена канала стала бы видна сверкой.
    """
    lines = [_render_line(line) for line in markdown.split("\n")]
    return "\n".join(lines).rstrip("\n")


def _render_line(line: str) -> str:
    """Ссылки защищают адреса от преобразования маркеров **/__ /~~."""
    # Маркер отсутствует во входе, поэтому пользователь не может подставить
    # готовый HTML вместо ссылки. Защищаем узлы до inline-форматирования,
    # чтобы **[слово](url)** работало, а __ внутри URL не стало тегом.
    marker = "\x00"
    if marker in line:
        # Все занятые суффиксы собираем одним проходом. Повторный поиск
        # каждого кандидата во всей строке превращал рендер в O(n²).
        used = set(re.findall(r"\x00([0-9]+):", line))
        serial = 0
        while str(serial) in used:
            serial += 1
        marker = f"\x00{serial}:"
    nodes: list[str] = []
    parts: list[str] = []
    cursor = 0
    ends = _link_ends(line)
    for match in _LINK_START.finditer(line):
        if match.start() < cursor:
            continue
        end = ends.get(match.end() - 1)
        if end is None:
            continue
        target = line[match.end():end - 1]
        if not target:
            continue
        image, label = match.group(1, 2)
        emoji = _EMOJI_TARGET.fullmatch(target)
        if image:
            if emoji is None:
                continue  # Обычные картинки plain не поддерживает.
            node = (
                f"<tg-emoji emoji-id=\"{emoji[1]}\">"
                f"{html.escape(label, quote=False)}</tg-emoji>"
            )
        else:
            node = f"<a href=\"{html.escape(target, quote=True)}\">{_styles(label)}</a>"
        parts.extend((line[cursor:match.start()], f"{marker}{len(nodes)}{marker}"))
        nodes.append(node)
        cursor = end
    parts.append(line[cursor:])
    marked = _styles("".join(parts))
    placeholder = re.compile(re.escape(marker) + r"([0-9]+)" + re.escape(marker))
    marked = placeholder.sub(lambda match: nodes[int(match[1])], marked)
    # Чекбокс `- [ ]` Telegram не рисует, а квадратные скобки в списке читаются
    # как опечатка; отмеченный `- [x]` канал оставляет как есть.
    unchecked = _UNCHECKED.sub("- ", marked)
    # Горизонтальная линейка сама по себе не отображается, но зазор между
    # секциями держит — пустой строкой.
    return _RULE.sub("", unchecked)


def _link_ends(line: str) -> dict[int, int]:
    """Парные скобки без пробелов: один проход даже для незакрытых ссылок."""
    stack: list[int] = []
    ends: dict[int, int] = {}
    for index, character in enumerate(line):
        if character.isspace():
            stack.clear()
        elif character == "(":
            stack.append(index)
        elif character == ")" and stack:
            ends[stack.pop()] = index + 1
    return ends


def _styles(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _STRIKE.sub(r"<s>\1</s>", _ITALIC.sub(r"<i>\1</i>", _BOLD.sub(r"<b>\1</b>", escaped)))


def prepare_chunks(markdown: str, limit: int) -> list[str]:
    """Прежняя нарезка по длине HTML; каждая строка рендерится один раз."""
    chunks: list[str] = []
    buffer: list[str] = []
    size = trimmed_size = 0
    has_buffer = False  # Как прежний непустой сырой BUFFER, даже для ---.
    for line in markdown.split("\n"):
        rendered = _render_line(line)
        candidate_size = (size + 1 if has_buffer else 0) + len(rendered)
        candidate_trimmed = candidate_size if rendered else (trimmed_size if has_buffer else 0)
        if candidate_trimmed > limit and has_buffer:
            chunks.append("\n".join(buffer).rstrip("\n"))
            buffer = [rendered]
            size = trimmed_size = len(rendered)
            has_buffer = bool(line)
        else:
            if has_buffer:
                buffer.append(rendered)
            else:
                buffer = [rendered]
            size, trimmed_size = candidate_size, candidate_trimmed
            has_buffer = has_buffer or bool(line)
    if has_buffer:
        chunks.append("\n".join(buffer).rstrip("\n"))
    return chunks


# Декодирование и запись байтов заданы явно: PYTHONIOENCODING/локаль хоста
# не меняют смысл разметки и байтовый паритет со шлюзом.
markdown = sys.stdin.buffer.read().decode("utf-8")
if sys.argv[1:2] == ["--chunks"]:
    if "\x00" in markdown:
        raise ValueError("NUL cannot be passed in shell message arguments")
    for chunk in prepare_chunks(markdown, int(sys.argv[2])):
        sys.stdout.buffer.write(chunk.encode("utf-8") + b"\x00")
else:
    sys.stdout.buffer.write(render_plain(markdown).encode("utf-8"))
' "$@"
}

# --- 4. Подготовка чанков: один раз на все адресаты -------------------------
# Один процесс готовит все чанки до отправки. Закрытый файл mktemp хранит NUL-
# разделённые UTF-8 чанки: read -d работает и в Bash 3.2. Ошибку конвертации
# проверяем до чтения файла, поэтому частичный результат не уйдёт в сеть.
CHUNK_FILE="$(mktemp)" || { echo "ERROR: cannot create chunk file" >&2; exit 3; }
trap 'rm -f "$CHUNK_FILE"' EXIT
if ! printf '%s' "$INPUT" | convert --chunks "$MAX_LENGTH" > "$CHUNK_FILE"; then
  echo "ERROR: markdown conversion failed; nothing sent" >&2
  exit 3
fi
CHUNKS=()
while IFS= read -r -d '' chunk; do
  CHUNKS+=("$chunk")
done < "$CHUNK_FILE"
rm -f "$CHUNK_FILE"
trap - EXIT

# --- 5. Отправка одного чанка -----------------------------------------------
# Печатает тело ответа. Коды: 0 — ok:true, 1 — подтверждённый отказ,
# 2 — исход НЕИЗВЕСТЕН (curl не смог получить ответ).
send_chunk() {
  local chat="$1" html="$2" raw curl_rc http body
  # -w выносит HTTP-код последней строкой: без него 5xx и HTML-страница прокси
  # неотличимы от честного отказа Telegram, и «исход неизвестен» превращался бы
  # в «не доставлено» — то есть в разрешение повторить уже принятое.
  # -m: без потолка зависшее соединение вешает CronJob бессрочно.
  raw=$(curl -s -m 30 -w '\n%{http_code}' \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$chat" \
    -d parse_mode=HTML \
    --data-urlencode text="$html")
  curl_rc=$?
  http="${raw##*$'\n'}"
  body="${raw%$'\n'*}"
  echo "$body"

  # Обрыв связи: ответа нет вовсе — принял ли Telegram, неизвестно.
  if [ "$curl_rc" -ne 0 ] || [ -z "$body" ]; then
    return 2
  fi
  # 5xx — сбой на стороне сервера: запрос мог быть применён. 4xx Telegram
  # разобрал и отверг — это подтверждённый отказ, повтор безопасен.
  case "$http" in
    5*) return 2 ;;
  esac

  RESP="$body" python3 - <<'PY'
import os, json, sys
try:
    parsed = json.loads(os.environ["RESP"])
except Exception:
    # Тело не разбирается: прокси, обрыв, HTML-заглушка. Telegram мог принять
    # сообщение — считать это отказом значило бы разрешить повтор поверх него.
    sys.exit(2)
if not isinstance(parsed, dict):
    sys.exit(2)
sys.exit(0 if parsed.get("ok") is True else 1)
PY
}

# --- 6. Цикл по адресатам ---------------------------------------------------
# Частичный отказ не откатывается: в Telegram отозвать доставленное нельзя.
# Сводка различает ТРИ исхода, а не два — «не доставлено» можно повторить,
# «исход неизвестен» повторять опасно.
SENT=0
ANY_FAILED=0
ANY_UNKNOWN=0
SUMMARY=""

for i in "${!ALIASES[@]}"; do
  alias="${ALIASES[$i]}"
  chat="${CHATS[$i]}"
  ok_parts=0
  bad_parts=0
  unknown_parts=0

  for c in "${CHUNKS[@]}"; do
    [ "$SENT" -gt 0 ] && sleep 1   # анти-throttle: между чанками и между адресатами
    SENT=$((SENT + 1))
    send_chunk "$chat" "$c"
    case $? in
      0) ok_parts=$((ok_parts + 1)) ;;
      2) unknown_parts=$((unknown_parts + 1)) ;;
      *) bad_parts=$((bad_parts + 1)) ;;
    esac
  done

  total="${#CHUNKS[@]}"
  if [ "$unknown_parts" -gt 0 ]; then
    ANY_UNKNOWN=1
    SUMMARY="${SUMMARY}  ❓ $alias: исход неизвестен ($ok_parts/$total доставлено, $unknown_parts без ответа) — не повторять вслепую"$'\n'
  elif [ "$bad_parts" -gt 0 ]; then
    ANY_FAILED=1
    SUMMARY="${SUMMARY}  ❌ $alias: $ok_parts/$total доставлено"$'\n'
  else
    SUMMARY="${SUMMARY}  ✅ $alias: $ok_parts/$total доставлено"$'\n'
  fi
done

# Сводка — в stdout, рядом с телами ответов. НЕ в stderr: потребители канала
# (dev/infra/runner/alerts.py, скилл meeting-agenda) берут «stderr или stdout»,
# и сводка в stderr вытеснила бы тело ответа Telegram — вместе с ним пропала бы
# диагностика вида «bot is not a member», по которой классифицируется отказ.
if [ "${#ALIASES[@]}" -gt 1 ] || [ "$ANY_FAILED" -eq 1 ] || [ "$ANY_UNKNOWN" -eq 1 ]; then
  printf 'Доставка:\n%s' "$SUMMARY"
fi

[ "$ANY_UNKNOWN" -eq 1 ] && exit 2
[ "$ANY_FAILED" -eq 1 ] && exit 1
exit 0
