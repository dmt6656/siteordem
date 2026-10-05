from functools import wraps
import json
import os
import random
import re
import shutil
import queue
import sqlite3
import threading
import uuid
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g, send_from_directory
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps

from database import get_db as _connect_db, init_db

try:
    from flask_sock import Sock  # WebSocket do chat (pip install flask-sock)
except ImportError:  # sem a biblioteca o chat segue funcionando por polling
    Sock = None

# As imagens enviadas pelos usuários (avatares, itens, banners, mural,
# símbolos de ritual) ficam fora desta pasta, em "preservar/static", para
# que esta pasta (atualizavel/) possa ser sobrescrita em atualizações sem
# apagar nenhuma imagem já enviada. O static_folder do Flask passa a
# apontar para lá; o CSS/JS deste projeto são servidos por rotas próprias
# logo abaixo.
PRESERVAR_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'preservar', 'static'
)
CODE_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

app = Flask(__name__, static_folder=PRESERVAR_STATIC_DIR)
app.secret_key = os.environ.get('SECRET_KEY', 'troque-esta-chave-em-producao')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB por requisição (o editor de imagem já reduz o tamanho antes de enviar)


@app.route('/static/style.css')
def _serve_style_css():
    return send_from_directory(CODE_STATIC_DIR, 'style.css')


@app.route('/static/notify.js')
def _serve_notify_js():
    return send_from_directory(CODE_STATIC_DIR, 'notify.js')


@app.route('/static/image-editor.js')
def _serve_image_editor_js():
    return send_from_directory(CODE_STATIC_DIR, 'image-editor.js')


@app.route('/static/favicon.svg')
def _serve_favicon_svg():
    return send_from_directory(CODE_STATIC_DIR, 'favicon.svg', mimetype='image/svg+xml')


@app.route('/static/favicon.png')
def _serve_favicon_png():
    return send_from_directory(CODE_STATIC_DIR, 'favicon.png', mimetype='image/png')


@app.route('/favicon.ico')
def _serve_favicon_ico():
    # Alguns navegadores pedem /favicon.ico por padrão; devolvemos o PNG.
    return send_from_directory(CODE_STATIC_DIR, 'favicon.png', mimetype='image/png')


@app.route('/static/sounds/rolagemdedados.mp3')
def _serve_dice_roll_sound():
    return send_from_directory(os.path.join(CODE_STATIC_DIR, 'sounds'), 'rolagemdedados.mp3')


@app.route('/static/dice/d20_roll.webm')
def _serve_dice_roll_video():
    # Vídeo do dado (WebM com transparência) usado no portrait do OBS.
    resp = send_from_directory(os.path.join(CODE_STATIC_DIR, 'dice'), 'd20_roll.webm',
                               mimetype='video/webm')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/static/fonts/TwinMarker.ttf')
def _serve_twinmarker_font():
    # Fonte usada nos textos do portrait (página do OBS).
    resp = send_from_directory(os.path.join(CODE_STATIC_DIR, 'fonts'), 'TwinMarker.ttf',
                               mimetype='font/ttf')
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


def _asset_version(filename):
    """Retorna a data de modificação (em segundos) de um arquivo estático
    do projeto (style.css, notify.js, image-editor.js). Usado como parâmetro
    "?v=" nos links desses arquivos nos templates: assim, toda vez que o
    arquivo muda no servidor, a URL muda junto e o navegador é obrigado a
    baixar a versão nova, em vez de continuar usando uma cópia antiga
    guardada em cache (o que fazia atualizações de CSS/JS às vezes não
    aparecerem pro usuário mesmo depois de reenviar o projeto)."""
    try:
        return int(os.path.getmtime(os.path.join(CODE_STATIC_DIR, filename)))
    except OSError:
        return 0


app.jinja_env.globals['asset_version'] = _asset_version


@app.context_processor
def _inject_chat_dock():
    """Ícones de ritual usados pelo chat fixo (_chat_dock.html), que aparece
    em todas as abas."""
    return {'chat_dock_ritual_icons': RITUAL_ELEMENT_ICONS}


@app.context_processor
def _inject_sem_sanidade():
    """Disponibiliza `sem_sanidade` (modo \"Jogar sem Sanidade\") em todos os templates."""
    try:
        row = get_db().execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()
        flag = bool(row and row['sem_sanidade'])
    except Exception:
        flag = False
    # No modo "Jogar sem Sanidade", PE e Sanidade viram PD (Pontos de Determinação).
    # PD reaproveita os mesmos valores do PE; só o nome exibido muda.
    return {
        'sem_sanidade': flag,
        'pe_label': 'PD' if flag else 'PE',
        'pe_t': Markup('<span class="t-pe">PE</span><span class="t-pd">PD</span>'),
    }


def _pe_label():
    """Nome exibido do PE: 'PD' no modo Jogar sem Sanidade, senão 'PE'."""
    try:
        row = get_db().execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()
        return 'PD' if row and row['sem_sanidade'] else 'PE'
    except Exception:
        return 'PE'


# ---------------------------------------------------------------------------
# PD (Pontos de Determinação) — regras do modo "Jogar sem Sanidade".
#   PD iniciais = base da classe + Presença (em NEX 5%)
#   A cada novo NEX (5% em 5%, até 99%) = ganho da classe + Presença
# ---------------------------------------------------------------------------
PD_RULES = {
    'Combatente':   {'inicial': 6,  'por_nex': 3},
    'Especialista': {'inicial': 8,  'por_nex': 4},
    'Ocultista':    {'inicial': 10, 'por_nex': 5},
}


def _pd_max_formula(user):
    """PD máximo pelas regras, ou None se a classe ainda não foi escolhida."""
    rule = PD_RULES.get((user['character_class'] or '').strip())
    if not rule:
        return None
    nex = user['character_nex'] or CHARACTER_NEX_OPTIONS[0]
    try:
        steps = CHARACTER_NEX_OPTIONS.index(nex)
    except ValueError:
        steps = 0
    pre = max(0, user['presenca'] or 0)
    return rule['inicial'] + pre + steps * (rule['por_nex'] + pre)


def _apply_pd_rules(db):
    """Com o modo ligado: guarda o PE original (uma vez) e mantém o PD máximo
    de cada jogador de acordo com classe, NEX e Presença. Só recalcula quando
    um desses três muda (pd_key), então um ajuste manual do mestre no PD
    máximo continua valendo até a próxima mudança. Ao subir o máximo, o PD
    atual sobe junto."""
    users = db.execute(
        'SELECT id, character_class, character_nex, presenca, pe, pe_max, points_locked, '
        'pe_max_backup, pd_key FROM users WHERE is_admin = 0'
    ).fetchall()
    changed = False
    for u in users:
        if u['pe_max_backup'] is None:
            db.execute('UPDATE users SET pe_backup = ?, pe_max_backup = ? WHERE id = ?',
                       (u['pe'], u['pe_max'], u['id']))
            changed = True
        if not u['points_locked']:
            continue
        target = _pd_max_formula(u)
        if target is None:
            continue
        key = f"{u['character_class']}|{u['character_nex'] or 0}|{u['presenca'] or 0}"
        if key == u['pd_key']:
            continue
        if u['pd_key'] is None:
            new_pe = target  # primeira vez no modo: PD começa cheio
        else:
            new_pe = max(0, min(target, (u['pe'] or 0) + (target - (u['pe_max'] or 0))))
        db.execute('UPDATE users SET pe_max = ?, pe = ?, pd_key = ? WHERE id = ?',
                   (target, new_pe, key, u['id']))
        changed = True
    if changed:
        db.commit()


def _restore_pe_from_backup(db):
    """Modo desligado: devolve o PE/PE máximo que cada jogador tinha antes."""
    db.execute(
        'UPDATE users SET pe = pe_backup, pe_max = pe_max_backup, '
        'pe_backup = NULL, pe_max_backup = NULL, pd_key = NULL '
        'WHERE pe_max_backup IS NOT NULL'
    )
    db.execute('UPDATE users SET pd_key = NULL WHERE pd_key IS NOT NULL')
    db.commit()


@app.after_request
def _keep_pd_in_sync(resp):
    """Depois de qualquer alteração (POST) com o modo ligado, reaplica as regras
    do PD. Falhas aqui nunca derrubam a requisição."""
    try:
        if request.method == 'POST' and resp.status_code < 400 \
                and not (request.path or '').startswith('/static/'):
            db = get_db()
            row = db.execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()
            if row and row['sem_sanidade']:
                _apply_pd_rules(db)
    except Exception:
        try:
            get_db().rollback()
        except Exception:
            pass
    return resp


# ---------------------------------------------------------------------------
# Otimização de respostas: compressão gzip + cache de arquivos estáticos.
# - Páginas HTML, CSS, JS e JSON grandes saem comprimidos (bem menores na rede).
# - style.css / notify.js / image-editor.js já são pedidos com "?v=<mtime>",
#   então podem ficar em cache "para sempre": quando o arquivo muda, a URL muda.
# - Imagens enviadas pelos jogadores têm nome único (uuid), então também podem
#   ficar em cache no navegador por alguns dias.
# ---------------------------------------------------------------------------
import gzip as _gzip

_GZIP_MIMETYPES = {
    'text/html', 'text/css', 'text/plain', 'text/javascript',
    'application/javascript', 'application/json', 'image/svg+xml',
}
_GZIP_MIN_BYTES = 1024
_gzip_cache = {}  # ETag -> corpo comprimido (só para arquivos estáticos)


@app.after_request
def _optimize_response(resp):
    try:
        path = request.path or ''

        # ---- Cache de estáticos ----
        if resp.status_code in (200, 304) and request.method == 'GET':
            if request.args.get('v') and path.startswith('/static/') and \
                    path.rsplit('.', 1)[-1] in ('css', 'js', 'png', 'svg'):
                resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            elif request.endpoint == 'static':
                resp.headers['Cache-Control'] = 'public, max-age=604800'

        # ---- gzip ----
        if resp.status_code != 200 or request.method != 'GET':
            return resp
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return resp
        if resp.headers.get('Content-Encoding') or resp.is_streamed and not resp.direct_passthrough:
            return resp
        if 'Range' in request.headers or resp.mimetype not in _GZIP_MIMETYPES:
            return resp

        etag = resp.headers.get('ETag')
        resp.direct_passthrough = False
        data = resp.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return resp

        compressed = _gzip_cache.get(etag) if etag else None
        if compressed is None:
            compressed = _gzip.compress(data, compresslevel=5)
            if etag:
                if len(_gzip_cache) > 64:
                    _gzip_cache.clear()
                _gzip_cache[etag] = compressed

        resp.set_data(compressed)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = str(len(compressed))
        resp.headers.add('Vary', 'Accept-Encoding')
        if etag and not etag.startswith('W/'):
            resp.headers['ETag'] = 'W/' + etag
    except Exception:
        # Qualquer falha aqui nunca deve derrubar a página: devolve a resposta original.
        pass
    return resp


def get_db():
    """Devolve a conexão SQLite da requisição atual, abrindo-a apenas uma vez
    por requisição (e reaproveitando entre chamadas). Antes, cada rota abria
    e fechava sua própria conexão manualmente — o que, além de repetitivo,
    vazava a conexão sempre que um erro acontecia entre o open e o close.
    Agora o fechamento é automático em teardown_appcontext, mesmo em caso
    de exceção."""
    if 'db' not in g:
        g.db = _connect_db()
    return g.db


@app.teardown_appcontext
def _close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

ITEM_IMAGES_DIR = os.path.join(app.static_folder, 'item_images')
os.makedirs(ITEM_IMAGES_DIR, exist_ok=True)
AVATAR_IMAGES_DIR = os.path.join(app.static_folder, 'avatar_images')
os.makedirs(AVATAR_IMAGES_DIR, exist_ok=True)
RITUAL_SYMBOLS_DIR = os.path.join(app.static_folder, 'ritual_symbols')
os.makedirs(RITUAL_SYMBOLS_DIR, exist_ok=True)
CAMPAIGN_BANNERS_DIR = os.path.join(app.static_folder, 'campaign_banners')
os.makedirs(CAMPAIGN_BANNERS_DIR, exist_ok=True)
MURAL_IMAGES_DIR = os.path.join(app.static_folder, 'mural_images')
os.makedirs(MURAL_IMAGES_DIR, exist_ok=True)
NPC_IMAGES_DIR = os.path.join(app.static_folder, 'npc_images')
os.makedirs(NPC_IMAGES_DIR, exist_ok=True)
COMBAT_IMAGES_DIR = os.path.join(app.static_folder, 'combat_images')
os.makedirs(COMBAT_IMAGES_DIR, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

RITUAL_ELEMENTS = ['Conhecimento', 'Energia', 'Medo', 'Morte', 'Sangue']
ABILITY_KINDS = ['habilidade', 'poder']

# Elementos que o jogador pode escolher como afinidade a partir de 50% de NEX.
# Medo fica de fora: não é uma afinidade elemental escolhível, só existe como
# elemento de ritual.
AFFINITY_ELEMENTS = [el for el in RITUAL_ELEMENTS if el != 'Medo']

# A partir de qual NEX o pop-up de escolha de afinidade elemental passa a
# poder aparecer (o jogador também pode abri-lo manualmente depois disso,
# pelo botão ao lado do NEX, caso feche o pop-up sem escolher).
ELEMENT_AFFINITY_NEX_THRESHOLD = 50

# Lista fixa de estados que um NPC da Equipe de Suporte pode assumir, cada
# um com uma cor própria — mostrada como uma etiqueta colorida no painel do
# admin e na aba Agentes (visível aos jogadores).
NPC_STATES = [
    ('Vivo', '#3ecf5e'),
    ('Morto', '#1a1a1a'),
    ('Inconsciente', '#9e9e9e'),
    ('Possuído', '#8e44ad'),
    ('Atordoado', '#f1c40f'),
    ('Ferido', '#e74c3c'),
    ('Doente', '#a6e05a'),
    ('Envenenado', '#c58af9'),
    ('Paralisado', '#3b82f6'),
    ('Dormindo', '#1e3a8a'),
    ('Catatônico', '#8b5e3c'),
    ('Em transe', '#ec4899'),
]
NPC_STATE_COLORS = dict(NPC_STATES)

# Ícone (SVG) de cada estado — os arquivos usam fill/stroke="currentColor",
# então a cor final vem do CSS (ver --state-color no template), e o
# width/height="1em" original é removido para o tamanho ficar 100% sob
# controle do CSS (.npc-state-icon svg), sem crescer o chip do estado.
NPC_STATE_ICON_FILES = {
    'Vivo': 'vivo.svg',
    'Morto': 'morto.svg',
    'Inconsciente': 'inconsciente.svg',
    'Possuído': 'possuido.svg',
    'Atordoado': 'atordoado.svg',
    'Ferido': 'ferido.svg',
    'Doente': 'doente.svg',
    'Envenenado': 'envenenado.svg',
    'Paralisado': 'paralisado.svg',
    'Dormindo': 'dormindo.svg',
    'Catatônico': 'catatonico.svg',
    'Em transe': 'emtranse.svg',
}
NPC_STATE_ICONS_DIR = os.path.join(CODE_STATIC_DIR, 'npc_state_icons')


def _load_npc_state_icons():
    icons = {}
    for label, filename in NPC_STATE_ICON_FILES.items():
        path = os.path.join(NPC_STATE_ICONS_DIR, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                svg = f.read()
            svg = svg.replace(' width="1em" height="1em"', '', 1)
            icons[label] = Markup(svg)
        except OSError:
            pass
    return icons


NPC_STATE_ICONS = _load_npc_state_icons()

# Cor de cada elemento de ritual, igual às variáveis --elem-* do CSS —
# usada para colorir a mensagem de chat quando um ritual é usado.
RITUAL_ELEMENT_COLORS = {
    'Energia': '#a566e8',
    'Conhecimento': '#e6c229',
    'Medo': '#f2f2f2',
    'Morte': '#0a0a0a',
    'Sangue': '#e2453c',
}

# Custo em PE de cada círculo de ritual — escolhido pelo jogador/mestre ao
# cadastrar o ritual; o gasto de PE continua automático a partir disso.
RITUAL_CIRCLE_PE_COSTS = {1: 1, 2: 3, 3: 6, 4: 10}

# Tipos de inimigo que o mestre pode escolher ao adicionar algo ao combate.
ENEMY_TYPES = {
    'monstro': 'Monstro',
    'criatura': 'Criatura',
    'comum': 'Inimigo comum',
}


def _normalize_enemy_type(value):
    value = (value or '').strip().lower()
    return value if value in ENEMY_TYPES else 'criatura'

RITUAL_ELEMENT_ICONS = {
    'conhecimento': '<g transform="translate(0.000000,800.000000) scale(0.100000,-0.100000)"\nfill="currentColor" stroke="none">\n<path d="M3719 7856 c-29 -10 -143 -69 -157 -81 -1 -1 17 -38 42 -81 24 -43\n42 -81 40 -83 -2 -2 -26 17 -54 43 -28 26 -55 44 -60 41 -16 -10 -11 -73 11\n-149 12 -39 23 -73 25 -75 2 -2 19 8 38 23 19 14 37 26 40 26 3 0 6 -20 6 -45\n0 -31 -8 -55 -25 -81 -38 -56 -34 -108 10 -150 32 -30 35 -37 35 -93 0 -59 0\n-60 -35 -71 -19 -6 -40 -10 -47 -8 -7 2 -45 37 -84 79 l-71 75 -2 93 c-1 101\n-4 96 57 74 6 -3 12 -1 12 5 0 5 -7 12 -15 16 -8 3 -15 12 -15 21 0 9 -6 15\n-13 13 -6 -2 -22 7 -35 20 -37 37 -222 172 -237 172 -25 0 -22 -25 8 -90 32\n-69 44 -83 65 -75 7 3 26 -2 42 -10 23 -12 33 -27 45 -73 9 -31 25 -82 36\n-113 10 -31 19 -70 19 -87 0 -17 5 -34 10 -37 6 -3 10 -15 10 -26 0 -11 7 -19\n16 -19 9 0 42 -24 75 -53 94 -86 91 -85 216 -47 59 17 119 33 132 34 13 2 28\n11 34 22 6 10 13 21 17 24 4 3 15 30 25 60 9 30 32 78 50 105 43 67 78 136 71\n142 -3 3 -16 -1 -30 -10 -28 -19 -36 -14 -36 25 0 17 -4 27 -10 23 -5 -3 -10\n0 -10 7 0 8 14 13 38 14 72 1 119 31 168 110 24 39 44 76 44 82 0 18 -31 14\n-69 -9 -45 -27 -84 -42 -76 -29 13 22 -15 7 -96 -48 -141 -97 -168 -117 -169\n-123 0 -3 27 -5 60 -5 53 1 61 -2 65 -19 23 -100 23 -108 8 -140 -9 -19 -21\n-31 -29 -28 -11 4 -61 -87 -79 -144 -9 -32 -80 -34 -90 -3 -3 11 -3 25 0 30 4\n6 1 18 -6 26 -20 24 -21 64 -1 64 10 0 25 17 36 40 28 58 18 116 -27 155 -34\n30 -67 98 -54 112 4 3 20 -6 35 -20 51 -49 63 -36 73 78 4 44 9 89 11 100 5\n33 -33 30 -67 -5 -16 -16 -32 -30 -37 -30 -10 0 2 30 24 59 10 13 18 39 18 58\n0 19 7 41 15 49 18 19 20 54 3 54 -7 -1 -29 -7 -49 -14z m5 -485 c10 -11 16\n-31 14 -48 -3 -25 -7 -28 -38 -28 -31 0 -35 3 -38 28 -3 26 20 67 38 67 4 0\n15 -9 24 -19z"/>\n<path d="M4955 7788 c-16 -10 -37 -17 -46 -14 -17 4 -25 -14 -49 -121 l-12\n-53 -51 0 c-28 0 -66 -5 -84 -12 -28 -10 -36 -10 -42 1 -7 10 -10 9 -14 -3 -4\n-9 -13 -16 -22 -16 -25 0 -17 -19 11 -26 14 -4 49 -4 77 -2 29 3 63 2 76 -1\n19 -5 12 -9 -35 -19 -109 -22 -183 -115 -190 -241 -3 -50 -2 -61 6 -46 8 16 9\n13 6 -18 -6 -43 24 -94 91 -160 l39 -37 -18 -88 c-10 -48 -18 -96 -18 -107 0\n-38 27 7 50 80 28 94 44 117 74 109 27 -6 73 13 60 26 -5 5 -22 10 -38 12 -16\n2 -31 8 -33 15 -3 8 20 11 79 10 63 -1 93 4 122 18 22 10 34 21 27 23 -7 2 0\n17 18 38 35 40 61 118 61 186 0 56 -20 66 -21 11 l0 -38 -13 35 c-28 74 -69\n141 -97 155 -50 26 -39 31 62 27 l100 -4 -12 25 c-13 30 -34 38 -108 43 -29 2\n-56 5 -58 8 -3 3 4 42 16 86 12 45 21 89 19 99 -3 17 -4 17 -33 -1z m-125\n-342 c0 -28 -26 -58 -64 -75 -44 -19 -46 -22 -46 -60 0 -47 3 -49 32 -21 41\n38 47 21 24 -69 -18 -66 -21 -69 -46 -46 -25 22 -27 4 -5 -39 32 -61 7 -75\n-32 -17 -32 47 -47 130 -33 181 13 48 70 116 114 135 32 14 56 18 56 11z m122\n-43 c54 -47 59 -55 53 -72 -3 -7 0 -20 6 -27 13 -15 -4 -57 -50 -129 -35 -54\n-38 -56 -96 -53 -44 3 -50 6 -53 26 -3 22 -1 23 43 21 43 -3 50 0 81 34 19 20\n34 46 34 57 0 40 -29 90 -64 111 -20 12 -34 24 -33 27 2 4 7 15 10 25 9 25 22\n21 69 -20z m-64 -102 c21 -13 10 -52 -20 -74 -15 -12 -30 -19 -32 -16 -2 2 1\n25 7 52 10 47 20 55 45 38z"/>\n<path d="M4160 7669 c0 -5 5 -7 10 -4 6 3 10 8 10 11 0 2 -4 4 -10 4 -5 0 -10\n-5 -10 -11z"/>\n<path d="M2523 7604 c-43 -35 -75 -53 -86 -50 -10 2 -27 -4 -38 -15 -10 -10\n-24 -19 -29 -19 -6 0 -18 -4 -28 -9 -9 -5 -64 -32 -122 -59 -78 -37 -117 -62\n-148 -96 -41 -44 -60 -83 -27 -56 18 15 36 4 33 -18 -5 -28 35 -26 62 3 13 14\n30 25 38 25 8 0 32 15 52 33 64 55 92 77 96 77 2 0 13 -19 25 -42 12 -24 35\n-58 50 -76 44 -50 38 -67 -13 -39 -110 57 -208 0 -233 -137 -7 -34 -6 -38 9\n-33 13 5 16 -1 16 -43 0 -27 -4 -52 -10 -55 -12 -7 -13 -65 -2 -81 32 -48 104\n-28 123 34 13 45 -1 85 -26 76 -11 -5 -15 2 -15 27 0 19 5 41 10 49 7 11 5 17\n-8 21 -26 9 38 69 73 69 56 0 155 -68 155 -107 0 -15 -2 -15 -10 -3 -21 33\n-17 -1 11 -101 33 -120 81 -216 113 -229 12 -4 28 -15 35 -23 23 -29 107 -30\n174 -2 12 5 17 3 17 -8 0 -8 -5 -17 -11 -19 -6 -2 -3 -13 9 -28 17 -20 23 -23\n33 -12 7 6 20 12 29 12 45 0 66 76 31 106 -18 16 -18 18 -1 58 30 67 36 158\n14 203 l-18 38 -22 -92 c-25 -110 -41 -137 -90 -153 -97 -32 -180 37 -234 196\n-23 65 -23 67 -11 60 5 -3 13 -25 19 -48 11 -43 41 -94 42 -71 0 7 -9 35 -20\n62 -21 53 -20 55 24 65 61 15 103 62 111 124 4 38 4 39 16 22 18 -26 2 71 -20\n120 -11 25 -21 59 -21 76 0 39 -27 64 -68 64 -59 0 -81 -66 -38 -117 13 -15\n26 -38 30 -50 4 -13 13 -22 19 -20 7 1 13 -14 15 -41 3 -36 -1 -47 -27 -73\n-47 -47 -69 -37 -95 41 -31 92 -73 180 -86 180 -6 0 -1 -19 11 -42 28 -54 80\n-201 73 -207 -9 -10 -22 16 -43 86 -11 39 -35 102 -51 139 -22 49 -28 70 -20\n78 8 8 9 6 4 -8 -6 -16 -4 -18 12 -12 10 5 42 16 70 26 62 22 111 46 119 60 4\n7 -3 8 -22 4 l-28 -7 33 27 c30 26 47 66 21 50 -8 -5 -10 0 -6 17 11 42 -26\n32 -100 -27z"/>\n<path d="M4110 7650 c0 -5 4 -10 9 -10 6 0 13 5 16 10 3 6 -1 10 -9 10 -9 0\n-16 -4 -16 -10z"/>\n<path d="M5704 7346 c-89 -17 -114 -25 -114 -37 0 -5 10 -9 23 -9 21 -1 21 -1\n2 -12 -44 -25 -41 -51 5 -41 25 4 30 2 30 -14 0 -10 36 -90 79 -178 73 -146\n85 -185 59 -185 -5 0 -23 13 -39 29 -44 44 -107 60 -229 58 -122 -2 -161 -17\n-247 -93 -47 -40 -53 -50 -53 -84 0 -73 78 -105 132 -53 23 22 40 28 90 31 37\n3 70 11 80 20 15 12 35 14 115 8 53 -4 99 -9 101 -11 3 -2 -3 -16 -12 -30 -14\n-21 -24 -25 -64 -25 -26 -1 -66 -5 -90 -9 -47 -9 -77 -30 -54 -38 9 -4 10 -9\n2 -18 -13 -16 9 -22 105 -29 l70 -5 -50 13 c-31 9 -40 14 -25 14 14 1 41 -5\n60 -11 32 -11 35 -15 32 -47 -1 -19 -10 -52 -19 -73 -13 -29 -14 -42 -5 -62\n13 -29 29 -31 58 -11 18 13 22 30 28 116 4 62 2 108 -4 120 -12 22 16 93 34\n87 18 -6 46 -127 53 -228 5 -89 4 -100 -17 -134 -19 -30 -21 -41 -12 -60 13\n-28 57 -43 81 -28 21 13 26 68 8 93 -11 15 -13 39 -10 84 7 76 -6 211 -17 182\n-8 -23 -13 -16 -30 39 -7 22 -19 50 -26 63 -11 16 -12 27 -4 42 6 11 14 20 18\n20 4 0 27 -27 52 -61 51 -70 116 -119 158 -119 28 0 92 18 92 26 0 2 -54 56\n-120 120 -66 64 -120 121 -120 127 0 5 25 -14 56 -43 30 -29 58 -49 62 -45 4\n3 -19 31 -51 62 -33 31 -56 62 -53 69 3 7 42 70 87 139 81 125 119 165 157\n165 23 0 37 15 23 24 -5 3 -44 11 -87 17 -44 6 -85 16 -92 22 -21 16 -227 18\n-308 3z m121 -16 c-54 -16 -146 -27 -151 -19 -3 5 5 11 18 13 13 3 32 7 43 9\n11 3 43 5 70 5 44 0 47 -1 20 -8z m88 -82 c32 -6 56 -14 55 -18 -35 -85 -95\n-200 -104 -200 -14 0 -154 208 -154 229 0 11 13 13 73 7 39 -3 98 -11 130 -18z\nm-216 -390 c24 -12 49 -29 56 -37 10 -12 6 -13 -28 -8 -79 13 -277 28 -296 22\n-10 -4 -19 -2 -19 3 0 10 57 34 105 46 40 10 139 -4 182 -26z"/>\n<path d="M3470 7071 c0 -5 5 -13 10 -16 6 -3 10 -2 10 4 0 5 -4 13 -10 16 -5\n3 -10 2 -10 -4z"/>\n<path d="M1547 7048 c-44 -12 -77 -52 -58 -71 6 -6 11 -18 11 -27 0 -9 11 -27\n25 -40 14 -13 25 -28 25 -33 0 -11 -65 -91 -141 -175 -33 -36 -49 -49 -45 -34\n4 12 3 22 -2 22 -18 0 -33 -56 -33 -127 0 -60 5 -82 24 -118 17 -29 21 -45 14\n-48 -15 -5 26 -51 68 -77 72 -43 188 -65 231 -42 10 6 40 18 65 26 26 9 55 26\n64 36 18 20 45 27 45 11 0 -15 66 -92 96 -112 32 -21 49 -25 39 -9 -3 6 3 10\n14 10 11 0 22 -4 25 -9 3 -5 24 -4 48 1 24 6 64 11 90 11 31 1 53 8 65 21 17\n18 17 18 -7 11 l-25 -8 20 22 c18 21 18 25 4 49 -21 38 -62 41 -93 8 -20 -22\n-32 -26 -80 -26 -41 0 -60 5 -71 18 -9 9 -29 32 -46 50 -35 39 -29 54 27 72\n42 14 64 7 64 -18 0 -27 33 -53 70 -54 39 -1 56 16 63 65 7 47 -22 79 -68 76\n-16 -1 -46 1 -66 5 -28 6 -51 2 -97 -17 l-60 -24 -98 98 -98 99 16 27 c49 84\n4 110 -50 28 -18 -27 -37 -49 -42 -47 -6 1 -10 -9 -10 -22 0 -40 156 -216 179\n-202 5 4 18 -5 27 -19 16 -24 16 -27 -2 -41 -74 -57 -170 -66 -266 -24 -50 22\n-60 31 -77 71 -17 38 -18 52 -9 80 21 66 39 95 113 176 48 53 72 88 69 98 -4\n8 1 26 9 39 28 41 49 133 38 155 -11 19 -50 52 -61 50 -3 -1 -22 -6 -43 -11z"/>\n<path d="M4302 6718 c-7 -13 -19 -43 -27 -68 -9 -25 -29 -68 -45 -97 -17 -29\n-30 -59 -30 -66 0 -7 -11 -23 -24 -36 l-23 -22 -12 93 c-7 51 -21 110 -31 130\n-11 20 -20 44 -20 52 0 47 -39 1 -62 -74 -2 -8 17 -61 43 -119 l48 -104 -20\n-28 c-19 -27 -26 -89 -12 -103 4 -4 12 12 18 35 5 24 15 44 20 46 6 2 19 -30\n29 -71 24 -95 29 -104 51 -86 22 18 27 10 60 -109 32 -114 36 -121 46 -93 5\n13 -2 58 -21 126 -26 99 -27 106 -11 120 24 22 34 20 28 -4 -9 -34 7 -22 27\n20 11 22 23 40 28 40 5 0 6 5 3 10 -4 6 -2 16 3 23 5 7 9 19 9 28 -1 26 37\n109 48 106 11 -4 97 152 90 163 -3 4 9 26 26 50 35 49 31 61 -14 38 -18 -9\n-40 -19 -49 -22 -25 -8 -66 -94 -113 -240 -33 -101 -47 -132 -66 -142 -44 -25\n-47 -20 -42 69 4 70 47 285 68 340 10 25 -10 21 -23 -5z m-92 -351 c-6 -38 -7\n-80 -4 -94 5 -19 3 -24 -7 -21 -7 2 -20 35 -28 73 l-14 68 28 23 c16 13 30 22\n32 21 1 -2 -2 -33 -7 -70z"/>\n<path d="M4885 6605 c-14 -13 -25 -33 -25 -43 0 -11 -18 -60 -40 -110 -22 -49\n-40 -96 -40 -105 0 -19 -39 -108 -54 -123 -7 -6 -18 -31 -25 -55 -7 -24 -33\n-91 -57 -148 -46 -109 -52 -139 -14 -74 12 21 26 45 31 53 5 8 43 83 84 165\n115 232 168 327 203 362 18 17 32 35 32 40 0 12 -49 63 -61 63 -5 0 -21 -11\n-34 -25z"/>\n<path d="M3613 6592 c-12 -9 -32 -43 -44 -74 -12 -31 -26 -56 -32 -55 -11 2\n-33 -46 -121 -261 -30 -72 -50 -134 -46 -137 19 -12 138 137 209 263 67 118\n91 147 91 108 0 -8 -9 -34 -21 -60 -20 -46 -43 -155 -34 -164 2 -3 31 20 65\n51 33 31 73 64 90 74 46 27 40 46 -9 28 -22 -7 -49 -19 -60 -25 -31 -16 -27 6\n9 47 24 27 30 43 30 79 0 55 -20 106 -51 128 -30 20 -48 20 -76 -2z"/>\n<path d="M5030 6468 c-23 -29 -44 -59 -47 -65 -8 -19 -20 -16 -26 7 -7 29 -45\n26 -59 -5 -14 -30 5 -59 33 -50 23 8 24 -3 3 -30 -15 -20 -16 -20 -39 7 -12\n15 -26 24 -29 21 -13 -13 -5 -61 13 -77 18 -17 18 -18 -2 -67 -16 -37 -32 -56\n-63 -74 -26 -15 -44 -34 -48 -51 -10 -39 0 -41 34 -9 34 33 37 25 15 -55 -8\n-30 -15 -63 -15 -73 0 -28 17 -11 25 25 l7 33 14 -39 c23 -64 47 -80 84 -56\n13 8 13 13 -2 37 -25 44 -38 102 -28 129 8 21 13 22 35 14 42 -16 103 -1 141\n35 19 18 34 37 34 43 0 6 -18 30 -40 53 -30 32 -45 40 -64 37 -46 -9 -49 6\n-12 68 19 32 51 81 70 109 66 91 36 120 -34 33z m-12 -258 c35 -16 43 -24 40\n-42 -6 -45 -76 -59 -122 -25 -17 12 -17 18 -7 50 14 43 27 45 89 17z"/>\n<path d="M2680 6415 c-17 -25 -35 -45 -39 -45 -23 0 -131 -213 -131 -256 0 -8\n9 -23 19 -33 19 -19 20 -19 33 -2 12 16 25 17 151 8 103 -8 137 -13 137 -24 0\n-7 -9 -13 -20 -13 -21 0 -100 -65 -100 -83 0 -6 -8 -20 -19 -31 -56 -63 -54\n-224 5 -263 24 -15 27 -15 52 4 23 17 27 28 27 69 1 27 9 64 18 82 10 18 15\n37 12 43 -9 13 55 119 71 119 23 0 22 -28 -2 -76 -60 -119 4 -133 74 -15 l21\n35 38 -42 c43 -47 57 -51 73 -21 9 16 8 25 -6 40 -9 10 -21 19 -27 19 -5 0\n-22 11 -39 25 -16 14 -42 28 -56 32 -28 6 -42 20 -42 40 0 34 210 -52 229 -93\n20 -45 91 -21 76 25 -6 21 -38 36 -54 27 -5 -3 -26 6 -46 20 -46 31 -130 65\n-139 56 -11 -10 -90 -1 -98 11 -10 16 11 27 52 27 45 1 115 32 129 58 10 19\n15 72 7 72 -2 0 -34 -11 -70 -25 -79 -30 -77 -29 -69 -8 5 12 2 15 -9 10 -38\n-14 -98 -30 -102 -26 -1 2 -25 44 -51 92 -52 97 -67 146 -53 171 7 12 5 16 -6\n16 -8 0 -29 -20 -46 -45z m60 -186 c23 -33 40 -62 37 -65 -6 -7 -180 -22 -190\n-17 -10 6 85 143 99 143 6 0 30 -27 54 -61z m118 -237 c-14 -15 -45 -57 -68\n-94 -23 -38 -44 -68 -47 -68 -10 0 10 67 33 107 23 40 92 99 103 88 2 -3 -7\n-18 -21 -33z"/>\n<path d="M6870 6321 c-8 -5 -37 -12 -65 -15 -27 -4 -59 -9 -70 -12 -16 -4 -17\n-3 -5 5 32 22 -6 22 -147 1 -275 -41 -408 -65 -408 -75 0 -6 25 -19 55 -30 50\n-18 78 -20 313 -19 141 1 257 -2 257 -6 0 -18 -50 -37 -124 -48 -76 -12 -196\n-56 -196 -73 0 -4 26 -13 57 -19 31 -7 93 -27 137 -46 44 -19 84 -34 88 -34\n21 0 3 24 -51 67 -33 26 -58 50 -56 53 2 4 33 2 69 -3 57 -9 71 -8 108 9 60\n27 124 93 138 143 14 51 1 77 -45 97 -40 16 -36 16 -55 5z"/>\n<path d="M852 6218 c-18 -18 -15 -33 15 -67 24 -27 67 -144 96 -258 8 -34 7\n-46 -6 -67 -24 -36 -23 -167 2 -222 10 -22 19 -53 19 -68 0 -16 6 -32 13 -37\n7 -4 10 -11 7 -15 -4 -3 -41 19 -83 49 -91 67 -333 191 -360 185 -17 -3 -15\n-7 12 -21 18 -9 38 -28 45 -42 7 -13 32 -55 54 -92 84 -138 138 -298 128 -376\n-4 -29 -11 -58 -17 -64 -6 -9 -6 -13 1 -13 6 0 13 4 16 9 3 5 20 11 38 14 18\n3 53 12 78 20 25 9 77 19 115 23 39 4 64 9 58 11 -7 3 -13 10 -13 18 0 17 -41\n24 -127 19 -47 -2 -73 1 -73 7 0 6 -11 20 -25 31 -18 14 -25 29 -25 54 0 44\n-54 186 -80 209 -11 10 -20 23 -20 29 0 6 -11 28 -25 49 -14 20 -21 37 -16 37\n12 0 41 -28 41 -40 0 -4 15 -17 33 -27 17 -11 76 -54 131 -97 54 -42 100 -75\n103 -73 4 5 -40 43 -147 130 -52 42 -6 16 85 -47 78 -55 125 -70 125 -41 0 8\n-11 49 -25 92 -31 92 -46 238 -25 231 8 -2 26 -40 41 -85 41 -123 106 -284\n150 -368 21 -43 39 -79 39 -80 0 -2 -27 -3 -60 -3 -33 0 -63 -2 -66 -5 -3 -3\n0 -14 7 -25 10 -16 32 -24 84 -33 38 -6 84 -20 102 -30 47 -29 46 -7 -4 93\n-24 49 -43 90 -43 93 0 3 -27 65 -59 138 -32 73 -69 164 -81 202 -12 39 -29\n80 -37 93 -29 46 -5 46 38 1 35 -37 49 -64 68 -125 13 -44 30 -99 39 -124 16\n-46 52 -90 74 -90 8 0 4 8 -8 18 -24 18 -54 73 -54 94 1 7 8 0 17 -17 26 -50\n54 -75 85 -75 15 0 28 -5 28 -12 0 -9 5 -8 15 2 20 21 125 259 125 285 0 33\n-10 40 -283 189 -74 41 -127 60 -127 46 0 -23 70 -71 192 -133 94 -48 139 -76\n146 -92 7 -16 8 -26 1 -30 -5 -4 -21 -30 -35 -58 -15 -29 -38 -64 -52 -79\nl-26 -28 -23 22 c-13 12 -23 32 -23 46 0 14 -13 36 -30 52 -16 15 -30 34 -30\n42 0 19 -69 107 -113 142 -31 25 -40 28 -47 16 -10 -17 -14 -9 -62 139 -17 53\n-38 107 -46 119 -12 19 -13 28 -4 43 17 26 15 43 -6 62 -22 20 -63 22 -80 5z"/>\n<path d="M1101 6030 c-14 -34 -7 -76 9 -50 8 13 60 16 120 7 36 -5 19 20 -30\n46 -68 34 -82 34 -99 -3z"/>\n<path d="M5555 6013 c-58 -13 -91 -29 -126 -60 l-39 -35 -64 23 c-36 13 -68\n20 -72 16 -4 -4 4 -20 17 -35 31 -34 109 -155 109 -169 0 -13 -54 -25 -76 -16\n-29 11 -12 -12 93 -121 117 -122 135 -128 120 -37 -9 54 -7 61 26 128 37 73\n52 84 127 98 19 4 43 9 54 12 16 4 17 1 11 -37 -6 -37 -5 -42 14 -47 34 -9 13\n-23 -34 -23 -24 0 -47 -4 -50 -10 -3 -5 -19 -10 -35 -10 -35 0 -60 -9 -60 -22\n0 -5 3 -7 8 -5 4 3 79 10 167 17 88 7 162 15 165 19 3 3 14 11 24 17 11 6 21\n15 23 21 4 11 -65 23 -139 23 -48 0 -48 0 -42 28 9 36 10 37 72 50 65 15 75\n15 67 2 -3 -5 -2 -10 3 -10 10 0 62 71 62 84 0 11 -66 1 -160 -25 -204 -54\n-241 -55 -212 -9 19 31 -5 25 -33 -9 -14 -15 -50 -46 -81 -67 -50 -34 -57 -37\n-65 -21 -5 9 -23 31 -39 49 -36 39 -37 47 -6 41 18 -4 35 6 71 40 39 38 55 46\n98 51 66 8 114 -6 148 -44 30 -32 59 -40 59 -15 0 28 -68 84 -122 100 -28 8\n-54 14 -57 14 -3 -1 -15 -3 -26 -6z m-26 -240 c0 -5 -10 -30 -23 -58 l-23 -50\n-16 32 c-23 43 -22 47 16 66 36 18 47 20 46 10z m-105 -97 c14 -25 26 -47 26\n-50 0 -9 -108 77 -109 87 -1 4 12 7 28 7 24 0 34 -8 55 -44z"/>\n<path d="M1272 5978 c6 -18 28 -21 28 -4 0 9 -7 16 -16 16 -9 0 -14 -5 -12\n-12z"/>\n<path d="M1980 5869 c0 -25 -3 -30 -17 -25 -10 3 8 -18 40 -48 l58 -54 -31\n-63 c-28 -59 -32 -89 -12 -89 4 0 16 16 25 35 22 46 32 44 33 -8 1 -23 11 -61\n23 -85 22 -43 92 -99 110 -88 5 4 12 3 15 -2 6 -11 58 -7 112 8 43 12 44 12\n83 -23 22 -19 49 -41 61 -47 19 -10 21 -10 12 7 -6 10 -21 30 -35 43 -38 40\n-60 77 -48 85 12 7 15 55 3 55 -4 0 -13 -9 -20 -21 -7 -12 -15 -19 -18 -16 -6\n6 29 67 39 67 4 0 7 20 7 45 0 58 -38 118 -99 157 -45 28 -64 30 -43 5 10 -11\n2 -13 -43 -9 -36 3 -60 0 -70 -8 -27 -22 -25 -6 6 43 34 55 36 67 10 67 -18 0\n-51 -32 -78 -76 -10 -18 -14 -16 -60 29 -27 26 -52 47 -56 47 -4 0 -7 -14 -7\n-31z m343 -147 c10 -9 27 -33 37 -52 16 -31 16 -39 5 -68 -8 -17 -21 -32 -30\n-32 -19 0 -19 19 0 41 45 49 -42 128 -106 95 -26 -13 -49 -11 -49 5 0 17 63\n39 94 34 16 -3 38 -13 49 -23z m-155 -99 c2 -31 9 -46 28 -57 24 -17 28 -15\n25 17 -3 23 4 22 45 -9 36 -28 43 -44 19 -44 -8 0 -15 -4 -15 -10 0 -5 16 -10\n35 -10 19 0 35 -4 35 -10 0 -16 -82 -11 -119 8 -47 23 -72 61 -79 117 -4 38\n-2 47 9 43 8 -3 15 -22 17 -45z m124 22 c6 -14 8 -28 4 -32 -10 -10 -50 33\n-42 46 11 18 26 13 38 -14z"/>\n<path d="M7360 5860 c-25 -4 -48 -6 -52 -4 -4 3 -9 0 -13 -6 -3 -5 -14 -10\n-23 -10 -10 0 -40 -36 -75 -89 -32 -49 -73 -109 -92 -132 -32 -41 -38 -44 -83\n-44 -36 0 -59 -7 -90 -28 -52 -34 -55 -52 -5 -26 37 19 113 26 113 11 0 -4\n-40 -48 -90 -97 -49 -49 -90 -92 -90 -96 0 -4 14 -9 30 -12 17 -4 30 -12 30\n-18 0 -12 -138 -175 -189 -223 -17 -17 -31 -34 -31 -38 0 -23 76 34 171 129\nl106 105 34 -17 c31 -17 32 -18 14 -31 -10 -8 -22 -14 -27 -14 -4 0 -8 -4 -8\n-9 0 -6 12 -6 31 1 17 6 50 8 74 6 32 -4 45 -2 49 9 4 10 10 11 25 3 26 -14\n31 -13 25 5 -4 9 -1 15 7 15 8 0 72 7 144 15 71 8 131 12 134 10 4 -5 -13 -12\n-49 -19 -14 -3 22 -3 80 0 57 3 110 10 117 16 8 7 40 8 88 3 83 -7 98 4 43 33\n-17 9 -43 28 -57 41 -23 22 -30 23 -110 18 -80 -5 -349 -61 -415 -87 -25 -9\n-36 -7 -83 17 -29 15 -53 31 -53 35 0 4 27 21 59 38 33 16 75 40 93 53 19 12\n123 52 232 87 150 50 191 66 172 71 -13 4 -50 0 -83 -7 -123 -30 -365 -46\n-386 -25 -6 6 2 16 19 27 55 35 214 176 209 185 -3 4 10 26 30 47 33 37 45 64\n28 61 -5 -1 -28 -5 -53 -9z m-217 -356 c26 -9 47 -19 47 -22 0 -4 -27 -18 -60\n-31 -34 -12 -84 -39 -112 -58 -38 -25 -57 -32 -71 -26 -16 7 -9 18 56 81 41\n39 79 72 84 72 5 0 30 -7 56 -16z"/>\n<path d="M3898 5657 l-36 -62 -48 3 c-86 5 -154 -59 -154 -145 0 -39 -3 -44\n-39 -63 -22 -11 -42 -24 -45 -28 -3 -5 33 -15 79 -21 64 -10 85 -17 85 -28 0\n-12 3 -13 13 -5 8 7 24 8 43 3 l29 -8 -37 -8 c-20 -4 -41 -4 -46 -1 -5 3 -17\n-3 -26 -13 -17 -19 -16 -19 26 -16 24 1 76 3 116 4 80 1 93 14 42 43 -20 11\n-38 36 -55 74 -23 53 -24 58 -10 88 9 17 22 40 31 51 15 20 15 20 33 -4 10\n-13 23 -21 29 -17 6 4 -4 -21 -24 -55 -19 -34 -33 -64 -31 -66 2 -2 36 38 74\n89 100 132 111 197 18 104 -37 -38 -46 -42 -56 -30 -10 12 -8 24 10 60 12 24\n27 41 33 38 11 -7 1 55 -11 68 -4 4 -23 -20 -43 -55z m-70 -129 c-13 -18 -24\n-35 -26 -37 -1 -3 -9 4 -17 15 -8 10 -15 17 -15 14 0 -3 -2 -37 -3 -77 -2 -40\n-5 -74 -7 -76 -3 -2 -24 -2 -48 1 -36 3 -41 6 -30 19 6 8 11 33 9 57 -1 35 4\n48 32 80 26 31 42 40 78 45 24 4 45 3 47 -1 2 -4 -7 -22 -20 -40z m-13 -149\nc19 -36 15 -44 -15 -34 -14 4 -19 12 -16 22 3 8 6 21 6 29 0 22 8 17 25 -17z"/>\n<path d="M4470 5630 c0 -5 -9 -19 -20 -30 -13 -13 -20 -33 -20 -58 0 -50 -36\n-155 -61 -178 -18 -17 -21 -17 -36 -1 -16 16 -15 20 25 60 42 43 52 73 30 95\n-17 17 -74 15 -92 -4 -20 -20 -21 -38 -1 -31 25 10 27 -11 3 -45 -13 -18 -25\n-35 -28 -38 -3 -3 -6 -18 -7 -33 -1 -35 42 -60 105 -61 55 -1 73 20 107 130\n29 94 30 107 10 80 -17 -22 -22 -2 -6 23 10 16 67 9 95 -12 17 -13 17 -15 -1\n-53 -39 -79 -113 -201 -136 -222 l-24 -23 -48 21 c-36 15 -51 18 -59 9 -8 -8\n-4 -15 14 -27 13 -10 36 -27 50 -39 l25 -21 -30 -31 -29 -31 27 -28 28 -27 28\n33 27 34 18 -40 c17 -37 58 -69 81 -63 6 2 15 4 20 5 19 3 35 17 35 31 0 7\n-26 41 -59 76 l-58 63 33 61 c18 33 40 83 49 110 10 28 23 57 31 66 8 8 14 28\n14 42 0 38 19 34 80 -14 36 -28 48 -44 42 -52 -5 -7 -28 -47 -51 -89 -36 -69\n-44 -108 -21 -108 5 0 14 -2 22 -5 8 -3 30 20 58 61 42 63 45 71 47 140 l2 73\n-47 39 c-99 83 -160 115 -218 119 -32 2 -54 -1 -54 -7z"/>\n<path d="M1305 5560 c3 -5 8 -10 11 -10 2 0 4 5 4 10 0 6 -5 10 -11 10 -5 0\n-7 -4 -4 -10z"/>\n<path d="M3165 5496 c6 -63 6 -64 -15 -59 -19 5 -21 2 -18 -23 3 -25 7 -29 32\n-26 19 2 27 -1 24 -10 -3 -7 -15 -12 -26 -10 -12 2 -22 -1 -22 -5 0 -15 23\n-33 43 -33 28 0 50 -62 38 -107 -7 -26 -7 -41 0 -50 13 -15 32 -6 25 12 -12\n33 10 20 35 -20 16 -25 34 -45 40 -45 7 0 4 9 -7 22 -19 21 -19 21 1 13 46\n-18 63 -17 75 4 9 18 8 22 -7 27 -55 18 -75 29 -84 47 -11 19 1 36 23 37 13 0\n38 58 38 89 0 26 -4 31 -35 37 -25 4 -43 1 -61 -11 -15 -9 -30 -13 -35 -8 -4\n4 -14 42 -20 83 -13 75 -23 100 -41 100 -7 0 -8 -24 -3 -64z m168 -138 c7 -24\n-6 -49 -30 -59 -35 -13 -39 -12 -51 16 -10 22 -9 26 16 39 34 19 59 20 65 4z"/>\n<path d="M3027 5523 c-10 -10 -8 -51 2 -57 5 -4 22 -39 37 -79 15 -39 36 -85\n46 -102 10 -16 18 -39 18 -50 0 -11 13 -45 29 -75 15 -30 34 -73 41 -95 11\n-36 30 -69 30 -52 0 16 -32 125 -58 200 -55 151 -83 253 -76 270 3 10 1 24 -6\n32 -13 15 -51 20 -63 8z"/>\n<path d="M4591 5382 c-12 -23 4 -52 29 -52 23 0 43 32 34 54 -8 22 -51 20 -63\n-2z"/>\n<path d="M2743 5353 c-10 -33 -9 -43 14 -88 14 -27 45 -81 69 -118 41 -63 55\n-109 36 -121 -12 -7 -95 75 -158 159 -58 76 -89 91 -40 18 14 -21 26 -41 26\n-46 0 -5 12 -33 27 -63 14 -30 23 -57 20 -61 -4 -3 -30 9 -59 27 -50 31 -137\n64 -146 55 -7 -7 41 -85 52 -85 6 0 43 -9 82 -21 59 -17 72 -25 81 -49 6 -15\n21 -33 33 -40 l23 -12 -22 36 c-13 20 -20 36 -17 36 4 0 33 -14 66 -30 57 -29\n60 -29 60 -10 0 25 6 25 71 -4 72 -31 99 -39 99 -29 0 5 -36 27 -80 48 -44 21\n-80 45 -80 53 0 9 7 11 20 7 18 -6 17 -1 -9 47 -42 75 -57 107 -71 153 -16 50\n-73 175 -80 175 -3 0 -10 -17 -17 -37z m116 -380 c2 -2 2 -5 -1 -8 -3 -3 -26\n4 -52 17 -40 19 -46 26 -46 52 l0 30 47 -44 c26 -25 50 -46 52 -47z"/>\n<path d="M6058 5383 c-22 -6 -23 -17 -3 -33 22 -18 18 -29 -12 -36 -16 -4 -39\n-13 -52 -21 -18 -12 -25 -12 -31 -3 -5 9 -12 3 -22 -16 -8 -16 -20 -28 -26\n-26 -7 1 -12 -5 -12 -13 0 -24 39 -95 55 -102 8 -3 15 -14 15 -24 0 -10 7 -19\n15 -19 8 0 23 -5 33 -11 26 -16 107 21 205 94 80 59 102 95 24 40 -23 -16 -49\n-33 -57 -37 -22 -11 38 41 83 71 37 25 38 26 53 7 27 -34 64 -93 64 -102 0\n-20 -269 -136 -362 -157 -66 -14 -63 -16 -102 63 -18 36 -32 52 -46 52 -24 0\n-25 -16 -4 -60 10 -18 17 -49 18 -68 1 -33 -1 -35 -43 -46 -46 -11 -75 -40\n-55 -54 6 -4 10 -17 9 -29 -2 -28 19 -30 71 -8 l36 15 -7 -43 c-3 -23 -8 -54\n-11 -69 -3 -17 -1 -28 5 -28 6 0 11 -11 11 -25 0 -30 37 -50 59 -31 11 9 13 7\n7 -12 -6 -18 -4 -21 11 -15 47 16 48 24 30 242 -2 19 4 26 25 32 44 12 168 71\n168 80 0 11 114 69 134 69 8 0 24 14 37 31 12 17 25 27 29 22 12 -12 60 -186\n54 -195 -3 -4 -11 -8 -18 -8 -13 0 -80 -28 -183 -76 -52 -24 -69 -53 -42 -69\n6 -4 8 -13 5 -21 -7 -18 17 -17 69 2 22 8 61 21 86 29 80 26 177 122 162 162\n-5 12 -3 14 7 8 30 -19 -20 188 -77 318 -24 53 -43 73 -43 45 0 -7 -11 4 -25\n25 -25 36 -45 48 -45 26 0 -6 -10 -10 -22 -9 -41 4 -47 1 -125 -71 -88 -80\n-137 -109 -186 -109 -42 0 -45 3 -39 41 4 24 8 30 24 26 30 -8 140 10 155 26\n23 22 3 78 -37 105 -33 22 -40 24 -72 15z"/>\n<path d="M1809 5367 c-23 -18 -30 -55 -12 -66 11 -6 12 -10 4 -15 -14 -9 -14\n-65 0 -162 l12 -77 18 24 c10 13 19 30 19 38 0 14 22 11 74 -9 22 -8 56 -50\n41 -50 -9 0 14 -41 46 -81 24 -31 26 -39 16 -53 -10 -14 -21 -16 -50 -11 -29\n6 -41 14 -54 42 -25 54 -52 67 -128 58 -45 -4 -65 -3 -65 4 0 7 11 16 25 21\n14 5 25 13 25 18 0 5 -30 30 -66 56 -72 52 -89 50 -73 -8 8 -30 8 -31 -8 -8\n-24 34 -92 102 -102 102 -5 0 -8 -39 -8 -87 l2 -88 32 3 c18 2 43 0 55 -4 21\n-7 21 -8 -12 -19 -19 -6 -36 -19 -38 -28 -3 -16 8 -21 111 -46 l37 -9 0 34 0\n35 30 -18 c17 -9 30 -22 30 -28 0 -6 13 -23 28 -38 29 -28 70 -35 90 -15 8 8\n21 5 51 -15 35 -23 39 -29 32 -54 -6 -24 -15 -29 -62 -39 -30 -6 -61 -14 -69\n-18 -17 -7 -130 49 -130 65 0 5 5 9 10 9 6 0 10 5 10 10 0 6 -6 10 -12 10 -20\n0 -222 -82 -238 -97 -23 -20 1 -35 59 -36 42 -1 54 3 67 21 8 12 23 22 32 22\n20 0 149 -29 177 -41 44 -17 67 -20 79 -9 8 6 18 9 23 8 5 -2 31 8 58 22 28\n14 52 26 53 27 2 1 14 43 27 93 13 50 26 94 29 97 3 4 -14 29 -37 57 -24 28\n-50 65 -59 81 -46 91 -68 115 -68 75 0 -21 -7 -25 -27 -12 -13 8 -6 92 8 92 4\n0 -7 22 -24 49 -18 26 -36 55 -42 64 -8 13 -13 14 -26 4z m54 -405 c13 -14 14\n-21 5 -30 -7 -7 -17 -12 -24 -12 -18 0 -37 28 -30 45 7 20 29 19 49 -3z"/>\n<path d="M5048 5296 c-10 -7 -18 -16 -18 -20 0 -8 -50 -66 -200 -235 -170\n-189 -157 -183 79 43 80 76 158 143 174 148 29 11 37 44 15 66 -15 15 -29 15\n-50 -2z"/>\n<path d="M4580 5195 c-18 -22 -8 -50 19 -50 28 0 47 25 38 49 -8 20 -40 21\n-57 1z"/>\n<path d="M5120 5170 c-53 -35 -56 -36 -63 -10 -3 13 -11 18 -20 14 -23 -9 -30\n-37 -13 -50 17 -12 11 -24 -11 -24 -7 0 -13 7 -13 16 0 9 -6 14 -12 11 -8 -2\n-12 -18 -10 -37 3 -38 -38 -90 -71 -90 -27 0 -57 -18 -57 -35 0 -18 12 -19 28\n-3 24 24 22 -1 -3 -37 -27 -41 -37 -85 -10 -49 14 19 14 19 15 -3 0 -36 11\n-53 34 -53 21 0 22 4 18 53 -5 65 13 89 50 63 29 -20 77 -20 106 0 36 25 14\n86 -38 106 l-25 9 35 30 c19 16 54 43 78 60 23 16 42 36 42 44 0 21 -9 19 -60\n-15z m-70 -164 c13 -15 20 -32 17 -40 -9 -22 -50 -20 -72 4 -17 19 -17 21 0\n40 23 25 27 25 55 -4z"/>\n<path d="M4120 5059 c-14 -4 -41 -10 -60 -13 -44 -8 -130 -61 -130 -81 0 -8 6\n-15 13 -15 6 0 38 -21 71 -48 l58 -47 -57 -7 c-66 -9 -106 -34 -130 -83 -13\n-29 -14 -39 -5 -51 18 -21 27 -18 78 25 49 42 98 66 106 53 3 -5 -11 -16 -30\n-27 -51 -27 -45 -48 11 -39 43 6 45 5 45 -19 0 -30 17 -44 33 -28 8 8 5 25\n-11 63 -22 52 -23 66 -3 53 16 -9 51 -81 51 -103 0 -30 24 -46 40 -27 10 13\n10 19 -2 33 -9 9 -19 27 -23 40 -3 12 -23 38 -42 57 -20 19 -34 37 -31 40 2 3\n21 -4 42 -15 30 -15 43 -17 62 -8 28 13 27 30 -2 38 -10 3 -36 16 -58 29 l-38\n24 6 56 c4 31 14 66 22 79 19 29 16 32 -16 21z m-48 -100 c-2 -19 -5 -36 -7\n-38 -6 -6 -68 33 -72 45 -4 11 53 34 72 29 6 -1 8 -17 7 -36z m-22 -139 c0 -5\n-15 -13 -32 -18 -18 -6 -42 -13 -52 -18 -33 -13 -7 16 28 32 37 16 56 17 56 4z"/>\n<path d="M3496 5005 c-16 -12 -17 -18 -7 -46 15 -43 16 -42 -24 -63 -19 -11\n-35 -25 -35 -33 0 -15 16 -17 25 -3 12 19 26 11 15 -9 -31 -60 9 -143 75 -155\n28 -6 38 -15 55 -49 l20 -42 -5 44 c-4 33 -1 48 11 62 10 11 13 19 9 19 -5 0\n3 12 17 28 30 31 34 46 24 84 -9 33 -32 53 -68 62 -33 8 -36 22 -8 31 23 7 27\n35 5 35 -9 0 -27 -7 -40 -16 -23 -15 -24 -15 -31 20 -8 44 -14 48 -38 31z\nm122 -147 c7 -7 12 -16 12 -22 0 -6 -5 -5 -12 2 -7 7 -20 12 -29 12 -10 0 -21\n5 -24 10 -9 14 39 12 53 -2z m-91 -38 c-12 -31 -2 -54 13 -30 8 12 10 12 16\n-3 3 -9 3 -25 0 -34 -4 -9 -1 -20 6 -25 11 -6 11 -8 0 -8 -42 0 -80 88 -50\n118 21 21 27 13 15 -18z m73 -15 c0 -8 -4 -15 -10 -15 -5 0 -10 7 -10 15 0 8\n5 15 10 15 6 0 10 -7 10 -15z m25 -35 c-10 -11 -22 -17 -27 -14 -4 3 2 14 14\n24 29 23 37 17 13 -10z"/>\n<path d="M6268 4999 c-21 -12 -24 -49 -6 -67 28 -28 88 0 88 41 0 30 -49 46\n-82 26z"/>\n<path d="M6085 4804 c-19 -20 -16 -53 6 -67 42 -26 107 34 73 68 -20 20 -58\n19 -79 -1z"/>\n<path d="M5319 4791 c-17 -13 -24 -30 -24 -52 0 -30 -7 -39 -70 -82 -38 -26\n-70 -51 -70 -55 0 -4 -3 -11 -6 -17 -4 -5 -9 -27 -13 -48 -6 -34 -3 -42 39\n-91 l46 -53 84 -7 c46 -3 91 -9 100 -13 14 -5 16 -2 10 15 -9 30 11 28 38 -3\n16 -19 36 -27 76 -32 48 -5 53 -4 49 13 -2 10 -13 24 -26 30 -12 6 -30 18 -39\n26 -29 25 -113 71 -119 65 -3 -3 -1 -15 5 -26 10 -17 7 -23 -14 -37 -21 -14\n-32 -15 -69 -5 -25 6 -56 11 -70 11 -33 0 -40 14 -19 35 12 13 27 16 57 12 37\n-5 43 -3 77 34 33 36 66 57 40 25 -19 -22 2 -27 61 -14 64 14 64 14 48 33 -11\n13 -9 15 11 17 13 0 29 2 34 3 6 2 16 3 24 4 17 1 -8 32 -53 64 l-29 20 -18\n-21 c-22 -27 -33 -28 -26 -2 8 31 -6 32 -52 2 -38 -24 -54 -52 -31 -52 6 0 10\n-4 10 -10 0 -5 -23 -10 -50 -10 -46 0 -53 -3 -68 -30 -20 -34 -59 -48 -79 -27\n-21 20 18 102 55 115 15 5 35 13 45 17 12 5 17 2 17 -11 0 -33 16 0 38 78 31\n107 28 117 -19 79z m21 -260 c0 -6 -5 -13 -10 -16 -15 -9 -43 3 -35 15 8 13\n45 13 45 1z"/>\n<path d="M2397 4773 c-22 -25 -22 -51 3 -88 11 -16 20 -39 20 -50 0 -18 135\n-275 155 -292 4 -5 5 23 2 60 -4 47 -20 102 -52 179 -24 61 -43 113 -41 116 9\n8 27 -10 45 -46 21 -41 77 -98 82 -85 2 4 2 49 -1 98 -5 95 -6 95 -38 15 l-10\n-25 -6 30 c-10 41 -19 56 -47 77 -38 29 -91 34 -112 11z"/>\n<path d="M7717 4754 c-17 -23 -45 -35 -31 -13 8 13 -62 6 -171 -18 -98 -22\n-187 -34 -280 -38 -44 -2 -102 -11 -130 -19 -27 -7 -106 -21 -175 -30 -69 -9\n-128 -20 -132 -24 -12 -12 217 -2 342 14 63 8 216 19 339 24 189 8 230 8 267\n-5 53 -18 66 -14 98 35 l25 38 -24 26 c-13 14 -30 27 -37 27 -7 1 -26 3 -43 5\n-23 2 -34 -3 -48 -22z"/>\n<path d="M360 4730 c-23 -17 -24 -24 -22 -100 2 -82 2 -82 -21 -76 -49 12\n-159 25 -173 19 -10 -4 -8 -13 12 -39 14 -18 23 -38 19 -44 -3 -5 -5 -10 -3\n-11 2 0 43 -6 93 -12 l90 -12 9 -57 c5 -32 13 -58 17 -58 4 0 10 -17 14 -38 8\n-49 21 -70 34 -57 7 7 7 32 0 77 -14 89 -8 130 10 68 25 -87 149 -171 249\n-169 33 0 91 16 67 18 -5 1 -3 6 5 11 8 5 22 7 31 5 19 -6 101 65 131 113 21\n33 21 34 92 32 39 -1 89 -2 111 -1 58 2 25 18 -78 38 -88 16 -111 28 -102 51\n6 16 -20 62 -35 62 -6 0 -10 -18 -10 -40 0 -22 -4 -40 -10 -40 -5 0 -10 3 -11\n8 0 4 -2 15 -4 24 -2 9 -6 42 -9 73 -5 59 -40 129 -60 122 -6 -3 -26 5 -45 16\n-42 25 -187 37 -217 18 -16 -10 -12 -11 21 -7 34 5 37 4 21 -7 -10 -6 -46 -30\n-79 -51 -44 -29 -64 -50 -75 -78 -8 -20 -19 -35 -23 -32 -5 3 -9 40 -10 82 0\n42 -3 85 -7 94 -7 16 -9 16 -32 -2z m331 -75 c114 -40 130 -55 132 -122 2 -43\n0 -48 -20 -51 -23 -3 -29 15 -25 70 2 22 -40 55 -86 68 -33 9 -39 7 -79 -26\n-24 -19 -43 -44 -43 -55 0 -15 -7 -19 -31 -19 -29 0 -31 2 -24 27 3 15 17 40\n31 56 13 16 24 33 24 39 0 6 10 8 21 5 12 -3 28 0 36 9 17 17 11 17 64 -1z\nm29 -125 c24 -24 26 -45 4 -36 -9 3 -31 6 -50 6 -26 0 -34 4 -34 18 0 10 3 22\n7 25 13 14 54 6 73 -13z m-154 -83 c0 -1 11 -19 23 -39 20 -33 27 -38 60 -38\n49 0 56 8 26 32 -45 35 -32 45 46 37 78 -9 89 -13 71 -31 -7 -7 -12 -16 -12\n-20 0 -13 54 3 63 19 10 17 37 17 37 0 0 -33 -84 -84 -154 -93 -76 -11 -169\n34 -201 96 -22 43 -19 53 12 45 16 -4 28 -7 29 -8z"/>\n<path d="M3181 4713 c-20 -77 -32 -136 -27 -141 3 -3 12 1 20 7 23 20 56 -3\n56 -39 0 -16 5 -41 12 -55 6 -14 9 -28 5 -31 -11 -11 -47 7 -47 24 0 29 -30\n62 -55 62 -29 0 -41 16 -20 27 12 7 11 14 -12 46 -16 22 -32 36 -40 33 -8 -3\n-18 5 -25 19 -15 33 -24 31 -37 -6 -17 -50 -14 -69 12 -69 20 -1 21 -2 5 -14\n-24 -18 -23 -21 12 -46 24 -17 34 -19 45 -10 11 9 17 5 29 -24 12 -28 21 -36\n40 -36 28 0 51 -32 42 -57 -15 -37 -134 17 -121 55 3 8 -9 12 -39 12 -55 0\n-106 -10 -106 -21 0 -14 58 -40 65 -29 11 18 34 11 84 -25 26 -19 59 -35 72\n-35 13 0 34 1 46 1 16 0 31 15 53 54 l31 54 -22 76 c-16 53 -26 74 -36 71 -16\n-6 -17 16 -2 31 13 13 -1 95 -18 100 -7 3 -16 -12 -22 -34z m-16 -211 c0 -18\n-20 -15 -23 4 -3 10 1 15 10 12 7 -3 13 -10 13 -16z"/>\n<path d="M4618 4734 c-4 -3 -32 -12 -65 -19 -74 -17 -213 -68 -213 -77 0 -13\n101 -9 165 7 98 24 119 19 55 -13 -74 -37 -74 -49 2 -54 35 -3 65 -4 66 -2 2\n1 -8 9 -23 17 l-28 14 32 6 c32 7 71 55 71 89 0 24 -47 48 -62 32z"/>\n<path d="M7433 4588 c-11 -13 -23 -35 -27 -50 -5 -22 -13 -28 -44 -33 -20 -4\n-56 -10 -80 -14 -35 -7 -47 -5 -67 10 -33 27 -91 50 -109 44 -9 -3 -23 -7 -32\n-10 -13 -4 -14 -8 -4 -20 7 -8 20 -15 29 -15 9 -1 30 -7 46 -14 l30 -13 -70\n-22 c-74 -23 -177 -69 -159 -70 6 -1 37 8 69 19 32 11 60 20 62 20 2 0 -18\n-21 -45 -48 -47 -46 -76 -100 -63 -118 3 -5 18 -15 33 -21 25 -11 28 -10 52\n30 67 111 163 162 172 92 0 -5 2 -17 2 -26 1 -9 20 -37 43 -63 29 -33 54 -50\n86 -59 50 -15 96 -11 90 7 -3 6 5 21 16 34 16 17 21 35 20 73 0 28 -5 54 -11\n58 -6 3 -15 19 -21 34 -9 23 -8 29 5 34 9 3 90 3 179 0 90 -3 173 -3 184 0 38\n10 23 31 -31 43 -58 12 -298 31 -309 24 -4 -2 -13 4 -20 13 -10 12 -10 14 -1\n9 8 -5 12 -1 12 13 0 11 5 23 10 26 6 3 10 13 10 21 0 22 -34 17 -57 -8z m-33\n-163 c6 -7 17 -43 23 -79 12 -74 3 -96 -42 -96 -56 0 -98 55 -97 125 1 39 2\n40 46 52 59 15 56 15 70 -2z"/>\n<path d="M7536 4595 c-33 -34 -9 -68 45 -63 39 4 59 30 43 56 -15 23 -67 27\n-88 7z"/>\n<path d="M6615 4512 c-6 -5 -55 -15 -110 -22 -55 -7 -108 -17 -117 -21 -9 -5\n-58 -11 -109 -14 -51 -3 -97 -9 -103 -14 -6 -5 -58 -14 -116 -21 -239 -28\n-102 -40 175 -15 234 21 382 27 390 15 11 -18 63 -12 75 9 14 26 13 64 -2 79\n-14 14 -69 16 -83 4z"/>\n<path d="M1215 4400 c-3 -6 -1 -27 5 -46 7 -20 13 -106 15 -193 2 -116 6 -166\n18 -193 15 -35 37 -52 37 -28 0 6 8 10 18 8 14 -2 17 4 15 37 -1 22 -7 66 -14\n98 -7 32 -10 59 -8 61 2 2 33 6 69 9 l65 6 -32 -27 c-38 -33 -48 -62 -33 -106\n15 -46 52 -78 95 -82 24 -1 45 -12 60 -28 29 -31 56 -33 80 -5 18 19 18 22 3\n47 -20 33 -65 53 -77 34 -12 -20 -91 35 -91 63 0 34 77 94 96 75 16 -15 143\n-40 206 -40 52 0 65 4 98 30 29 23 40 40 45 71 4 25 10 37 15 29 15 -24 45 -7\n48 28 4 43 -12 62 -53 62 -20 0 -39 8 -51 21 -31 35 -101 71 -130 67 -25 -3\n-23 -6 35 -50 59 -46 67 -61 62 -108 -3 -28 -59 -70 -94 -70 -18 0 -37 4 -43\n9 -5 5 -25 11 -44 13 -34 3 -35 5 -40 50 -4 29 -14 56 -28 71 -12 13 -19 26\n-16 30 10 10 -42 26 -122 38 -79 12 -104 5 -104 -31 0 -36 33 -51 103 -46 55\n4 68 2 86 -16 15 -13 21 -29 19 -46 -3 -24 -6 -26 -33 -22 -16 3 -67 5 -112 5\nl-81 0 5 71 c5 66 4 71 -18 86 -35 22 -66 30 -74 18z"/>\n<path d="M6408 4383 c-9 -10 -20 -27 -26 -39 -8 -16 -24 -23 -61 -27 -42 -5\n-58 -2 -95 19 -36 20 -49 23 -66 14 -29 -15 -25 -30 10 -36 17 -4 30 -10 30\n-14 0 -4 -32 -18 -70 -30 -39 -12 -73 -27 -76 -32 -11 -18 4 -19 35 -4 43 23\n46 20 17 -15 -36 -43 -44 -79 -21 -96 27 -19 33 -17 50 16 22 44 57 77 88 85\n21 5 27 3 27 -11 0 -45 90 -123 142 -123 23 0 58 61 58 100 0 19 -7 42 -17 52\n-9 10 -13 21 -10 24 7 7 234 7 245 0 13 -8 47 23 36 34 -8 8 -122 29 -162 30\n-10 0 -10 4 2 16 21 21 4 54 -28 54 -34 0 -53 -26 -38 -51 11 -17 9 -19 -18\n-19 -28 0 -29 2 -20 26 6 14 10 30 10 35 0 15 -26 10 -42 -8z m-18 -142 c6\n-16 10 -44 8 -62 -2 -28 -8 -35 -29 -37 -32 -4 -54 21 -63 70 -6 36 -5 38 26\n47 18 5 36 10 39 10 4 1 12 -12 19 -28z"/>\n<path d="M4879 4371 c-13 -11 -24 -27 -24 -36 0 -10 -22 -28 -55 -45 -68 -36\n-86 -53 -94 -92 -8 -42 39 -127 74 -133 45 -9 95 -22 113 -30 12 -5 17 -3 17\n9 0 23 15 20 27 -7 13 -27 81 -53 98 -36 7 7 -11 29 -55 69 -36 33 -68 60 -73\n60 -4 0 -5 -11 -1 -24 4 -18 1 -25 -18 -29 -22 -6 -102 17 -123 35 -17 15 17\n28 58 21 33 -5 42 -2 69 23 28 27 30 27 24 7 -6 -18 -3 -23 12 -23 42 0 82 13\n82 27 0 11 7 13 30 8 38 -8 39 5 1 47 l-29 33 -22 -20 -23 -20 7 23 c9 27 -11\n29 -55 6 -25 -12 -28 -18 -18 -30 11 -14 1 -20 -26 -14 -29 6 -60 1 -69 -12\n-17 -24 -51 -31 -66 -13 -27 33 79 112 109 82 13 -13 9 -17 44 56 38 81 34 97\n-14 58z m-14 -200 c7 -12 -12 -24 -25 -16 -11 7 -4 25 10 25 5 0 11 -4 15 -9z"/>\n<path d="M4129 4366 c-2 -2 -47 -9 -99 -15 -127 -16 -199 -35 -295 -80 -83\n-39 -135 -81 -85 -68 24 6 24 5 6 -8 -11 -8 -26 -15 -33 -15 -16 0 -17 -48 0\n-76 18 -31 124 -144 136 -144 5 0 19 -13 31 -30 l21 -29 -107 -106 c-58 -59\n-103 -109 -100 -113 4 -3 50 38 104 91 54 53 102 97 107 97 5 0 -40 -51 -102\n-112 -113 -115 -143 -157 -143 -202 0 -13 -6 -26 -12 -28 -22 -8 43 -75 97\n-102 85 -42 218 -70 322 -67 48 1 103 57 103 105 0 20 4 36 9 36 18 0 -8 55\n-49 104 -22 27 -40 52 -40 57 0 6 -23 42 -51 82 l-51 72 34 27 c98 80 210 157\n222 152 17 -6 79 68 126 151 27 48 30 60 24 101 -8 57 -41 97 -92 112 -40 12\n-75 15 -83 8z m-75 -146 c33 -12 59 -53 51 -80 -8 -26 -61 -87 -88 -101 -14\n-8 -18 -8 -13 0 15 24 -11 8 -80 -49 l-73 -60 -27 35 c-14 19 -31 35 -37 35\n-9 0 -73 68 -90 96 -9 16 71 65 146 90 26 8 47 19 47 24 0 18 123 26 164 10z\nm-120 -251 c-10 -12 -32 -30 -49 -41 -18 -12 -12 -4 15 21 49 44 64 53 34 20z\nm-69 -241 c10 -29 42 -87 71 -128 30 -41 54 -85 54 -97 0 -28 -31 -60 -64 -69\n-35 -8 -172 48 -215 89 -30 29 -32 30 -21 7 12 -23 12 -24 -3 -11 -23 19 -21\n58 4 79 11 9 29 33 39 52 16 31 103 130 114 130 2 0 12 -24 21 -52z"/>\n<path d="M2319 4230 c-7 -22 -16 -40 -20 -40 -4 0 -13 -16 -20 -35 -10 -28\n-18 -35 -39 -35 -50 0 -145 22 -163 37 -24 23 -31 11 -14 -28 8 -19 17 -50 21\n-70 9 -52 82 -161 104 -157 9 2 17 10 17 18 0 14 99 141 114 148 4 1 7 -16 8\n-39 4 -79 53 -152 124 -185 36 -16 41 -16 56 -2 18 19 11 68 -11 68 -16 0\n-106 131 -106 154 0 3 6 6 13 6 6 0 21 -17 32 -38 12 -23 29 -38 43 -40 26 -4\n26 -6 7 49 -17 48 -13 56 30 61 19 2 30 9 30 18 0 22 -65 17 -114 -8 -50 -26\n-63 -12 -19 21 51 38 97 59 117 52 22 -7 44 17 35 39 -7 18 -39 22 -50 5 -3\n-6 -27 -20 -53 -30 -25 -11 -63 -39 -83 -62 -23 -26 -37 -36 -38 -26 0 8 4 19\n9 25 5 5 14 27 20 49 8 31 8 44 -4 62 -22 33 -32 29 -46 -17z m-79 -162 c0\n-16 -49 -98 -59 -98 -8 0 -41 82 -41 102 0 4 23 8 50 8 31 0 50 -4 50 -12z\nm180 -140 c0 -5 -5 -6 -11 -2 -17 10 -49 86 -48 114 0 18 8 8 29 -39 17 -35\n30 -68 30 -73z"/>\n<path d="M2725 4187 c-2 -6 -4 -25 -4 -42 0 -16 -6 -65 -14 -109 -17 -92 -13\n-118 17 -114 18 3 21 10 21 55 0 29 3 53 8 53 4 0 19 0 34 0 l26 0 -21 -23\nc-12 -13 -22 -32 -22 -43 0 -25 27 -64 44 -64 7 0 21 -9 31 -20 35 -39 79 -14\n49 28 -8 12 -21 18 -32 15 -26 -6 -50 40 -32 62 17 21 40 19 102 -10 65 -30\n111 -32 138 -5 11 11 20 25 20 30 0 6 7 10 16 8 9 -2 21 5 28 16 9 15 7 20 -8\n30 -11 6 -38 28 -61 49 -56 49 -75 47 -35 -4 28 -35 31 -43 22 -64 -16 -34\n-54 -41 -94 -18 -26 15 -32 25 -30 45 2 15 -4 38 -13 51 -19 29 -96 61 -125\n52 -13 -4 -22 -1 -26 9 -7 19 -34 29 -39 13z m107 -74 c18 -3 39 -10 46 -15\n14 -12 16 -38 3 -38 -10 0 -114 22 -119 26 -2 1 0 14 4 28 5 21 9 24 20 15 7\n-6 28 -13 46 -16z"/>\n<path d="M5514 4103 c-17 -10 -39 -23 -48 -29 -10 -6 -24 -33 -31 -60 -12 -48\n-13 -49 -58 -54 -25 -3 -48 -8 -50 -10 -2 -3 -18 -24 -36 -48 -28 -35 -31 -45\n-20 -58 19 -23 53 -14 57 16 2 14 11 33 19 43 19 20 83 24 83 4 0 -19 -20 -36\n-37 -30 -7 3 -23 -3 -33 -12 -19 -17 -19 -19 -4 -37 10 -10 22 -18 28 -18 18\n1 62 44 77 76 13 26 20 30 74 35 55 5 62 4 81 -19 26 -32 36 -18 21 31 l-12\n37 -60 0 c-34 0 -66 -5 -73 -12 -9 -9 -15 -9 -23 -1 -16 16 18 81 52 98 15 8\n42 15 58 15 38 0 57 -27 71 -98 12 -65 55 -132 84 -132 49 0 66 63 21 74 -14\n4 -30 4 -35 1 -12 -7 -23 31 -25 87 -2 55 -16 75 -70 99 -50 23 -72 23 -111 2z"/>\n<path d="M7280 3864 c-6 -14 -10 -28 -10 -32 0 -4 -30 -67 -65 -141 l-66 -134\n-24 23 c-13 13 -33 40 -44 62 -10 22 -21 36 -24 31 -3 -4 -15 -51 -27 -103\n-13 -52 -38 -144 -56 -204 -32 -102 -33 -110 -17 -122 15 -11 26 -5 81 43 55\n48 69 56 110 58 26 2 79 6 119 10 l72 7 80 -83 c45 -45 81 -86 81 -91 0 -4\n-17 -12 -38 -18 -20 -7 -45 -17 -55 -24 -16 -12 -16 -16 -2 -50 22 -53 6 -44\n-42 24 -24 33 -48 59 -53 58 -6 -2 -35 24 -66 57 -74 80 -64 62 89 -152 74\n-105 136 -183 145 -183 9 0 26 -10 39 -22 13 -12 23 -16 23 -10 0 6 7 9 15 6\n20 -8 19 -1 -5 38 -11 18 -20 40 -20 48 0 8 -20 46 -45 83 l-45 68 38 16 c20\n8 40 17 43 19 12 7 150 -138 145 -151 -6 -16 12 -23 73 -27 81 -6 78 -1 -192\n262 -9 8 -41 42 -72 75 -63 66 -64 87 -6 93 44 5 38 19 -11 27 -51 8 -162 46\n-218 75 l-41 21 21 34 c12 19 20 41 17 48 -6 17 43 108 55 101 5 -3 7 -12 4\n-19 -11 -28 38 -62 110 -75 76 -14 104 -25 104 -41 0 -6 13 -28 29 -48 62 -79\n80 -146 56 -205 -18 -43 -17 -87 3 -94 22 -7 56 86 56 158 0 141 -95 255 -244\n293 l-55 14 -8 59 c-5 32 -16 75 -25 94 -8 19 -15 38 -14 43 5 18 -10 4 -18\n-19z m-186 -288 l26 -34 -50 -94 c-49 -91 -60 -99 -40 -26 9 32 14 76 19 166\n1 31 14 28 45 -12z m115 -99 c6 -8 29 -22 51 -32 22 -10 40 -21 40 -25 0 -8\n-179 -34 -186 -27 -3 2 8 25 23 50 28 47 52 58 72 34z"/>\n<path d="M510 3853 c0 -21 -9 -41 -25 -57 -28 -28 -25 -50 11 -78 24 -19 38\n-93 35 -184 -1 -28 5 -72 14 -98 9 -26 13 -56 10 -66 -4 -12 2 -36 15 -58 16\n-29 31 -41 60 -49 49 -13 71 -2 49 24 -9 10 -30 45 -48 77 -27 48 -34 73 -37\n130 -1 39 -12 105 -22 146 -17 65 -18 77 -6 91 22 24 17 57 -11 75 -14 9 -25\n24 -25 33 0 9 -5 23 -10 31 -7 11 -10 7 -10 -17z"/>\n<path d="M6310 3854 c-63 -29 -95 -57 -116 -97 -17 -32 -46 -30 -77 6 -15 17\n-21 19 -35 9 -9 -7 -29 -18 -44 -25 -38 -16 -35 -25 10 -33 45 -7 143 -31 147\n-36 2 -2 6 -23 9 -46 2 -24 12 -52 22 -63 9 -10 15 -22 12 -25 -3 -3 -40 1\n-82 9 -42 8 -79 11 -81 6 -3 -4 11 -11 32 -15 21 -3 44 -13 50 -22 7 -8 30\n-21 50 -29 99 -34 190 5 113 49 -18 10 -41 40 -60 79 l-32 62 51 -6 c57 -7 62\n7 6 17 -19 4 -37 10 -39 15 -3 4 14 28 37 54 39 42 47 46 103 53 56 6 65 4\n102 -21 68 -45 126 -158 104 -202 -14 -27 -22 -93 -12 -93 30 0 59 132 46 202\n-8 44 -65 111 -123 145 -63 37 -124 39 -193 7z"/>\n<path d="M6397 3794 c-3 -3 -1 -19 5 -36 17 -47 -15 -291 -40 -307 -15 -9 -6\n-111 9 -111 5 0 9 7 9 15 0 9 12 21 26 27 21 10 24 16 19 37 -10 40 -6 91 11\n143 8 25 16 62 17 82 2 19 6 38 10 42 17 17 6 66 -19 89 -28 26 -36 29 -47 19z"/>\n<path d="M4912 3768 c-12 -6 -32 -26 -43 -45 -19 -30 -24 -32 -57 -27 -32 4\n-42 0 -77 -30 -36 -32 -39 -37 -24 -51 17 -17 49 -11 49 10 0 22 27 36 54 29\n26 -6 35 -27 16 -39 -6 -4 -16 -2 -23 4 -19 15 -39 -3 -35 -30 5 -35 29 -34\n73 4 37 32 40 33 83 23 32 -7 46 -16 53 -34 12 -31 24 -22 23 20 -1 38 -33 60\n-90 60 -40 0 -42 8 -11 42 32 37 79 44 107 16 19 -19 22 -30 17 -74 -6 -64 9\n-142 30 -150 18 -7 53 20 53 40 0 8 -11 16 -25 20 -21 5 -25 12 -25 46 0 22 5\n48 10 59 8 14 7 27 -4 48 -33 62 -96 86 -154 59z"/>\n<path d="M703 3728 c-65 -19 -85 -35 -81 -61 2 -14 17 -24 53 -34 33 -10 68\n-32 105 -65 88 -81 88 -90 -1 -102 -27 -4 -49 -10 -49 -15 0 -4 21 -4 48 0 76\n13 75 13 68 -26 -11 -59 -54 -139 -83 -158 -72 -45 -116 -59 -160 -53 -64 9\n-129 40 -182 88 -60 55 -85 94 -87 137 -2 56 -16 121 -25 121 -17 0 -20 -87\n-5 -160 9 -41 16 -78 16 -83 0 -4 19 -28 42 -53 81 -88 222 -137 328 -114 65\n14 128 49 110 60 -9 6 -7 10 6 14 56 19 113 99 121 168 3 29 11 56 17 60 21\n15 74 8 100 -13 29 -22 46 -18 46 12 0 10 3 19 8 19 10 1 75 61 70 66 -5 5\n-204 -31 -230 -41 -11 -4 -18 -3 -18 4 0 6 -7 8 -15 5 -8 -4 -15 -1 -15 6 0 7\n-9 25 -21 41 -11 16 -22 37 -23 47 -2 9 -26 31 -54 48 l-51 31 67 6 c93 9 192\n28 192 37 0 5 -27 6 -61 3 -46 -5 -63 -3 -70 8 -12 19 -99 18 -166 -3z"/>\n<path d="M3063 3704 c-3 -8 -3 -20 0 -28 8 -20 -21 -56 -44 -56 -24 0 -25 28\n-1 37 37 14 32 53 -8 53 -18 0 -45 -50 -45 -83 0 -13 -15 -25 -49 -38 -46 -17\n-51 -17 -68 -2 -25 22 -24 -3 1 -36 17 -22 20 -23 57 -11 21 7 43 16 49 21 36\n34 49 -31 14 -75 -33 -42 -72 -36 -108 17 -16 23 -47 59 -67 79 -30 29 -42 34\n-56 27 -10 -5 -18 -21 -18 -34 0 -22 4 -25 35 -25 30 0 37 -5 54 -39 11 -21\n18 -41 15 -44 -3 -2 7 -14 22 -26 19 -15 41 -21 74 -21 42 0 52 4 79 36 28 31\n31 41 26 79 -5 42 -4 44 29 55 31 10 36 17 45 61 6 28 11 51 11 53 0 12 -42\n12 -47 0z"/>\n<path d="M1726 3653 c-20 -25 -36 -56 -36 -67 0 -35 -12 -42 -99 -55 l-84 -13\n-24 31 c-14 17 -29 31 -34 31 -14 0 -11 -38 6 -70 9 -17 15 -33 15 -37 0 -11\n146 -10 194 1 l39 9 -5 -41 c-7 -47 -33 -85 -84 -120 -74 -50 -141 -1 -171\n127 -10 41 -24 81 -33 90 -9 9 -29 35 -44 59 -20 29 -36 42 -52 42 -29 0 -64\n-33 -64 -59 0 -42 11 -51 60 -51 l48 0 22 -81 c18 -67 19 -83 8 -91 -33 -22\n121 -118 189 -118 32 0 120 49 140 78 20 30 22 36 29 107 6 50 6 50 42 51 49\n0 92 12 92 25 0 6 7 17 15 26 9 8 19 28 22 44 3 15 11 30 19 33 26 10 16 50\n-16 65 -40 17 -64 0 -56 -39 5 -26 -14 -65 -38 -82 -16 -11 -96 -10 -96 2 0 5\n5 20 12 34 9 20 16 24 35 20 27 -7 63 19 63 46 0 19 -40 50 -63 50 -8 0 -31\n-21 -51 -47z"/>\n<path d="M171 3663 c-48 -59 -50 -93 -10 -251 14 -58 29 -92 49 -114 l29 -32\n-4 35 c-2 19 -13 68 -25 109 -12 43 -19 96 -18 125 l3 50 53 -2 c45 -2 52 0\n52 16 0 16 -82 91 -100 91 -3 0 -16 -12 -29 -27z"/>\n<path d="M2304 3671 c-40 -10 -56 -20 -49 -32 3 -5 -5 -25 -19 -46 l-25 -37\n-16 38 c-27 65 -47 12 -20 -55 5 -14 -3 -18 -44 -24 -50 -7 -65 -20 -36 -31 8\n-4 12 -11 9 -16 -4 -6 14 -7 49 -3 l56 7 23 -46 c12 -25 28 -46 35 -46 15 0\n18 26 3 35 -5 3 -10 13 -10 21 0 11 7 10 29 -7 24 -18 39 -21 84 -17 63 5 87\n23 115 88 17 40 24 46 66 58 63 17 60 35 -5 26 -44 -6 -52 -4 -65 14 -8 12\n-21 19 -28 16 -9 -3 -17 3 -21 15 -7 23 -54 52 -82 50 -10 -1 -32 -4 -49 -8z\nm112 -69 c16 -21 17 -29 4 -37 -6 -3 -15 6 -20 20 -9 23 -15 26 -47 23 -32 -2\n-40 -8 -52 -35 -18 -40 -36 -43 -27 -5 11 44 44 65 90 58 22 -4 45 -14 52 -24z\nm-43 -37 c1 -5 -6 -11 -15 -13 -11 -2 -18 3 -18 13 0 17 30 18 33 0z m97 -34\nc0 -20 -72 -71 -100 -71 -32 0 -80 19 -80 31 0 16 18 18 30 4 7 -8 23 -15 36\n-15 25 0 33 15 12 23 -10 4 -9 8 2 16 19 14 63 14 55 1 -3 -5 -1 -10 5 -10 6\n0 13 7 16 15 7 16 24 21 24 6z"/>\n<path d="M6686 3590 c-5 -62 -13 -96 -25 -111 -17 -21 -18 -21 -45 -4 -42 28\n-53 6 -23 -43 53 -82 105 -33 126 119 8 58 7 83 -2 103 -7 14 -15 26 -18 26\n-4 0 -10 -40 -13 -90z"/>\n<path d="M5460 3575 c-25 -13 -55 -24 -67 -25 -14 0 -23 -6 -23 -16 0 -13 8\n-15 49 -9 57 8 86 -4 95 -39 3 -13 22 -45 42 -71 19 -26 34 -49 32 -51 -3 -2\n-33 12 -69 31 -42 22 -73 32 -87 28 -27 -7 -27 -16 -2 -68 11 -22 22 -56 26\n-75 5 -29 -3 -21 -45 49 -27 46 -63 101 -80 122 -37 46 -38 49 -7 49 25 0 43\n16 32 28 -7 7 -119 21 -125 16 -2 -2 23 -42 55 -88 65 -95 133 -206 133 -220\n2 -21 -49 40 -78 94 -34 63 -43 70 -81 60 -21 -5 -28 -18 -47 -85 -19 -66 -21\n-82 -10 -93 8 -7 58 -28 112 -46 113 -38 138 -62 56 -54 -28 3 -51 2 -51 -1 0\n-10 63 -31 93 -31 23 0 27 4 27 28 0 39 -22 56 -110 83 -41 13 -79 26 -84 30\n-12 10 13 99 28 99 6 0 42 -29 78 -65 37 -36 75 -65 85 -65 19 0 88 -110 88\n-140 1 -24 15 -40 35 -40 29 0 34 19 13 43 -48 54 -77 121 -79 184 -1 44 -8\n72 -24 98 -25 41 -20 56 13 35 24 -15 199 -71 204 -66 2 2 -20 28 -49 58 -61\n62 -118 162 -118 207 0 37 -2 37 -60 6z"/>\n<path d="M4584 3353 c14 -48 46 -246 41 -251 -12 -13 -25 24 -25 74 0 29 -5\n56 -12 61 -34 26 -47 22 -89 -34 l-43 -56 67 -61 c37 -33 67 -64 67 -69 0 -4\n-16 -1 -35 7 -44 19 -45 8 -2 -22 30 -21 33 -21 45 -5 20 28 15 40 -43 89 -30\n26 -55 53 -55 59 0 6 11 21 23 34 28 25 37 18 57 -42 6 -21 23 -51 36 -66 20\n-25 25 -41 26 -101 0 -39 5 -73 10 -76 21 -13 27 14 17 78 -9 55 -8 72 5 97 9\n16 16 48 16 71 0 22 5 40 10 40 6 0 10 -4 10 -8 0 -12 121 -114 128 -108 3 3\n0 11 -6 18 -22 28 -44 132 -39 187 l5 56 -66 -1 c-54 -1 -73 3 -104 22 -45 29\n-51 29 -44 7z m133 -59 c44 -8 49 -18 56 -94 l5 -65 -37 44 c-50 59 -77 55\n-72 -11 5 -88 -13 -75 -25 17 -6 50 -15 98 -19 109 -9 20 -3 20 92 0z"/>\n<path d="M3152 3223 c-62 -69 -86 -79 -57 -24 17 34 10 42 -12 14 -34 -44 -35\n-53 -7 -65 24 -11 29 -9 82 51 l57 63 29 -30 28 -30 -45 -21 c-25 -12 -49 -21\n-53 -21 -5 0 -19 -11 -33 -24 -21 -20 -38 -26 -93 -28 -63 -4 -102 -18 -81\n-31 5 -3 42 -1 82 4 54 8 76 7 87 -1 7 -7 35 -14 61 -15 26 -2 49 -8 51 -14 2\n-6 -1 -11 -6 -11 -10 0 -102 -108 -102 -120 0 -4 17 1 38 11 46 23 166 41 196\n29 22 -8 23 -7 21 58 -1 52 3 76 21 109 12 23 20 44 18 46 -2 2 -36 -5 -76\n-15 -83 -21 -197 -35 -185 -23 4 5 35 12 68 16 65 7 73 12 83 44 4 16 -6 28\n-48 58 -30 20 -57 37 -59 37 -3 0 -32 -30 -65 -67z m228 -114 c0 -20 -20 -99\n-27 -108 -2 -2 -27 -8 -56 -12 -28 -4 -61 -10 -72 -14 -12 -4 -1 11 26 37 59\n56 58 83 -5 74 -23 -4 -51 -3 -62 1 -13 5 8 12 71 24 124 23 125 23 125 -2z"/>\n<path d="M2595 3140 c-16 -4 -57 -8 -89 -9 -47 -1 -57 -4 -48 -13 7 -7 12 -16\n12 -20 0 -15 -27 -8 -45 12 -14 15 -31 20 -70 20 -28 0 -55 -3 -58 -6 -9 -10\n12 -44 26 -44 7 0 33 -9 57 -21 76 -37 120 -50 120 -36 0 7 -5 18 -12 25 -10\n10 -8 17 8 32 16 16 36 21 93 23 54 3 74 0 78 -10 8 -20 -26 -42 -63 -43 -33\n0 -74 -35 -74 -62 0 -9 -7 -21 -15 -28 -13 -11 -15 -8 -15 14 0 32 -2 32 -69\n-3 -47 -24 -52 -29 -36 -38 17 -10 17 -11 -6 -19 -29 -10 -35 -11 -54 -13 -28\n-2 -15 -17 38 -44 62 -31 67 -32 67 -13 0 7 7 19 15 26 12 10 15 9 15 -7 0\n-35 13 -34 52 4 41 40 47 53 22 53 -31 0 -7 25 27 28 39 4 69 27 69 52 0 10\n13 23 31 30 25 11 33 11 44 -1 24 -24 -9 -103 -53 -129 -27 -17 -37 -18 -43\n-8 -13 21 -17 7 -24 -89 -8 -102 -1 -114 35 -63 15 21 20 38 16 58 -6 23 1 35\n52 85 57 57 59 59 61 117 l2 59 -50 38 c-28 20 -51 41 -51 45 0 9 -23 9 -65\n-2z m0 -141 c0 -7 -8 -15 -17 -17 -18 -3 -25 18 -11 32 10 10 28 1 28 -15z"/>\n<path d="M6153 3144 c-12 -3 -47 -32 -77 -64 -45 -49 -56 -57 -66 -45 -6 8\n-32 24 -57 36 -33 16 -47 19 -56 10 -6 -6 -18 -11 -28 -11 -9 0 -29 -10 -45\n-22 -15 -12 -34 -23 -41 -24 -8 -1 -20 -7 -28 -14 -11 -9 -11 -16 2 -40 8 -16\n19 -32 24 -35 17 -11 48 6 61 33 17 35 79 53 105 30 10 -8 26 -18 36 -21 25\n-9 21 -32 -9 -52 -25 -16 -27 -16 -48 4 -40 38 -86 22 -86 -30 0 -37 27 -62\n57 -55 12 3 35 9 51 12 15 4 43 19 64 35 l36 28 87 -42 c78 -38 85 -44 80 -65\n-8 -31 3 -62 20 -62 8 0 17 15 21 38 4 20 11 45 16 55 6 12 5 23 -3 33 -21 26\n-147 94 -162 89 -7 -3 -19 0 -25 8 -10 11 -8 20 9 41 31 39 71 56 137 56 51 0\n61 -3 83 -27 36 -39 36 -92 3 -173 -23 -56 -26 -78 -25 -154 2 -95 12 -115 57\n-116 22 0 74 41 74 59 0 5 -18 25 -41 45 l-41 35 26 79 c19 55 33 81 45 85 17\n4 18 10 10 51 -9 52 -30 96 -53 115 -9 8 -16 20 -16 27 0 32 -127 63 -197 48z"/>\n<path d="M1885 2990 c-66 -38 -126 -74 -133 -80 -12 -11 -32 -6 -32 9 0 15 65\n91 77 91 7 0 13 5 13 10 0 6 -7 10 -15 10 -8 0 -15 -4 -15 -9 0 -5 -10 -11\n-22 -15 -13 -3 -39 -19 -58 -37 l-34 -32 32 -28 c18 -16 42 -29 54 -29 12 0\n71 30 132 66 76 46 114 64 123 58 8 -5 27 -32 43 -60 26 -47 27 -52 12 -65\n-12 -10 -47 -14 -118 -15 -86 -1 -107 -5 -143 -24 -41 -24 -41 -24 -113 -7\n-40 10 -82 17 -94 17 -11 0 -29 9 -39 20 -10 11 -26 20 -35 20 -24 0 -52 -34\n-44 -54 7 -17 12 -19 60 -16 17 1 69 -8 115 -19 66 -16 89 -27 109 -50 31 -36\n114 -81 149 -81 24 -1 70 -26 60 -33 -2 -2 -20 -10 -39 -17 -40 -15 -202 -109\n-240 -139 -14 -11 -22 -23 -18 -27 4 -4 14 -1 23 6 18 15 253 9 300 -9 17 -6\n42 -13 57 -16 14 -3 41 -19 58 -36 17 -17 32 -30 34 -28 2 2 8 31 15 64 6 33\n22 81 36 108 22 44 23 47 5 47 -22 0 -36 -14 -69 -68 -18 -31 -32 -42 -58 -47\n-19 -3 -40 -2 -46 4 -17 13 -136 34 -161 27 -24 -6 -54 8 -38 18 5 3 52 22\n103 41 98 38 138 61 84 50 l-30 -6 28 16 c52 30 36 45 -78 71 -34 7 -119 53\n-112 60 3 3 78 -9 167 -25 90 -17 180 -31 201 -31 22 0 39 -2 39 -5 0 -2 -9\n-19 -20 -37 -25 -42 -25 -49 1 -45 12 2 40 25 64 50 24 26 48 47 55 47 6 0 10\n5 8 12 -3 8 -62 18 -164 30 -87 9 -202 27 -253 39 -85 20 -92 24 -65 31 33 9\n79 4 176 -20 53 -13 72 -14 92 -4 17 7 24 17 20 27 -4 8 1 17 10 21 9 3 16 11\n16 18 0 24 -126 191 -145 193 -11 1 -74 -29 -140 -67z"/>\n<path d="M4130 3044 c0 -3 5 -25 11 -50 11 -44 11 -46 -19 -75 -31 -31 -42\n-25 -42 21 0 10 -4 21 -9 24 -15 10 -12 -97 4 -121 21 -32 31 -29 45 11 6 19\n18 40 27 47 13 11 18 10 30 -6 15 -19 15 -19 8 3 -7 25 9 29 49 11 37 -17 38\n-33 2 -57 -34 -23 -141 -56 -160 -49 -6 3 -19 -2 -28 -11 -16 -14 -17 -16 -1\n-27 12 -9 23 -8 52 6 20 10 62 23 95 30 44 10 61 18 69 36 13 30 29 19 21 -15\n-14 -57 -41 -87 -97 -106 -82 -28 -105 -86 -34 -86 34 0 110 31 121 49 10 15\n-5 14 -47 -4 -50 -21 -79 -20 -75 2 2 12 14 18 36 20 51 4 90 30 112 73 23 44\n25 85 7 117 -18 32 -77 73 -105 73 -21 0 -24 4 -20 28 3 23 -1 32 -24 45 -15\n9 -28 14 -28 11z"/>\n<path d="M3740 3004 c-6 -14 -10 -30 -10 -35 0 -5 -6 -9 -14 -9 -7 0 -19 -7\n-26 -15 -7 -8 -20 -15 -30 -15 -25 0 -47 -30 -55 -73 -6 -32 -4 -41 20 -65\nl28 -27 -32 -3 c-41 -4 -35 -26 8 -30 32 -3 32 -4 26 -43 -6 -36 -5 -40 12\n-37 11 2 22 17 29 38 10 32 14 35 52 38 42 3 60 22 21 22 -20 0 -19 3 10 35\n41 47 42 92 1 138 -25 29 -28 39 -23 70 7 43 -3 49 -17 11z m31 -129 c4 -30\n-10 -64 -32 -75 -13 -8 -19 -7 -19 0 0 6 7 13 15 16 17 7 21 34 5 34 -5 0 -10\n9 -10 20 0 11 5 20 11 20 6 0 8 7 5 18 -6 15 -5 15 8 2 8 -8 16 -24 17 -35z\nm-61 25 c0 -5 -8 -10 -18 -10 -10 0 -24 -6 -31 -12 -11 -11 -11 -9 -2 10 12\n22 51 31 51 12z m-10 -39 c0 -6 -4 -13 -10 -16 -5 -3 -10 1 -10 9 0 9 5 16 10\n16 6 0 10 -4 10 -9z m-24 -58 c-9 -9 -36 18 -36 35 0 12 5 11 21 -7 12 -12 18\n-25 15 -28z"/>\n<path d="M4920 2973 c0 -5 22 -31 50 -57 49 -47 50 -48 35 -76 -8 -16 -15 -41\n-15 -57 l0 -27 -45 44 c-24 24 -47 41 -50 38 -3 -2 4 -13 15 -23 11 -10 20\n-26 20 -36 0 -28 66 -89 96 -89 23 0 25 3 18 23 -4 12 -7 46 -6 76 2 62 22 86\n81 96 87 15 141 -40 141 -144 0 -50 -10 -68 -77 -139 -7 -7 -13 -16 -14 -20 0\n-4 -2 -21 -4 -38 -10 -73 53 -62 122 21 35 43 62 100 43 93 -4 -2 -26 -27 -49\n-55 -24 -31 -49 -53 -61 -53 -23 0 -40 42 -19 48 27 9 96 90 102 121 14 66\n-23 157 -75 189 -48 28 -122 36 -164 16 -38 -18 -64 -7 -64 27 0 13 -7 19 -24\n19 -13 0 -31 3 -40 6 -9 3 -16 2 -16 -3z"/>\n<path d="M5172 2819 c-21 -46 -125 -162 -154 -172 -10 -4 -18 -15 -18 -26 0\n-11 -4 -23 -10 -26 -5 -3 -10 -11 -10 -17 0 -7 4 -7 13 0 6 5 23 12 37 14 17\n2 26 10 28 25 2 12 20 37 40 56 60 55 122 126 122 140 0 7 -6 22 -14 32 -12\n17 -15 16 -34 -26z"/>\n<path d="M5060 2842 c0 -4 9 -13 20 -20 22 -13 27 -3 8 16 -14 14 -28 16 -28\n4z"/>\n<path d="M1133 2765 c-153 -83 -285 -164 -299 -183 -13 -19 -12 -20 19 -18 29\n3 244 124 289 163 20 18 68 13 78 -7 5 -8 24 -43 44 -77 40 -67 46 -102 19\n-104 -10 -1 -79 -2 -153 -3 -147 -1 -247 -24 -236 -52 7 -19 -8 -18 -136 11\n-61 14 -123 25 -138 25 -18 0 -33 8 -44 25 -20 31 -58 33 -87 4 -11 -11 -19\n-30 -17 -42 3 -20 9 -22 68 -23 39 -1 114 -14 185 -34 107 -29 122 -36 140\n-63 25 -40 143 -107 198 -114 23 -3 49 -12 57 -19 9 -7 26 -11 38 -7 13 3 22\n1 22 -6 0 -6 -5 -11 -11 -11 -41 0 -317 -149 -395 -214 -40 -33 -39 -44 3 -23\n43 23 270 15 369 -13 41 -11 83 -20 95 -20 24 0 102 -51 122 -81 14 -19 15\n-19 20 8 14 77 55 192 94 264 2 4 -3 9 -12 13 -23 8 -57 -28 -95 -102 -22 -42\n-37 -61 -46 -58 -8 3 -26 1 -40 -5 -20 -7 -32 -6 -51 6 -34 23 -172 44 -270\n42 -82 -2 -109 7 -55 18 32 7 163 55 177 65 6 4 40 17 77 29 37 13 71 27 74\n33 8 13 2 11 -115 -32 -46 -17 -85 -29 -87 -27 -2 2 35 21 83 42 49 21 98 48\n111 59 21 19 22 22 7 32 -9 7 -50 20 -90 29 -100 22 -235 91 -214 109 2 2 38\n-5 79 -15 93 -23 296 -57 381 -65 35 -3 79 -7 98 -10 l33 -5 -25 -27 c-23 -25\n-47 -64 -47 -76 0 -3 11 -6 26 -6 18 0 43 18 87 64 34 35 68 66 76 68 24 7 2\n28 -29 28 -50 0 -411 49 -485 66 -38 9 -94 18 -123 21 -29 3 -55 10 -58 14 -5\n9 63 29 104 31 40 1 144 -20 186 -37 51 -22 146 -19 146 5 0 4 -21 5 -47 3\n-27 -3 -59 0 -73 6 -21 9 -18 10 20 5 54 -7 100 9 107 36 4 15 11 19 24 15 15\n-5 17 -3 11 13 -14 40 -167 249 -188 259 -19 9 -36 3 -101 -32z"/>\n<path d="M3027 2767 c-12 -13 -36 -29 -54 -36 -17 -7 -42 -24 -55 -38 -21 -24\n-21 -25 -3 -20 106 32 103 32 125 12 30 -27 25 -64 -15 -108 -34 -36 -36 -37\n-59 -21 -42 27 -91 10 -137 -48 -30 -38 -37 -54 -30 -66 8 -14 4 -19 -22 -28\n-44 -15 -47 -40 -5 -49 18 -4 74 -30 124 -56 102 -54 152 -63 150 -27 -1 17\n-13 26 -56 41 -30 10 -57 20 -58 22 -2 2 2 19 9 39 13 36 29 39 29 5 0 -29 60\n-52 96 -38 15 5 34 19 41 29 8 11 20 20 28 20 23 0 47 41 33 57 -18 23 -61 11\n-76 -19 -13 -28 -52 -46 -71 -33 -17 10 -13 71 6 86 9 8 36 40 60 73 51 69 55\n105 18 142 -21 21 -24 29 -14 40 10 12 8 18 -7 29 -26 19 -31 19 -57 -8z m-93\n-243 c23 -9 20 -18 -18 -74 -19 -27 -36 -55 -38 -61 -3 -8 -12 -7 -31 6 -26\n17 -27 18 -9 31 11 7 25 28 33 46 7 18 20 38 29 45 18 15 15 14 34 7z"/>\n<path d="M890 2750 c0 -5 7 -10 15 -10 8 0 15 5 15 10 0 6 -7 10 -15 10 -8 0\n-15 -4 -15 -10z"/>\n<path d="M819 2710 c-25 -16 -53 -38 -63 -49 -17 -19 -17 -21 10 -50 31 -35\n49 -40 41 -12 -5 21 10 49 51 94 40 43 16 54 -39 17z"/>\n<path d="M4580 2641 c0 -5 7 -25 15 -46 12 -28 13 -40 5 -51 -7 -7 -10 -19 -7\n-26 2 -7 -4 -24 -14 -37 -27 -35 -23 -80 10 -120 23 -29 36 -35 71 -37 55 -3\n61 -11 25 -39 -26 -21 -27 -25 -11 -31 11 -4 28 1 46 15 16 11 32 21 34 21 3\n0 15 -19 27 -42 15 -29 24 -38 27 -28 3 8 10 18 16 22 8 5 4 21 -12 49 l-24\n41 36 37 c20 20 36 40 36 44 0 12 -23 7 -41 -9 -17 -15 -18 -14 -12 19 13 82\n-42 150 -123 150 -39 0 -46 3 -69 38 -25 39 -35 47 -35 30z m125 -107 c18 -8\n38 -28 45 -44 14 -34 10 -82 -9 -88 -10 -3 -12 3 -6 26 3 17 2 39 -4 49 -10\n17 -10 17 -11 1 0 -10 -4 -18 -9 -18 -16 0 -33 30 -27 46 4 10 -1 22 -11 30\n-24 18 -8 17 32 -2z m-55 -43 c0 -5 -7 -14 -15 -21 -20 -16 -19 -54 1 -70 8\n-8 28 -13 45 -13 34 0 34 -20 0 -25 -52 -8 -100 80 -65 119 18 20 34 25 34 10z\nm26 -69 c-10 -10 -29 15 -21 28 6 9 10 8 17 -4 6 -10 7 -20 4 -24z"/>\n<path d="M3435 2554 c-15 -15 -13 -19 19 -49 41 -37 40 -35 24 -70 -13 -29\n-21 -31 -38 -10 -10 12 -10 19 -2 27 18 18 14 36 -8 48 -15 8 -24 6 -40 -10\n-19 -19 -20 -21 -3 -48 9 -15 25 -37 35 -48 18 -21 19 -24 3 -78 -9 -32 -23\n-59 -31 -62 -50 -15 -49 -15 -34 -24 8 -6 30 -10 47 -10 37 0 58 29 69 99 4\n23 10 41 14 41 38 0 70 -48 70 -104 0 -27 -6 -40 -26 -53 -23 -14 -35 -15 -89\n-6 -58 10 -139 4 -166 -12 -18 -11 -9 -54 13 -66 19 -10 25 -8 44 15 27 31 69\n34 119 10 31 -14 36 -14 69 1 54 26 86 69 86 115 0 53 -14 80 -60 113 -38 27\n-38 27 -23 58 17 35 9 65 -32 112 -28 31 -37 33 -60 11z"/>\n<path d="M1260 2549 c0 -5 5 -7 10 -4 6 3 10 8 10 11 0 2 -4 4 -10 4 -5 0 -10\n-5 -10 -11z"/>\n<path d="M6836 2529 c-36 -43 -58 -87 -60 -121 -1 -16 -9 -33 -16 -38 -12 -7\n-12 -14 3 -45 10 -20 13 -34 7 -30 -19 12 -10 -11 13 -32 43 -40 128 -85 243\n-129 131 -49 131 -49 115 -30 -7 8 -49 29 -94 47 -83 33 -197 86 -197 93 0 6\n332 -122 343 -133 12 -12 -19 -100 -59 -169 l-26 -43 -21 21 c-12 12 -67 51\n-122 87 -199 131 -337 241 -353 284 -2 4 16 38 39 75 23 36 43 79 44 95 2 38\n-25 48 -45 17 -27 -42 -95 -121 -112 -132 -15 -9 -26 -2 -66 37 -46 45 -83 59\n-98 36 -3 -6 -1 -16 5 -22 6 -6 11 -16 11 -21 0 -6 -5 -5 -12 2 -20 20 -31 14\n-25 -13 4 -25 4 -25 -14 -9 -11 10 -21 15 -24 12 -3 -3 17 -26 44 -51 36 -34\n55 -45 68 -40 16 6 16 5 5 -10 -8 -8 -46 -28 -85 -43 -81 -31 -123 -60 -72\n-49 27 5 27 4 -10 -16 -48 -26 -41 -18 -49 -52 l-7 -27 37 15 c46 19 57 12 15\n-10 -17 -8 -31 -20 -31 -25 0 -14 6 -13 43 6 l32 17 -33 -37 c-18 -21 -30 -41\n-27 -47 14 -22 62 -7 115 36 30 25 60 45 65 45 6 0 33 23 60 50 27 28 53 50\n58 50 5 0 27 -15 50 -34 65 -53 187 -133 187 -123 0 3 -31 25 -70 51 -38 26\n-73 52 -76 58 -9 13 31 -9 166 -97 53 -34 107 -68 120 -75 14 -7 38 -24 53\n-39 16 -14 38 -26 50 -27 12 0 35 -1 50 -2 l28 -2 -18 -31 c-10 -16 -52 -67\n-93 -111 -68 -73 -78 -81 -103 -74 -15 3 -26 10 -24 15 1 5 -26 28 -60 52 -33\n24 -75 54 -92 67 -20 15 -31 19 -31 11 0 -7 -17 4 -39 25 -23 22 -41 34 -45\n27 -4 -5 -13 -8 -21 -4 -9 3 -16 -1 -17 -8 0 -8 -2 -18 -3 -24 -2 -5 -3 -16\n-4 -23 -1 -9 -8 -11 -23 -6 -21 6 -22 5 -8 -11 32 -39 188 -157 239 -181 30\n-15 86 -30 124 -35 l69 -9 112 113 c62 61 134 141 160 176 62 82 126 204 126\n239 0 21 -2 24 -10 12 -8 -13 -11 -12 -25 5 -9 11 -20 29 -25 41 -11 28 -62\n51 -144 64 -99 16 -212 66 -264 117 l-42 43 20 39 c11 22 25 43 31 47 8 4 7 0\n-1 -16 -6 -12 -9 -24 -6 -27 3 -3 8 2 12 11 11 28 28 18 59 -35 38 -64 107\n-129 138 -129 32 0 87 63 87 100 0 47 -12 90 -25 90 -7 0 -15 7 -19 15 -8 21\n-26 19 -26 -3 0 -10 -7 -27 -15 -39 -14 -20 -15 -20 -57 19 -23 21 -47 37 -53\n35 -5 -3 -25 8 -44 23 -27 23 -38 27 -58 19 -22 -8 -24 -7 -18 11 9 28 1 25\n-29 -11z"/>\n<path d="M6945 2530 c3 -5 8 -10 11 -10 2 0 4 5 4 10 0 6 -5 10 -11 10 -5 0\n-7 -4 -4 -10z"/>\n<path d="M4131 2408 c-73 -119 -130 -192 -131 -170 0 19 18 50 62 105 43 55\n44 59 23 81 -8 8 -15 19 -15 25 0 6 -1 11 -2 11 -24 0 -165 -33 -172 -40 -7\n-7 -6 -50 1 -122 6 -62 9 -115 6 -118 -8 -8 -34 33 -40 63 -3 16 -9 25 -14 20\n-7 -7 6 -101 15 -111 9 -9 47 11 60 31 11 17 12 40 4 106 -5 46 -7 90 -4 97 3\n9 22 14 50 14 47 0 64 -12 48 -33 -19 -25 -54 -136 -51 -164 4 -32 -37 -94\n-93 -145 -23 -20 -28 -31 -23 -48 8 -26 38 -23 44 5 6 34 91 129 123 138 16 4\n50 28 74 52 24 25 47 45 49 45 3 0 5 -21 5 -47 0 -63 19 -175 31 -187 5 -6 9\n-1 9 13 0 61 132 251 173 251 26 0 20 14 -13 33 -17 10 -42 34 -58 53 -46 58\n-61 33 -18 -31 28 -41 25 -63 -14 -105 -15 -16 -35 -45 -44 -65 -9 -19 -19\n-35 -22 -35 -3 0 -4 34 -3 75 3 60 0 78 -14 92 -16 16 -20 14 -70 -40 -62 -67\n-91 -76 -44 -13 19 24 56 78 82 120 l49 77 18 -23 c13 -17 20 -20 28 -12 9 9\n8 19 -4 42 -9 17 -16 40 -16 52 0 39 -19 19 -89 -92z"/>\n<path d="M5420 2452 c-44 -3 -46 -5 -98 -82 -30 -44 -58 -80 -63 -80 -5 0 2\n-28 15 -62 13 -35 26 -90 30 -122 5 -33 10 -65 13 -73 7 -18 23 -17 23 2 0 19\n14 19 29 1 9 -11 7 -22 -12 -50 -14 -19 -27 -32 -31 -29 -3 4 -4 -7 -1 -23 3\n-16 9 -49 13 -74 6 -35 12 -46 28 -48 25 -4 61 42 42 54 -8 5 -7 10 4 18 12 9\n68 190 68 220 0 5 -16 -4 -36 -19 l-35 -27 -27 23 c-45 39 -51 50 -46 78 5 24\n-5 82 -22 126 -3 7 4 19 16 25 33 17 81 -25 86 -76 4 -50 51 -95 93 -92 29 2\n61 -16 61 -33 0 -5 -11 -7 -24 -3 -37 9 -41 -13 -9 -57 15 -22 35 -52 43 -66\n20 -33 40 -41 40 -16 0 37 13 32 32 -10 40 -86 47 -92 65 -50 29 68 52 127 50\n128 -1 1 -23 9 -49 19 -61 22 -69 36 -19 29 56 -8 63 13 19 51 -20 17 -50 39\n-67 49 l-31 18 -6 -30 c-4 -17 -9 -31 -11 -31 -13 0 -42 48 -43 71 0 42 -36\n79 -75 79 -27 0 -39 7 -60 34 -26 34 -26 35 -9 61 13 20 23 25 43 21 14 -3 47\n-8 74 -12 40 -5 53 -13 87 -53 36 -42 38 -47 23 -58 -31 -23 -17 -26 133 -24\n74 1 116 5 128 14 17 13 16 15 -18 35 -54 32 -80 36 -110 17 -25 -16 -26 -16\n-102 37 -43 29 -88 62 -100 72 -22 20 -64 25 -154 18z m95 -232 c7 -23 -1 -40\n-19 -40 -20 0 -39 27 -32 45 8 22 44 18 51 -5z"/>\n<path d="M2663 2310 c-43 -47 -48 -49 -84 -44 -31 5 -39 3 -39 -9 0 -8 10 -17\n22 -20 12 -4 19 -9 16 -12 -3 -4 -25 3 -49 14 -51 25 -87 27 -117 5 -12 -8\n-22 -12 -22 -8 0 10 -60 -67 -76 -98 -18 -35 -18 -49 1 -42 12 5 15 -3 15 -38\n0 -24 7 -54 16 -66 19 -28 14 -28 -46 3 -60 31 -60 31 -60 1 0 -18 10 -28 38\n-41 64 -30 64 -32 21 -88 -21 -29 -39 -56 -39 -60 0 -5 14 -5 32 -2 27 6 30 5\n24 -11 -7 -21 34 21 70 72 l21 29 67 -24 c61 -21 69 -22 83 -8 14 14 12 17\n-26 28 -45 13 -54 29 -17 29 56 0 131 52 160 110 21 42 19 104 -5 156 -17 36\n-20 50 -11 63 43 61 70 111 61 111 -5 0 -31 -22 -56 -50z m-117 -121 c12 -21\n-8 -30 -32 -14 -23 15 -28 15 -62 -2 -32 -15 -37 -23 -40 -56 -2 -27 2 -45 14\n-58 13 -14 14 -23 7 -30 -6 -6 -16 -8 -21 -5 -18 11 -42 68 -35 80 4 6 6 18 5\n26 -1 8 21 32 48 52 45 33 54 36 80 26 17 -5 33 -14 36 -19z m82 -68 c-5 -68\n-21 -92 -80 -122 -48 -24 -81 -25 -76 -1 2 11 14 18 38 20 42 4 60 18 60 43 0\n16 -5 18 -27 13 l-26 -7 28 39 c16 21 31 41 32 43 2 3 10 -4 18 -15 13 -18 14\n-16 15 26 0 32 3 41 11 33 7 -7 10 -35 7 -72z m-118 11 c0 -5 -9 -17 -19 -26\n-21 -19 -37 -7 -27 19 6 16 46 21 46 7z"/>\n<path d="M3235 2048 c-26 -86 -47 -145 -53 -155 -4 -7 -30 -13 -57 -14 -28 -1\n-61 -8 -73 -16 -13 -8 -26 -13 -28 -10 -3 2 9 41 25 86 31 83 28 112 -4 41\n-10 -22 -23 -40 -29 -40 -6 0 -21 -19 -33 -42 -54 -105 -32 -194 30 -123 23\n26 131 70 147 60 5 -3 2 -24 -6 -47 -19 -55 -8 -63 13 -10 9 23 19 42 23 42\n11 0 35 -24 61 -60 21 -29 25 -46 25 -99 -1 -62 -2 -64 -48 -106 -31 -27 -68\n-49 -104 -61 -52 -16 -59 -16 -102 -1 -26 8 -58 19 -72 23 -23 6 -24 5 -11\n-10 31 -38 128 -72 186 -67 73 7 185 100 205 169 12 43 13 96 3 106 -8 7 -9\n12 -11 54 -1 13 -24 45 -52 73 l-50 50 20 30 c12 16 28 29 36 29 16 0 19 16 5\n24 -5 4 -12 24 -16 46 -7 39 -22 53 -30 28z"/>\n<path d="M4584 1996 c-9 -49 1 -59 51 -52 51 8 81 -12 100 -67 10 -29 10 -39\n0 -47 -19 -16 -69 6 -61 27 9 21 -21 53 -50 53 -16 0 -28 -10 -38 -29 -13 -25\n-13 -31 0 -50 8 -11 20 -21 26 -21 6 0 20 -7 29 -16 11 -10 38 -17 66 -19 l47\n-1 29 -88 29 -87 -26 -24 c-14 -13 -26 -29 -26 -35 0 -16 33 -11 60 10 14 11\n30 20 36 20 7 0 15 7 18 16 9 23 -33 166 -52 180 -9 6 -13 18 -9 28 9 23 83\n21 125 -4 123 -75 105 -163 -48 -236 -44 -21 -80 -41 -80 -46 0 -5 -21 -28\n-46 -52 -38 -37 -45 -48 -39 -68 12 -37 25 -48 59 -48 49 0 58 11 54 66 l-3\n49 74 39 c50 26 76 35 80 27 5 -7 14 -4 28 11 36 40 52 84 55 151 3 60 0 68\n-28 104 -55 68 -85 82 -170 79 -71 -3 -74 -3 -74 19 0 47 -21 84 -61 104 -22\n12 -50 21 -64 21 -13 0 -27 5 -30 10 -3 6 -17 10 -30 10 -20 0 -25 -7 -31 -34z"/>\n<path d="M6433 1994 c-15 -24 -15 -29 -2 -53 26 -47 110 -55 124 -11 7 23 -18\n75 -40 84 -35 13 -65 6 -82 -20z"/>\n<path d="M7263 1893 c-45 -60 -124 -154 -178 -210 -53 -57 -90 -103 -82 -103\n30 0 222 217 312 353 65 98 32 73 -52 -40z"/>\n<path d="M6790 1940 c-28 -28 -25 -64 6 -89 32 -25 52 -26 82 -5 29 20 28 46\n-4 83 -30 36 -56 39 -84 11z"/>\n<path d="M2046 1917 c3 -10 -3 -13 -25 -9 -23 3 -33 -1 -45 -21 -19 -29 -13\n-54 22 -82 17 -15 25 -34 29 -67 5 -43 3 -48 -40 -92 -25 -25 -50 -46 -56 -46\n-17 0 -41 43 -41 72 0 17 5 28 13 28 22 0 57 44 57 72 0 36 -29 58 -78 58 -58\n0 -72 -24 -68 -116 2 -54 9 -83 28 -117 l25 -45 -89 -101 -90 -101 -29 16\nc-37 18 -84 15 -84 -6 0 -8 26 -28 57 -45 32 -16 58 -36 58 -42 0 -7 3 -13 6\n-13 34 0 204 164 204 197 0 12 8 27 19 32 14 8 23 5 38 -12 67 -73 74 -135 31\n-259 -18 -53 -69 -80 -134 -72 -61 8 -84 20 -190 95 -46 33 -89 57 -95 53 -10\n-6 -81 12 -147 37 -20 8 -30 3 -59 -26 -29 -29 -34 -40 -28 -63 8 -35 32 -71\n51 -79 18 -7 74 20 99 47 10 11 24 20 30 20 7 0 47 -24 90 -52 43 -29 92 -62\n109 -73 24 -16 27 -20 13 -23 -9 -2 -17 -8 -17 -13 0 -15 48 -22 129 -17 59 3\n85 10 109 26 16 12 37 20 45 16 20 -7 73 59 97 124 21 54 28 172 11 172 -5 0\n-12 13 -16 29 -3 16 -24 50 -46 75 l-39 45 36 28 c42 31 94 98 94 120 0 8 -5\n11 -11 8 -7 -5 -8 4 -4 27 4 23 1 48 -11 74 -9 21 -16 52 -15 67 2 16 -5 37\n-15 48 -20 22 -36 25 -28 6z"/>\n<path d="M4070 1813 c-99 -127 -228 -273 -241 -273 -21 0 30 82 90 143 36 38\n69 76 73 85 10 25 9 44 -1 38 -12 -8 -32 18 -29 35 2 11 -18 13 -107 10 -60\n-2 -117 -7 -126 -12 -21 -11 -29 -72 -29 -220 0 -96 -3 -119 -14 -119 -8 0\n-21 19 -28 43 -8 23 -19 42 -24 42 -11 0 -18 -60 -10 -91 8 -31 47 -32 87 -3\nl29 20 0 133 c0 72 4 136 9 141 15 16 144 6 138 -10 -3 -7 -1 -17 4 -22 6 -6\n2 -16 -11 -28 -11 -10 -20 -26 -20 -36 0 -10 -6 -22 -14 -26 -21 -11 -57 -100\n-58 -142 -1 -30 -12 -48 -65 -104 -34 -37 -63 -72 -63 -77 0 -6 -11 -10 -25\n-10 -42 0 -57 -69 -18 -84 21 -8 31 0 47 37 19 46 152 171 172 163 24 -9 118\n42 160 86 19 21 40 38 45 38 9 0 5 -90 -7 -164 -6 -41 7 -186 17 -186 5 0 9\n11 9 25 0 59 178 281 248 311 18 7 36 13 40 13 23 -4 19 5 -17 38 -23 20 -57\n61 -77 90 -54 81 -78 43 -28 -43 30 -52 28 -108 -6 -119 -21 -7 -110 -106\n-110 -122 0 -5 -9 -18 -20 -28 -20 -18 -20 -18 -14 31 3 27 12 77 20 112 17\n79 17 92 -1 68 -14 -19 -15 -19 -15 6 0 46 -19 42 -78 -15 -57 -54 -119 -94\n-129 -83 -3 3 21 34 55 68 79 84 135 149 173 205 l32 46 18 -36 c11 -20 23\n-38 29 -42 19 -11 22 44 5 95 -9 28 -14 57 -11 65 3 8 1 17 -4 20 -5 3 -50\n-48 -100 -112z"/>\n<path d="M5926 1888 c4 -7 25 -39 46 -70 21 -31 38 -62 38 -68 0 -6 47 -80\n105 -163 58 -84 127 -188 154 -233 27 -45 68 -110 91 -145 28 -41 44 -78 46\n-103 5 -52 28 -77 68 -72 51 6 65 16 75 52 8 30 6 38 -17 64 -15 16 -31 30\n-37 30 -5 1 -32 35 -59 76 -27 41 -69 96 -93 122 -68 72 -133 153 -133 165 0\n18 -61 107 -74 107 -6 0 -54 56 -105 125 -51 69 -97 125 -103 125 -5 0 -6 -6\n-2 -12z"/>\n<path d="M2830 1760 c-26 -5 -31 -8 -18 -14 9 -4 19 -18 22 -31 7 -27 31 -39\n54 -26 17 9 96 -13 122 -34 8 -7 35 -18 60 -25 25 -7 57 -18 72 -26 35 -19 46\n-18 85 9 50 34 42 44 -39 44 -53 1 -84 6 -113 21 -22 11 -64 26 -94 33 -35 9\n-59 21 -68 36 -16 24 -24 25 -83 13z"/>\n<path d="M5810 1654 c0 -4 19 -24 43 -46 36 -34 38 -38 17 -33 -14 4 -50 9\n-80 12 -46 4 -58 2 -73 -15 -11 -12 -14 -23 -9 -26 6 -3 8 -15 5 -26 -5 -18 0\n-20 48 -20 73 0 129 -17 157 -47 l23 -26 -30 -31 c-35 -36 -61 -93 -61 -135 0\n-56 40 -128 64 -114 6 4 24 6 41 5 41 -3 108 37 115 68 11 47 34 34 78 -42 22\n-40 57 -108 77 -150 35 -78 70 -113 85 -88 9 14 -12 80 -54 173 -29 64 -32 77\n-14 59 36 -36 88 5 66 53 -20 44 -83 47 -90 3 -3 -22 -5 -21 -26 12 -20 32\n-20 35 -4 42 9 3 30 7 45 7 36 1 33 29 -5 50 -22 13 -33 13 -57 4 -29 -10 -33\n-9 -61 23 -57 66 -65 84 -65 156 0 55 -3 70 -20 82 -38 29 -48 21 -41 -35 l7\n-51 -83 71 c-78 66 -98 80 -98 65z m225 -316 l37 -43 -53 -47 c-29 -27 -61\n-48 -70 -48 -42 0 -62 56 -38 103 12 22 68 76 81 77 3 0 23 -19 43 -42z"/>\n<path d="M3625 1619 c-10 -15 3 -25 16 -12 7 7 7 13 1 17 -6 3 -14 1 -17 -5z"/>\n<path d="M2850 1515 c-66 -24 -49 -85 31 -119 68 -28 154 -46 184 -37 l30 8\n-25 11 c-14 5 -60 20 -104 33 -43 13 -81 29 -83 37 -3 7 4 26 15 42 31 41 17\n49 -48 25z"/>\n<path d="M5090 1351 c0 -16 79 -172 97 -193 8 -9 12 -21 9 -27 -4 -5 -2 -12 3\n-15 9 -6 -62 -86 -77 -86 -5 0 -15 -21 -22 -47 -20 -79 -21 -81 -35 -43 -19\n53 -96 203 -101 197 -3 -2 5 -28 16 -57 15 -38 18 -56 10 -65 -32 -38 75 -230\n123 -223 19 3 22 10 25 58 3 59 66 210 88 210 7 0 26 -25 44 -56 39 -69 51\n-65 15 5 -14 29 -23 56 -19 62 4 7 36 9 85 6 77 -4 81 -5 130 -48 77 -67 88\n-91 85 -180 -1 -55 -8 -87 -24 -117 -12 -24 -22 -48 -22 -55 -1 -18 -68 -87\n-86 -87 -18 0 -53 -18 -72 -37 -7 -7 -22 -13 -33 -13 -10 0 -19 -5 -19 -11 0\n-21 91 0 159 36 36 20 80 52 98 72 81 90 94 282 25 384 -27 40 -95 111 -78 82\n11 -20 -9 -15 -29 7 -10 11 -23 20 -28 20 -6 0 -19 8 -30 18 -15 13 -37 17\n-107 17 l-89 0 -11 41 c-9 31 -9 47 -1 63 15 27 5 44 -21 35 -11 -3 -23 -2\n-27 3 -12 17 -81 55 -81 44z"/>\n<path d="M2885 1239 c-5 -4 -32 -28 -60 -52 -27 -25 -73 -58 -101 -75 -124\n-72 -154 -106 -89 -99 15 1 19 -3 17 -18 -2 -11 -4 -30 -4 -42 0 -27 -2 -28\n-32 -8 -13 9 -47 17 -75 19 -48 4 -59 0 -123 -39 -38 -24 -73 -47 -76 -50 -13\n-13 25 -25 81 -25 31 0 57 -4 57 -10 0 -11 61 -7 180 11 137 20 140 23 66 57\nl-43 21 16 61 c18 65 52 104 82 95 17 -6 52 17 110 73 36 33 37 34 68 18 22\n-12 31 -24 31 -41 0 -14 -5 -25 -11 -25 -6 0 -9 -9 -6 -20 3 -12 -1 -33 -9\n-48 -8 -15 -14 -21 -14 -14 0 21 -16 13 -64 -31 -41 -39 -46 -48 -46 -88 0\n-24 6 -54 14 -65 16 -22 8 -85 -11 -91 -6 -2 -16 12 -23 31 -19 58 -42 46 -96\n-49 -15 -27 -34 -54 -41 -58 -7 -4 -13 -17 -13 -29 0 -18 4 -20 38 -15 20 3\n46 8 56 13 31 12 9 -14 -35 -42 -22 -14 -44 -36 -49 -49 -5 -14 -16 -25 -25\n-25 -12 0 -35 -36 -35 -55 0 -2 51 -3 113 -2 l112 2 -4 88 c-1 49 1 87 6 87 5\n0 17 -25 27 -55 31 -98 56 -83 85 50 11 49 18 91 16 93 -2 3 -22 0 -45 -6 -45\n-11 -46 -9 -25 41 9 23 29 44 54 58 62 35 87 102 55 147 -15 22 -14 26 13 78\n28 55 28 55 67 49 22 -3 42 -8 46 -10 4 -2 12 -30 19 -62 7 -32 20 -74 28 -93\n13 -33 12 -38 -24 -117 -37 -80 -55 -98 -67 -68 -3 8 -10 15 -16 15 -16 0 -8\n-53 10 -67 8 -7 12 -13 8 -13 -15 0 117 -270 132 -270 17 0 30 48 30 111 0 53\n-3 60 -23 68 -43 17 -51 49 -32 141 9 46 18 98 20 116 1 19 7 39 12 46 6 7 13\n30 16 50 4 27 2 38 -10 42 -10 4 -14 15 -10 32 3 15 -2 31 -11 41 -9 8 -26 37\n-38 63 -12 26 -26 50 -30 53 -5 3 -58 15 -119 26 -60 12 -117 26 -125 30 -8 4\n-19 5 -25 0z m75 -294 c16 -19 1 -59 -29 -75 -17 -9 -23 -6 -36 14 -32 48 28\n105 65 61z"/>\n<path d="M4058 1123 c9 -3 5 -12 -13 -28 -30 -29 -31 -36 -7 -66 15 -17 28\n-22 59 -21 34 1 46 -4 77 -34 34 -33 36 -39 36 -99 0 -73 -8 -81 -62 -54 -43\n22 -54 37 -39 52 38 38 -26 120 -74 95 -29 -16 -55 -48 -55 -70 0 -32 101\n-123 157 -142 58 -19 56 -14 69 -192 l6 -92 -35 -11 c-46 -16 -64 -36 -48 -55\n11 -13 20 -12 67 4 30 11 61 17 69 14 8 -3 19 4 26 17 15 30 7 238 -11 249\n-21 13 -8 55 19 58 66 10 168 -67 210 -157 32 -68 18 -118 -45 -162 -39 -26\n-62 -33 -164 -47 -94 -13 -120 -19 -123 -33 -3 -9 -18 -20 -35 -24 -17 -3 -51\n-20 -76 -37 -43 -28 -46 -33 -46 -73 0 -40 3 -44 40 -64 22 -12 49 -21 58 -21\n23 0 42 19 36 35 -3 7 -1 16 5 20 6 3 11 24 11 46 0 49 11 54 164 75 128 16\n126 16 103 3 -9 -6 -15 -15 -11 -20 7 -12 71 34 106 76 33 39 58 89 58 115 0\n11 5 20 11 20 18 0 4 103 -21 154 -24 51 -95 130 -108 122 -5 -3 -28 3 -51 14\n-22 11 -61 23 -86 27 l-45 6 6 51 c8 64 -10 138 -31 130 -9 -4 -18 3 -24 18\n-5 14 -26 32 -53 43 -23 11 -53 30 -65 42 -13 14 -33 23 -50 22 -15 0 -22 -3\n-15 -6z"/>\n<path d="M5459 944 c-18 -34 -128 -124 -152 -124 -18 0 -104 -41 -171 -83 -32\n-20 -44 -23 -63 -14 -18 8 -27 6 -43 -8 -11 -10 -20 -24 -20 -31 0 -8 -11 -21\n-25 -30 -30 -20 -34 -39 -4 -23 17 9 28 7 54 -7 29 -15 36 -16 50 -4 10 8 21\n24 26 37 9 24 96 70 178 94 25 8 61 24 80 38 20 13 50 28 68 33 44 12 71 53\n70 103 -2 49 -27 58 -48 19z"/>\n<path d="M2410 830 c0 -5 7 -10 15 -10 8 0 15 5 15 10 0 6 -7 10 -15 10 -8 0\n-15 -4 -15 -10z"/>\n<path d="M5585 531 c-11 -3 -47 -24 -80 -46 -84 -56 -139 -79 -169 -71 -21 5\n-25 13 -28 48 -4 53 -32 65 -49 21 -6 -15 -15 -36 -20 -45 -24 -48 13 -88 80\n-88 64 0 237 96 276 153 25 38 24 40 -10 28z"/>\n</g>',
    'energia': '<g transform="translate(0.000000,800.000000) scale(0.100000,-0.100000)"\nfill="currentColor" stroke="none">\n<path d="M4835 7808 c-73 -104 -78 -108 -119 -115 -49 -8 -186 -99 -356 -237\n-117 -96 -119 -98 -194 -162 -39 -34 -141 -113 -226 -177 -85 -63 -184 -140\n-220 -170 -36 -30 -114 -96 -175 -147 -277 -233 -508 -462 -804 -795 -112\n-126 -194 -299 -99 -208 15 15 28 22 28 16 0 -6 -7 -16 -15 -23 -8 -7 -15 -16\n-15 -21 0 -15 30 -10 36 5 3 8 13 17 21 21 9 3 -1 -14 -23 -39 -21 -24 -54\n-49 -71 -56 -25 -9 -38 -23 -53 -58 -12 -26 -19 -57 -17 -70 2 -13 -2 -26 -10\n-30 -11 -7 -12 -21 -2 -83 39 -257 104 -338 354 -444 122 -52 457 -352 585\n-525 77 -104 126 -157 163 -176 35 -19 29 -28 -30 -44 -93 -25 -176 -79 -256\n-164 -59 -63 -173 -217 -236 -319 -120 -196 -174 -253 -100 -105 59 116 88\n255 57 274 -15 10 -51 -35 -150 -189 -46 -70 -110 -166 -144 -212 l-61 -85\n-74 101 c-181 245 -262 347 -360 456 -98 108 -235 233 -256 233 -8 0 -67 48\n-132 108 -154 141 -181 164 -352 299 -140 111 -371 253 -411 253 -35 0 -19\n-35 27 -60 24 -13 43 -26 41 -27 -6 -6 -152 55 -253 105 -54 27 -122 57 -151\n67 -52 19 -52 19 -73 -2 -16 -15 -20 -28 -15 -46 7 -31 33 -50 146 -109 127\n-67 180 -102 193 -128 8 -15 36 -34 77 -50 75 -31 196 -111 326 -215 103 -82\n96 -75 262 -303 9 -13 45 -60 79 -104 35 -44 63 -83 63 -86 0 -4 -37 30 -82\n74 -117 114 -178 164 -199 164 -27 0 -23 -15 14 -57 18 -21 49 -58 67 -83 30\n-39 90 -107 239 -276 87 -99 177 -208 263 -321 51 -66 128 -166 170 -222 l78\n-101 -33 -16 c-104 -54 -249 -188 -320 -296 -20 -31 -60 -110 -88 -175 -28\n-65 -54 -127 -59 -138 -5 -11 -21 -56 -36 -100 -14 -44 -48 -116 -75 -160 -49\n-83 -123 -175 -140 -175 -21 0 -131 -83 -139 -105 -9 -24 -72 -57 -185 -98\n-79 -29 -203 -30 -262 -3 -44 20 -89 69 -124 135 -11 23 -25 41 -30 41 -16 0\n-41 -62 -35 -86 5 -19 2 -26 -17 -33 -42 -16 -50 -35 -26 -65 20 -25 20 -28 5\n-45 -16 -17 -15 -21 16 -61 44 -57 97 -85 197 -104 97 -19 175 -20 255 -5 57\n10 83 6 57 -10 -25 -16 -214 -32 -263 -22 -35 6 -52 5 -65 -5 -17 -12 -17 -14\n5 -36 19 -20 35 -24 105 -26 158 -7 389 62 499 149 21 17 55 35 75 41 90 25\n286 277 415 532 110 219 160 291 283 407 54 50 166 139 176 139 4 0 9 8 11 17\n2 13 17 -2 48 -47 48 -71 92 -129 157 -205 24 -28 71 -108 107 -180 133 -271\n215 -444 261 -550 25 -61 53 -124 61 -142 8 -17 14 -34 14 -38 0 -3 8 -25 19\n-48 10 -23 30 -73 45 -112 31 -82 52 -134 78 -192 23 -54 23 -61 -3 -32 l-20\n24 5 -50 c5 -61 43 -234 90 -410 73 -278 136 -441 181 -471 21 -14 26 -14 49\n1 17 11 26 13 26 6 0 -7 12 -24 26 -38 14 -14 48 -69 74 -123 27 -53 55 -102\n63 -108 23 -20 37 8 44 87 4 47 10 71 18 71 7 0 15 -7 19 -15 11 -30 36 -14\n60 38 57 125 89 202 125 303 21 60 41 116 44 124 9 23 32 89 98 285 95 283\n272 741 360 932 10 24 19 46 19 50 0 38 520 931 543 933 4 0 7 -6 7 -13 0 -7\n24 -31 53 -52 62 -47 277 -254 277 -267 0 -5 11 -24 25 -42 13 -17 65 -113\n115 -211 51 -99 108 -202 129 -230 132 -177 161 -212 206 -251 50 -44 85 -56\n85 -30 0 13 -93 138 -142 190 -10 11 -18 24 -18 30 0 5 26 -16 58 -49 153\n-156 319 -248 507 -280 93 -16 108 -16 213 -1 62 8 129 23 147 32 75 35 149\n144 99 144 -13 0 -12 5 5 26 25 31 26 47 6 64 -11 9 -21 7 -47 -13 -57 -42\n-63 -42 -30 -1 42 52 50 92 24 123 -23 30 -34 24 -62 -35 -31 -64 -48 -68\n-210 -56 -197 15 -270 48 -433 194 -75 67 -91 77 -107 68 -18 -10 -26 2 -90\n137 -176 368 -228 454 -352 575 -117 114 -162 140 -251 142 -45 1 -71 -2 -79\n-11 -5 -7 -14 -10 -19 -7 -5 3 -3 11 3 19 7 7 37 49 67 92 161 230 306 424\n451 603 99 123 117 143 272 309 112 121 267 322 268 349 0 6 -6 12 -12 12 -26\n1 192 133 332 203 147 73 178 87 240 108 19 7 46 18 60 26 14 7 31 13 38 13\n21 0 192 71 237 98 25 15 43 32 41 37 -6 21 -113 16 -216 -10 -114 -28 -123\n-29 -97 -14 24 14 21 37 -6 50 -19 9 -46 3 -143 -29 -130 -43 -222 -67 -230\n-59 -3 3 33 26 80 51 48 26 86 54 86 62 0 18 2 18 -77 -11 -138 -52 -413 -212\n-568 -330 -154 -117 -243 -194 -413 -360 -139 -134 -228 -246 -247 -309 -10\n-34 -31 -68 -62 -102 -62 -66 -188 -215 -238 -281 -22 -29 -47 -55 -55 -58 -8\n-2 -28 -28 -44 -57 -16 -29 -57 -96 -91 -148 -34 -52 -67 -105 -74 -118 -11\n-21 -14 -21 -34 -7 -35 24 -157 148 -152 153 3 3 16 -5 30 -18 23 -21 55 -22\n55 -2 0 5 -20 36 -45 67 -48 63 -52 84 -7 43 46 -43 75 -58 109 -58 18 0 33 4\n33 9 0 4 -27 29 -59 55 -95 74 -143 140 -237 321 -103 200 -146 266 -222 343\n-50 50 -145 122 -161 122 -3 0 -13 -5 -21 -10 -12 -8 0 -24 65 -86 44 -41 73\n-72 65 -68 -8 4 -28 20 -45 36 -83 77 -176 119 -310 139 -38 6 -74 15 -79 20\n-6 5 -20 9 -32 9 -16 0 -23 8 -28 31 -14 73 -151 308 -272 469 -75 99 -148\n173 -212 214 -20 13 -48 36 -62 51 -28 31 -127 106 -241 182 -88 60 -120 95\n-144 156 -16 43 -16 46 5 88 28 56 84 144 135 213 22 29 47 64 55 76 27 42\n214 252 350 395 192 200 733 745 737 741 2 -2 -12 -34 -31 -72 -20 -38 -48\n-98 -64 -134 -16 -36 -62 -134 -101 -217 -39 -84 -71 -156 -71 -160 0 -5 -7\n-21 -16 -38 -34 -66 -10 -181 77 -355 26 -52 50 -111 54 -130 3 -19 10 -44 15\n-55 8 -19 40 -148 74 -300 9 -38 18 -119 20 -180 6 -120 -4 -95 -24 60 -19\n149 -39 235 -58 242 -22 9 -23 -2 -8 -88 16 -88 22 -133 55 -439 10 -102 22\n-192 25 -200 3 -8 10 -96 16 -195 10 -175 22 -245 41 -245 5 0 11 11 15 24 7\n29 -24 457 -37 506 -12 47 -12 117 1 104 5 -5 14 -63 19 -129 29 -342 40 -417\n63 -451 13 -19 28 -34 34 -34 6 0 19 28 30 63 17 56 19 94 18 402 -1 206 -6\n395 -15 480 -28 290 -17 653 27 890 20 103 36 131 27 48 -6 -62 2 -79 24 -50\n13 16 71 179 128 362 58 184 163 503 174 530 5 11 21 56 36 100 15 44 33 94\n40 110 7 17 40 100 73 185 68 177 87 251 72 281 -19 35 -48 22 -95 -43z m-172\n-319 c-21 -41 -44 -93 -51 -116 -12 -40 -32 -67 -32 -43 0 12 6 27 54 132 14\n31 26 60 26 64 0 10 32 45 37 40 2 -2 -13 -37 -34 -77z m-863 -3169 c0 -9 -38\n-6 -47 4 -4 3 -9 19 -11 34 l-3 27 30 -29 c17 -17 31 -33 31 -36z m2143 -187\nc-59 -60 -144 -151 -187 -203 -44 -52 -91 -102 -105 -111 -14 -9 -39 -31 -56\n-49 -16 -17 -39 -37 -50 -42 -11 -6 18 31 65 82 119 131 419 430 431 430 6 0\n-38 -48 -98 -107z m-1965 60 c-10 -2 -26 -2 -35 0 -10 3 -2 5 17 5 19 0 27 -2\n18 -5z m407 -113 c10 -11 16 -20 13 -20 -3 0 -13 9 -23 20 -10 11 -16 20 -13\n20 3 0 13 -9 23 -20z m-250 -146 c86 -51 92 -60 155 -232 56 -156 76 -216 121\n-374 38 -135 89 -237 150 -303 32 -34 75 -70 96 -81 21 -10 40 -20 42 -21 2\n-1 -35 -80 -82 -175 -46 -95 -91 -189 -100 -208 -25 -59 -44 -103 -70 -160\n-25 -58 -77 -188 -122 -305 -14 -38 -30 -79 -35 -90 -5 -11 -23 -58 -40 -104\n-17 -46 -35 -91 -40 -100 -9 -15 -9 -14 -10 1 0 9 5 20 10 23 6 4 8 11 4 16\n-3 5 -2 15 4 22 11 15 34 128 27 136 -10 10 -26 -17 -61 -105 -19 -49 -39 -98\n-44 -109 -5 -11 -14 -38 -20 -60 -6 -22 -15 -49 -20 -60 -5 -11 -16 -45 -24\n-75 -14 -55 -52 -191 -81 -285 -25 -86 -78 -328 -99 -457 -16 -101 -27 -114\n-39 -51 -5 23 -3 32 6 30 17 -3 14 51 -11 163 -39 179 -80 308 -172 540 -22\n58 -55 143 -72 190 -17 47 -34 90 -38 95 -4 6 -13 28 -20 50 -11 37 -20 58\n-64 157 -9 20 -16 39 -16 43 0 6 -19 49 -77 175 -41 89 -46 100 -114 255 -28\n66 -59 131 -69 145 -9 14 -42 72 -73 130 -30 58 -66 124 -79 146 -99 176 -97\n171 -70 181 23 9 146 107 203 163 62 60 114 143 135 216 9 32 19 65 21 72 8\n19 -21 14 -51 -9 -23 -18 -25 -18 -18 -2 22 51 57 113 64 113 5 0 4 -9 -2 -20\n-9 -16 -8 -22 5 -26 9 -4 17 -5 19 -3 1 2 19 29 39 59 20 30 62 80 94 111 73\n70 254 181 229 140 -4 -5 1 -294 10 -643 8 -348 14 -700 13 -783 -3 -194 -2\n-212 11 -199 6 6 17 91 25 190 9 98 19 195 24 214 5 21 10 -190 13 -510 3\n-361 8 -553 15 -570 l11 -25 11 29 c7 16 14 55 18 87 5 55 7 58 29 53 63 -16\n80 159 65 693 -6 214 -10 417 -10 453 l2 65 13 -70 c20 -108 26 -126 37 -122\n21 7 28 72 38 392 14 423 16 719 5 779 -9 48 -8 48 15 41 13 -4 42 -18 64 -31z\nm-405 17 c0 -11 -31 -24 -39 -16 -3 3 1 10 9 15 19 12 30 12 30 1z"/>\n<path d="M1017 5015 c-7 -16 -4 -18 153 -98 155 -79 243 -128 261 -143 8 -8\n24 -14 35 -14 35 1 -94 105 -226 182 -135 79 -212 104 -223 73z"/>\n<path d="M6380 1696 c0 -50 263 -96 360 -63 27 9 35 18 35 36 0 24 -1 24 -115\n26 -63 0 -152 5 -197 10 -74 7 -83 6 -83 -9z"/>\n<path d="M4539 1485 c-88 -144 -248 -511 -333 -765 -31 -94 -43 -136 -77 -276\n-5 -22 -9 -76 -7 -120 3 -70 5 -79 22 -79 25 0 71 88 110 210 16 50 34 108 41\n130 12 37 36 123 70 255 51 194 157 541 192 622 23 54 11 70 -18 23z"/>\n</g>',
    'medo': '<g transform="translate(0.000000,800.000000) scale(0.100000,-0.100000)"\nfill="currentColor" stroke="none">\n<path d="M3845 7780 c-99 -17 -162 -36 -284 -84 -103 -41 -221 -116 -221 -141\n0 -6 -26 -29 -57 -51 -32 -21 -101 -75 -153 -118 -148 -124 -51 -18 121 132\n103 90 169 136 296 210 80 45 107 71 61 56 -43 -13 -264 -131 -333 -177 -238\n-160 -507 -495 -633 -790 -23 -54 -42 -104 -42 -111 0 -19 -16 -30 -88 -56\n-162 -59 -262 -161 -333 -340 -22 -55 -19 -215 5 -265 l20 -40 6 50 c6 47 6\n48 14 20 21 -80 32 -105 74 -168 25 -38 63 -82 86 -99 26 -19 41 -37 41 -51 0\n-16 11 -25 47 -39 46 -17 84 -15 72 3 -3 5 40 9 100 9 57 0 111 3 120 6 10 4\n16 1 16 -9 0 -20 14 -29 34 -21 11 4 14 13 11 25 -4 13 -1 21 12 26 10 3 30\n15 45 25 41 28 57 20 21 -11 -38 -32 -41 -41 -13 -41 34 0 112 45 159 91 95\n95 181 263 181 354 0 47 -19 93 -41 100 -11 4 -19 15 -19 28 0 59 -113 236\n-191 299 -21 17 -39 37 -39 43 0 16 111 185 168 255 99 124 216 233 412 383\n76 59 78 60 295 122 139 40 407 44 560 10 39 -9 108 -23 155 -31 135 -24 260\n-73 320 -123 15 -12 -11 -3 -65 23 -138 67 -305 107 -305 72 0 -8 26 -20 68\n-31 138 -37 358 -150 416 -214 15 -17 16 -23 7 -35 -17 -20 -4 -38 20 -31 11\n4 27 1 35 -5 12 -11 12 -14 -2 -24 -9 -7 -15 -21 -12 -32 4 -24 48 -33 48 -9\n0 9 5 13 10 10 14 -9 120 -142 120 -151 0 -4 6 -15 14 -23 24 -27 57 -92 82\n-162 20 -55 24 -84 23 -175 0 -60 -7 -136 -14 -169 -10 -40 -13 -47 -10 -20\n17 147 17 193 1 270 -17 83 -37 118 -56 99 -8 -8 -7 -20 1 -40 17 -46 23 -213\n10 -305 -22 -154 -81 -344 -137 -439 -51 -86 -236 -266 -331 -322 -107 -63\n-117 -61 -19 4 42 28 99 71 126 95 45 40 120 132 120 147 0 15 -56 4 -76 -15\n-13 -12 -32 -27 -43 -33 -11 -6 -65 -37 -120 -68 -56 -32 -105 -58 -109 -58\n-4 0 -13 -5 -19 -11 -15 -15 -190 -86 -393 -159 -91 -33 -172 -64 -181 -69 -9\n-5 -35 -14 -57 -20 -95 -26 -188 -121 -232 -238 -30 -81 -51 -241 -51 -396 0\n-147 7 -170 45 -165 12 1 27 -1 34 -6 19 -12 24 -3 33 61 5 37 6 1 2 -97 -3\n-85 -6 -156 -7 -157 -1 -2 -45 8 -97 21 l-94 24 65 4 c36 2 64 7 63 12 -1 5\n-51 28 -110 52 -59 24 -117 48 -128 53 -11 5 -51 22 -90 36 -109 42 -301 141\n-405 210 -202 133 -467 371 -640 573 -94 111 -121 132 -144 113 -41 -34 68\n-195 269 -396 289 -290 770 -616 967 -657 45 -9 63 -18 70 -34 15 -33 98 -68\n142 -60 47 9 106 -15 106 -43 0 -18 -5 -20 -32 -15 -111 19 -340 107 -513 198\n-164 86 -288 172 -510 352 -240 196 -462 465 -594 722 -29 57 -62 114 -72 125\n-11 12 -27 30 -36 39 -17 18 -113 177 -113 187 0 3 -18 26 -40 51 -33 37 -41\n42 -50 30 -5 -8 -10 -31 -10 -50 0 -20 -5 -41 -11 -47 -8 -8 1 -35 34 -100 25\n-48 60 -119 77 -157 27 -60 29 -71 15 -78 -8 -5 -15 -18 -15 -28 0 -47 91\n-285 184 -483 25 -53 46 -99 46 -102 0 -2 18 -42 39 -87 21 -46 49 -105 61\n-133 13 -27 38 -84 57 -125 19 -41 53 -120 75 -175 22 -55 44 -106 49 -115 5\n-8 38 -82 74 -165 175 -396 210 -472 247 -534 22 -36 43 -72 47 -80 4 -8 12\n-16 18 -18 6 -2 -3 34 -19 79 -16 46 -33 95 -38 110 -11 37 -58 155 -76 195\n-8 17 -14 34 -14 38 0 4 -6 21 -14 38 -8 18 -21 52 -30 77 -8 25 -29 79 -47\n120 -45 108 -71 190 -53 166 8 -11 14 -24 14 -29 0 -5 6 -23 14 -40 8 -18 31\n-75 51 -127 20 -52 40 -104 45 -115 5 -11 38 -92 73 -180 64 -159 87 -211 155\n-355 19 -41 51 -122 71 -180 25 -74 41 -106 54 -108 23 -5 22 4 -18 148 -20\n69 -43 152 -52 185 -8 33 -20 66 -25 73 -6 7 -7 12 -1 12 12 0 30 -39 84 -176\n23 -60 50 -121 61 -136 10 -15 30 -57 44 -95 14 -37 33 -77 43 -88 9 -11 26\n-40 36 -65 24 -59 41 -78 56 -63 8 8 -3 55 -44 180 -30 92 -67 206 -82 253\n-40 131 -118 342 -222 603 -52 131 -93 241 -90 243 4 4 33 -59 159 -345 63\n-145 86 -177 109 -154 10 10 10 21 2 45 -25 75 -165 430 -202 513 -30 65 -35\n89 -20 80 12 -7 57 -35 134 -83 11 -6 45 -24 75 -39 41 -21 50 -29 35 -31 -28\n-5 -55 -32 -55 -53 0 -22 78 -72 225 -144 130 -64 199 -90 319 -119 l90 -22 0\n-416 1 -416 -75 -12 c-122 -20 -272 -73 -343 -123 -60 -42 -114 -65 -140 -60\n-21 4 -27 1 -32 -19 -4 -14 -20 -33 -38 -43 -17 -10 -43 -32 -57 -48 -57 -67\n-138 -181 -160 -224 -21 -43 -24 -45 -31 -25 -10 26 -41 29 -64 5 -37 -36\n-132 -262 -165 -392 -19 -71 -22 -104 -18 -205 4 -134 -6 -103 123 -352 20\n-40 84 -130 142 -200 57 -71 112 -138 121 -149 16 -19 86 -103 142 -170 46\n-54 174 -190 213 -225 69 -62 230 -179 239 -174 10 7 -13 46 -48 85 -52 55\n-20 36 49 -29 298 -285 444 -405 493 -407 12 -1 35 -7 53 -15 17 -7 35 -10 39\n-6 4 4 -52 65 -124 136 -72 71 -134 138 -137 149 -6 24 -4 25 23 9 32 -20 34\n-1 5 34 -31 37 -154 174 -200 223 -113 120 -54 70 135 -112 109 -106 216 -193\n237 -193 18 0 3 30 -61 119 -43 61 -46 69 -46 130 0 49 5 74 20 96 11 17 20\n42 20 57 0 29 15 46 23 26 4 -10 8 -9 16 2 7 9 11 89 11 210 0 116 4 189 9\n181 30 -47 51 171 51 530 0 144 3 260 6 256 3 -3 10 -92 14 -199 5 -106 12\n-201 16 -211 11 -30 29 -13 36 34 4 24 15 91 24 149 12 73 18 188 21 375 l5\n271 86 -43 c48 -23 119 -51 157 -63 39 -12 96 -33 128 -46 32 -13 68 -24 80\n-24 19 0 19 2 -9 31 l-29 31 68 -33 c61 -30 83 -49 57 -49 -20 0 -10 -39 12\n-46 16 -5 26 -1 38 14 15 18 19 18 43 5 15 -7 27 -20 27 -28 0 -20 39 -19 47\n1 4 11 14 14 32 11 16 -3 46 3 71 14 l45 19 -57 -54 c-48 -45 -55 -56 -43 -66\n29 -24 130 57 228 180 l27 35 -16 -40 c-9 -22 -24 -68 -34 -103 -53 -189 94 2\n416 537 54 90 70 108 121 141 70 44 143 134 124 153 -9 9 -19 8 -41 -5 -35\n-20 -35 -19 -15 46 8 27 16 77 16 110 1 34 3 53 6 44 2 -10 9 -18 15 -18 6 0\n16 -7 23 -17 19 -25 47 -9 43 24 -2 22 -8 28 -25 27 -20 0 -23 5 -23 35 0 20\n-12 58 -25 85 l-24 50 41 145 c44 160 123 472 157 622 12 53 21 123 20 155 -3\n100 9 172 83 477 5 22 15 37 24 37 31 0 48 103 18 114 -16 6 -16 12 3 124 3\n17 11 32 18 32 18 0 37 77 33 140 -3 53 -4 55 -29 53 -25 -1 -28 3 -44 65 -9\n37 -25 81 -36 98 -28 45 -24 47 19 9 20 -19 43 -35 50 -35 19 0 14 34 -9 64\n-13 17 -18 33 -14 47 16 48 -78 99 -182 99 -192 0 -523 -152 -523 -240 0 -12\n-7 -31 -15 -44 -14 -21 -14 -25 0 -40 21 -21 10 -38 -19 -30 -47 11 -178 -73\n-303 -195 -90 -88 -218 -230 -283 -316 -187 -244 -325 -391 -360 -380 -13 4\n-43 -18 -110 -80 -50 -47 -96 -85 -101 -85 -6 0 -8 12 -5 28 10 44 25 222 26\n290 0 35 -7 97 -16 139 l-15 76 28 29 c15 16 58 48 96 70 70 42 82 58 45 58\n-40 -1 -105 -37 -159 -89 -29 -28 -55 -51 -57 -51 -8 0 45 67 72 90 46 39 220\n140 242 140 3 0 4 -7 1 -15 -8 -19 8 -28 33 -20 25 8 26 30 3 40 -14 5 -8 11\n27 24 48 18 145 63 246 112 33 17 74 42 90 55 l29 25 -30 -6 c-58 -12 -80 -15\n-65 -9 8 4 45 22 82 40 61 30 69 31 77 16 9 -16 12 -16 43 2 107 62 219 202\n313 391 36 72 65 122 65 112 0 -10 -4 -26 -9 -35 -4 -9 -11 -34 -14 -54 -9\n-60 17 -47 41 20 52 146 73 244 88 407 8 89 27 133 51 114 10 -9 17 -9 25 -1\n9 9 9 23 0 58 -10 36 -10 70 -1 155 19 173 -17 341 -90 425 -17 19 -31 41 -31\n49 0 24 -41 18 -47 -6 -6 -21 -6 -21 -19 2 -18 32 -17 49 2 49 27 0 13 44 -16\n48 -34 5 -45 16 -101 98 -81 120 -279 304 -327 304 -12 0 -22 -3 -22 -7 0 -8\n117 -116 152 -141 10 -7 18 -18 18 -25 0 -6 -19 7 -42 29 -24 22 -59 52 -79\n67 -44 32 -339 183 -383 196 -17 5 -53 18 -81 30 -27 11 -79 29 -115 40 -106\n32 -41 24 109 -14 160 -40 151 -39 151 -21 0 14 -79 52 -165 80 -147 47 -463\n60 -650 26z m478 -47 c-7 -2 -21 -2 -30 0 -10 3 -4 5 12 5 17 0 24 -2 18 -5z\nm-687 -339 c-26 -14 -67 -32 -90 -40 -81 -28 -187 -88 -267 -152 -45 -35 -79\n-58 -77 -51 9 26 171 149 242 184 136 66 303 118 192 59z m-736 -664 c0 -5 -5\n-10 -11 -10 -5 0 -7 5 -4 10 3 6 8 10 11 10 2 0 4 -4 4 -10z m-106 -281 c25\n-7 58 -30 90 -62 43 -43 55 -65 75 -129 30 -96 26 -169 -15 -253 -47 -96 -103\n-138 -223 -169 -77 -20 -92 -20 -164 -1 -60 15 -151 76 -175 115 -23 37 -6 33\n24 -5 29 -38 99 -85 127 -85 27 0 20 14 -16 33 -43 24 -94 90 -108 141 -18 66\n-8 203 19 257 37 76 122 146 197 162 40 9 131 7 169 -4z m2736 -964 c0 -7 -63\n-45 -75 -45 -5 0 6 11 25 25 34 24 50 30 50 20z m416 -160 c7 -30 -16 -234\n-27 -241 -5 -3 -7 32 -6 78 2 68 -1 83 -13 83 -11 0 -17 -18 -24 -68 -8 -60\n-11 -67 -28 -61 -27 8 -46 -12 -54 -55 -3 -20 -10 -49 -15 -66 -5 -16 -23 -88\n-40 -160 -32 -136 -142 -523 -228 -805 -28 -91 -62 -210 -77 -265 -32 -122\n-43 -141 -87 -149 -46 -9 -140 -77 -169 -123 -53 -85 -78 -203 -43 -203 11 0\n15 -12 15 -49 0 -34 -4 -51 -15 -55 -22 -8 -18 -36 5 -36 10 0 24 -9 30 -20 9\n-17 8 -20 -10 -20 -14 0 -20 -7 -20 -24 0 -31 28 -42 46 -19 7 10 14 13 14 7\n0 -6 -8 -18 -18 -28 -11 -9 -36 -42 -57 -74 -22 -31 -79 -106 -128 -165 -167\n-204 -178 -231 -84 -203 80 24 157 92 247 219 36 50 48 61 29 25 -25 -48 -123\n-169 -170 -212 -68 -61 -139 -77 -218 -50 -29 10 -63 26 -75 35 -23 18 -269\n151 -300 163 -10 3 -17 9 -14 11 5 6 51 -12 143 -55 99 -46 178 -77 213 -85\n31 -6 33 -5 28 13 -5 14 -1 21 14 25 28 7 25 26 -7 38 -32 12 -53 5 -53 -16 0\n-15 -3 -15 -27 -1 -30 17 -161 76 -168 76 -2 0 -32 13 -67 30 -35 16 -119 54\n-188 84 l-125 55 0 108 c1 59 4 230 9 378 7 251 4 324 -18 376 -6 13 3 20 36\n33 24 10 73 35 110 56 42 24 75 36 87 33 15 -4 21 -1 21 11 0 9 23 42 51 73\n28 32 62 71 75 86 24 28 24 47 0 47 -7 0 -31 -13 -52 -28 -74 -53 -68 -45 50\n68 97 93 265 273 306 326 61 81 235 281 311 359 53 55 110 115 124 133 15 18\n30 31 33 28 5 -6 -71 -95 -262 -306 -190 -209 -278 -318 -340 -417 -4 -8 -2\n-13 6 -13 7 0 69 63 137 140 69 77 128 140 132 140 5 0 -37 -63 -92 -139 -91\n-128 -114 -171 -90 -171 4 0 19 10 32 23 13 13 41 31 63 41 60 28 151 128 270\n296 174 246 238 330 316 413 111 118 279 237 335 237 14 0 25 4 25 9 0 8 66\n49 82 50 4 1 10 -10 14 -24z m-1151 -14 c-6 -5 -48 -26 -95 -46 -98 -41 -108\n-46 -252 -114 -129 -62 -188 -100 -250 -164 -57 -59 -76 -100 -58 -122 10 -12\n10 -19 2 -27 -7 -7 -12 -35 -13 -62 -1 -49 -1 -50 -9 -16 -5 18 -15 39 -24 45\n-12 9 -14 22 -7 62 9 57 30 91 93 149 36 34 159 114 173 114 2 0 37 15 77 34\n114 52 146 66 152 66 5 0 106 44 171 75 36 16 53 19 40 6z m-715 -48 c-52 -24\n-112 -61 -134 -81 -38 -37 -38 -37 -23 -7 21 41 54 75 73 75 9 0 28 7 42 15\n34 19 102 44 122 44 8 0 -28 -21 -80 -46z m-217 -348 c-5 -77 -8 -95 -16 -77\n-6 12 -16 22 -24 22 -19 0 -16 30 7 75 11 22 20 47 20 55 0 16 10 33 16 27 1\n-1 0 -47 -3 -102z m257 -609 c-5 -5 -12 -7 -15 -4 -5 4 4 170 17 333 1 17 4\n-48 5 -144 2 -108 0 -178 -7 -185z m-1385 162 c26 -20 26 -22 9 -29 -28 -11\n-31 -11 -38 9 -3 9 -9 23 -12 30 -9 19 10 14 41 -10z m2992 -110 c-3 -8 -6 -5\n-6 6 -1 11 2 17 5 13 3 -3 4 -12 1 -19z m-7 -36 c0 -10 -7 -27 -15 -38 -13\n-17 -13 -15 -3 19 12 41 18 47 18 19z m-1982 -113 c53 -13 81 -34 67 -48 -6\n-6 -35 4 -75 24 -69 36 -67 42 8 24z m66 -405 c-1 -21 -2 -198 -3 -391 -1\n-233 -4 -353 -11 -353 -7 0 -10 132 -9 395 1 292 4 395 13 392 6 -2 10 -21 10\n-43z m297 -69 l-9 -60 -1 66 c0 36 2 69 7 73 12 12 13 -15 3 -79z m1418 -36\nc-13 -43 -25 -66 -27 -55 -4 18 38 139 46 132 2 -2 -7 -37 -19 -77z m58 -265\nc18 -14 45 -48 59 -76 22 -43 25 -62 22 -115 -6 -101 -61 -154 -161 -155 -60\n-1 -97 18 -115 57 -6 14 -19 25 -28 25 -14 0 -16 -7 -11 -37 6 -35 5 -36 -8\n-19 -21 27 -29 79 -22 139 6 45 12 58 52 96 30 28 43 47 37 53 -13 13 -43 3\n-70 -23 -29 -26 -28 -11 2 22 37 43 75 58 145 59 54 0 70 -4 98 -26z m209 -29\nc21 -51 17 -77 -6 -35 -18 34 -26 70 -14 70 3 0 12 -16 20 -35z m-1674 -283\nc2 -100 0 -182 -4 -182 -5 0 -8 62 -8 138 0 77 -2 175 -5 218 -4 60 -2 70 4\n44 6 -19 11 -117 13 -218z m1104 -230 c-17 -16 -18 -16 -5 5 7 12 15 20 18 17\n3 -2 -3 -12 -13 -22z m-1871 -187 c-39 -24 -53 -26 -40 -5 8 13 43 29 65 29 8\n0 -3 -11 -25 -24z m388 -442 c4 -258 10 -492 13 -521 7 -57 25 -65 35 -16 6\n35 17 -184 28 -551 4 -148 11 -279 16 -289 16 -37 35 11 36 93 1 70 2 73 10\n36 5 -22 8 -82 7 -133 l-3 -93 -105 117 c-105 117 -210 238 -305 350 -96 113\n-211 240 -326 359 -118 122 -141 155 -153 211 -5 24 -3 33 8 37 9 4 16 17 16\n31 0 35 -18 78 -30 71 -14 -8 -13 2 10 140 27 165 30 173 104 279 36 52 66 99\n66 105 0 14 -13 14 -28 -1 -6 -6 -14 -9 -18 -6 -3 4 20 39 51 78 32 39 60 68\n63 65 3 -3 -3 -14 -13 -25 -21 -24 -12 -40 13 -22 18 13 69 40 147 80 71 36\n209 70 286 71 l66 1 6 -467z m56 8 c-2 -252 -7 -457 -11 -454 -15 8 -4 913 10\n913 2 0 3 -207 1 -459z m-1097 -498 c3 -27 9 -56 13 -67 6 -14 4 -17 -11 -14\n-16 3 -19 14 -22 71 -3 78 13 85 20 10z m886 -810 c13 -19 23 -35 21 -38 -5\n-4 -155 165 -177 199 -18 27 123 -118 156 -161z"/>\n<path d="M2785 6410 c-8 -14 118 -151 128 -141 13 13 -1 63 -26 91 -44 50 -88\n72 -102 50z"/>\n<path d="M2874 6209 c-4 -7 -7 -33 -5 -58 1 -33 -5 -60 -23 -96 -27 -51 -26\n-66 1 -49 41 25 58 64 58 129 0 64 -15 100 -31 74z"/>\n<path d="M2930 6186 c0 -20 -8 -64 -17 -99 -23 -83 -90 -150 -175 -175 -62\n-18 -73 -39 -17 -34 127 12 247 160 237 293 -4 54 -28 66 -28 15z"/>\n<path d="M4634 4179 c-9 -15 12 -33 30 -26 9 4 16 13 16 22 0 17 -35 21 -46 4z"/>\n<path d="M5066 3418 c-20 -28 -20 -52 -2 -67 12 -10 17 -9 24 6 10 18 15 47\n13 71 -2 19 -18 14 -35 -10z"/>\n<path d="M5441 3416 c-17 -20 13 -43 34 -26 12 10 13 16 4 26 -6 8 -15 14 -19\n14 -4 0 -13 -6 -19 -14z"/>\n<path d="M3727 1543 c-4 -3 -7 -46 -7 -95 0 -87 1 -89 23 -86 20 3 22 9 25 69\n4 85 -16 138 -41 112z"/>\n<path d="M3747 1334 c-14 -14 -7 -35 11 -32 9 2 17 10 17 17 0 16 -18 25 -28\n15z"/>\n<path d="M3712 1198 c-7 -7 -12 -20 -12 -30 0 -22 42 -25 60 -3 26 32 -18 63\n-48 33z"/>\n<path d="M4740 7612 c0 -11 21 -30 53 -49 69 -43 87 -49 87 -31 0 17 -31 47\n-81 76 -44 27 -59 28 -59 4z"/>\n<path d="M4880 7606 c0 -10 9 -16 21 -16 24 0 21 23 -4 28 -10 2 -17 -3 -17\n-12z"/>\n<path d="M5100 6901 c0 -4 25 -54 56 -110 54 -99 81 -122 77 -66 -4 52 -133\n223 -133 176z"/>\n<path d="M5544 6289 c-3 -6 0 -18 9 -26 18 -18 37 -9 37 18 0 21 -34 27 -46 8z"/>\n<path d="M5117 6063 c-15 -15 -7 -43 13 -43 15 0 20 7 20 25 0 24 -18 34 -33\n18z"/>\n<path d="M2237 5943 c-19 -18 -3 -56 44 -107 46 -51 79 -70 79 -47 0 5 -15 27\n-34 50 -19 22 -44 57 -56 76 -24 38 -24 38 -33 28z"/>\n<path d="M5053 5918 c-43 -47 -56 -77 -43 -93 16 -19 26 -12 60 42 39 61 26\n98 -17 51z"/>\n<path d="M2686 5694 c-22 -21 -17 -48 10 -52 28 -4 51 24 38 46 -15 23 -29 25\n-48 6z"/>\n<path d="M6140 5455 c0 -14 6 -25 13 -25 20 0 25 9 20 31 -8 28 -33 24 -33 -6z"/>\n<path d="M4620 5155 c0 -8 5 -15 10 -15 6 0 10 7 10 15 0 8 -4 15 -10 15 -5 0\n-10 -7 -10 -15z"/>\n<path d="M6075 4748 c-19 -65 -20 -118 -1 -118 25 0 35 25 42 98 6 62 5 72 -9\n72 -11 0 -21 -17 -32 -52z"/>\n<path d="M6054 4535 c-3 -14 -4 -28 -1 -31 10 -10 27 7 27 26 0 30 -19 33 -26\n5z"/>\n<path d="M3092 3643 c2 -10 10 -18 18 -18 8 0 16 8 18 18 2 12 -3 17 -18 17\n-15 0 -20 -5 -18 -17z"/>\n<path d="M3127 3556 c-7 -18 10 -82 30 -113 13 -19 18 -21 30 -11 14 12 14 17\n-8 101 -10 38 -41 52 -52 23z"/>\n<path d="M5850 3425 c0 -8 7 -15 15 -15 8 0 15 7 15 15 0 8 -7 15 -15 15 -8 0\n-15 -7 -15 -15z"/>\n<path d="M3207 3303 c-21 -20 9 -93 39 -93 8 0 20 11 26 25 10 22 8 29 -12 50\n-23 25 -41 31 -53 18z"/>\n<path d="M5840 3195 c0 -8 5 -15 10 -15 6 0 10 7 10 15 0 8 -4 15 -10 15 -5 0\n-10 -7 -10 -15z"/>\n<path d="M4794 2278 c-8 -13 30 -48 52 -48 22 0 17 27 -8 44 -27 19 -35 20\n-44 4z"/>\n<path d="M4136 1594 c-10 -26 -7 -92 4 -99 22 -13 40 17 40 66 0 41 -3 49 -19\n49 -10 0 -22 -7 -25 -16z"/>\n<path d="M4131 1442 c-6 -11 -6 -24 0 -35 12 -21 23 -22 43 -1 12 12 13 20 6\n35 -13 24 -36 25 -49 1z"/>\n<path d="M3952 538 c-17 -17 -15 -32 8 -53 19 -17 22 -17 37 -3 18 19 12 42\n-15 57 -13 7 -22 7 -30 -1z"/>\n<path d="M4015 460 c-8 -13 13 -40 30 -40 15 0 24 19 18 38 -6 15 -39 16 -48\n2z"/>\n<path d="M4126 231 c-8 -13 21 -34 39 -27 20 7 8 36 -15 36 -11 0 -21 -4 -24\n-9z"/>\n</g>',
    'morte': '<g transform="translate(0.000000,800.000000) scale(0.100000,-0.100000)"\nfill="currentColor" stroke="none">\n<path d="M4974 7895 c-4 -15 -1 -32 6 -40 15 -19 64 -21 57 -2 -31 72 -52 86\n-63 42z"/>\n<path d="M4960 7749 c-7 -12 -9 -34 -5 -53 4 -17 7 -49 8 -71 1 -74 -24 -588\n-38 -780 -8 -104 -17 -232 -21 -282 -6 -78 -9 -93 -25 -98 -25 -8 -24 -22 1\n-35 13 -7 20 -21 20 -40 0 -25 -4 -30 -25 -30 -14 0 -25 -4 -25 -8 0 -15 24\n-32 46 -32 30 0 40 -17 14 -25 -33 -11 -23 -23 25 -31 30 -4 49 -13 56 -26 10\n-18 12 -18 27 -5 15 14 25 14 87 1 246 -51 493 -191 719 -408 43 -42 82 -76\n86 -76 48 0 -37 168 -131 261 -31 31 -81 70 -112 88 -30 17 -61 39 -68 48 -8\n9 -72 44 -142 79 -115 57 -164 75 -309 115 -27 7 -38 15 -38 29 0 15 8 19 38\n22 48 4 49 27 2 42 l-35 11 0 540 c0 532 -1 540 -20 540 -18 0 -20 -6 -16 -62\n4 -79 -12 -82 -28 -5 -28 133 -32 169 -26 207 12 76 9 87 -32 99 -16 5 -25 1\n-33 -15z"/>\n<path d="M5160 7570 c0 -13 5 -20 13 -17 6 2 12 10 12 17 0 7 -6 15 -12 18 -8\n2 -13 -5 -13 -18z"/>\n<path d="M5190 6453 c0 -6 39 -27 88 -47 48 -20 128 -63 177 -96 50 -33 96\n-60 103 -60 49 0 -60 90 -183 150 -98 48 -185 73 -185 53z"/>\n<path d="M4415 6409 c-44 -5 -123 -20 -175 -34 -52 -13 -154 -40 -226 -59\n-202 -53 -399 -144 -614 -284 -206 -135 -478 -398 -565 -546 -16 -28 -31 -43\n-38 -39 -31 18 -277 268 -433 440 -152 167 -190 203 -210 203 -11 0 21 -46 48\n-68 16 -14 45 -50 65 -81 57 -89 188 -243 352 -413 140 -145 165 -178 142\n-192 -11 -7 -420 409 -536 544 -249 291 -470 520 -502 520 -20 0 -43 -18 -43\n-33 0 -5 37 -48 82 -96 101 -107 282 -321 276 -326 -2 -3 -55 47 -118 110 -63\n63 -118 115 -122 115 -18 0 -5 -29 38 -83 37 -47 42 -58 24 -52 -21 7 -21 6\n-5 -18 9 -14 36 -42 61 -62 24 -20 81 -78 126 -128 45 -51 179 -193 297 -317\n119 -124 246 -257 283 -296 38 -39 68 -77 68 -84 0 -8 -22 -75 -49 -149 -62\n-170 -67 -192 -81 -376 -12 -163 -2 -376 24 -490 75 -330 248 -694 417 -879\n53 -58 206 -199 268 -248 l35 -26 -56 -74 c-30 -40 -91 -114 -136 -165 -95\n-108 -117 -143 -91 -143 6 0 37 25 68 55 54 51 59 54 64 34 7 -26 -7 -48 -173\n-269 -64 -85 -135 -186 -159 -225 -24 -38 -75 -115 -114 -170 -59 -82 -71\n-107 -74 -145 -2 -25 2 -55 8 -66 9 -18 5 -29 -26 -70 -36 -47 -35 -65 3 -45\n21 11 66 64 264 309 136 169 172 212 166 197 -18 -47 -122 -246 -140 -267 -12\n-15 -19 -31 -15 -34 15 -16 62 35 142 153 111 166 197 290 304 438 48 66 118\n164 157 217 38 54 74 98 81 98 7 0 43 -12 80 -26 137 -52 239 -82 298 -89 33\n-3 70 -11 83 -17 17 -9 25 -9 34 0 19 19 48 14 57 -10 11 -31 23 -523 26\n-1111 2 -434 -1 -514 -20 -495 -2 2 -5 147 -6 323 -3 318 -5 390 -28 890 -14\n299 -17 325 -36 325 -22 0 -29 -130 -21 -460 7 -324 14 -452 26 -445 15 9 21\n-144 19 -436 -2 -290 -2 -300 18 -318 17 -16 21 -37 28 -130 4 -70 12 -116 20\n-124 13 -13 52 -427 42 -444 -9 -16 28 -108 45 -111 21 -4 25 35 34 328 3 124\n10 247 15 275 5 27 14 83 19 124 5 40 12 76 15 79 3 3 5 -94 5 -216 0 -122 5\n-289 11 -372 8 -115 8 -154 -1 -165 -9 -11 -8 -19 4 -37 9 -12 16 -30 16 -40\n0 -10 4 -18 9 -18 13 0 21 79 26 256 9 317 21 601 36 879 12 234 12 308 -1\n300 -6 -3 -10 -25 -11 -48 -1 -23 -5 -53 -9 -67 -6 -18 -9 -4 -10 50 -1 41 -3\n80 -5 85 -1 6 -3 33 -4 60 -5 136 -32 -58 -47 -330 -8 -157 -12 -196 -19 -203\n-9 -9 -3 792 7 1035 5 114 12 211 17 216 7 7 118 -116 182 -203 20 -26 49 -41\n49 -24 0 6 -187 314 -223 366 -6 9 -8 21 -3 25 5 4 13 0 20 -11 55 -90 154\n-227 257 -356 68 -85 173 -222 233 -305 318 -438 458 -614 488 -615 4 0 12 9\n18 20 15 27 1 53 -207 374 -97 149 -203 318 -236 376 -85 150 -161 279 -239\n406 -181 297 -8 81 278 -346 132 -198 156 -230 173 -230 17 0 13 16 -14 70\n-29 57 -18 65 25 17 69 -74 -4 43 -257 413 -91 134 -213 341 -207 351 3 5 25\n9 49 9 102 0 385 86 512 156 106 58 233 145 233 160 0 27 -20 22 -101 -23\n-317 -176 -517 -244 -668 -228 -35 4 -59 10 -55 14 5 5 53 13 107 20 94 12\n231 44 252 60 5 4 40 20 77 35 42 17 68 34 71 46 3 11 60 55 128 99 68 44 145\n96 171 115 59 46 78 46 44 1 -37 -48 -19 -44 31 8 92 95 177 232 310 500 107\n213 151 370 158 567 3 69 7 161 10 205 10 150 -33 426 -84 539 -24 54 -231\n308 -340 417 -248 247 -490 351 -876 375 -150 9 -121 19 86 27 178 8 251 -2\n428 -58 197 -62 311 -119 446 -222 104 -79 151 -129 218 -233 63 -97 127 -165\n127 -136 0 27 -65 151 -118 226 -67 93 -110 133 -262 241 -153 109 -264 172\n-395 222 -146 56 -176 60 -431 54 -205 -5 -243 -8 -364 -35 -270 -59 -454\n-135 -615 -255 -128 -95 -328 -277 -382 -348 -44 -57 -101 -159 -121 -217 -14\n-42 -10 -44 -166 105 -98 94 -116 116 -111 134 5 15 10 18 18 10 20 -20 36\n-11 58 30 11 23 40 63 63 88 59 65 79 99 72 126 -5 18 12 39 97 122 206 200\n476 339 834 431 178 46 265 62 391 71 62 5 118 13 123 18 15 15 -19 52 -59 63\n-20 5 -55 10 -77 10 -22 0 -47 4 -55 10 -11 7 -8 11 15 20 17 6 41 9 56 6 18\n-3 28 1 37 14 14 22 1 33 -55 44 -34 8 -221 -10 -257 -24 -13 -5 -81 -18 -152\n-30 -269 -45 -648 -217 -892 -403 -180 -138 -192 -144 -87 -42 104 102 209\n179 350 257 110 61 317 158 380 178 22 7 65 23 96 36 58 24 382 108 478 124\n52 9 81 24 81 41 0 10 -43 10 -155 -2z m-1374 -792 c-19 -18 -30 -25 -26 -17\n13 25 48 60 54 55 3 -3 -10 -20 -28 -38z m1837 -52 c130 -24 143 -29 265 -95\n226 -123 360 -246 478 -440 100 -163 108 -179 163 -347 66 -202 83 -398 51\n-607 -16 -109 -69 -241 -155 -391 -47 -83 -173 -247 -231 -302 -79 -74 -194\n-162 -242 -186 -156 -77 -289 -129 -362 -142 -80 -15 -377 -21 -422 -9 -23 6\n-23 8 -23 178 0 94 4 177 9 184 5 8 44 17 102 22 152 15 317 53 394 93 86 43\n221 162 252 222 7 13 38 61 68 106 37 56 64 109 80 163 50 166 64 401 30 526\n-22 84 -83 212 -130 272 -43 55 -135 121 -161 116 -15 -2 -6 -13 38 -48 51\n-41 128 -139 128 -163 0 -5 -25 16 -56 47 -60 60 -189 141 -321 200 -88 40\n-231 67 -358 70 -69 1 -70 1 -76 31 l-7 30 -1 -31 c-1 -40 -7 -44 -40 -31 -24\n9 -31 7 -49 -11 -12 -12 -29 -22 -37 -22 -8 0 -30 -14 -50 -31 l-35 -31 0\n-155 c0 -100 -3 -152 -10 -148 -6 4 -10 83 -10 203 0 108 -3 207 -6 220 -8 26\n-24 29 -24 4 0 -10 -5 -23 -10 -28 -6 -6 -11 -44 -11 -85 0 -42 -4 -91 -9\n-110 -7 -30 -8 -26 -9 29 -1 34 -4 62 -8 62 -5 0 -8 -61 -8 -136 -1 -116 -3\n-137 -18 -145 -15 -9 -15 -11 3 -29 11 -11 17 -25 15 -32 -3 -7 -34 -38 -69\n-68 -56 -49 -110 -118 -146 -190 -37 -71 -51 -214 -30 -292 11 -40 58 -103 69\n-93 3 4 6 61 6 128 0 155 15 202 90 283 34 36 58 55 64 49 4 -6 11 -198 15\n-428 5 -356 4 -419 -8 -423 -15 -6 -99 18 -216 62 -79 29 -146 86 -249 210\n-98 118 -164 236 -190 341 -35 137 -24 368 25 533 22 76 155 336 208 407 117\n159 243 258 427 334 40 16 77 33 83 39 7 5 53 23 104 40 227 76 388 88 620 45z\nm-1668 -160 c-7 -8 -17 -15 -22 -15 -6 0 -5 7 2 15 7 8 17 15 22 15 6 0 5 -7\n-2 -15z m3 -453 c48 -47 87 -89 87 -94 0 -4 -17 -8 -39 -8 -37 0 -45 6 -150\n119 -61 65 -111 123 -111 128 0 5 5 14 12 21 13 13 41 -10 201 -166z m-271\n-109 c-21 -54 -63 -287 -74 -407 -5 -67 -11 -123 -13 -124 -2 -2 -7 17 -11 43\n-15 104 10 303 58 455 26 80 28 84 41 71 7 -7 6 -19 -1 -38z m142 -102 l108\n-108 -17 -78 c-39 -173 5 -503 86 -655 30 -57 148 -200 171 -209 6 -2 -6 21\n-26 50 -82 124 -146 272 -176 407 -30 133 -27 472 4 442 2 -3 0 -41 -6 -85\n-16 -129 8 -318 51 -391 6 -10 24 -54 41 -98 16 -43 48 -111 71 -150 51 -86\n209 -247 291 -295 32 -18 126 -55 210 -83 84 -27 160 -53 168 -57 12 -6 16\n-32 18 -115 3 -100 13 -133 25 -84 10 37 16 17 26 -97 12 -129 4 -230 -19\n-230 -10 0 -17 14 -20 45 -3 25 -9 44 -13 44 -45 -9 -48 -12 -41 -48 5 -31 4\n-36 -13 -36 -50 0 -187 45 -277 91 -54 28 -104 48 -110 46 -7 -3 -23 1 -37 9\n-13 7 -30 13 -38 14 -20 0 5 -28 43 -47 17 -9 42 -25 56 -37 14 -12 105 -48\n203 -80 168 -56 215 -83 124 -70 -133 17 -305 90 -439 185 -96 69 -230 205\n-322 329 -97 131 -230 376 -277 508 -29 84 -1 57 41 -38 20 -46 42 -86 48 -88\n15 -5 16 -8 -12 72 -14 39 -34 77 -45 85 -15 11 -27 44 -43 111 -18 81 -22\n125 -22 305 0 230 30 545 52 545 4 0 56 -49 116 -109z m1553 20 c116 -24 200\n-68 278 -146 78 -78 126 -158 166 -276 48 -141 50 -213 9 -343 -25 -78 -43\n-117 -72 -152 -65 -78 -208 -185 -293 -219 l-40 -15 35 29 c19 16 42 36 50 45\n8 10 29 29 45 44 80 72 136 138 160 187 43 86 20 124 -25 42 -49 -90 -88 -134\n-156 -179 -38 -25 -72 -44 -77 -41 -4 2 15 30 43 60 54 61 130 197 131 235 0\n12 -2 79 -4 148 -5 139 -20 192 -68 253 -21 26 -26 37 -15 37 20 0 87 -91 119\n-162 44 -96 62 -83 31 22 -43 145 -247 315 -404 338 -36 6 -78 9 -95 8 -29 -1\n-30 1 -33 46 -2 38 0 47 15 51 40 10 113 6 200 -12z m-53 -244 c78 -41 152\n-116 185 -186 23 -50 25 -68 25 -166 0 -153 -37 -241 -154 -366 -54 -58 -208\n-156 -221 -141 -10 10 -14 222 -11 586 l2 319 57 -7 c34 -4 80 -19 117 -39z\nm-423 -392 c-6 -170 -11 -358 -11 -417 0 -60 -4 -108 -9 -108 -11 0 -22 211\n-17 330 3 52 8 192 11 310 5 136 11 220 19 229 18 23 19 -10 7 -344z m388\n-561 c-28 -15 -109 -19 -109 -4 0 11 26 18 80 23 47 4 58 -3 29 -19z m-1209\n-638 c0 -7 -18 -33 -40 -57 l-41 -44 27 45 c42 70 54 83 54 56z m180 -115 c0\n-5 -7 -14 -15 -21 -12 -10 -15 -10 -15 2 0 8 3 18 7 21 9 10 23 9 23 -2z m-83\n-106 c-8 -18 -181 -235 -185 -230 -5 5 90 148 132 197 40 47 63 62 53 33z\nm718 -1731 c0 -10 -8 -20 -17 -22 -18 -3 -26 27 -11 42 12 11 28 0 28 -20z"/>\n<path d="M4505 5493 c-102 -23 -307 -88 -369 -117 -43 -20 -81 -36 -85 -36\n-17 0 -173 -110 -240 -170 -113 -100 -225 -260 -183 -260 11 0 52 46 86 97 84\n127 254 241 481 324 44 17 130 49 190 74 61 24 129 46 153 50 32 5 42 11 42\n26 0 19 -26 23 -75 12z"/>\n<path d="M5100 5385 c0 -2 30 -22 68 -44 114 -66 320 -296 402 -449 38 -70 58\n-97 66 -89 13 13 -22 87 -99 212 -96 157 -149 221 -230 279 -75 53 -207 112\n-207 91z"/>\n<path d="M3547 4843 c-13 -13 -7 -53 8 -53 17 0 39 36 30 50 -7 11 -28 13 -38\n3z"/>\n<path d="M5663 4752 c-14 -9 34 -92 53 -92 19 0 18 35 -2 65 -17 27 -36 36\n-51 27z"/>\n<path d="M3478 4693 c-38 -44 -70 -191 -69 -322 0 -106 13 -171 34 -171 9 0\n11 39 9 148 -4 129 -1 154 17 206 11 32 28 67 36 76 22 24 24 73 4 77 -8 2\n-22 -4 -31 -14z"/>\n<path d="M3929 4581 c-115 -74 -196 -208 -214 -355 -7 -57 18 -206 35 -206 4\n0 5 38 3 84 -12 192 67 366 208 456 27 18 49 36 49 41 0 18 -37 9 -81 -20z"/>\n<path d="M5739 4569 c-10 -20 -9 -46 6 -149 2 -14 4 -33 4 -42 1 -11 9 -18 22\n-18 15 0 19 4 15 16 -3 9 -9 60 -13 115 -3 54 -10 99 -14 99 -5 0 -13 -10 -20\n-21z"/>\n<path d="M5450 4326 c0 -9 5 -16 10 -16 6 0 10 4 10 9 0 6 -4 13 -10 16 -5 3\n-10 -1 -10 -9z"/>\n<path d="M5750 4191 c-13 -25 -13 -71 0 -91 16 -24 30 6 30 63 0 49 -12 61\n-30 28z"/>\n<path d="M5423 4153 c-4 -9 -7 -35 -7 -57 -1 -30 3 -41 15 -44 13 -2 18 8 23\n48 4 28 4 55 1 60 -9 15 -25 12 -32 -7z"/>\n<path d="M5347 3953 c-3 -5 -23 -55 -46 -113 -51 -130 -89 -183 -191 -264 -95\n-74 -197 -128 -297 -155 -81 -22 -96 -29 -88 -41 9 -15 110 -8 160 12 148 58\n344 214 423 336 41 63 82 198 67 222 -7 12 -22 13 -28 3z"/>\n<path d="M3820 3935 c0 -8 5 -15 10 -15 6 0 10 7 10 15 0 8 -4 15 -10 15 -5 0\n-10 -7 -10 -15z"/>\n<path d="M3916 3904 c-8 -21 1 -34 24 -34 10 0 21 4 24 9 10 16 -5 41 -24 41\n-10 0 -21 -7 -24 -16z"/>\n<path d="M4564 3340 c-33 -13 -39 -30 -10 -30 33 0 76 17 76 30 0 12 -36 12\n-66 0z"/>\n<path d="M3527 3613 c-12 -11 -8 -21 24 -52 53 -54 71 -29 20 27 -32 34 -34\n36 -44 25z"/>\n<path d="M3330 3301 c0 -24 18 -37 32 -23 8 8 7 16 -2 27 -17 21 -30 19 -30\n-4z"/>\n<path d="M3360 3237 c0 -17 88 -105 124 -124 21 -11 31 -12 40 -3 8 8 6 14 -9\n22 -11 7 -45 36 -76 65 -55 53 -79 65 -79 40z"/>\n<path d="M4947 4153 c-11 -10 -8 -41 4 -49 13 -8 29 13 29 39 0 17 -20 23 -33\n10z"/>\n<path d="M4614 6405 c-4 -9 -2 -21 4 -27 16 -16 47 -5 47 17 0 26 -42 34 -51\n10z"/>\n<path d="M4707 6404 c-16 -17 -5 -25 31 -22 20 2 37 8 37 13 0 12 -57 19 -68\n9z"/>\n<path d="M2044 6206 c-7 -19 3 -36 22 -36 10 0 14 8 12 22 -4 26 -26 36 -34\n14z"/>\n<path d="M5950 5776 c0 -20 39 -66 56 -66 22 0 17 28 -11 55 -27 28 -45 32\n-45 11z"/>\n<path d="M6033 5684 c-3 -9 4 -27 16 -42 12 -15 34 -48 49 -74 26 -46 62 -65\n62 -34 0 8 3 21 6 30 4 11 -2 19 -20 26 -14 5 -26 15 -26 21 0 15 -59 89 -71\n89 -5 0 -12 -7 -16 -16z"/>\n<path d="M2577 5163 c-11 -10 -8 -123 3 -123 6 0 10 11 10 25 0 14 7 36 15 49\n23 34 -2 76 -28 49z"/>\n<path d="M2535 5052 c-23 -16 -45 -64 -59 -129 -59 -280 -59 -277 -53 -483 8\n-294 37 -470 105 -645 52 -131 163 -372 199 -431 38 -63 53 -71 86 -50 28 19\n27 42 -2 51 -24 7 -151 274 -195 410 -34 107 -74 299 -95 460 -6 44 -16 114\n-22 155 -10 68 -7 273 5 325 8 36 48 229 53 255 6 35 -9 91 -22 82z"/>\n<path d="M6086 4885 l-29 -23 25 -59 c34 -76 113 -357 128 -453 13 -77 24\n-110 39 -110 17 0 0 234 -25 345 -14 61 -29 121 -34 135 -5 14 -19 58 -31 97\n-27 90 -35 98 -73 68z"/>\n<path d="M6272 4138 c-6 -7 -13 -25 -17 -41 -6 -24 -5 -27 19 -27 18 0 26 6\n27 18 3 45 -11 71 -29 50z"/>\n<path d="M6269 3966 c-7 -14 -20 -26 -31 -26 -26 0 -24 -53 4 -79 16 -15 18\n-24 10 -33 -10 -12 -25 -90 -50 -253 -13 -81 -77 -317 -103 -374 -11 -25 -31\n-73 -43 -106 -31 -80 -100 -218 -143 -282 -34 -52 -73 -144 -73 -172 0 -9 -12\n-25 -27 -37 -33 -26 -27 -40 9 -23 17 7 36 35 58 82 18 40 42 84 52 100 23 32\n44 37 28 6 -19 -34 -5 -32 24 2 38 45 118 208 151 309 16 47 43 120 61 162 43\n102 100 312 114 418 11 93 12 261 1 299 -9 34 -29 37 -42 7z"/>\n<path d="M6024 3944 c-3 -10 -17 -44 -30 -75 -27 -63 -30 -83 -12 -77 36 12\n83 137 60 160 -9 9 -13 7 -18 -8z"/>\n<path d="M6146 3704 c-3 -9 -6 -28 -6 -44 0 -49 -39 -199 -86 -335 -54 -155\n-62 -189 -43 -183 31 9 114 204 138 326 17 82 26 252 14 252 -6 0 -14 -7 -17\n-16z"/>\n<path d="M5767 3403 c-65 -110 -90 -162 -83 -178 10 -27 30 -11 48 38 9 25 38\n75 63 112 25 37 43 71 40 76 -14 21 -36 5 -68 -48z"/>\n<path d="M2854 3235 c-4 -9 18 -38 58 -78 65 -67 93 -83 117 -68 20 13 6 41\n-20 41 -22 0 -79 53 -79 75 0 17 -33 45 -54 45 -9 0 -19 -7 -22 -15z"/>\n<path d="M5549 3060 c-9 -28 -9 -44 -2 -51 17 -17 43 18 43 57 0 48 -26 45\n-41 -6z"/>\n<path d="M5714 2555 c-8 -20 -4 -24 27 -27 18 -2 24 2 24 17 0 25 -42 34 -51\n10z"/>\n<path d="M4410 2031 c0 -33 16 -49 33 -32 7 7 7 18 -1 36 -16 34 -32 32 -32\n-4z"/>\n<path d="M4646 1954 c-8 -20 9 -39 43 -49 22 -7 23 -5 17 21 -9 34 -6 30 -32\n38 -15 5 -24 2 -28 -10z"/>\n<path d="M2540 1825 c0 -20 -10 -37 -32 -56 -29 -24 -31 -28 -15 -34 24 -10\n45 11 58 57 7 29 7 44 0 51 -8 8 -11 3 -11 -18z"/>\n<path d="M4762 1828 c-16 -16 -15 -35 3 -42 19 -7 46 20 39 39 -7 18 -26 19\n-42 3z"/>\n<path d="M4850 1725 c0 -26 30 -75 45 -75 21 0 19 10 -10 53 -28 41 -35 45\n-35 22z"/>\n<path d="M5257 1654 c-11 -11 -8 -49 4 -61 14 -14 39 -6 39 12 0 19 -34 58\n-43 49z"/>\n<path d="M2478 1639 c-22 -12 -24 -47 -4 -55 24 -9 58 15 54 39 -3 25 -25 32\n-50 16z"/>\n<path d="M5416 1412 c-8 -13 18 -57 37 -60 21 -4 22 16 1 46 -16 23 -29 28\n-38 14z"/>\n<path d="M4096 691 c-4 -6 -3 -19 3 -28 9 -17 10 -17 17 0 8 21 -10 45 -20 28z"/>\n</g>',
    'sangue': '<g transform="translate(0.000000,800.000000) scale(0.100000,-0.100000)"\nfill="currentColor" stroke="none">\n<path d="M3800 7839 c0 -21 -8 -39 -24 -54 -21 -20 -25 -33 -30 -131 -16 -284\n-17 -294 -32 -294 -8 0 -14 -9 -14 -20 0 -11 -4 -20 -9 -20 -9 0 -21 57 -21\n102 0 15 -5 39 -11 55 -16 44 -29 10 -29 -81 0 -119 -14 -81 -20 54 -3 71 -10\n124 -17 131 -26 26 -33 -3 -33 -142 0 -76 -4 -139 -9 -139 -4 0 -11 48 -15\n106 -4 58 -10 112 -13 120 -9 23 -32 16 -40 -13 -11 -39 -8 -553 7 -1348 14\n-772 14 -758 28 -763 7 -2 12 25 15 80 2 50 4 23 5 -69 2 -129 0 -153 -13\n-153 -31 0 -45 -54 -46 -182 -1 -105 -3 -116 -10 -73 -5 28 -7 95 -5 150 8\n169 -11 256 -49 225 -13 -11 -15 -90 -15 -650 0 -350 -3 -671 -6 -712 l-7 -75\n-706 -6 c-701 -7 -1110 -20 -1139 -36 -13 -7 -11 -11 9 -24 13 -8 42 -18 64\n-21 56 -9 651 -9 938 0 163 4 242 3 249 -4 7 -7 1 -12 -23 -16 -19 -3 -306 -8\n-639 -12 -333 -3 -656 -7 -717 -9 -62 -1 -113 1 -113 4 0 3 11 24 24 46 25 41\n21 65 -11 65 -19 0 -86 -35 -166 -87 -38 -25 -71 -43 -74 -40 -2 3 21 24 51\n47 101 76 61 110 -49 43 -101 -62 -442 -301 -590 -413 -77 -59 -160 -120 -185\n-135 -58 -37 -100 -75 -100 -91 0 -24 19 -25 59 -4 50 25 54 25 46 1 -7 -23\n40 -61 75 -61 l22 0 -23 -20 c-19 -15 -21 -22 -11 -28 17 -12 389 -22 796 -22\n297 0 1037 24 1094 36 12 2 22 11 22 19 0 13 -45 15 -327 16 -207 0 -321 4\n-308 10 25 11 787 39 798 29 5 -4 -3 -10 -16 -14 -14 -3 -28 -14 -31 -23 -16\n-41 5 -43 489 -43 l461 0 12 -187 c7 -104 17 -279 24 -390 9 -168 14 -203 26\n-203 13 0 14 40 9 315 -4 173 -3 315 1 315 9 0 26 -826 17 -840 -9 -14 -6\n-394 4 -433 5 -21 15 -44 23 -50 10 -9 12 -38 8 -127 -5 -99 -7 -109 -14 -72\n-9 46 -39 73 -59 53 -5 -5 -15 -88 -21 -183 l-11 -172 -35 -8 c-38 -9 -320\n-23 -722 -38 -142 -4 -262 -11 -266 -14 -5 -3 -114 -8 -243 -11 -129 -4 -283\n-11 -344 -16 -60 -6 -281 -12 -490 -15 -221 -3 -384 -9 -390 -15 -6 -6 -6 -16\n1 -27 9 -14 35 -19 143 -26 72 -4 131 -12 131 -17 0 -9 -566 -17 -755 -11\n-119 3 -139 -2 -131 -33 3 -13 1 -27 -5 -31 -44 -27 108 -425 208 -544 3 -3\n10 3 17 12 12 16 15 15 32 -17 18 -35 55 -48 69 -25 9 14 77 9 104 -9 43 -27\n738 -39 924 -15 28 4 47 11 47 19 0 22 -141 35 -395 35 -241 0 -319 11 -168\n24 42 3 207 6 368 6 279 0 293 -1 310 -20 10 -11 34 -24 54 -29 20 -5 223 -12\n451 -15 457 -6 462 -6 406 -15 -42 -6 -68 -33 -50 -51 12 -12 1546 -17 1638\n-5 43 6 57 5 52 -4 -4 -6 -15 -11 -24 -11 -28 -1 -58 -28 -45 -41 13 -13 29\n-14 163 -5 164 11 848 36 1250 46 212 6 407 15 433 21 55 12 58 32 6 37 -60 6\n-57 22 3 22 31 0 87 7 125 16 37 9 84 13 104 10 32 -5 44 0 102 44 36 28 69\n50 72 50 3 0 5 -11 5 -24 0 -55 50 -41 146 42 38 32 137 109 219 171 373 278\n483 369 471 389 -8 14 -72 -17 -146 -71 -25 -18 -76 -55 -115 -82 -38 -27\n-137 -100 -219 -162 -82 -62 -154 -113 -159 -113 -7 0 -7 3 -2 9 45 42 374\n289 510 381 82 56 199 146 238 183 25 23 28 33 23 61 -6 33 -6 33 56 55 35 12\n69 24 76 26 6 3 12 9 12 14 0 18 -44 22 -110 11 -36 -7 -105 -18 -155 -25 -49\n-8 -155 -23 -235 -35 -228 -34 -340 -48 -560 -69 -186 -18 -358 -35 -605 -61\n-49 -5 -100 -10 -112 -10 -14 0 -27 -10 -35 -27 -13 -26 -14 -26 -113 -24 -55\n0 -170 6 -255 11 -85 6 -177 12 -205 13 -47 2 -48 3 -15 9 19 4 127 11 239 17\n245 12 263 19 139 48 -52 13 -182 16 -720 20 -581 4 -657 7 -663 20 -3 9 -10\n73 -16 142 -9 113 -13 129 -34 151 -27 28 -34 70 -11 70 15 0 21 165 32 957 4\n238 9 432 13 431 29 -3 31 5 31 116 0 100 2 115 18 120 9 3 166 5 347 5 182\n-1 355 3 385 7 108 17 58 29 -150 36 -344 12 -316 26 55 27 266 1 335 4 355\n16 37 21 58 18 90 -13 l28 -29 399 -6 c219 -4 484 -7 590 -7 117 0 194 -4 198\n-10 3 -5 5 -11 3 -11 -2 -1 -224 -4 -495 -8 -375 -5 -495 -9 -505 -19 -52 -52\n178 -65 1161 -69 686 -3 780 -2 822 12 44 15 50 15 72 0 28 -18 77 -6 77 18 0\n15 -31 47 -46 47 -16 0 -464 224 -464 232 0 5 8 4 18 -2 9 -5 44 -21 77 -36\n33 -14 87 -39 120 -54 33 -16 83 -38 110 -50 28 -11 59 -25 70 -30 117 -53\n165 -64 165 -40 0 23 -96 81 -295 179 -519 256 -760 373 -907 440 -32 14 -67\n21 -109 21 -51 0 -60 2 -50 14 16 19 3 45 -28 54 -29 8 -581 10 -768 2 -89 -4\n-123 -8 -123 -17 0 -9 19 -13 58 -14 131 -3 110 -17 -28 -18 -117 -1 -261 -15\n-325 -31 -5 -1 -40 -2 -77 -1 -61 1 -66 3 -61 21 5 20 0 20 -242 20 l-246 0 6\n22 7 21 -190 -5 -189 -6 -14 32 c-19 42 -45 46 -54 7 -11 -53 -25 -34 -25 34\n0 64 12 86 24 43 4 -17 8 -20 17 -11 15 15 31 845 25 1293 -2 190 -4 455 -3\n590 1 135 2 507 2 827 0 619 -1 628 -45 588 -20 -18 -20 -18 -21 146 -2 228\n-4 268 -17 281 -17 17 -26 2 -32 -54 -7 -67 -26 -73 -41 -14 -13 51 -16 54\n-34 29 -11 -15 -14 -12 -19 27 -3 25 -7 60 -8 79 -4 48 -28 53 -28 6 0 -20 -5\n-79 -12 -130 -9 -77 -15 -98 -35 -116 l-23 -22 -1 89 c-2 125 -12 207 -26 212\n-9 2 -13 -7 -13 -29z m300 -2963 c-12 -12 -17 165 -11 394 6 241 16 182 19\n-109 1 -176 -1 -278 -8 -285z m1110 -1066 c11 -7 -1 -10 -40 -10 -39 0 -51 3\n-40 10 8 5 26 10 40 10 14 0 32 -5 40 -10z m1360 -56 c0 -9 -44 14 -48 25 -3\n8 5 7 22 -4 14 -10 26 -19 26 -21z m-1790 -2624 c14 -4 -83 -8 -220 -8 -135 0\n-254 3 -265 8 -26 11 451 11 485 0z m-2475 -240 c-8 -12 -511 -12 -540 0 -12\n5 96 9 263 9 186 1 281 -2 277 -9z m-1175 -51 c19 -10 -170 -12 -197 -2 -15 6\n-13 9 12 14 36 8 164 0 185 -12z m5386 -280 c-15 -12 -50 -41 -78 -65 -29 -24\n-57 -44 -62 -44 -14 0 46 57 109 102 52 38 78 44 31 7z m-2216 -211 c-31 -13\n-939 -25 -927 -12 5 5 160 11 345 14 585 10 610 10 582 -2z m-1372 -15 c-32\n-2 -84 -2 -115 0 -32 2 -6 3 57 3 63 0 89 -1 58 -3z m2689 -29 c-3 -3 -12 -4\n-19 -1 -8 3 -5 6 6 6 11 1 17 -2 13 -5z"/>\n</g>',
}
DICE_TYPES = [4, 6, 8, 10, 12, 20, 100]
SKILL_LEVELS = (0, 5, 10, 15)
TRAINING_LABELS = {0: 'Destreinado', 5: 'Treinado', 10: 'Veterano', 15: 'Mestre'}
ATTRIBUTE_LABELS = {
    'agilidade': 'Agilidade',
    'forca': 'Força',
    'intelecto': 'Intelecto',
    'presenca': 'Presença',
    'vigor': 'Vigor',
}

# Limites padrão usados para pré-preencher o formulário de pontos do jogador.
POINT_DEFAULTS = {
    'pv_max': 10,
    'pe_max': 2,
    'sanidade_max': 10,
}

CHARACTER_ORIGINS = [
    'Acadêmico', 'Agente de Saúde', 'Amnésico', 'Amigo dos Animais', 'Artista',
    'Astronauta', 'Atleta', 'Chef', 'Chef do Outro Lado', 'Colegial', 'Cosplayer',
    'Criminoso', 'Cultista Arrependido', 'Desgarrado', 'Diplomata', 'Engenheiro',
    'Executivo', 'Explorador', 'Experimento', 'Fanático por Criaturas', 'Fotógrafo',
    'Inventor Paranormal', 'Investigador', 'Jovem Místico', 'Legista do Turno da Noite',
    'Lutador', 'Magnata', 'Mateiro', 'Mercenário', 'Mergulhador', 'Militar', 'Motorista',
    'Nerd Entusiasta', 'Operário', 'Policial', 'Profetizado', 'Psicólogo', 'Religioso',
    'Repórter Investigativo', 'Servidor Público', 'Teórico da Conspiração', 'T.I.',
    'Trabalhador Rural', 'Trambiqueiro', 'Universitário', 'Vítima',
]

CHARACTER_CLASSES = ['Combatente', 'Especialista', 'Ocultista']

# NEX (Nível de Exposição Paranormal): sobe de 5 em 5, partindo de 5%,
# até o teto de 99% (o passo de 95% vai direto para 99%, nunca chega a 100%).
CHARACTER_NEX_OPTIONS = list(range(5, 100, 5)) + [99]

# Regra geral "Aumento de Atributo" (vale para qualquer classe): a cada um
# desses NEX, o agente ganha +1 em um atributo à sua escolha, sem poder
# passar de ATTRIBUTE_INCREASE_MAX por esse meio.
ATTRIBUTE_INCREASE_NEX_THRESHOLDS = [20, 50, 80, 95]
ATTRIBUTE_INCREASE_MAX = 5


def _attribute_increases_earned(nex):
    """Quantos aumentos de atributo o personagem já deveria ter ganhado,
    dado seu NEX atual (conta quantos limiares de ATTRIBUTE_INCREASE_NEX_THRESHOLDS
    já foram alcançados)."""
    nex = nex or 0
    return sum(1 for t in ATTRIBUTE_INCREASE_NEX_THRESHOLDS if nex >= t)

MURAL_CATEGORIES = ['Documento', 'Foto', 'Mapa', 'Prova', 'Ilustração', 'Outro']

ITEM_LEVELS = ['0', 'I', 'II', 'III']

ITEM_TYPES = ['Arma', 'Munição', 'Proteção', 'Acessórios de Carga', 'Paranormal', 'Consumível', 'Documento', 'Miscelânea']

# Tipo de item cujo campo "carga_bonus" soma espaços à capacidade de carga do
# personagem (ex.: mochila, cinto tático).
ITEM_TYPE_CARGA = 'Acessórios de Carga'

# Tipo de item que tem um elemento associado (campo "element"), escolhido entre
# RITUAL_ELEMENTS (Conhecimento, Energia, Medo, Morte, Sangue).
ITEM_TYPE_PARANORMAL = 'Paranormal'


def _parse_critical(crit_str):
    """Lê o campo 'crítico' de uma arma no formato usado por Ordem Paranormal:
       '19'      -> margem de ameaça 19-20, multiplicador padrão x2
       'x3'      -> margem de ameaça padrão (só 20), multiplicador x3
       '19/x3'   -> margem de ameaça 19-20, multiplicador x3
       Vazio/inválido -> margem 20, multiplicador x2 (padrão de d20).
    """
    threshold = 20
    multiplier = 2
    raw = (crit_str or '').strip().lower().replace(' ', '')
    if not raw:
        return threshold, multiplier

    for part in raw.split('/'):
        if not part:
            continue
        if part.startswith('x'):
            try:
                multiplier = int(part[1:])
            except ValueError:
                continue
        else:
            try:
                threshold = int(part)
            except ValueError:
                continue

    threshold = max(2, min(threshold, 20))
    multiplier = max(2, min(multiplier, 10))
    return threshold, multiplier


def _roll_dice_expression(expr):
    """Rola uma expressão de dados tipo '2d6+3', '1d8-1', '2d6+1d4+2' ou um
       valor fixo tipo '5'. Retorna (total, texto_detalhado) ou None se a
       expressão não tiver nenhum termo válido."""
    raw = (expr or '').strip().lower().replace(' ', '')
    if not raw:
        return None

    tokens = re.findall(r'[+-]?[^+-]+', raw)
    total = 0
    parts = []
    found_valid = False

    for tok in tokens:
        sign = -1 if tok.startswith('-') else 1
        term = tok.lstrip('+-')
        if not term:
            continue

        m = re.fullmatch(r'(\d*)d(\d+)', term)
        if m:
            count = int(m.group(1)) if m.group(1) else 1
            sides = int(m.group(2))
            count = max(1, min(count, 50))
            sides = max(2, min(sides, 1000))
            rolls = [random.randint(1, sides) for _ in range(count)]
            total += sum(rolls) * sign
            found_valid = True
            parts.append(f'{"+" if sign > 0 else "-"}{count}d{sides}[{",".join(str(r) for r in rolls)}]')
        elif term.isdigit():
            total += int(term) * sign
            found_valid = True
            parts.append(f'{"+" if sign > 0 else "-"}{term}')

    if not found_valid:
        return None

    detail = ' '.join(parts)
    if detail.startswith('+'):
        detail = detail[1:]
    return total, detail


def _roll_attribute_dice(attr_value):
    """Regra de rolagem por atributo: nº de d20 = valor do atributo (se 0 ou
    menor, rola 2d20 e usa o pior resultado; caso contrário, rola
    (atributo)d20 e usa o melhor). Retorna (rolls, chosen, dice_desc,
    natural_20) — natural_20 indica se o d20 efetivamente usado (chosen)
    veio de uma face 20, antes de qualquer bônus de treinamento."""
    if attr_value <= 0:
        rolls = [random.randint(1, 20) for _ in range(2)]
        chosen = min(rolls)
        dice_desc = '2d20 (pior)'
    else:
        rolls = [random.randint(1, 20) for _ in range(attr_value)]
        chosen = max(rolls)
        dice_desc = f'{attr_value}d20 (melhor)'
    natural_20 = chosen == 20
    return rolls, chosen, dice_desc, natural_20


def get_display_name(user_row):
    """Nome a ser exibido publicamente para um usuário. Para o admin, usa o
    nome escolhido em character_name (se definido) em vez do login 'admin'.
    Para os demais jogadores, mantém o username normalmente."""
    if not user_row:
        return ''
    if user_row['is_admin'] and user_row['character_name']:
        return user_row['character_name']
    return user_row['username']


app.jinja_env.globals['display_name'] = get_display_name
app.jinja_env.globals['element_colors'] = RITUAL_ELEMENT_COLORS
app.jinja_env.globals['element_icons'] = RITUAL_ELEMENT_ICONS
app.jinja_env.globals['element_affinity_nex_threshold'] = ELEMENT_AFFINITY_NEX_THRESHOLD
app.jinja_env.globals['attribute_increase_thresholds'] = ATTRIBUTE_INCREASE_NEX_THRESHOLDS
app.jinja_env.globals['attribute_increase_max'] = ATTRIBUTE_INCREASE_MAX
app.jinja_env.globals['attribute_increases_earned'] = _attribute_increases_earned
app.jinja_env.globals['attribute_labels_map'] = ATTRIBUTE_LABELS


def _stat_pct(value, max_value):
    """Calcula a porcentagem (0-100) de um valor atual sobre o máximo,
    protegendo contra divisão por zero e valores fora da faixa."""
    try:
        value = float(value)
        max_value = float(max_value)
    except (TypeError, ValueError):
        return 0
    if max_value <= 0:
        return 0
    pct = round((value / max_value) * 100)
    return max(0, min(100, pct))


def _defesa_valor(user_row, overloaded=False):
    """Defesa é um valor fixo (não um pool de pontos que se gasta): 10 + Agilidade
    + bônus de equipamento + outros bônus. É esse número que o ataque do
    oponente precisa igualar ou superar para acertar — a Defesa não diminui
    quando o personagem é atingido. A única redução aplicada é a penalidade de
    -5 por sobrecarga de inventário, enquanto ela durar."""
    penalty = 5 if overloaded else 0
    total = 10 + (user_row['agilidade'] or 0) + (user_row['defesa_equip_bonus'] or 0) + (user_row['defesa_outros_bonus'] or 0) - penalty
    return max(0, total)


def _capacidade_max(user_row, carga_bonus=0):
    """Capacidade de carga máxima = 5 espaços por ponto de Força, mais o bônus
    dado pelos itens do tipo "Acessórios de Carga" (parâmetro `carga_bonus`).
    Com Força 0 (ou negativa), o personagem ainda consegue carregar 2 espaços."""
    forca = user_row['forca'] or 0
    base = 2 if forca <= 0 else forca * 5
    return base + max(0, carga_bonus or 0)


def _carga_bonus_from_items(items):
    """Soma o bônus de carga (carga_bonus × quantidade) dos itens do tipo
    "Acessórios de Carga" de uma lista de itens já carregada."""
    total = 0
    for i in items:
        if i['item_type'] == ITEM_TYPE_CARGA:
            total += (i['carga_bonus'] or 0) * (i['quantity'] or 0)
    return total


def _carga_bonus(db, user_id):
    """Bônus de carga total de um usuário (uma consulta)."""
    row = db.execute(
        'SELECT COALESCE(SUM(carga_bonus * quantity), 0) AS total FROM items WHERE user_id = ? AND item_type = ?',
        (user_id, ITEM_TYPE_CARGA)
    ).fetchone()
    return row['total'] if row else 0


def _carga_bonus_map(db):
    """Bônus de carga de TODOS os usuários numa única consulta agrupada —
    mesmo motivo de _used_spaces_map (evita uma consulta por agente nas
    rotas consultadas por polling)."""
    rows = db.execute(
        'SELECT user_id, COALESCE(SUM(carga_bonus * quantity), 0) AS total FROM items WHERE item_type = ? GROUP BY user_id',
        (ITEM_TYPE_CARGA,)
    ).fetchall()
    return {r['user_id']: r['total'] for r in rows}


def _used_spaces(db, user_id):
    """Soma dos espaços ocupados pelo inventário de um usuário."""
    row = db.execute(
        'SELECT COALESCE(SUM(quantity * spaces), 0) AS total FROM items WHERE user_id = ?',
        (user_id,)
    ).fetchone()
    return row['total'] if row else 0


def _used_spaces_map(db):
    """Soma dos espaços ocupados por TODOS os usuários de uma vez só (uma
    única consulta agrupada), em vez de rodar uma consulta por usuário
    dentro de um loop. Usado nas rotas de /agentes, que são consultadas a
    cada poucos segundos por todo mundo conectado (polling) — sem isso,
    cada atualização multiplicava 1 consulta extra por agente cadastrado,
    deixando o site cada vez mais lento conforme a lista de agentes/itens
    crescia."""
    rows = db.execute(
        'SELECT user_id, COALESCE(SUM(quantity * spaces), 0) AS total FROM items GROUP BY user_id'
    ).fetchall()
    return {r['user_id']: r['total'] for r in rows}


def _is_overloaded(db, user_row):
    """True se o personagem está carregando mais espaços do que sua capacidade."""
    return _used_spaces(db, user_row['id']) > _capacidade_max(user_row, _carga_bonus(db, user_row['id']))


def _log_money_transaction(db, user_id, amount, description):
    """Registra uma transação no extrato bancário do agente.
    amount positivo = dinheiro recebido, amount negativo = dinheiro gasto."""
    if not amount:
        return
    db.execute(
        'INSERT INTO money_transactions (user_id, amount, description) VALUES (?, ?, ?)',
        (user_id, amount, (description or '').strip()[:300])
    )


def _log_action(db, user_row, action):
    """Registra uma ação de jogador no log visível apenas para o mestre
    (aba 'Log'). Ações do próprio mestre não são registradas — o log é
    só para acompanhar o que os jogadores fizeram."""
    if not user_row or user_row['is_admin']:
        return
    db.execute(
        'INSERT INTO action_logs (user_id, username, character_name, action) VALUES (?, ?, ?, ?)',
        (user_row['id'], user_row['username'], user_row['character_name'] or '', (action or '').strip()[:500])
    )


def _wants_json():
    """True quando a requisição veio de uma chamada fetch() do front-end
    (formulários da aba Personagem que atualizam a página sem recarregar),
    em vez de um envio de formulário tradicional. Mantém compatibilidade:
    quem enviar o formulário sem JavaScript continua recebendo o redirect
    normal para o painel."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'

SKILL_DEFS = [
    {'key': 'acrobacia', 'label': 'Acrobacia+', 'attr': 'agilidade', 'load_penalty': True},
    {'key': 'adestramento', 'label': 'Adestramento*', 'attr': 'presenca'},
    {'key': 'artes', 'label': 'Artes*', 'attr': 'presenca'},
    {'key': 'atletismo', 'label': 'Atletismo', 'attr': 'forca'},
    {'key': 'atualidades', 'label': 'Atualidades', 'attr': 'intelecto'},
    {'key': 'ciencias', 'label': 'Ciências*', 'attr': 'intelecto'},
    {'key': 'crime', 'label': 'Crime*+', 'attr': 'agilidade', 'load_penalty': True},
    {'key': 'diplomacia', 'label': 'Diplomacia', 'attr': 'presenca'},
    {'key': 'enganacao', 'label': 'Enganação', 'attr': 'presenca'},
    {'key': 'fortitude', 'label': 'Fortitude', 'attr': 'vigor'},
    {'key': 'furtividade', 'label': 'Furtividade+', 'attr': 'agilidade', 'load_penalty': True},
    {'key': 'iniciativa', 'label': 'Iniciativa', 'attr': 'agilidade'},
    {'key': 'intimidacao', 'label': 'Intimidação', 'attr': 'presenca'},
    {'key': 'intuicao', 'label': 'Intuição', 'attr': 'presenca'},
    {'key': 'investigacao', 'label': 'Investigação', 'attr': 'intelecto'},
    {'key': 'luta', 'label': 'Luta', 'attr': 'forca'},
    {'key': 'medicina', 'label': 'Medicina', 'attr': 'intelecto'},
    {'key': 'ocultismo', 'label': 'Ocultismo*', 'attr': 'intelecto'},
    {'key': 'percepcao', 'label': 'Percepção', 'attr': 'presenca'},
    {'key': 'pilotagem', 'label': 'Pilotagem*', 'attr': 'agilidade'},
    {'key': 'pontaria', 'label': 'Pontaria', 'attr': 'agilidade'},
    {'key': 'profissao_1', 'label': 'Profissão*', 'is_text': True, 'attr': 'intelecto'},
    {'key': 'profissao_2', 'label': 'Profissão*', 'is_text': True, 'attr': 'intelecto'},
    {'key': 'reflexos', 'label': 'Reflexos', 'attr': 'agilidade'},
    {'key': 'religiao', 'label': 'Religião*', 'attr': 'presenca'},
    {'key': 'sobrevivencia', 'label': 'Sobrevivência', 'attr': 'intelecto'},
    {'key': 'tatica', 'label': 'Tática*', 'attr': 'intelecto'},
    {'key': 'tecnologia', 'label': 'Tecnologia*', 'attr': 'intelecto'},
    {'key': 'vontade', 'label': 'Vontade', 'attr': 'presenca'},
]
SKILL_KEYS = {s['key'] for s in SKILL_DEFS}
SKILL_ATTR_BY_KEY = {s['key']: s['attr'] for s in SKILL_DEFS}
SKILL_LABEL_BY_KEY = {s['key']: s['label'].rstrip('+*') for s in SKILL_DEFS}
SKILL_LOAD_PENALTY_KEYS = {s['key'] for s in SKILL_DEFS if s.get('load_penalty')}


def _roll_iniciativa_for_user(db, user):
    """Rola a iniciativa de um agente para o combate, usando a mesma regra
    da perícia Iniciativa (Agilidade + treinamento, sem penalidade de
    carga). Publica o resultado no Chat, igual a qualquer outra rolagem de
    perícia, e devolve o total."""
    skill_row = db.execute(
        'SELECT * FROM skills WHERE user_id = ? AND skill_key = ?',
        (user['id'], 'iniciativa')
    ).fetchone()

    attr_value = user['agilidade']
    training = skill_row['level'] if skill_row else 0
    if training not in SKILL_LEVELS:
        training = 0

    rolls, chosen, dice_desc, natural_20 = _roll_attribute_dice(attr_value)
    total = chosen + training
    rolls_str = ', '.join(str(r) for r in rolls)
    training_label = TRAINING_LABELS[training]
    natural_20_str = ' — 20 NATURAL!' if natural_20 else ''

    content = (
        f'rolou Iniciativa (combate) [Agilidade {attr_value}]: '
        f'{dice_desc} [{rolls_str}] = {chosen} + {training} ({training_label}) = {total}{natural_20_str}'
    )

    db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color)
           VALUES (?, ?, ?, ?, 'roll', ?, ?)''',
        (user['id'], get_display_name(user), '' if user['is_admin'] else user['character_name'], user['is_admin'], content, user['color'])
    )

    return total


def _ensure_creature_columns(db):
    """Garante que as colunas usadas pelas criaturas (dados de iniciativa e VD)
    existem no banco. Normalmente o init_db() já as cria ao iniciar o servidor;
    isto cobre o caso de o código novo estar rodando sobre um banco que ainda
    não foi migrado (ex: servidor não reiniciado), em vez de dar erro 500."""
    cols = {row[1] for row in db.execute('PRAGMA table_info(combat_participants)').fetchall()}
    if 'initiative_dice' not in cols:
        db.execute("ALTER TABLE combat_participants ADD COLUMN initiative_dice TEXT NOT NULL DEFAULT ''")
    if 'challenge' not in cols:
        db.execute('ALTER TABLE combat_participants ADD COLUMN challenge INTEGER')
    if 'initiative_hidden' not in cols:
        db.execute('ALTER TABLE combat_participants ADD COLUMN initiative_hidden INTEGER NOT NULL DEFAULT 0')
    db.commit()


def _parse_challenge(value):
    """VD (Valor de Desafio) vindo do formulário: inteiro de 0 a 99999, ou
    None se vazio/inválido (criatura sem VD)."""
    try:
        n = int(str(value or '').strip())
    except (TypeError, ValueError):
        return None
    return max(0, min(n, 99999))


INITIATIVE_DICE_RE = re.compile(r'(\d*)d(\d+)(?:([+-])(\d+))?', re.IGNORECASE)


def _parse_initiative_dice(text):
    """Lê a iniciativa de uma ameaça, escrita como '6d20+35', 'd20+5', '3d20'
    ou só '5' (= 1d20+5). Retorna (quantidade, lados, bônus) ou None se vazio
    ou inválido. Mesmos limites do chat: 1 a 50 dados, 2 a 1000 lados."""
    raw = re.sub(r'\s+', '', text or '').lower()
    if not raw:
        return None
    m = INITIATIVE_DICE_RE.fullmatch(raw)
    if m:
        count = max(1, min(int(m.group(1) or 1), 50))
        sides = max(2, min(int(m.group(2)), 1000))
        bonus = int(m.group(4) or 0) * (-1 if m.group(3) == '-' else 1)
    elif re.fullmatch(r'[+-]?\d+', raw):
        count, sides, bonus = 1, 20, int(raw)
    else:
        return None
    return count, sides, max(-9999, min(bonus, 9999))


def _normalize_initiative_dice(text):
    """Forma padronizada para guardar no banco ('6d20+35'), ou '' se inválida."""
    parsed = _parse_initiative_dice(text)
    if not parsed:
        return ''
    count, sides, bonus = parsed
    out = f'{count}d{sides}'
    if bonus:
        out += f'{"+" if bonus > 0 else "-"}{abs(bonus)}'
    return out


def _initiative_is_hidden(participant):
    """True se a iniciativa deste inimigo (criatura/NPC) está oculta dos
    jogadores. Agentes nunca têm a iniciativa oculta."""
    if participant['kind'] == 'agente':
        return False
    try:
        return bool(participant['initiative_hidden'])
    except (IndexError, KeyError):
        return False


def _roll_iniciativa_for_criatura(db, admin_user, participant):
    """Rola a iniciativa de uma criatura/ameaça do combate usando os dados
    cadastrados pelo mestre (ex: '6d20+35': rola 6d20, usa o melhor e soma
    35 — mesma regra de rolagem por atributo). Sem dados cadastrados, mantém
    o comportamento antigo: 1d20 + bônus. Publica no Chat como uma ação do
    mestre e devolve o total."""
    parsed = _parse_initiative_dice(participant['initiative_dice'])
    if parsed:
        count, sides, bonus = parsed
    else:
        count, sides, bonus = 1, 20, (participant['initiative_bonus'] or 0)

    rolls = [random.randint(1, sides) for _ in range(count)]
    best = max(rolls)
    total = best + bonus
    natural_20_str = ' — 20 NATURAL!' if sides == 20 and best == 20 else ''

    desc = f'{count}d{sides}' + (' (melhor)' if count > 1 else '')
    content = (
        f"rolou Iniciativa (combate) de {participant['name']}: "
        f'{desc} [{", ".join(str(r) for r in rolls)}]'
    )
    if count > 1 and bonus:
        content += f' = {best}'
    if bonus:
        content += f' {"+" if bonus > 0 else "-"} {abs(bonus)}'
    content += f' = {total}{natural_20_str}'

    # Iniciativa oculta: o Chat é público, então a rolagem não é publicada
    # (senão o resultado vazaria para os jogadores).
    if not _initiative_is_hidden(participant):
        db.execute(
            '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color)
               VALUES (?, ?, ?, 1, 'roll', ?, ?)''',
            (admin_user['id'], get_display_name(admin_user), '', content, admin_user['color'])
        )

    return total


init_db()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


_SENTENCE_BOUNDARY_RE = re.compile(r'([.!?]\s+)([a-zà-ú])')


def capitalize_sentences(text):
    """Deixa maiúscula a primeira letra do texto e a primeira letra de cada
    frase seguinte (após ., ! ou ?). Usado para padronizar os textos
    digitados ao cadastrar um ritual (nome, execução, alcance, etc.)."""
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return _SENTENCE_BOUNDARY_RE.sub(lambda m: m.group(1) + m.group(2).upper(), text)


# Dimensão máxima (largura, altura) usada ao redimensionar cada tipo de
# imagem enviada. A imagem é sempre encolhida para caber dentro desses
# limites mantendo a proporção original (nunca é esticada/ampliada caso já
# seja menor). Isso substitui o antigo comportamento de salvar o arquivo do
# jeito que o usuário mandou, o que podia deixar uploads gigantes e pesados.
AVATAR_MAX_SIZE = (768, 768)
ITEM_IMAGE_MAX_SIZE = (1000, 1000)
RITUAL_SYMBOL_MAX_SIZE = (640, 640)
CAMPAIGN_BANNER_MAX_SIZE = (1920, 1080)
MURAL_IMAGE_MAX_SIZE = (1920, 1920)
NPC_AVATAR_MAX_SIZE = (768, 768)
COMBAT_AVATAR_MAX_SIZE = (768, 768)


def save_uploaded_image(file, directory, max_size=None):
    """Salva um arquivo de imagem enviado na pasta indicada, redimensionando-o
    com Pillow (mantendo a proporção e sem ampliar imagens já pequenas)
    quando max_size é informado, e retorna o nome gerado, ou None.

    GIFs não são redimensionados aqui: o Pillow só processa o primeiro
    quadro ao salvar, o que destruiria a animação de um GIF animado.
    """
    if not (file and file.filename and allowed_file(file.filename)):
        return None

    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f'{uuid.uuid4().hex}.{ext}'
    path = os.path.join(directory, filename)

    if max_size and ext != 'gif':
        try:
            img = Image.open(file.stream)
            # Corrige a rotação de fotos tiradas com celular (metadado EXIF).
            img = ImageOps.exif_transpose(img)
            img.thumbnail(max_size, Image.LANCZOS)

            save_kwargs = {}
            if ext in ('jpg', 'jpeg'):
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                save_kwargs = {'quality': 95, 'optimize': True, 'subsampling': 0}
            elif ext == 'png':
                save_kwargs = {'optimize': True}
            elif ext == 'webp':
                save_kwargs = {'quality': 95, 'method': 6}

            img.save(path, **save_kwargs)
        except Exception:
            # Se a imagem vier corrompida ou em formato que o Pillow não
            # entenda direito, cai de volta para salvar o arquivo original
            # em vez de travar o upload do usuário.
            file.stream.seek(0)
            file.save(path)
    else:
        file.save(path)

    return filename


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        exists = db.execute('SELECT 1 FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not exists:
            # Sessão antiga apontando para uma conta que não existe mais
            # (ex.: usuário foi excluído ou o banco de dados foi trocado).
            session.clear()
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT is_admin FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not user:
            session.clear()
            return redirect(url_for('login'))
        if not user['is_admin']:
            return redirect(url_for('player_panel'))
        return view(*args, **kwargs)
    return wrapped


_ROLL_TOTAL_RE = re.compile(r'=\s*(\d+)(\s*—\s*20 NATURAL!)?\s*$')
_ROLL_NATURAL_RE = re.compile(r'—\s*20 NATURAL!')
_ROLL_CRITICO_RE = re.compile(r'CRÍTICO\s*x\d+!')


def format_roll_content(content):
    """Realça o resultado de uma mensagem de rolagem no Chat: o total final
    (depois do último "=") fica em destaque, maior e com fundo, e "CRÍTICO"
    / "20 NATURAL!" ganham uma cor de alerta — para o resultado saltar aos
    olhos em vez de se misturar ao resto da frase.

    O texto já é sempre montado pelo próprio servidor (nunca digitado
    livremente pelo jogador), mas alguns pedaços — nome de item, nome
    customizado de perícia — vêm de campos preenchidos pelo usuário. Por
    isso escapamos tudo antes de inserir as tags de destaque, em vez de
    simplesmente confiar no texto."""
    escaped = Markup.escape(content)

    def total_sub(m):
        total = m.group(1)
        nat = m.group(2) or ''
        return f'= <span class="chat-roll-total">{total}</span>{nat}'

    new_content = _ROLL_TOTAL_RE.sub(total_sub, escaped)
    new_content = _ROLL_NATURAL_RE.sub(
        lambda m: f'<span class="chat-roll-highlight">{m.group(0)}</span>', new_content
    )
    new_content = _ROLL_CRITICO_RE.sub(
        lambda m: f'<span class="chat-roll-highlight">{m.group(0)}</span>', new_content
    )
    return Markup(new_content)


app.jinja_env.filters['roll_format'] = format_roll_content


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_panel') if session.get('is_admin') else url_for('player_panel'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            return redirect(url_for('admin_panel') if session['is_admin'] else url_for('player_panel'))

        return render_template('login.html', error='Usuário ou senha inválidos.')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/admin')
@admin_required
def admin_panel():
    db = get_db()
    users = db.execute('SELECT * FROM users WHERE is_admin = 0 ORDER BY username ASC').fetchall()
    current_user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    # Antes, itens/rituais/habilidades eram buscados com 3 queries POR
    # jogador (um SELECT ... WHERE user_id = ? por usuário, dentro do loop).
    # Com N jogadores isso é 3*N idas ao banco. Aqui buscamos as 3 tabelas
    # inteiras de uma vez (3 queries no total) e agrupamos em memória por
    # user_id, o que escala muito melhor conforme o número de jogadores cresce.
    user_ids = [u['id'] for u in users]
    items_by_user = {uid: [] for uid in user_ids}
    rituals_by_user = {uid: [] for uid in user_ids}
    abilities_by_user = {uid: [] for uid in user_ids}

    if user_ids:
        placeholders = ','.join('?' * len(user_ids))
        for row in db.execute(
            f'SELECT * FROM items WHERE user_id IN ({placeholders}) ORDER BY sort_order ASC, id ASC', user_ids
        ).fetchall():
            items_by_user[row['user_id']].append(row)
        for row in db.execute(
            f'SELECT * FROM rituals WHERE user_id IN ({placeholders}) ORDER BY sort_order ASC, id ASC', user_ids
        ).fetchall():
            rituals_by_user[row['user_id']].append(row)
        for row in db.execute(
            f'SELECT * FROM class_abilities WHERE user_id IN ({placeholders}) ORDER BY sort_order ASC, id ASC', user_ids
        ).fetchall():
            abilities_by_user[row['user_id']].append(row)

    used_spaces_by_user = {}
    capacidade_max_by_user = {}
    point_pcts_by_user = {}
    defesa_by_user = {}
    for u in users:
        item_rows = items_by_user[u['id']]
        used_spaces_by_user[u['id']] = sum(r['quantity'] * r['spaces'] for r in item_rows)
        capacidade_max_by_user[u['id']] = _capacidade_max(u, _carga_bonus_from_items(item_rows))
        overloaded = used_spaces_by_user[u['id']] > capacidade_max_by_user[u['id']]

        point_pcts_by_user[u['id']] = {
            'pv': _stat_pct(u['pv'], u['pv_max']),
            'pe': _stat_pct(u['pe'], u['pe_max']),
            'sanidade': _stat_pct(u['sanidade'], u['sanidade_max']),
        }
        defesa_by_user[u['id']] = _defesa_valor(u, overloaded)

    money_requests = db.execute(
        '''SELECT mr.*, u.username AS req_username, u.character_name AS req_character_name,
                  u.avatar AS req_avatar
           FROM money_requests mr
           JOIN users u ON u.id = mr.user_id
           WHERE mr.status = 'pending'
           ORDER BY mr.id ASC'''
    ).fetchall()

    npcs = db.execute('SELECT * FROM npcs ORDER BY sort_order ASC, id ASC').fetchall()

    return render_template(
        'admin.html',
        users=users,
        current_user=current_user,
        items_by_user=items_by_user,
        used_spaces_by_user=used_spaces_by_user,
        capacidade_max_by_user=capacidade_max_by_user,
        rituals_by_user=rituals_by_user,
        abilities_by_user=abilities_by_user,
        point_pcts_by_user=point_pcts_by_user,
        defesa_by_user=defesa_by_user,
        money_requests=money_requests,
        npcs=npcs,
        npc_states=NPC_STATES,
        npc_state_colors=NPC_STATE_COLORS,
        ritual_elements=RITUAL_ELEMENTS,
        ritual_element_icons=RITUAL_ELEMENT_ICONS,
        ritual_circle_costs=RITUAL_CIRCLE_PE_COSTS,
        character_origins=CHARACTER_ORIGINS,
        character_classes=CHARACTER_CLASSES,
        character_nex_options=CHARACTER_NEX_OPTIONS,
        item_levels=ITEM_LEVELS,
        item_types=ITEM_TYPES
    )


@app.route('/admin/log')
@admin_required
def admin_log():
    db = get_db()
    logs = db.execute(
        'SELECT * FROM action_logs ORDER BY id DESC LIMIT 500'
    ).fetchall()
    return render_template('log.html', logs=logs)


@app.route('/admin/api/log/clear', methods=['POST'])
@admin_required
def api_log_clear():
    db = get_db()
    db.execute('DELETE FROM action_logs')
    db.commit()
    return jsonify({'ok': True})


@app.route('/admin/create_user', methods=['POST'])
@admin_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    if username and password:
        db = get_db()
        try:
            db.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, generate_password_hash(password))
            )
            db.commit()
        except sqlite3.IntegrityError:
            pass

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    if user_id == session['user_id']:
        return redirect(url_for('admin_panel'))
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ? AND is_admin = 0', (user_id,))
    db.commit()
    return redirect(url_for('admin_panel'))


@app.route('/admin/add_npc', methods=['POST'])
@admin_required
def add_npc():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    state = request.form.get('state', '').strip()
    if state not in NPC_STATE_COLORS:
        state = ''
    color = request.form.get('color', '').strip()
    if not HEX_COLOR_RE.match(color):
        color = '#c62f27'

    if name:
        filename = save_uploaded_image(request.files.get('avatar'), NPC_IMAGES_DIR, max_size=NPC_AVATAR_MAX_SIZE)
        db = get_db()
        next_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM npcs').fetchone()[0]
        db.execute(
            'INSERT INTO npcs (name, avatar, description, color, state, sort_order) VALUES (?, ?, ?, ?, ?, ?)',
            (name, filename, description, color, state, next_order)
        )
        db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_npc/<int:npc_id>', methods=['POST'])
@admin_required
def delete_npc(npc_id):
    db = get_db()
    npc = db.execute('SELECT avatar FROM npcs WHERE id = ?', (npc_id,)).fetchone()
    # Se o NPC estava no combate, sai dele junto (passando a vez, se for o caso).
    in_combat = db.execute(
        "SELECT id FROM combat_participants WHERE kind = 'npc' AND npc_id = ?", (npc_id,)
    ).fetchone()
    if in_combat:
        state = _get_turn_state(db)
        if state['active'] and state['current_id'] == in_combat['id']:
            _turn_next(db)
        db.execute('DELETE FROM combat_participants WHERE id = ?', (in_combat['id'],))
        if not _turn_order_ids(db):
            _turn_stop(db)
    db.execute('DELETE FROM npcs WHERE id = ?', (npc_id,))
    db.commit()

    if npc and npc['avatar']:
        path = os.path.join(NPC_IMAGES_DIR, npc['avatar'])
        if os.path.exists(path):
            os.remove(path)

    return redirect(url_for('admin_panel'))


@app.route('/admin/set_npc_color/<int:npc_id>', methods=['POST'])
@admin_required
def set_npc_color(npc_id):
    color = request.form.get('color', '').strip()

    if HEX_COLOR_RE.match(color):
        db = get_db()
        db.execute('UPDATE npcs SET color = ? WHERE id = ?', (color, npc_id))
        db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/admin/set_npc_state/<int:npc_id>', methods=['POST'])
@admin_required
def set_npc_state(npc_id):
    state = request.form.get('state', '').strip()
    if state not in NPC_STATE_COLORS:
        state = ''

    db = get_db()
    db.execute('UPDATE npcs SET state = ? WHERE id = ?', (state, npc_id))
    db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/admin/api/reorder_npcs', methods=['POST'])
@admin_required
def api_reorder_npcs():
    """Salva a nova ordem da Equipe de Suporte (arrastar e organizar no
    painel do admin). Recebe {"order": [id1, id2, ...]} com os ids na ordem
    desejada e grava essa posição em sort_order — essa é a mesma ordem usada
    para listar os NPCs na aba Agentes."""
    data = request.get_json(silent=True) or {}
    order = data.get('order')

    if not isinstance(order, list) or not order:
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    try:
        npc_ids = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    db = get_db()
    existing_ids = {row['id'] for row in db.execute('SELECT id FROM npcs').fetchall()}

    for position, npc_id in enumerate(npc_ids):
        if npc_id in existing_ids:
            db.execute('UPDATE npcs SET sort_order = ? WHERE id = ?', (position, npc_id))
    db.commit()

    return jsonify({'ok': True})


@app.route('/admin/update_my_avatar', methods=['POST'])
@admin_required
def admin_update_my_avatar():
    filename = save_uploaded_image(request.files.get('avatar'), AVATAR_IMAGES_DIR, max_size=AVATAR_MAX_SIZE)

    if filename:
        db = get_db()
        old = db.execute('SELECT avatar FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        db.execute('UPDATE users SET avatar = ? WHERE id = ?', (filename, session['user_id']))
        db.commit()

        if old and old['avatar']:
            old_path = os.path.join(AVATAR_IMAGES_DIR, old['avatar'])
            if os.path.exists(old_path):
                os.remove(old_path)

        if _wants_json():
            return jsonify({'ok': True, 'avatar_url': url_for('static', filename='avatar_images/' + filename)})
    elif _wants_json():
        return jsonify({'ok': False, 'error': 'Envie uma imagem válida.'}), 400

    return redirect(url_for('admin_panel'))


@app.route('/admin/set_display_name', methods=['POST'])
@admin_required
def admin_set_display_name():
    name = request.form.get('display_name', '').strip()[:100]

    db = get_db()
    db.execute('UPDATE users SET character_name = ? WHERE id = ?', (name, session['user_id']))
    db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/admin/api/update_user/<int:user_id>', methods=['POST'])
@admin_required
def api_update_user(user_id):
    data = request.get_json(force=True) or {}

    fields = [
        'agilidade', 'forca', 'intelecto', 'presenca', 'vigor',
        'pv', 'pv_max', 'pe', 'pe_max', 'sanidade', 'sanidade_max',
        'character_money'
    ]
    # Capacidade de carga não é mais um campo editável: agora é sempre
    # calculada automaticamente a partir da Força (veja _capacidade_max).
    lock_fields = ['attributes_locked', 'points_locked']

    updates = {}
    for f in fields:
        if f in data:
            try:
                updates[f] = int(data[f])
            except (ValueError, TypeError):
                return jsonify({'ok': False, 'error': f'Valor inválido para {f}'}), 400
            if f == 'character_money':
                updates[f] = max(0, updates[f])

    for f in lock_fields:
        if f in data:
            updates[f] = 1 if data[f] else 0

    if not updates:
        return jsonify({'ok': False, 'error': 'Nada para atualizar'}), 400

    db = get_db()

    if 'character_money' in updates:
        prev_money = db.execute(
            'SELECT character_money FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        prev_money = prev_money['character_money'] if prev_money else 0
        diff = updates['character_money'] - prev_money
        if diff != 0:
            _log_money_transaction(db, user_id, diff, 'Ajuste do mestre')

    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE users SET {set_clause} WHERE id = ?', (*updates.values(), user_id))
    db.commit()

    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

    if user is None:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    user_data = dict(user)
    user_data.pop('password_hash', None)
    return jsonify({'ok': True, 'user': user_data})


@app.route('/admin/api/money_requests/<int:request_id>/approve', methods=['POST'])
@admin_required
def api_approve_money_request(request_id):
    db = get_db()
    req_row = db.execute('SELECT * FROM money_requests WHERE id = ?', (request_id,)).fetchone()

    if not req_row or req_row['status'] != 'pending':
        return jsonify({'ok': False, 'error': 'Pedido não encontrado ou já resolvido.'}), 404

    db.execute(
        'UPDATE users SET character_money = character_money + ? WHERE id = ?',
        (req_row['amount'], req_row['user_id'])
    )
    db.execute(
        "UPDATE money_requests SET status = 'approved', resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (request_id,)
    )
    _log_money_transaction(db, req_row['user_id'], req_row['amount'], req_row['reason'] or 'Solicitação aprovada pelo mestre')
    db.commit()
    new_money = db.execute(
        'SELECT character_money FROM users WHERE id = ?', (req_row['user_id'],)
    ).fetchone()['character_money']

    return jsonify({'ok': True, 'id': request_id, 'user_id': req_row['user_id'], 'money': new_money})


@app.route('/admin/api/money_requests/<int:request_id>/reject', methods=['POST'])
@admin_required
def api_reject_money_request(request_id):
    db = get_db()
    req_row = db.execute('SELECT * FROM money_requests WHERE id = ?', (request_id,)).fetchone()

    if not req_row or req_row['status'] != 'pending':
        return jsonify({'ok': False, 'error': 'Pedido não encontrado ou já resolvido.'}), 404

    db.execute(
        "UPDATE money_requests SET status = 'rejected', resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (request_id,)
    )
    db.commit()

    return jsonify({'ok': True, 'id': request_id, 'user_id': req_row['user_id']})


def _create_item(user_id, form, files):
    name = form.get('name', '').strip()
    if name:
        name = name[0].upper() + name[1:]
    quantity = form.get('quantity', '1')
    spaces = form.get('spaces', '1')
    description = form.get('description', '').strip()
    level = form.get('level', '0').strip()
    item_type = form.get('item_type', '').strip()
    damage = form.get('damage', '').strip()
    critical = form.get('critical', '').strip()
    defense = form.get('defense', '0').strip()
    carga_bonus = form.get('carga_bonus', '0').strip()
    element = form.get('element', '').strip()

    try:
        quantity = max(1, int(quantity))
    except ValueError:
        quantity = 1
    try:
        spaces = max(0, int(spaces))
    except ValueError:
        spaces = 1
    try:
        defense = max(0, int(defense))
    except ValueError:
        defense = 0
    try:
        carga_bonus = max(0, int(carga_bonus))
    except ValueError:
        carga_bonus = 0

    if not name:
        return

    if level not in ITEM_LEVELS:
        level = '0'
    if item_type not in ITEM_TYPES:
        item_type = ''

    # Dano e crítico só fazem sentido para itens do tipo Arma.
    if item_type != 'Arma':
        damage = ''
        critical = ''
    # Defesa só faz sentido para itens do tipo Proteção.
    if item_type != 'Proteção':
        defense = 0
    # Bônus de carga só faz sentido para itens do tipo Acessórios de Carga.
    if item_type != ITEM_TYPE_CARGA:
        carga_bonus = 0
    # Elemento só faz sentido para itens do tipo Paranormal.
    if item_type != ITEM_TYPE_PARANORMAL or element not in RITUAL_ELEMENTS:
        element = ''

    image_filename = save_uploaded_image(files.get('image'), ITEM_IMAGES_DIR, max_size=ITEM_IMAGE_MAX_SIZE)

    db = get_db()
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session.get('user_id'),)).fetchone()
    next_order = db.execute(
        'SELECT COALESCE(MAX(sort_order), 0) + 1 FROM items WHERE user_id = ?', (user_id,)
    ).fetchone()[0]
    db.execute(
        '''INSERT INTO items
           (user_id, name, image, quantity, spaces, description, level, item_type, damage, critical, defense, carga_bonus, element, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, name, image_filename, quantity, spaces, description, level, item_type, damage, critical, defense, carga_bonus, element, next_order)
    )
    _log_action(db, actor, f'adicionou o item "{name}" (x{quantity}) ao inventário')
    # Itens de Proteção aumentam diretamente o bônus de defesa de equipamento.
    if item_type == 'Proteção' and defense:
        db.execute(
            'UPDATE users SET defesa_equip_bonus = defesa_equip_bonus + ? WHERE id = ?',
            (defense * quantity, user_id)
        )

    # Se for um Documento e o usuário optou por publicar no mural, copia a
    # imagem do item para a pasta do mural e cria a postagem correspondente.
    if item_type == 'Documento' and image_filename and form.get('post_to_mural') == '1':
        ext = image_filename.rsplit('.', 1)[1]
        mural_filename = f'{uuid.uuid4().hex}.{ext}'
        src_path = os.path.join(ITEM_IMAGES_DIR, image_filename)
        dst_path = os.path.join(MURAL_IMAGES_DIR, mural_filename)
        if os.path.exists(src_path):
            shutil.copy2(src_path, dst_path)
            db.execute(
                'INSERT INTO mural_images (image, caption, category, user_id, username) VALUES (?, ?, ?, ?, ?)',
                (mural_filename, name, 'Documento', session.get('user_id'), get_display_name(actor))
            )

    db.commit()


def _update_item(item_id, user_id, form, files):
    """Atualiza um item já cadastrado no inventário. Retorna True em sucesso.

    Só atualiza um item pertencente a `user_id` (quando não-None, usado no
    fluxo do jogador); o mestre pode editar o item de qualquer um. Uma nova
    imagem é opcional — se não for enviada, a imagem atual é mantida. Se o
    item for do tipo Proteção, o bônus de defesa de equipamento do usuário
    é reajustado pela diferença entre a contribuição antiga e a nova.
    """
    db = get_db()
    if user_id is not None:
        existing = db.execute(
            'SELECT * FROM items WHERE id = ? AND user_id = ?', (item_id, user_id)
        ).fetchone()
    else:
        existing = db.execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()

    if existing is None:
        return False

    owner_id = existing['user_id']

    name = form.get('name', '').strip()
    if name:
        name = name[0].upper() + name[1:]
    if not name:
        return False

    quantity = form.get('quantity', existing['quantity'])
    spaces = form.get('spaces', existing['spaces'])
    description = form.get('description', '').strip()
    level = form.get('level', '0').strip()
    item_type = form.get('item_type', '').strip()
    damage = form.get('damage', '').strip()
    critical = form.get('critical', '').strip()
    defense = form.get('defense', '0').strip()
    carga_bonus = form.get('carga_bonus', '0').strip()
    element = form.get('element', '').strip()

    try:
        quantity = max(1, int(quantity))
    except (TypeError, ValueError):
        quantity = existing['quantity']
    try:
        spaces = max(0, int(spaces))
    except (TypeError, ValueError):
        spaces = existing['spaces']
    try:
        defense = max(0, int(defense))
    except (TypeError, ValueError):
        defense = 0
    try:
        carga_bonus = max(0, int(carga_bonus))
    except (TypeError, ValueError):
        carga_bonus = 0

    if level not in ITEM_LEVELS:
        level = '0'
    if item_type not in ITEM_TYPES:
        item_type = ''

    if item_type != 'Arma':
        damage = ''
        critical = ''
    if item_type != 'Proteção':
        defense = 0
    if item_type != ITEM_TYPE_CARGA:
        carga_bonus = 0
    if item_type != ITEM_TYPE_PARANORMAL or element not in RITUAL_ELEMENTS:
        element = ''

    new_image_filename = save_uploaded_image(files.get('image'), ITEM_IMAGES_DIR, max_size=ITEM_IMAGE_MAX_SIZE)
    image_filename = new_image_filename or existing['image']

    old_defense_contrib = existing['defense'] * existing['quantity'] if existing['item_type'] == 'Proteção' else 0
    new_defense_contrib = defense * quantity if item_type == 'Proteção' else 0
    defense_delta = new_defense_contrib - old_defense_contrib

    db.execute(
        '''UPDATE items SET
               name = ?, quantity = ?, spaces = ?, description = ?, level = ?,
               item_type = ?, damage = ?, critical = ?, defense = ?, carga_bonus = ?, element = ?, image = ?
           WHERE id = ?''',
        (name, quantity, spaces, description, level, item_type, damage, critical, defense, carga_bonus, element, image_filename, item_id)
    )
    if defense_delta:
        db.execute(
            'UPDATE users SET defesa_equip_bonus = MAX(0, defesa_equip_bonus + ?) WHERE id = ?',
            (defense_delta, owner_id)
        )
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session.get('user_id'),)).fetchone()
    _log_action(db, actor, f'editou o item "{name}" no inventário')
    db.commit()

    if new_image_filename and existing['image']:
        old_path = os.path.join(ITEM_IMAGES_DIR, existing['image'])
        if os.path.exists(old_path):
            os.remove(old_path)

    return True


@app.route('/player/api/item/<int:item_id>')
@login_required
def api_get_item(item_id):
    db = get_db()
    item = db.execute(
        'SELECT * FROM items WHERE id = ? AND user_id = ?', (item_id, session['user_id'])
    ).fetchone()
    if item is None:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404
    return jsonify({'ok': True, 'item': dict(item)})


@app.route('/player/edit_item/<int:item_id>', methods=['POST'])
@login_required
def player_edit_item(item_id):
    _update_item(item_id, session['user_id'], request.form, request.files)
    return redirect(url_for('player_panel'))


@app.route('/admin/add_item/<int:user_id>', methods=['POST'])
@admin_required
def add_item(user_id):
    _create_item(user_id, request.form, request.files)
    return redirect(url_for('admin_panel'))


@app.route('/player/add_item', methods=['POST'])
@login_required
def player_add_item():
    _create_item(session['user_id'], request.form, request.files)
    return redirect(url_for('player_panel'))


@app.route('/player/delete_item/<int:item_id>', methods=['POST'])
@login_required
def player_delete_item(item_id):
    db = get_db()
    row = db.execute(
        'SELECT image, quantity, item_type, defense, name FROM items WHERE id = ? AND user_id = ?',
        (item_id, session['user_id'])
    ).fetchone()

    if row is not None:
        try:
            qty_to_remove = int(request.form.get('quantity', 1))
        except (TypeError, ValueError):
            qty_to_remove = 1
        qty_to_remove = max(1, min(qty_to_remove, row['quantity']))

        if row['item_type'] == 'Proteção' and row['defense']:
            db.execute(
                'UPDATE users SET defesa_equip_bonus = MAX(0, defesa_equip_bonus - ?) WHERE id = ?',
                (row['defense'] * qty_to_remove, session['user_id'])
            )

        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, f'removeu o item "{row["name"]}" (x{qty_to_remove}) do inventário')

        if qty_to_remove < row['quantity']:
            db.execute(
                'UPDATE items SET quantity = quantity - ? WHERE id = ? AND user_id = ?',
                (qty_to_remove, item_id, session['user_id'])
            )
            db.commit()
        else:
            db.execute('DELETE FROM items WHERE id = ? AND user_id = ?', (item_id, session['user_id']))
            db.commit()
            if row['image']:
                path = os.path.join(ITEM_IMAGES_DIR, row['image'])
                if os.path.exists(path):
                    os.remove(path)

    return redirect(url_for('player_panel'))


@app.route('/player/api/reorder_items', methods=['POST'])
@login_required
def api_reorder_items():
    """Salva a nova ordem dos itens do inventário do jogador (arrastar e
    organizar). Recebe {"order": [id1, id2, ...]} com os ids na ordem
    desejada e grava essa posição em sort_order."""
    data = request.get_json(silent=True) or {}
    order = data.get('order')

    if not isinstance(order, list) or not order:
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    try:
        item_ids = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    db = get_db()
    owned_rows = db.execute(
        'SELECT id FROM items WHERE user_id = ?', (session['user_id'],)
    ).fetchall()
    owned_ids = {row['id'] for row in owned_rows}

    # Só reordena itens que realmente pertencem ao jogador logado, ignorando
    # qualquer id estranho enviado por engano ou má-fé.
    for position, item_id in enumerate(item_ids):
        if item_id in owned_ids:
            db.execute(
                'UPDATE items SET sort_order = ? WHERE id = ? AND user_id = ?',
                (position, item_id, session['user_id'])
            )
    db.commit()

    return jsonify({'ok': True})


@app.route('/player/api/reorder_abilities', methods=['POST'])
@login_required
def api_reorder_abilities():
    """Salva a nova ordem das habilidades/poderes do jogador (arrastar e
    organizar). Recebe {"order": [id1, id2, ...]} com os ids na ordem
    desejada e grava essa posição em sort_order."""
    data = request.get_json(silent=True) or {}
    order = data.get('order')

    if not isinstance(order, list) or not order:
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    try:
        ability_ids = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    db = get_db()
    owned_rows = db.execute(
        'SELECT id FROM class_abilities WHERE user_id = ?', (session['user_id'],)
    ).fetchall()
    owned_ids = {row['id'] for row in owned_rows}

    # Só reordena habilidades/poderes que realmente pertencem ao jogador
    # logado, ignorando qualquer id estranho enviado por engano ou má-fé.
    for position, ability_id in enumerate(ability_ids):
        if ability_id in owned_ids:
            db.execute(
                'UPDATE class_abilities SET sort_order = ? WHERE id = ? AND user_id = ?',
                (position, ability_id, session['user_id'])
            )
    db.commit()

    return jsonify({'ok': True})


@app.route('/player/api/reorder_rituals', methods=['POST'])
@login_required
def api_reorder_rituals():
    """Salva a nova ordem dos rituais do jogador (arrastar e organizar).
    Recebe {"order": [id1, id2, ...]} com os ids na ordem desejada e grava
    essa posição em sort_order."""
    data = request.get_json(silent=True) or {}
    order = data.get('order')

    if not isinstance(order, list) or not order:
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    try:
        ritual_ids = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Ordem inválida'}), 400

    db = get_db()
    owned_rows = db.execute(
        'SELECT id FROM rituals WHERE user_id = ?', (session['user_id'],)
    ).fetchall()
    owned_ids = {row['id'] for row in owned_rows}

    # Só reordena rituais que realmente pertencem ao jogador logado,
    # ignorando qualquer id estranho enviado por engano ou má-fé.
    for position, ritual_id in enumerate(ritual_ids):
        if ritual_id in owned_ids:
            db.execute(
                'UPDATE rituals SET sort_order = ? WHERE id = ? AND user_id = ?',
                (position, ritual_id, session['user_id'])
            )
    db.commit()

    return jsonify({'ok': True})


@app.route('/player/api/consume_item/<int:item_id>', methods=['POST'])
@login_required
def api_consume_item(item_id):
    """Consome 1 unidade de um item do tipo Consumível, removendo o item
    do inventário quando a última unidade é usada."""
    db = get_db()
    row = db.execute(
        'SELECT name, image, quantity, item_type FROM items WHERE id = ? AND user_id = ?',
        (item_id, session['user_id'])
    ).fetchone()

    if row is None:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404

    if row['item_type'] != 'Consumível':
        return jsonify({'ok': False, 'error': 'Este item não pode ser consumido'}), 400

    if row['quantity'] <= 0:
        return jsonify({'ok': False, 'error': 'Nenhuma unidade restante'}), 400

    new_quantity = row['quantity'] - 1

    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'consumiu 1x "{row["name"]}"')

    if new_quantity <= 0:
        db.execute('DELETE FROM items WHERE id = ? AND user_id = ?', (item_id, session['user_id']))
        db.commit()
        if row['image']:
            path = os.path.join(ITEM_IMAGES_DIR, row['image'])
            if os.path.exists(path):
                os.remove(path)
        return jsonify({'ok': True, 'remaining': 0, 'deleted': True, 'name': row['name']})

    db.execute(
        'UPDATE items SET quantity = ? WHERE id = ? AND user_id = ?',
        (new_quantity, item_id, session['user_id'])
    )
    db.commit()
    return jsonify({'ok': True, 'remaining': new_quantity, 'deleted': False, 'name': row['name']})


@app.route('/admin/delete_item/<int:item_id>', methods=['POST'])
@admin_required
def delete_item(item_id):
    db = get_db()
    row = db.execute(
        'SELECT user_id, image, quantity, item_type, defense FROM items WHERE id = ?', (item_id,)
    ).fetchone()

    if row is not None:
        try:
            qty_to_remove = int(request.form.get('quantity', 1))
        except (TypeError, ValueError):
            qty_to_remove = 1
        qty_to_remove = max(1, min(qty_to_remove, row['quantity']))

        if row['item_type'] == 'Proteção' and row['defense']:
            db.execute(
                'UPDATE users SET defesa_equip_bonus = MAX(0, defesa_equip_bonus - ?) WHERE id = ?',
                (row['defense'] * qty_to_remove, row['user_id'])
            )

        if qty_to_remove < row['quantity']:
            db.execute('UPDATE items SET quantity = quantity - ? WHERE id = ?', (qty_to_remove, item_id))
            db.commit()
        else:
            db.execute('DELETE FROM items WHERE id = ?', (item_id,))
            db.commit()
            if row['image']:
                path = os.path.join(ITEM_IMAGES_DIR, row['image'])
                if os.path.exists(path):
                    os.remove(path)

    return redirect(url_for('admin_panel'))


@app.route('/admin/api/update_item/<int:item_id>', methods=['POST'])
@admin_required
def api_update_item(item_id):
    # Antes só aceitava JSON, então não tinha como mandar um arquivo de
    # imagem junto (o campo "image" era simplesmente ignorado). Agora lê
    # de request.form, que funciona tanto para o multipart/form-data usado
    # quando há uma imagem nova quanto para o JSON antigo (fallback).
    data = request.form if request.form else (request.get_json(silent=True) or {})

    updates = {}
    if 'quantity' in data:
        try:
            updates['quantity'] = max(0, int(data['quantity']))
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'Quantidade inválida'}), 400
    if 'spaces' in data:
        try:
            updates['spaces'] = max(0, int(data['spaces']))
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'Espaços inválidos'}), 400
    if 'description' in data:
        updates['description'] = str(data['description'])[:2000]
    if 'level' in data:
        level = str(data['level']).strip()
        if level not in ITEM_LEVELS:
            return jsonify({'ok': False, 'error': 'Categoria inválida'}), 400
        updates['level'] = level
    if 'item_type' in data:
        item_type = str(data['item_type']).strip()
        if item_type not in ITEM_TYPES and item_type != '':
            return jsonify({'ok': False, 'error': 'Categoria de item inválida'}), 400
        updates['item_type'] = item_type
    if 'damage' in data:
        updates['damage'] = str(data['damage']).strip()[:100]
    if 'critical' in data:
        updates['critical'] = str(data['critical']).strip()[:100]
    if 'defense' in data:
        try:
            updates['defense'] = max(0, int(data['defense']))
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'Valor de defesa inválido'}), 400
    if 'carga_bonus' in data:
        try:
            updates['carga_bonus'] = max(0, int(data['carga_bonus']))
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'Valor de bônus de carga inválido'}), 400

    if 'element' in data:
        element = str(data['element']).strip()
        if element and element not in RITUAL_ELEMENTS:
            return jsonify({'ok': False, 'error': 'Elemento inválido'}), 400
        updates['element'] = element

    # Nova imagem, se enviada (substitui a anterior — o arquivo antigo é
    # apagado do disco depois do UPDATE, mais abaixo).
    new_image_filename = save_uploaded_image(request.files.get('image'), ITEM_IMAGES_DIR, max_size=ITEM_IMAGE_MAX_SIZE)
    if new_image_filename:
        updates['image'] = new_image_filename

    # Dano e crítico só fazem sentido para armas — se o tipo final não for
    # Arma, os dois campos são zerados para não deixar dado inconsistente.
    if updates.get('item_type', None) is not None and updates['item_type'] != 'Arma':
        updates['damage'] = ''
        updates['critical'] = ''
    # Defesa só faz sentido para itens de Proteção — mesma lógica acima.
    if updates.get('item_type', None) is not None and updates['item_type'] != 'Proteção':
        updates['defense'] = 0
    # Bônus de carga só faz sentido para Acessórios de Carga — mesma lógica.
    if updates.get('item_type', None) is not None and updates['item_type'] != ITEM_TYPE_CARGA:
        updates['carga_bonus'] = 0
    # Elemento só faz sentido para itens Paranormais — mesma lógica.
    if updates.get('item_type', None) is not None and updates['item_type'] != ITEM_TYPE_PARANORMAL:
        updates['element'] = ''

    if not updates:
        return jsonify({'ok': False, 'error': 'Nada para atualizar'}), 400

    db = get_db()
    old_item = db.execute(
        'SELECT user_id, item_type, defense, quantity, image FROM items WHERE id = ?', (item_id,)
    ).fetchone()
    if old_item is None:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404

    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE items SET {set_clause} WHERE id = ?', (*updates.values(), item_id))

    # Recalcula o bônus de defesa de equipamento se o tipo, a defesa ou a
    # quantidade do item mudaram, para manter os pontos de defesa em dia
    # com os itens de Proteção do inventário.
    new_item_type = updates.get('item_type', old_item['item_type'])
    new_defense = updates.get('defense', old_item['defense'])
    new_quantity = updates.get('quantity', old_item['quantity'])
    old_contribution = (old_item['defense'] or 0) * old_item['quantity'] if old_item['item_type'] == 'Proteção' else 0
    new_contribution = (new_defense or 0) * new_quantity if new_item_type == 'Proteção' else 0
    delta = new_contribution - old_contribution
    if delta != 0:
        db.execute(
            'UPDATE users SET defesa_equip_bonus = MAX(0, defesa_equip_bonus + ?) WHERE id = ?',
            (delta, old_item['user_id'])
        )

    db.commit()

    # Se uma imagem nova substituiu a antiga, remove o arquivo antigo do
    # disco (mesmo padrão já usado para avatar/banner da campanha).
    if new_image_filename and old_item['image']:
        old_path = os.path.join(ITEM_IMAGES_DIR, old_item['image'])
        if os.path.exists(old_path):
            os.remove(old_path)

    item = db.execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()

    if item is None:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404

    return jsonify({'ok': True, 'item': dict(item)})


def _create_ritual(user_id, form, files, require_symbol=False):
    """Cria um ritual para o usuário indicado. Retorna True em sucesso.

    Quando require_symbol=True, o ritual só é criado se uma imagem de
    símbolo válida for enviada — usado no cadastro feito pelo jogador.
    """
    name = capitalize_sentences(form.get('name', '').strip())
    element = form.get('element', '').strip()
    try:
        circle = int(form.get('circle', 0))
    except ValueError:
        circle = 0
    execution = capitalize_sentences(form.get('execution', '').strip())
    range_ = capitalize_sentences(form.get('range', '').strip())
    target = capitalize_sentences(form.get('target', '').strip())
    duration = capitalize_sentences(form.get('duration', '').strip())
    resistance = capitalize_sentences(form.get('resistance', '').strip())
    effect_summary = capitalize_sentences(form.get('effect_summary', '').strip())
    description_discente = capitalize_sentences(form.get('description_discente', '').strip())
    description_verdadeira = capitalize_sentences(form.get('description_verdadeira', '').strip())

    try:
        pe_cost_discente = max(0, int(form.get('pe_cost_discente', 0) or 0))
    except ValueError:
        pe_cost_discente = 0
    try:
        pe_cost_verdadeira = max(0, int(form.get('pe_cost_verdadeira', 0) or 0))
    except ValueError:
        pe_cost_verdadeira = 0
    if circle not in RITUAL_CIRCLE_PE_COSTS:
        return False

    pe_cost_normal = RITUAL_CIRCLE_PE_COSTS[circle]

    if not name or element not in RITUAL_ELEMENTS:
        return False

    symbol_filename = save_uploaded_image(files.get('symbol'), RITUAL_SYMBOLS_DIR, max_size=RITUAL_SYMBOL_MAX_SIZE)

    if require_symbol and not symbol_filename:
        return False

    db = get_db()
    next_order = db.execute(
        'SELECT COALESCE(MAX(sort_order), 0) + 1 FROM rituals WHERE user_id = ?', (user_id,)
    ).fetchone()[0]
    db.execute(
        '''INSERT INTO rituals
           (user_id, name, element, circle, pe_cost, pe_cost_discente, pe_cost_verdadeira, pe_cost_normal, execution, range_, target, duration, resistance, effect_summary, description_discente, description_verdadeira, symbol, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            user_id, name, element, circle, pe_cost_discente, pe_cost_discente, pe_cost_verdadeira, pe_cost_normal,
            execution, range_, target, duration, resistance, effect_summary, description_discente, description_verdadeira,
            symbol_filename, next_order,
        )
    )
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session.get('user_id'),)).fetchone()
    _log_action(db, actor, f'cadastrou o ritual "{name}" ({element})')
    db.commit()
    return True


def _update_ritual(ritual_id, user_id, form, files):
    """Atualiza um ritual já cadastrado. Retorna True em sucesso.

    Só atualiza um ritual pertencente a `user_id` (quando não-None, usado
    no fluxo do jogador); o mestre pode editar o ritual de qualquer um.
    Uma nova imagem de símbolo é opcional — se não for enviada, o símbolo
    atual é mantido.
    """
    db = get_db()
    if user_id is not None:
        existing = db.execute(
            'SELECT * FROM rituals WHERE id = ? AND user_id = ?', (ritual_id, user_id)
        ).fetchone()
    else:
        existing = db.execute('SELECT * FROM rituals WHERE id = ?', (ritual_id,)).fetchone()

    if existing is None:
        return False

    name = capitalize_sentences(form.get('name', '').strip())
    element = form.get('element', '').strip()
    try:
        circle = int(form.get('circle', 0))
    except ValueError:
        circle = 0
    execution = capitalize_sentences(form.get('execution', '').strip())
    range_ = capitalize_sentences(form.get('range', '').strip())
    target = capitalize_sentences(form.get('target', '').strip())
    duration = capitalize_sentences(form.get('duration', '').strip())
    resistance = capitalize_sentences(form.get('resistance', '').strip())
    effect_summary = capitalize_sentences(form.get('effect_summary', '').strip())
    description_discente = capitalize_sentences(form.get('description_discente', '').strip())
    description_verdadeira = capitalize_sentences(form.get('description_verdadeira', '').strip())

    try:
        pe_cost_discente = max(0, int(form.get('pe_cost_discente', 0) or 0))
    except ValueError:
        pe_cost_discente = 0
    try:
        pe_cost_verdadeira = max(0, int(form.get('pe_cost_verdadeira', 0) or 0))
    except ValueError:
        pe_cost_verdadeira = 0

    if circle not in RITUAL_CIRCLE_PE_COSTS:
        return False
    if not name or element not in RITUAL_ELEMENTS:
        return False

    pe_cost_normal = RITUAL_CIRCLE_PE_COSTS[circle]

    new_symbol_filename = save_uploaded_image(files.get('symbol'), RITUAL_SYMBOLS_DIR, max_size=RITUAL_SYMBOL_MAX_SIZE)
    symbol_filename = new_symbol_filename or existing['symbol']

    db.execute(
        '''UPDATE rituals SET
               name = ?, element = ?, circle = ?, pe_cost_discente = ?, pe_cost_verdadeira = ?,
               pe_cost_normal = ?, execution = ?, range_ = ?, target = ?, duration = ?, resistance = ?,
               effect_summary = ?, description_discente = ?, description_verdadeira = ?, symbol = ?
           WHERE id = ?''',
        (
            name, element, circle, pe_cost_discente, pe_cost_verdadeira, pe_cost_normal,
            execution, range_, target, duration, resistance, effect_summary, description_discente,
            description_verdadeira, symbol_filename, ritual_id,
        )
    )
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session.get('user_id'),)).fetchone()
    _log_action(db, actor, f'editou o ritual "{name}" ({element})')
    db.commit()

    if new_symbol_filename and existing['symbol']:
        old_path = os.path.join(RITUAL_SYMBOLS_DIR, existing['symbol'])
        if os.path.exists(old_path):
            os.remove(old_path)

    return True


@app.route('/player/api/ritual/<int:ritual_id>')
@login_required
def api_get_ritual(ritual_id):
    db = get_db()
    ritual = db.execute(
        'SELECT * FROM rituals WHERE id = ? AND user_id = ?', (ritual_id, session['user_id'])
    ).fetchone()
    if ritual is None:
        return jsonify({'ok': False, 'error': 'Ritual não encontrado'}), 404
    return jsonify({'ok': True, 'ritual': dict(ritual)})


@app.route('/player/edit_ritual/<int:ritual_id>', methods=['POST'])
@login_required
def player_edit_ritual(ritual_id):
    _update_ritual(ritual_id, session['user_id'], request.form, request.files)
    return redirect(url_for('player_panel'))


@app.route('/admin/add_ritual/<int:user_id>', methods=['POST'])
@admin_required
def add_ritual(user_id):
    _create_ritual(user_id, request.form, request.files, require_symbol=False)
    return redirect(url_for('admin_panel'))


@app.route('/player/add_ritual', methods=['POST'])
@login_required
def player_add_ritual():
    ok = _create_ritual(session['user_id'], request.form, request.files, require_symbol=True)
    if not ok:
        return redirect(url_for('player_panel', ritual_error=1))
    return redirect(url_for('player_panel'))


@app.route('/admin/delete_ritual/<int:ritual_id>', methods=['POST'])
@admin_required
def delete_ritual(ritual_id):
    db = get_db()
    row = db.execute('SELECT symbol FROM rituals WHERE id = ?', (ritual_id,)).fetchone()
    db.execute('DELETE FROM rituals WHERE id = ?', (ritual_id,))
    db.commit()

    if row and row['symbol']:
        path = os.path.join(RITUAL_SYMBOLS_DIR, row['symbol'])
        if os.path.exists(path):
            os.remove(path)

    return redirect(url_for('admin_panel'))


@app.route('/player/delete_ritual/<int:ritual_id>', methods=['POST'])
@login_required
def player_delete_ritual(ritual_id):
    db = get_db()
    row = db.execute(
        'SELECT symbol, name FROM rituals WHERE id = ? AND user_id = ?', (ritual_id, session['user_id'])
    ).fetchone()

    if row is not None:
        db.execute('DELETE FROM rituals WHERE id = ? AND user_id = ?', (ritual_id, session['user_id']))
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, f'removeu o ritual "{row["name"]}"')
        db.commit()

    if row and row['symbol']:
        path = os.path.join(RITUAL_SYMBOLS_DIR, row['symbol'])
        if os.path.exists(path):
            os.remove(path)

    return redirect(url_for('player_panel'))


@app.route('/player/use_ritual/<int:ritual_id>', methods=['POST'])
@login_required
def player_use_ritual(ritual_id):
    db = get_db()
    ritual = db.execute(
        'SELECT * FROM rituals WHERE id = ? AND user_id = ?', (ritual_id, session['user_id'])
    ).fetchone()

    if ritual is None:
        return jsonify({'ok': False, 'error': 'Ritual não encontrado.'}), 404

    user = db.execute('SELECT pe, pe_max FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    version = request.values.get('version', 'normal').strip().lower()
    if version not in ('discente', 'verdadeira', 'normal'):
        version = 'normal'
    if version == 'verdadeira':
        pe_cost = ritual['pe_cost_verdadeira'] or 0
        version_label = 'Verdadeira'
    elif version == 'normal':
        pe_cost = ritual['pe_cost_normal'] or 0
        version_label = 'Normal'
    else:
        pe_cost = ritual['pe_cost_discente'] or 0
        version_label = 'Discente'

    if pe_cost > 0 and user['pe'] < pe_cost:
        return jsonify({
            'ok': False,
            'error': f'{_pe_label()} insuficiente para usar o ritual "{ritual["name"]}" (versão {version_label.lower()}, custo: {pe_cost} {_pe_label()}, disponível: {user["pe"]} {_pe_label()}).'
        }), 400

    new_pe = max(0, user['pe'] - pe_cost)

    db.execute('UPDATE users SET pe = ? WHERE id = ?', (new_pe, session['user_id']))
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'usou o ritual "{ritual["name"]}" (versão {version_label.lower()}, -{pe_cost} {_pe_label()})')

    ritual_content = (
        f'usou o ritual: {ritual["circle"]}º Círculo — {ritual["name"]} ({ritual["element"]}) — Versão {version_label}'
    )
    db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color, ritual_element)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            actor['id'], get_display_name(actor), '' if actor['is_admin'] else actor['character_name'],
            actor['is_admin'], 'ritual', ritual_content,
            RITUAL_ELEMENT_COLORS.get(ritual['element'], actor['color']), ritual['element'],
        )
    )
    db.commit()

    return jsonify({'ok': True, 'pe_current': new_pe, 'pe_max': user['pe_max'], 'pe_cost': pe_cost, 'name': ritual['name'], 'version': version})


@app.route('/admin/add_ability/<int:user_id>', methods=['POST'])
@admin_required
def add_ability(user_id):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    element = request.form.get('element', '').strip()
    if element not in RITUAL_ELEMENTS:
        element = ''
    kind = request.form.get('kind', 'habilidade').strip().lower()
    if kind not in ABILITY_KINDS:
        kind = 'habilidade'
    try:
        pe_cost = max(0, int(request.form.get('pe_cost', 0) or 0))
    except (TypeError, ValueError):
        pe_cost = 0

    if name:
        db = get_db()
        db.execute(
            'INSERT INTO class_abilities (user_id, name, description, pe_cost, element, kind) VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, name, description, pe_cost, element, kind)
        )
        db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_ability/<int:ability_id>', methods=['POST'])
@admin_required
def delete_ability(ability_id):
    db = get_db()
    db.execute('DELETE FROM class_abilities WHERE id = ?', (ability_id,))
    db.commit()

    return redirect(url_for('admin_panel'))


@app.route('/player/add_ability', methods=['POST'])
@login_required
def player_add_ability():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    element = request.form.get('element', '').strip()
    if element not in RITUAL_ELEMENTS:
        element = ''
    kind = request.form.get('kind', 'habilidade').strip().lower()
    if kind not in ABILITY_KINDS:
        kind = 'habilidade'
    try:
        pe_cost = max(0, int(request.form.get('pe_cost', 0) or 0))
    except (TypeError, ValueError):
        pe_cost = 0

    if name:
        db = get_db()
        db.execute(
            'INSERT INTO class_abilities (user_id, name, description, pe_cost, element, kind) VALUES (?, ?, ?, ?, ?, ?)',
            (session['user_id'], name, description, pe_cost, element, kind)
        )
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        label = 'poder' if kind == 'poder' else 'habilidade'
        _log_action(db, actor, f'cadastrou a {label} "{name}"')
        db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/use_ability/<int:ability_id>', methods=['POST'])
@login_required
def player_use_ability(ability_id):
    db = get_db()
    ability = db.execute(
        'SELECT * FROM class_abilities WHERE id = ? AND user_id = ?', (ability_id, session['user_id'])
    ).fetchone()

    if ability is None:
        return jsonify({'ok': False, 'error': 'Habilidade não encontrada.'}), 404

    user = db.execute('SELECT pe, pe_max FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    pe_cost = ability['pe_cost'] or 0
    label = 'poder' if ability['kind'] == 'poder' else 'habilidade'

    if pe_cost > 0 and user['pe'] < pe_cost:
        return jsonify({
            'ok': False,
            'error': f'{_pe_label()} insuficiente para usar {"o" if ability["kind"] == "poder" else "a"} {label} "{ability["name"]}" (custo: {pe_cost} {_pe_label()}, disponível: {user["pe"]} {_pe_label()}).'
        }), 400

    new_pe = max(0, user['pe'] - pe_cost)

    db.execute('UPDATE users SET pe = ? WHERE id = ?', (new_pe, session['user_id']))
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'usou {"o" if ability["kind"] == "poder" else "a"} {label} "{ability["name"]}" (-{pe_cost} {_pe_label()})')
    db.commit()

    return jsonify({'ok': True, 'pe_current': new_pe, 'pe_max': user['pe_max'], 'pe_cost': pe_cost, 'name': ability['name'], 'kind': ability['kind']})


@app.route('/player/delete_ability/<int:ability_id>', methods=['POST'])
@login_required
def player_delete_ability(ability_id):
    db = get_db()
    db.execute(
        'DELETE FROM class_abilities WHERE id = ? AND user_id = ?', (ability_id, session['user_id'])
    )
    db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/update_avatar', methods=['POST'])
@login_required
def player_update_avatar():
    filename = save_uploaded_image(request.files.get('avatar'), AVATAR_IMAGES_DIR, max_size=AVATAR_MAX_SIZE)

    if filename:
        db = get_db()
        old = db.execute('SELECT avatar FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        db.execute('UPDATE users SET avatar = ? WHERE id = ?', (filename, session['user_id']))
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, 'atualizou a imagem do avatar')
        db.commit()

        if old and old['avatar']:
            old_path = os.path.join(AVATAR_IMAGES_DIR, old['avatar'])
            if os.path.exists(old_path):
                os.remove(old_path)

        if _wants_json():
            return jsonify({'ok': True, 'avatar_url': url_for('static', filename='avatar_images/' + filename)})
    elif _wants_json():
        return jsonify({'ok': False, 'error': 'Envie uma imagem válida.'}), 400

    return redirect(url_for('player_panel'))


@app.route('/player/set_attributes', methods=['POST'])
@login_required
def player_set_attributes():
    db = get_db()
    user = db.execute('SELECT attributes_locked FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if user and not user['attributes_locked']:
        values = {}
        for key in ATTRIBUTE_LABELS:
            try:
                values[key] = max(1, int(request.form.get(key, 1)))
            except (TypeError, ValueError):
                values[key] = 1

        db.execute(
            '''UPDATE users
               SET agilidade = ?, forca = ?, intelecto = ?, presenca = ?, vigor = ?,
                   attributes_locked = 1
               WHERE id = ?''',
            (values['agilidade'], values['forca'], values['intelecto'],
             values['presenca'], values['vigor'], session['user_id'])
        )
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, (
            f'definiu e trancou os atributos (AGI {values["agilidade"]}, FOR {values["forca"]}, '
            f'INT {values["intelecto"]}, PRE {values["presenca"]}, VIG {values["vigor"]})'
        ))
        db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/set_points', methods=['POST'])
@login_required
def player_set_points():
    db = get_db()
    user = db.execute('SELECT points_locked FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if user and not user['points_locked']:
        try:
            pv_max = max(1, int(request.form.get('pv_max', POINT_DEFAULTS['pv_max'])))
        except (TypeError, ValueError):
            pv_max = POINT_DEFAULTS['pv_max']
        try:
            pe_max = max(0, int(request.form.get('pe_max', POINT_DEFAULTS['pe_max'])))
        except (TypeError, ValueError):
            pe_max = POINT_DEFAULTS['pe_max']
        try:
            sanidade_max = max(1, int(request.form.get('sanidade_max', POINT_DEFAULTS['sanidade_max'])))
        except (TypeError, ValueError):
            sanidade_max = POINT_DEFAULTS['sanidade_max']

        db.execute(
            '''UPDATE users
               SET pv = ?, pv_max = ?, pe = ?, pe_max = ?, sanidade = ?, sanidade_max = ?,
                   points_locked = 1
               WHERE id = ?''',
            (pv_max, pv_max, pe_max, pe_max, sanidade_max, sanidade_max, session['user_id'])
        )
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, f'definiu e trancou os pontos (PV {pv_max}, {_pe_label()} {pe_max}, Sanidade {sanidade_max})')
        db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/set_defesa_bonus', methods=['POST'])
@login_required
def player_set_defesa_bonus():
    data = request.get_json(force=True) or {}

    try:
        equip_bonus = int(data.get('defesa_equip_bonus', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Valor inválido para bônus de equipamento.'}), 400
    try:
        outros_bonus = int(data.get('defesa_outros_bonus', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Valor inválido para outros bônus.'}), 400

    db = get_db()
    db.execute(
        'UPDATE users SET defesa_equip_bonus = ?, defesa_outros_bonus = ? WHERE id = ?',
        (equip_bonus, outros_bonus, session['user_id'])
    )
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, user, f'ajustou os bônus de defesa (equipamento {equip_bonus}, outros {outros_bonus})')
    db.commit()

    overloaded = _used_spaces(db, user['id']) > _capacidade_max(user, _carga_bonus(db, user['id']))
    return jsonify({
        'ok': True,
        'defesa': _defesa_valor(user, overloaded),
        'defesa_equip_bonus': user['defesa_equip_bonus'],
        'defesa_outros_bonus': user['defesa_outros_bonus']
    })


POINT_FIELDS = {
    'pv': {'current': 'pv', 'max': 'pv_max', 'label': 'PV'},
    'pe': {'current': 'pe', 'max': 'pe_max', 'label': 'PE'},
    'sanidade': {'current': 'sanidade', 'max': 'sanidade_max', 'label': 'Sanidade'},
}


@app.errorhandler(Exception)
def _json_error_for_ajax(exc):
    """Chamadas fetch() com JSON recebem o erro em JSON (com o motivo),
    em vez da página HTML de erro — assim a tela mostra a causa real e não
    um genérico "Erro de conexão". Demais requisições seguem o padrão."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception('Erro não tratado em %s', request.path)
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': False, 'error': f'Erro interno: {type(exc).__name__}: {exc}'}), 500
    raise exc


@app.route('/player/reduce_point', methods=['POST'])
@login_required
def player_reduce_point():
    """Permite ao jogador aumentar ou reduzir seus próprios pontos atuais
    (PV, PE ou Sanidade). O valor máximo de cada um nunca é alterado
    aqui — só o mestre pode mudar isso (ver player_set_points).

    Defesa não entra aqui: ela não é um pool de pontos que se gasta, e sim um
    valor fixo (10 + AGI + equipamento + outros bônus) que o ataque do
    oponente precisa igualar ou superar para acertar. Ver _defesa_valor."""
    data = request.get_json(force=True) or {}
    field = data.get('field')
    direction = data.get('direction', 'decrease')
    if direction not in ('increase', 'decrease'):
        direction = 'decrease'

    try:
        amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        amount = 0

    if amount <= 0:
        return jsonify({'ok': False, 'error': 'Informe uma quantidade válida.'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    verb = 'recuperou' if direction == 'increase' else 'perdeu'

    if field not in POINT_FIELDS:
        return jsonify({'ok': False, 'error': 'Ponto inválido.'}), 400

    if not user['points_locked']:
        return jsonify({'ok': False, 'error': 'Defina seus pontos antes de reduzi-los.'}), 400

    cols = POINT_FIELDS[field]
    current_val = user[cols['current']] or 0
    max_val = user[cols['max']] or 0
    # O valor atual pode ultrapassar o máximo ao aumentar (ex: buffs
    # temporários) — só o piso de 0 é aplicado ao reduzir.
    if direction == 'increase':
        new_val = current_val + amount
    else:
        new_val = max(0, current_val - amount)

    db.execute(f"UPDATE users SET {cols['current']} = ? WHERE id = ?", (new_val, session['user_id']))
    db.commit()

    # O registro no log do mestre é secundário: se falhar (ex.: tabela de
    # log com esquema antigo), o ajuste do ponto já foi salvo e o jogador
    # não deve ver erro.
    try:
        _log_action(db, user, f'{verb} {amount} de {_pe_label() if field == "pe" else cols["label"]} ({current_val} → {new_val})')
        db.commit()
    except Exception as exc:
        db.rollback()
        app.logger.warning('Falha ao registrar log de ajuste de ponto: %s', exc)

    return jsonify({'ok': True, 'field': field, 'current': new_val, 'max': max_val})


@app.route('/player/set_color', methods=['POST'])
@login_required
def player_set_color():
    color = request.form.get('color', '').strip()

    if HEX_COLOR_RE.match(color):
        db = get_db()
        db.execute('UPDATE users SET color = ? WHERE id = ?', (color, session['user_id']))
        db.commit()
        if _wants_json():
            return jsonify({'ok': True, 'color': color})
    elif _wants_json():
        return jsonify({'ok': False, 'error': 'Cor inválida.'}), 400

    return redirect(url_for('player_panel'))


@app.route('/player/set_state', methods=['POST'])
@login_required
def player_set_state():
    """O jogador altera apenas o estado do próprio personagem."""
    if session.get('is_admin'):
        if _wants_json():
            return jsonify({'ok': False, 'error': 'O mestre não tem personagem.'}), 403
        return redirect(url_for('admin_panel'))

    state = request.form.get('state', '').strip()
    if state not in NPC_STATE_COLORS:
        state = ''

    db = get_db()
    db.execute('UPDATE users SET state = ? WHERE id = ?', (state, session['user_id']))
    db.commit()

    if _wants_json():
        return jsonify({'ok': True, 'state': state, 'state_color': NPC_STATE_COLORS.get(state, '')})
    return redirect(url_for('player_panel'))


@app.route('/admin/set_user_state/<int:user_id>', methods=['POST'])
@admin_required
def admin_set_user_state(user_id):
    """O mestre define o estado de qualquer agente (jogador)."""
    state = request.form.get('state', '').strip()
    if state not in NPC_STATE_COLORS:
        state = ''

    db = get_db()
    target = db.execute('SELECT id FROM users WHERE id = ? AND is_admin = 0', (user_id,)).fetchone()
    if not target:
        if _wants_json():
            return jsonify({'ok': False, 'error': 'Agente não encontrado.'}), 404
        return redirect(url_for('admin_panel'))

    db.execute('UPDATE users SET state = ? WHERE id = ?', (state, user_id))
    db.commit()

    if _wants_json():
        return jsonify({'ok': True, 'state': state, 'state_color': NPC_STATE_COLORS.get(state, '')})
    return redirect(url_for('admin_panel'))


@app.route('/player/set_title', methods=['POST'])
@login_required
def player_set_title():
    title = request.form.get('character_title', '').strip()[:60]
    color = request.form.get('character_title_color', '').strip()

    if not HEX_COLOR_RE.match(color):
        color = '#f5f5f5'

    db = get_db()
    db.execute(
        'UPDATE users SET character_title = ?, character_title_color = ? WHERE id = ?',
        (title, color, session['user_id']),
    )
    db.commit()

    if _wants_json():
        return jsonify({'ok': True, 'title': title, 'title_color': color})

    return redirect(url_for('player_panel'))


@app.route('/player/set_origin', methods=['POST'])
@login_required
def player_set_origin():
    origin = request.form.get('character_origin', '').strip()

    if origin in CHARACTER_ORIGINS or origin == '':
        db = get_db()
        db.execute('UPDATE users SET character_origin = ? WHERE id = ?', (origin, session['user_id']))
        if origin:
            actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            _log_action(db, actor, f'definiu a origem "{origin}"')
        db.commit()
        if _wants_json():
            return jsonify({'ok': True, 'origin': origin})
    elif _wants_json():
        return jsonify({'ok': False, 'error': 'Origem inválida.'}), 400

    return redirect(url_for('player_panel'))


@app.route('/player/set_class', methods=['POST'])
@login_required
def player_set_class():
    char_class = request.form.get('character_class', '').strip()

    if char_class in CHARACTER_CLASSES or char_class == '':
        db = get_db()
        db.execute('UPDATE users SET character_class = ? WHERE id = ?', (char_class, session['user_id']))
        if char_class:
            actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            _log_action(db, actor, f'definiu a classe "{char_class}"')
        db.commit()
        if _wants_json():
            return jsonify({'ok': True, 'character_class': char_class})
    elif _wants_json():
        return jsonify({'ok': False, 'error': 'Classe inválida.'}), 400

    return redirect(url_for('player_panel'))


@app.route('/player/set_class_track', methods=['POST'])
@login_required
def player_set_class_track():
    track = request.form.get('character_class_track', '').strip()[:200]

    db = get_db()
    db.execute('UPDATE users SET character_class_track = ? WHERE id = ?', (track, session['user_id']))
    db.commit()
    if _wants_json():
        return jsonify({'ok': True, 'character_class_track': track})

    return redirect(url_for('player_panel'))


@app.route('/player/set_patente', methods=['POST'])
@login_required
def player_set_patente():
    patente = request.form.get('character_patente', '').strip()[:60]

    db = get_db()
    db.execute('UPDATE users SET character_patente = ? WHERE id = ?', (patente, session['user_id']))
    if patente:
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, f'definiu a patente "{patente}"')
    db.commit()
    if _wants_json():
        return jsonify({'ok': True, 'character_patente': patente})

    return redirect(url_for('player_panel'))


@app.route('/player/spend_money', methods=['POST'])
@login_required
def player_spend_money():
    try:
        amount = int(request.form.get('amount', 0))
    except (ValueError, TypeError):
        amount = 0
    description = request.form.get('description', '').strip()[:300]

    db = get_db()
    current = db.execute(
        'SELECT character_money FROM users WHERE id = ?', (session['user_id'],)
    ).fetchone()['character_money']

    if amount <= 0:
        return jsonify({'ok': False, 'error': 'Informe um valor válido.', 'money': current})

    if not description:
        return jsonify({'ok': False, 'error': 'Informe a descrição do gasto.', 'money': current})

    if amount > current:
        return jsonify({
            'ok': False,
            'error': 'Dinheiro insuficiente!',
            'money': current
        })

    db.execute(
        'UPDATE users SET character_money = character_money - ? WHERE id = ?',
        (amount, session['user_id'])
    )
    _log_money_transaction(db, session['user_id'], -amount, description)
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'gastou $ {amount} ({description})')
    db.commit()
    new_money = db.execute(
        'SELECT character_money FROM users WHERE id = ?', (session['user_id'],)
    ).fetchone()['character_money']
    transaction = db.execute(
        'SELECT * FROM money_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 1',
        (session['user_id'],)
    ).fetchone()

    return jsonify({'ok': True, 'money': new_money, 'transaction': dict(transaction) if transaction else None})


@app.route('/player/clear_money_transactions', methods=['POST'])
@login_required
def player_clear_money_transactions():
    db = get_db()
    db.execute('DELETE FROM money_transactions WHERE user_id = ?', (session['user_id'],))
    db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/clear_money_requests', methods=['POST'])
@login_required
def player_clear_money_requests():
    db = get_db()
    db.execute('DELETE FROM money_requests WHERE user_id = ?', (session['user_id'],))
    db.commit()

    return redirect(url_for('player_panel'))


@app.route('/player/request_money', methods=['POST'])
@login_required
def player_request_money():
    data = request.get_json(force=True) or {}

    try:
        amount = int(data.get('amount', 0))
    except (ValueError, TypeError):
        amount = 0
    reason = (data.get('reason') or '').strip()[:300]

    if amount <= 0:
        return jsonify({'ok': False, 'error': 'Informe uma quantidade válida.'})
    if not reason:
        return jsonify({'ok': False, 'error': 'Informe o motivo do pedido.'})

    db = get_db()
    cur = db.execute(
        'INSERT INTO money_requests (user_id, amount, reason) VALUES (?, ?, ?)',
        (session['user_id'], amount, reason)
    )
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'solicitou $ {amount} ao mestre ({reason})')
    db.commit()
    new_id = cur.lastrowid
    req_row = db.execute('SELECT * FROM money_requests WHERE id = ?', (new_id,)).fetchone()

    return jsonify({'ok': True, 'request': dict(req_row)})


@app.route('/player/set_nex', methods=['POST'])
@login_required
def player_set_nex():
    # O jogador não escolhe mais o valor do NEX livremente: cada clique só
    # avança um degrau (5% em 5%, até o teto de 99%). Baixar o NEX é uma
    # ação exclusiva do mestre/admin, feita em /admin/api/update_character.
    db = get_db()
    user = db.execute('SELECT character_nex FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    current = user['character_nex'] if user and user['character_nex'] else 0

    try:
        next_index = CHARACTER_NEX_OPTIONS.index(current) + 1
    except ValueError:
        next_index = 0

    if next_index >= len(CHARACTER_NEX_OPTIONS):
        if _wants_json():
            return jsonify({'ok': False, 'error': 'O NEX já está no máximo (99%).'}), 400
        return redirect(url_for('player_panel'))

    nex = CHARACTER_NEX_OPTIONS[next_index]
    db.execute('UPDATE users SET character_nex = ? WHERE id = ?', (nex, session['user_id']))
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'subiu o NEX para {nex}%')
    db.commit()

    if _wants_json():
        return jsonify({'ok': True, 'character_nex': nex})

    return redirect(url_for('player_panel'))


@app.route('/player/increase_attribute', methods=['POST'])
@login_required
def player_increase_attribute():
    # Regra geral "Aumento de Atributo": em NEX 20%, 50%, 80% e 95%, o
    # jogador ganha +1 em um atributo à sua escolha (não pode passar de
    # ATTRIBUTE_INCREASE_MAX por esse meio). Vale para qualquer classe.
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    def fail(msg, code=400):
        if _wants_json():
            return jsonify({'ok': False, 'error': msg}), code
        return redirect(url_for('player_panel'))

    if not user:
        return fail('Usuário não encontrado.', 404)

    data = request.get_json(silent=True) or request.form
    attr = (data.get('attribute') or '').strip().lower()
    if attr not in ATTRIBUTE_LABELS:
        return fail('Atributo inválido.')

    earned = _attribute_increases_earned(user['character_nex'])
    used = user['attribute_increases_used'] or 0
    remaining = earned - used
    if remaining <= 0:
        return fail('Você não tem nenhum Aumento de Atributo disponível no momento.')

    current_value = user[attr]
    if current_value >= ATTRIBUTE_INCREASE_MAX:
        return fail(f'{ATTRIBUTE_LABELS[attr]} já está no máximo (+{ATTRIBUTE_INCREASE_MAX}) por essa regra.')

    new_value = current_value + 1
    new_used = used + 1
    db.execute(
        f'UPDATE users SET {attr} = ?, attribute_increases_used = ? WHERE id = ?',
        (new_value, new_used, session['user_id'])
    )
    _log_action(db, user, f'usou Aumento de Atributo em {ATTRIBUTE_LABELS[attr]} (agora {new_value})')
    db.commit()

    if _wants_json():
        return jsonify({
            'ok': True,
            attr: new_value,
            'attribute_increases_used': new_used,
            'attribute_increases_remaining': earned - new_used,
        })
    return redirect(url_for('player_panel'))


@app.route('/player/set_element_affinity', methods=['POST'])
@login_required
def player_set_element_affinity():
    # Escolha de afinidade elemental: liberada a partir de 50% de NEX (o
    # pop-up aparece sozinho nesse momento), mas o jogador também pode abrir
    # o mesmo formulário depois, pelo botão ao lado do NEX, caso não tenha
    # escolhido na hora. Uma vez escolhida, a afinidade fica fixa — não há
    # como o próprio jogador trocar depois.
    db = get_db()
    user = db.execute(
        'SELECT character_nex, character_element_affinity FROM users WHERE id = ?',
        (session['user_id'],)
    ).fetchone()

    if user and user['character_element_affinity']:
        error = 'Você já escolheu sua afinidade elemental.'
        if _wants_json():
            return jsonify({'ok': False, 'error': error}), 400
        return redirect(url_for('player_panel'))

    if not user or (user['character_nex'] or 0) < ELEMENT_AFFINITY_NEX_THRESHOLD:
        error = f'A afinidade elemental só pode ser escolhida a partir de {ELEMENT_AFFINITY_NEX_THRESHOLD}% de NEX.'
        if _wants_json():
            return jsonify({'ok': False, 'error': error}), 400
        return redirect(url_for('player_panel'))

    data = request.get_json(silent=True) or request.form
    element = (data.get('element') or '').strip()
    if element not in AFFINITY_ELEMENTS:
        error = 'Elemento inválido.'
        if _wants_json():
            return jsonify({'ok': False, 'error': error}), 400
        return redirect(url_for('player_panel'))

    db.execute(
        'UPDATE users SET character_element_affinity = ? WHERE id = ?',
        (element, session['user_id'])
    )
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f'escolheu afinidade com o elemento {element}')
    db.commit()

    if _wants_json():
        return jsonify({'ok': True, 'character_element_affinity': element})

    return redirect(url_for('player_panel'))


@app.route('/admin/api/remove_element_affinity/<int:user_id>', methods=['POST'])
@admin_required
def admin_remove_element_affinity(user_id):
    # Permite ao mestre desfazer a escolha de afinidade elemental de um
    # jogador (por exemplo, em caso de erro ou mudança de decisão da mesa),
    # já que o próprio jogador não tem como alterar depois de escolhida.
    db = get_db()
    target = db.execute(
        'SELECT username, character_element_affinity FROM users WHERE id = ?',
        (user_id,)
    ).fetchone()

    if target is None:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    if not target['character_element_affinity']:
        return jsonify({'ok': False, 'error': 'Este agente ainda não possui afinidade elemental.'}), 400

    db.execute('UPDATE users SET character_element_affinity = ? WHERE id = ?', ('', user_id))
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, f"removeu a afinidade elemental de {target['username']}")
    db.commit()

    return jsonify({'ok': True})


@app.route('/agentes')
@login_required
def agentes_panel():
    db = get_db()
    users = db.execute('SELECT * FROM users ORDER BY is_admin DESC, username ASC').fetchall()
    users_data = [dict(u) for u in users]

    npcs = db.execute('SELECT * FROM npcs ORDER BY sort_order ASC, id ASC').fetchall()

    return render_template('agentes.html', users=users_data, npcs=npcs, npc_state_colors=NPC_STATE_COLORS, npc_state_icons=NPC_STATE_ICONS)


@app.route('/agentes/api/list')
@login_required
def api_agentes_list():
    db = get_db()
    users = db.execute('SELECT id, is_admin, is_online, color, state FROM users').fetchall()

    result = []
    for u in users:
        is_online = bool(u['is_online'])
        entry = {'id': u['id'], 'is_online': is_online, 'color': u['color']}
        if not u['is_admin']:
            entry['state'] = u['state'] or ''
            entry['state_color'] = NPC_STATE_COLORS.get(u['state'], '')
            entry['state_icon'] = NPC_STATE_ICONS.get(u['state'], '')
        result.append(entry)

    return jsonify({'users': result})


@app.route('/admin/api/set_online/<int:user_id>', methods=['POST'])
@admin_required
def api_set_online(user_id):
    data = request.get_json(force=True) or {}
    online = bool(data.get('online'))

    db = get_db()
    db.execute('UPDATE users SET is_online = ? WHERE id = ?', (1 if online else 0, user_id))
    db.commit()
    user = db.execute('SELECT id, is_online FROM users WHERE id = ?', (user_id,)).fetchone()

    if user is None:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    return jsonify({'ok': True, 'id': user['id'], 'is_online': bool(user['is_online'])})


@app.route('/player/api/set_online', methods=['POST'])
@login_required
def api_player_set_online():
    data = request.get_json(force=True) or {}
    online = bool(data.get('online'))

    db = get_db()
    db.execute('UPDATE users SET is_online = ? WHERE id = ?', (1 if online else 0, session['user_id']))
    db.commit()
    user = db.execute('SELECT id, is_online FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    return jsonify({'ok': True, 'id': user['id'], 'is_online': bool(user['is_online'])})


def _combat_participant_dict(db, p, viewer, is_admin_view, used_spaces_map, carga_bonus_map=None):
    """Monta o dicionário de exibição de um participante do combate (agente
    ou criatura), já incluindo a permissão do usuário atual (`viewer`) para
    rolar a iniciativa desse participante."""
    entry = {
        'id': p['id'],
        'kind': p['kind'],
        'initiative': p['initiative'],
    }
    entry['is_mine'] = bool(p['kind'] == 'agente' and p['user_id'] == viewer['id'])

    # Iniciativa oculta (só inimigos): o mestre sempre vê o valor; para os
    # jogadores o número nunca é enviado — só o aviso de que já foi rolada.
    entry['initiative_hidden'] = _initiative_is_hidden(p)
    entry['initiative_concealed'] = False
    if entry['initiative_hidden'] and not is_admin_view:
        entry['initiative_concealed'] = p['initiative'] is not None
        entry['initiative'] = None

    # Anotação privada do mestre: só é incluída na visão do mestre, nunca é
    # enviada para os jogadores, mesmo que o participante seja um agente.
    if is_admin_view:
        entry['admin_notes'] = p['admin_notes'] or ''

    if p['kind'] == 'agente':
        u = db.execute('SELECT * FROM users WHERE id = ?', (p['user_id'],)).fetchone()
        if not u:
            return None
        entry['user_id'] = u['id']
        entry['is_admin'] = bool(u['is_admin'])
        entry['name'] = display_name_or_username(u)
        entry['avatar'] = u['avatar']
        entry['color'] = u['color']
        entry['is_online'] = bool(u['is_online'])
        entry['can_roll'] = (not is_admin_view and viewer['id'] == u['id']) or is_admin_view
        if not u['is_admin']:
            entry['character_title'] = u['character_title']
            entry['character_title_color'] = u['character_title_color']
            entry['character_nex'] = u['character_nex'] or 0
            entry['state'] = u['state'] or ''
            entry['state_color'] = NPC_STATE_COLORS.get(u['state'], '')
            entry['state_icon'] = NPC_STATE_ICONS.get(u['state'], '')
            if is_admin_view or u['is_online']:
                if carga_bonus_map is None:
                    carga_bonus_map = _carga_bonus_map(db)
                overloaded = used_spaces_map.get(u['id'], 0) > _capacidade_max(u, carga_bonus_map.get(u['id'], 0))
                entry['pv'] = u['pv']
                entry['pv_max'] = u['pv_max']
                entry['pe'] = u['pe']
                entry['pe_max'] = u['pe_max']
                entry['sanidade'] = u['sanidade']
                entry['sanidade_max'] = u['sanidade_max']
                entry['defesa'] = _defesa_valor(u, overloaded)
    elif p['kind'] == 'npc':
        # NPC da Equipe de Suporte: nome, imagem, cor e estado vêm sempre do
        # cadastro do NPC, então qualquer edição do mestre reflete no combate.
        npc = db.execute('SELECT * FROM npcs WHERE id = ?', (p['npc_id'],)).fetchone()
        if not npc:
            return None
        entry['npc_id'] = npc['id']
        entry['name'] = npc['name']
        entry['avatar'] = npc['avatar']
        entry['description'] = npc['description']
        entry['color'] = npc['color']
        entry['state'] = npc['state'] or ''
        entry['state_color'] = NPC_STATE_COLORS.get(npc['state'], '')
        entry['state_icon'] = NPC_STATE_ICONS.get(npc['state'], '')
        entry['can_roll'] = is_admin_view
    else:
        entry['name'] = p['name']
        entry['avatar'] = p['avatar']
        entry['description'] = p['description']
        entry['color'] = p['color']
        entry['enemy_type'] = _normalize_enemy_type(p['enemy_type'])
        entry['enemy_type_label'] = ENEMY_TYPES[entry['enemy_type']]
        entry['challenge'] = p['challenge']
        entry['can_roll'] = is_admin_view

    return entry


def display_name_or_username(u):
    if u['is_admin']:
        return get_display_name(u)
    return u['character_name'] or u['username']


def _combat_participants_sorted(db):
    return db.execute(
        '''SELECT * FROM combat_participants
           ORDER BY (initiative IS NULL) ASC, initiative DESC, sort_order ASC, id ASC'''
    ).fetchall()


def _get_turn_state(db):
    """Estado atual dos turnos (cria a linha única se ainda não existir)."""
    row = db.execute('SELECT * FROM combat_state WHERE id = 1').fetchone()
    if row is None:
        db.execute('INSERT OR IGNORE INTO combat_state (id) VALUES (1)')
        db.commit()
        row = db.execute('SELECT * FROM combat_state WHERE id = 1').fetchone()
    return row


def _turn_order_ids(db):
    """Ordem dos turnos: maior iniciativa primeiro. Quem ainda não rolou
    iniciativa fica de fora até rolar."""
    return [p['id'] for p in _combat_participants_sorted(db) if p['initiative'] is not None]


def _turn_state_dict(db):
    state = _get_turn_state(db)
    return {
        'active': bool(state['active']),
        'round': state['round'],
        'current_id': state['current_id'] if state['active'] else None,
    }


def _turn_start(db):
    order = _turn_order_ids(db)
    if not order:
        return False
    db.execute('UPDATE combat_state SET active = 1, round = 1, current_id = ? WHERE id = 1', (order[0],))
    return True


def _turn_stop(db):
    db.execute('UPDATE combat_state SET active = 0, round = 0, current_id = NULL WHERE id = 1')


def _turn_next(db):
    """Passa a vez para o próximo da ordem; depois do último volta ao
    primeiro e a rodada aumenta em 1."""
    state = _get_turn_state(db)
    order = _turn_order_ids(db)
    if not state['active'] or not order:
        return
    rnd = state['round'] or 1
    if state['current_id'] in order:
        idx = order.index(state['current_id']) + 1
        if idx >= len(order):
            idx = 0
            rnd += 1
    else:
        idx = 0
    db.execute('UPDATE combat_state SET round = ?, current_id = ? WHERE id = 1', (rnd, order[idx]))


def _turn_prev(db):
    state = _get_turn_state(db)
    order = _turn_order_ids(db)
    if not state['active'] or not order:
        return
    rnd = state['round'] or 1
    if state['current_id'] in order:
        idx = order.index(state['current_id']) - 1
        if idx < 0:
            if rnd > 1:
                idx = len(order) - 1
                rnd -= 1
            else:
                idx = 0
    else:
        idx = 0
    db.execute('UPDATE combat_state SET round = ?, current_id = ? WHERE id = 1', (rnd, order[idx]))


@app.route('/combate')
@login_required
def combate_panel():
    db = get_db()
    viewer = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    is_admin_view = bool(session.get('is_admin'))
    used_spaces_map = _used_spaces_map(db)
    carga_bonus_map = _carga_bonus_map(db)

    participants = []
    for p in _combat_participants_sorted(db):
        entry = _combat_participant_dict(db, p, viewer, is_admin_view, used_spaces_map, carga_bonus_map)
        if entry:
            participants.append(entry)

    available_users = []
    if is_admin_view:
        in_combat_user_ids = {p['user_id'] for p in db.execute(
            "SELECT user_id FROM combat_participants WHERE kind = 'agente'"
        ).fetchall()}
        available_users = [
            u for u in db.execute(
                'SELECT id, username, character_name FROM users WHERE is_admin = 0 ORDER BY username ASC'
            ).fetchall()
            if u['id'] not in in_combat_user_ids
        ]

    available_npcs = []
    if is_admin_view:
        in_combat_npc_ids = {r['npc_id'] for r in db.execute(
            "SELECT npc_id FROM combat_participants WHERE kind = 'npc'"
        ).fetchall()}
        available_npcs = [
            n for n in db.execute(
                'SELECT id, name FROM npcs ORDER BY sort_order ASC, id ASC'
            ).fetchall()
            if n['id'] not in in_combat_npc_ids
        ]

    return render_template(
        'combate.html',
        participants=participants,
        available_users=available_users,
        available_npcs=available_npcs,
        has_npcs=bool(db.execute('SELECT 1 FROM npcs LIMIT 1').fetchone()) if is_admin_view else False,
        enemy_types=ENEMY_TYPES,
        ritual_elements=RITUAL_ELEMENTS,
        ritual_circle_costs=RITUAL_CIRCLE_PE_COSTS,
        ritual_element_icons=RITUAL_ELEMENT_ICONS,
    )


@app.route('/combate/api/list')
@login_required
def api_combate_list():
    db = get_db()
    viewer = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    is_admin_view = bool(session.get('is_admin'))
    used_spaces_map = _used_spaces_map(db)
    carga_bonus_map = _carga_bonus_map(db)

    participants = []
    for p in _combat_participants_sorted(db):
        entry = _combat_participant_dict(db, p, viewer, is_admin_view, used_spaces_map, carga_bonus_map)
        if entry:
            participants.append(entry)

    return jsonify({'participants': participants, 'turn': _turn_state_dict(db)})


@app.route('/admin/combate/turn/<action>', methods=['POST'])
@admin_required
def api_combate_turn_admin(action):
    """Controles de turno do mestre: iniciar, próximo, anterior e parar."""
    db = get_db()
    if action == 'start':
        if not _turn_start(db):
            return jsonify({
                'ok': False,
                'error': 'Role a iniciativa de pelo menos um participante antes de iniciar os turnos.'
            }), 400
    elif action == 'next':
        _turn_next(db)
    elif action == 'prev':
        _turn_prev(db)
    elif action == 'stop':
        _turn_stop(db)
    else:
        return jsonify({'ok': False, 'error': 'Ação de turno inválida.'}), 400
    db.commit()
    return jsonify({'ok': True, 'turn': _turn_state_dict(db)})


@app.route('/combate/api/turn/end', methods=['POST'])
@login_required
def api_combate_turn_end():
    """Encerrar o próprio turno: o mestre pode sempre; o jogador só
    quando é a vez do seu personagem."""
    db = get_db()
    state = _get_turn_state(db)
    if not state['active']:
        return jsonify({'ok': False, 'error': 'Os turnos não estão ativos.'}), 400
    if not session.get('is_admin'):
        cur = db.execute(
            "SELECT user_id FROM combat_participants WHERE id = ? AND kind = 'agente'",
            (state['current_id'],)
        ).fetchone()
        if not cur or cur['user_id'] != session['user_id']:
            return jsonify({'ok': False, 'error': 'Não é a sua vez.'}), 403
    _turn_next(db)
    db.commit()
    return jsonify({'ok': True, 'turn': _turn_state_dict(db)})


@app.route('/admin/combate/add_agente', methods=['POST'])
@admin_required
def combate_add_agente():
    db = get_db()
    try:
        user_id = int(request.form.get('user_id', ''))
    except (TypeError, ValueError):
        return redirect(url_for('combate_panel'))

    user = db.execute('SELECT id FROM users WHERE id = ? AND is_admin = 0', (user_id,)).fetchone()
    already_in = db.execute(
        "SELECT 1 FROM combat_participants WHERE kind = 'agente' AND user_id = ?", (user_id,)
    ).fetchone()

    if user and not already_in:
        next_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM combat_participants').fetchone()[0]
        db.execute(
            "INSERT INTO combat_participants (kind, user_id, sort_order) VALUES ('agente', ?, ?)",
            (user_id, next_order)
        )
        db.commit()

    return redirect(url_for('combate_panel'))


@app.route('/admin/combate/add_npc', methods=['POST'])
@admin_required
def combate_add_npc():
    """Coloca um NPC da Equipe de Suporte no combate."""
    db = get_db()
    try:
        npc_id = int(request.form.get('npc_id', ''))
    except (TypeError, ValueError):
        return redirect(url_for('combate_panel'))
    try:
        initiative_bonus = int(request.form.get('initiative_bonus', 0) or 0)
    except (TypeError, ValueError):
        initiative_bonus = 0

    initiative_hidden = 1 if request.form.get('hide_initiative') else 0

    npc = db.execute('SELECT * FROM npcs WHERE id = ?', (npc_id,)).fetchone()
    already_in = db.execute(
        "SELECT 1 FROM combat_participants WHERE kind = 'npc' AND npc_id = ?", (npc_id,)
    ).fetchone()

    if npc and not already_in:
        _ensure_creature_columns(db)
        next_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM combat_participants').fetchone()[0]
        db.execute(
            '''INSERT INTO combat_participants (kind, npc_id, name, initiative_bonus, initiative_hidden, sort_order)
               VALUES ('npc', ?, ?, ?, ?, ?)''',
            (npc_id, npc['name'], initiative_bonus, initiative_hidden, next_order)
        )
        db.commit()

    return redirect(url_for('combate_panel'))


@app.route('/admin/combate/add_criatura', methods=['POST'])
@admin_required
def combate_add_criatura():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()[:2000]
    color = request.form.get('color', '').strip()
    if not HEX_COLOR_RE.match(color):
        color = '#c62f27'
    enemy_type = _normalize_enemy_type(request.form.get('enemy_type'))
    initiative_dice = _normalize_initiative_dice(request.form.get('initiative_dice'))
    challenge = _parse_challenge(request.form.get('challenge'))
    initiative_hidden = 1 if request.form.get('hide_initiative') else 0

    if name:
        filename = save_uploaded_image(request.files.get('avatar'), COMBAT_IMAGES_DIR, max_size=COMBAT_AVATAR_MAX_SIZE)
        db = get_db()
        _ensure_creature_columns(db)
        next_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM combat_participants').fetchone()[0]
        db.execute(
            '''INSERT INTO combat_participants (kind, name, avatar, description, color, initiative_dice, challenge, sort_order, enemy_type, initiative_hidden)
               VALUES ('criatura', ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (name, filename, description, color, initiative_dice, challenge, next_order, enemy_type, initiative_hidden)
        )
        db.commit()

    return redirect(url_for('combate_panel'))


@app.route('/admin/combate/api/criatura/<int:participant_id>')
@admin_required
def api_combate_get_criatura(participant_id):
    """Dados de uma criatura do combate para preencher o painel de edição."""
    db = get_db()
    p = db.execute(
        "SELECT * FROM combat_participants WHERE id = ? AND kind = 'criatura'", (participant_id,)
    ).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'Criatura não encontrada'}), 404
    return jsonify({'ok': True, 'participant': dict(p)})


@app.route('/admin/combate/initiative_visibility/<int:participant_id>', methods=['POST'])
@admin_required
def api_combate_initiative_visibility(participant_id):
    """Mestre revela ou oculta a iniciativa de um inimigo (criatura ou NPC)
    para os jogadores. Aceita {"hidden": true/false}; sem corpo, alterna."""
    db = get_db()
    _ensure_creature_columns(db)
    p = db.execute(
        "SELECT * FROM combat_participants WHERE id = ? AND kind IN ('criatura', 'npc')",
        (participant_id,)
    ).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'Inimigo não encontrado no combate.'}), 404

    data = request.get_json(silent=True) or {}
    if 'hidden' in data:
        hidden = 1 if data.get('hidden') else 0
    else:
        hidden = 0 if p['initiative_hidden'] else 1

    db.execute('UPDATE combat_participants SET initiative_hidden = ? WHERE id = ?', (hidden, participant_id))
    db.commit()
    return jsonify({'ok': True, 'id': participant_id, 'initiative_hidden': bool(hidden)})


@app.route('/admin/combate/edit_criatura/<int:participant_id>', methods=['POST'])
@admin_required
def combate_edit_criatura(participant_id):
    """Edita nome, descrição, cor, dados de iniciativa e (opcionalmente) o
    avatar de uma criatura já adicionada ao combate."""
    db = get_db()
    _ensure_creature_columns(db)
    p = db.execute(
        "SELECT * FROM combat_participants WHERE id = ? AND kind = 'criatura'", (participant_id,)
    ).fetchone()
    if not p:
        return redirect(url_for('combate_panel'))

    name = request.form.get('name', '').strip()
    if not name:
        return redirect(url_for('combate_panel'))
    description = request.form.get('description', '').strip()[:2000]
    color = request.form.get('color', '').strip()
    if not HEX_COLOR_RE.match(color):
        color = p['color'] or '#c62f27'
    enemy_type = _normalize_enemy_type(request.form.get('enemy_type') or p['enemy_type'])
    initiative_dice = _normalize_initiative_dice(request.form.get('initiative_dice'))
    challenge = _parse_challenge(request.form.get('challenge'))
    initiative_hidden = 1 if request.form.get('hide_initiative') else 0

    avatar = p['avatar']
    new_file = request.files.get('avatar')
    if new_file and new_file.filename:
        filename = save_uploaded_image(new_file, COMBAT_IMAGES_DIR, max_size=COMBAT_AVATAR_MAX_SIZE)
        if filename:
            if avatar:
                old_path = os.path.join(COMBAT_IMAGES_DIR, avatar)
                if os.path.exists(old_path):
                    os.remove(old_path)
            avatar = filename

    db.execute(
        '''UPDATE combat_participants
           SET name = ?, description = ?, color = ?, initiative_dice = ?, initiative_bonus = 0,
               challenge = ?, avatar = ?, enemy_type = ?, initiative_hidden = ?
           WHERE id = ?''',
        (name, description, color, initiative_dice, challenge, avatar, enemy_type, initiative_hidden, participant_id)
    )
    db.commit()

    return redirect(url_for('combate_panel'))


@app.route('/admin/combate/api/agente/<int:participant_id>/rituais')
@admin_required
def api_combate_agente_rituais(participant_id):
    """Lista os rituais do agente que está no combate, para o mestre editar."""
    db = get_db()
    p = db.execute(
        "SELECT * FROM combat_participants WHERE id = ? AND kind = 'agente'", (participant_id,)
    ).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'Agente não encontrado no combate.'}), 404
    rituals = db.execute(
        'SELECT id, name, element, circle, pe_cost_normal, symbol FROM rituals WHERE user_id = ? ORDER BY sort_order ASC, id ASC',
        (p['user_id'],)
    ).fetchall()
    return jsonify({'ok': True, 'rituals': [dict(r) for r in rituals]})


@app.route('/admin/combate/api/agente/<int:participant_id>/inventario')
@admin_required
def api_combate_agente_inventario(participant_id):
    """Lista os itens do agente que está no combate, para o mestre consultar."""
    db = get_db()
    p = db.execute(
        "SELECT * FROM combat_participants WHERE id = ? AND kind = 'agente'", (participant_id,)
    ).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'Agente não encontrado no combate.'}), 404
    items = db.execute(
        'SELECT id, name, image, item_type, level, spaces, quantity FROM items WHERE user_id = ? ORDER BY sort_order ASC, id ASC',
        (p['user_id'],)
    ).fetchall()
    return jsonify({'ok': True, 'items': [dict(i) for i in items]})


@app.route('/admin/api/ritual/<int:ritual_id>')
@admin_required
def admin_api_get_ritual(ritual_id):
    """Dados de um ritual de qualquer agente (o endpoint do jogador só
    devolve rituais do próprio usuário logado)."""
    db = get_db()
    ritual = db.execute('SELECT * FROM rituals WHERE id = ?', (ritual_id,)).fetchone()
    if ritual is None:
        return jsonify({'ok': False, 'error': 'Ritual não encontrado.'}), 404
    return jsonify({'ok': True, 'ritual': dict(ritual)})


@app.route('/admin/edit_ritual/<int:ritual_id>', methods=['POST'])
@admin_required
def admin_edit_ritual(ritual_id):
    """Salva a edição de um ritual feita pelo mestre e responde em JSON."""
    ok = _update_ritual(ritual_id, None, request.form, request.files)
    if not ok:
        return jsonify({
            'ok': False,
            'error': 'Não foi possível salvar: confira nome, elemento e círculo do ritual.'
        }), 400
    return jsonify({'ok': True})


@app.route('/admin/combate/notes/<int:participant_id>', methods=['POST'])
@admin_required
def combate_set_notes(participant_id):
    """Salva a anotação privada do mestre sobre um participante do combate
    (agente ou criatura) — visível apenas para o mestre."""
    db = get_db()
    notes = request.form.get('notes', '')[:2000]
    db.execute(
        'UPDATE combat_participants SET admin_notes = ? WHERE id = ?', (notes, participant_id)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/admin/combate/remove/<int:participant_id>', methods=['POST'])
@admin_required
def combate_remove_participant(participant_id):
    db = get_db()
    p = db.execute('SELECT * FROM combat_participants WHERE id = ?', (participant_id,)).fetchone()
    state = _get_turn_state(db)
    if state['active'] and state['current_id'] == participant_id:
        # Removeu quem estava na vez: passa a vez para o próximo antes de apagar.
        _turn_next(db)
    db.execute('DELETE FROM combat_participants WHERE id = ?', (participant_id,))
    if not _turn_order_ids(db):
        _turn_stop(db)
    db.commit()

    if p and p['kind'] == 'criatura' and p['avatar']:
        path = os.path.join(COMBAT_IMAGES_DIR, p['avatar'])
        if os.path.exists(path):
            os.remove(path)

    return redirect(url_for('combate_panel'))


@app.route('/admin/combate/end', methods=['POST'])
@admin_required
def combate_end():
    db = get_db()
    creatures = db.execute("SELECT avatar FROM combat_participants WHERE kind = 'criatura' AND avatar IS NOT NULL").fetchall()
    db.execute('DELETE FROM combat_participants')
    _turn_stop(db)
    db.commit()

    for c in creatures:
        path = os.path.join(COMBAT_IMAGES_DIR, c['avatar'])
        if os.path.exists(path):
            os.remove(path)

    return redirect(url_for('combate_panel'))


@app.route('/combate/api/roll/<int:participant_id>', methods=['POST'])
@login_required
def api_combate_roll(participant_id):
    db = get_db()
    p = db.execute('SELECT * FROM combat_participants WHERE id = ?', (participant_id,)).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'Participante não encontrado'}), 404
    if p['initiative'] is not None:
        return jsonify({'ok': False, 'error': 'Iniciativa já foi rolada'}), 400

    is_admin_view = bool(session.get('is_admin'))

    if p['kind'] == 'agente':
        if not (is_admin_view or session['user_id'] == p['user_id']):
            return jsonify({'ok': False, 'error': 'Sem permissão'}), 403
        user = db.execute('SELECT * FROM users WHERE id = ?', (p['user_id'],)).fetchone()
        if not user:
            return jsonify({'ok': False, 'error': 'Agente não encontrado'}), 404
        total = _roll_iniciativa_for_user(db, user)
    else:
        if not is_admin_view:
            return jsonify({'ok': False, 'error': 'Sem permissão'}), 403
        admin_user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        total = _roll_iniciativa_for_criatura(db, admin_user, p)

    db.execute('UPDATE combat_participants SET initiative = ? WHERE id = ?', (total, participant_id))
    db.commit()

    return jsonify({'ok': True, 'id': participant_id, 'initiative': total})


@app.route('/combate/api/roll_all', methods=['POST'])
@admin_required
def api_combate_roll_all():
    """Rola de uma vez a iniciativa de todo participante (agente ou
    criatura) que ainda não rolou, na ordem em que foram adicionados."""
    db = get_db()
    admin_user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    pending = db.execute(
        "SELECT * FROM combat_participants WHERE initiative IS NULL ORDER BY sort_order ASC, id ASC"
    ).fetchall()

    rolled = []
    for p in pending:
        if p['kind'] == 'agente':
            user = db.execute('SELECT * FROM users WHERE id = ?', (p['user_id'],)).fetchone()
            if not user:
                continue
            total = _roll_iniciativa_for_user(db, user)
        else:
            total = _roll_iniciativa_for_criatura(db, admin_user, p)

        db.execute('UPDATE combat_participants SET initiative = ? WHERE id = ?', (total, p['id']))
        rolled.append({'id': p['id'], 'initiative': total})

    db.commit()

    return jsonify({'ok': True, 'rolled': rolled})


@app.route('/campanha')
@login_required
def campanha_panel():
    db = get_db()
    campaign = db.execute('SELECT * FROM campaign WHERE id = 1').fetchone()
    return render_template('campanha.html', campaign=campaign)


@app.route('/campanha/api/status')
@login_required
def api_campanha_status():
    db = get_db()
    campaign = db.execute('SELECT * FROM campaign WHERE id = 1').fetchone()
    return jsonify(dict(campaign))


@app.route('/admin/campanha/update', methods=['POST'])
@admin_required
def campanha_update():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    db = get_db()
    old = db.execute('SELECT banner FROM campaign WHERE id = 1').fetchone()

    banner_filename = save_uploaded_image(request.files.get('banner'), CAMPAIGN_BANNERS_DIR, max_size=CAMPAIGN_BANNER_MAX_SIZE)

    if banner_filename:
        db.execute(
            'UPDATE campaign SET name = ?, description = ?, banner = ? WHERE id = 1',
            (name, description, banner_filename)
        )
    else:
        db.execute(
            'UPDATE campaign SET name = ?, description = ? WHERE id = 1',
            (name, description)
        )
    db.commit()

    if banner_filename and old and old['banner']:
        old_path = os.path.join(CAMPAIGN_BANNERS_DIR, old['banner'])
        if os.path.exists(old_path):
            os.remove(old_path)

    return redirect(url_for('campanha_panel'))


@app.route('/admin/campanha/options', methods=['POST'])
@admin_required
def campanha_options():
    financeiro_visible = 1 if request.form.get('financeiro_visible') else 0
    sem_sanidade = 1 if request.form.get('sem_sanidade') else 0

    db = get_db()
    old = db.execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()

    db.execute(
        'UPDATE campaign SET financeiro_visible = ?, sem_sanidade = ? WHERE id = 1',
        (financeiro_visible, sem_sanidade)
    )
    db.commit()

    was_on = bool(old and old['sem_sanidade'])
    if sem_sanidade and not was_on:
        _apply_pd_rules(db)
    elif was_on and not sem_sanidade:
        _restore_pe_from_backup(db)

    return redirect(url_for('campanha_panel'))


@app.route('/admin/campanha/remove_banner', methods=['POST'])
@admin_required
def campanha_remove_banner():
    db = get_db()
    old = db.execute('SELECT banner FROM campaign WHERE id = 1').fetchone()
    db.execute('UPDATE campaign SET banner = NULL WHERE id = 1')
    db.commit()

    if old and old['banner']:
        old_path = os.path.join(CAMPAIGN_BANNERS_DIR, old['banner'])
        if os.path.exists(old_path):
            os.remove(old_path)

    return redirect(url_for('campanha_panel'))


CHAT_SELECT = '''
    SELECT chat_messages.*, chat_messages.color AS user_color, users.avatar AS user_avatar
    FROM chat_messages
    LEFT JOIN users ON chat_messages.user_id = users.id
'''


@app.route('/chat')
@login_required
def chat_panel():
    # O chat agora fica dentro da aba Combate. A rota antiga continua
    # existindo só para links e favoritos antigos não quebrarem.
    return redirect(url_for('combate_panel'))


def _mark_chat_read(db, user_id):
    """Marca o chat como lido até a última mensagem existente, para o
    usuário atual. Chamado sempre que sabemos que ele está de fato vendo
    o chat em tempo real: ao abrir a aba, a cada poll de novas mensagens
    e ao enviar uma mensagem."""
    # Só escreve no banco quando há algo novo para marcar como lido. Antes,
    # cada poll do chat (a cada 3s por aba aberta) fazia UPDATE + COMMIT
    # mesmo sem nenhuma mensagem nova, disputando o lock de escrita do SQLite.
    row = db.execute(
        '''SELECT (SELECT COALESCE(MAX(id), 0) FROM chat_messages) AS last_id,
                  last_read_chat_id
           FROM users WHERE id = ?''',
        (user_id,)
    ).fetchone()
    if row is None or row['last_read_chat_id'] == row['last_id']:
        return
    db.execute(
        '''UPDATE users SET last_read_chat_id = (SELECT COALESCE(MAX(id), 0) FROM chat_messages)
           WHERE id = ?''',
        (user_id,)
    )
    db.commit()


@app.route('/chat/api/unread')
@login_required
def api_chat_unread():
    db = get_db()
    row = db.execute(
        '''SELECT COUNT(*) AS unread FROM chat_messages
           WHERE id > (SELECT last_read_chat_id FROM users WHERE id = ?)''',
        (session['user_id'],)
    ).fetchone()
    campaign = db.execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()
    return jsonify({
        'unread': row['unread'],
        # Permite que qualquer aba aberta ligue/desligue o modo sem Sanidade ao vivo.
        'sem_sanidade': bool(campaign and campaign['sem_sanidade']),
    })


@app.route('/chat/api/list')
@login_required
def api_chat_list():
    after_id = request.args.get('after_id', 0, type=int)

    db = get_db()
    if after_id:
        messages = db.execute(
            CHAT_SELECT + ' WHERE chat_messages.id > ? ORDER BY chat_messages.id ASC', (after_id,)
        ).fetchall()
    else:
        messages = db.execute(
            CHAT_SELECT + ' ORDER BY chat_messages.id ASC LIMIT 200'
        ).fetchall()

    # peek=1: o chat está recolhido, então só espia as mensagens novas sem
    # marcá-las como lidas (assim a bolinha de não lidas aparece no chip).
    if not request.args.get('peek'):
        _mark_chat_read(db, session['user_id'])
    return jsonify({'messages': [dict(m) for m in messages]})


DICE_COMMAND_RE = re.compile(r'^/(\d*d\d+(?:[+-]\d*d?\d+)*)$', re.IGNORECASE)


def _parse_dice_command(text):
    """Reconhece comandos de rolagem digitados no chat, tipo '/d20', '/d8',
       '/2d6+3'. Retorna a expressão de dados (sem a barra) ou None se o
       texto não for um comando de rolagem."""
    raw = (text or '').strip()
    m = DICE_COMMAND_RE.fullmatch(raw)
    if not m:
        return None
    return m.group(1)


BEST_DICE_COMMAND_RE = re.compile(r'^/m(\d+)d(\d+)$', re.IGNORECASE)


def _parse_best_dice_command(text):
    """Reconhece o comando '/m<quantidade>d<lados>' (ex: '/m3d20'), que rola
       vários dados e usa o melhor resultado. Retorna (quantidade, lados) ou
       None se o texto não for esse comando. Usa os mesmos limites do
       '/d20' comum: 1 a 50 dados e 2 a 1000 lados."""
    m = BEST_DICE_COMMAND_RE.fullmatch((text or '').strip())
    if not m:
        return None
    count = max(1, min(int(m.group(1)), 50))
    sides = max(2, min(int(m.group(2)), 1000))
    return count, sides


def _chat_create_message(db, user, text):
    """Cria uma mensagem de chat (texto ou comando de rolagem /d20, /2d6+3,
    /m3d20...) em nome de `user`. Usada pelo envio HTTP e pelo WebSocket.
    Devolve (payload, status_http); em caso de sucesso, payload['message']
    é a mensagem pronta para o cliente."""
    text = (text or '').strip()
    if not text:
        return {'ok': False, 'error': 'Mensagem vazia'}, 400
    if len(text) > 1000:
        text = text[:1000]

    best_dice = _parse_best_dice_command(text)
    dice_expr = _parse_dice_command(text)
    if best_dice is not None:
        count, sides = best_dice
        rolls = [random.randint(1, sides) for _ in range(count)]
        best = max(rolls)
        content = f'rolou /m{count}d{sides}: {count}d{sides} (melhor) [{", ".join(str(r) for r in rolls)}] = {best}'
        if sides == 20 and best == 20:
            content += ' — 20 NATURAL!'
        kind = 'roll'
    elif dice_expr is not None:
        result = _roll_dice_expression(dice_expr)
        if result is None:
            return {'ok': False, 'error': 'Expressão de dado inválida'}, 400
        total, detail = result
        content = f'rolou /{dice_expr}: {detail} = {total}'
        kind = 'roll'
    else:
        content = text
        kind = 'text'

    cur = db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (user['id'], get_display_name(user), '' if user['is_admin'] else user['character_name'], user['is_admin'], kind, content, user['color'])
    )
    new_id = cur.lastrowid

    # Só os comandos de rolagem digitados no chat (/d20, /2d6+3, /m3d20...)
    # disparam o dado no portrait do OBS; o mestre não tem portrait.
    if kind == 'roll' and not user['is_admin']:
        # 'label' = o comando usado (ex.: /d20, /m3d20, /2d6+3), mostrado abaixo da foto
        label = f'/m{count}d{sides}' if best_dice is not None else f'/{dice_expr}'
        db.execute('INSERT INTO portrait_dice (user_id, total, label) VALUES (?, ?, ?)',
                   (user['id'], best if best_dice is not None else total, label.lower()))
        db.execute("DELETE FROM portrait_dice WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')")
    db.commit()
    message = db.execute(CHAT_SELECT + ' WHERE chat_messages.id = ?', (new_id,)).fetchone()
    _mark_chat_read(db, user['id'])

    return {'ok': True, 'message': dict(message)}, 200


@app.route('/chat/api/send', methods=['POST'])
@login_required
def api_chat_send():
    data = request.get_json(force=True) or {}
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404
    payload, status = _chat_create_message(db, user, data.get('message'))
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Chat em tempo real (WebSocket)
# - Cada aba aberta mantém uma conexão em /chat/ws. Quando uma mensagem nova
#   entra no banco (por QUALQUER rota: chat, rolagens da ficha, rituais...),
#   o servidor empurra a mensagem para todas as conexões na hora.
# - Sem flask-sock instalado, ou se o servidor não aceitar WebSocket (ex.:
#   waitress), o navegador cai sozinho no polling antigo (/chat/api/list).
# - As conexões ficam na memória do processo: rode com UM processo só
#   (python app.py, ou gunicorn com --workers 1 --threads N).
# ---------------------------------------------------------------------------
class _ChatClient:
    def __init__(self, ws, user_id):
        self.ws = ws
        self.user_id = user_id
        self.outbox = queue.Queue(maxsize=200)
        self.alive = True

    def push(self, text):
        try:
            self.outbox.put_nowait(text)
        except queue.Full:  # cliente travado: derruba a conexão em vez de acumular
            self.alive = False
            try:
                self.ws.close()
            except Exception:
                pass

    def run_sender(self):
        """Roda numa thread própria: só ela escreve no socket."""
        try:
            while self.alive:
                item = self.outbox.get()
                if item is None:
                    break
                self.ws.send(item)
        except Exception:
            pass
        finally:
            self.alive = False
            try:
                self.ws.close()
            except Exception:
                pass


class ChatHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = set()
        self._last_id = None  # último id de mensagem já distribuído

    def ensure_initialized(self, db):
        """Marca o ponto de partida (última mensagem existente) antes de
        qualquer cliente conectar, para não retransmitir o histórico."""
        with self._lock:
            if self._last_id is None:
                self._last_id = db.execute(
                    'SELECT COALESCE(MAX(id), 0) AS m FROM chat_messages').fetchone()['m']

    def add(self, client):
        with self._lock:
            self._clients.add(client)

    def remove(self, client):
        with self._lock:
            self._clients.discard(client)
        client.alive = False
        try:
            client.outbox.put_nowait(None)
        except queue.Full:
            pass

    def broadcast(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.push(text)

    def flush_new_messages(self, db):
        """Distribui as mensagens inseridas desde a última chamada. Chamado
        depois de toda requisição que escreve (after_request): assim qualquer
        rota que insira no chat é transmitida sem precisar de código extra."""
        with self._lock:
            row = db.execute('SELECT COALESCE(MAX(id), 0) AS m FROM chat_messages').fetchone()
            max_id = row['m']
            if self._last_id is None:
                self._last_id = max_id  # primeira vez: não reenvia o histórico
                return
            if max_id <= self._last_id:
                self._last_id = max_id
                return
            rows = db.execute(
                CHAT_SELECT + ' WHERE chat_messages.id > ? ORDER BY chat_messages.id ASC',
                (self._last_id,)
            ).fetchall()
            self._last_id = max_id
            clients = list(self._clients)
        for r in rows:
            text = json.dumps({'type': 'message', 'message': dict(r)}, ensure_ascii=False)
            for c in clients:
                c.push(text)


chat_hub = ChatHub()


@app.after_request
def _broadcast_chat(resp):
    try:
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and resp.status_code < 400 \
                and not (request.path or '').startswith('/static/'):
            chat_hub.flush_new_messages(get_db())
    except Exception:
        pass  # transmitir ao vivo nunca pode derrubar a requisição
    return resp


def _ws_origin_ok():
    """Bloqueia WebSocket aberto por outro site (o cookie de sessão seguiria
    junto). Sem cabeçalho Origin (clientes que não são navegador) passa."""
    origin = request.headers.get('Origin')
    if not origin:
        return True
    return urlparse(origin).netloc == request.host


if Sock is not None:
    sock = Sock(app)
    app.config.setdefault('SOCK_SERVER_OPTIONS', {'ping_interval': 25})

    @sock.route('/chat/ws')
    def chat_ws(ws):
        user_id = session.get('user_id')
        if not user_id or not _ws_origin_ok():
            ws.close(reason=1008, message='não autorizado')
            return
        # Conexão própria (curta) para cada operação: a do get_db() ficaria
        # presa à thread durante toda a vida do WebSocket.
        def with_db(fn):
            db = _connect_db()
            try:
                return fn(db)
            finally:
                db.close()

        if not with_db(lambda db: db.execute('SELECT 1 FROM users WHERE id = ?', (user_id,)).fetchone()):
            ws.close(reason=1008, message='não autorizado')
            return

        with_db(chat_hub.ensure_initialized)
        client = _ChatClient(ws, user_id)
        chat_hub.add(client)
        threading.Thread(target=client.run_sender, daemon=True).start()
        try:
            while client.alive:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = data.get('type') if isinstance(data, dict) else None
                if kind == 'send':
                    def do_send(db):
                        user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
                        if not user:
                            return {'ok': False, 'error': 'Usuário não encontrado'}, 404
                        return _chat_create_message(db, user, data.get('message'))
                    payload, _status = with_db(do_send)
                    if payload.get('ok'):
                        # A mensagem chega para todos (inclusive quem enviou)
                        # pelo flush; só garantimos a entrega imediata aqui.
                        with_db(chat_hub.flush_new_messages)
                    else:
                        client.push(json.dumps({'type': 'error', 'error': payload.get('error', 'Erro')},
                                               ensure_ascii=False))
                elif kind == 'read':
                    with_db(lambda db: _mark_chat_read(db, user_id))
        except Exception:
            pass  # ConnectionClosed e afins: só encerra
        finally:
            chat_hub.remove(client)


@app.route('/admin/api/chat/delete/<int:msg_id>', methods=['POST'])
@admin_required
def api_chat_delete(msg_id):
    db = get_db()
    db.execute('DELETE FROM chat_messages WHERE id = ?', (msg_id,))
    db.commit()
    chat_hub.broadcast({'type': 'delete', 'id': msg_id})
    return jsonify({'ok': True, 'id': msg_id})


@app.route('/admin/api/chat/clear', methods=['POST'])
@admin_required
def api_chat_clear():
    db = get_db()
    db.execute('DELETE FROM chat_messages')
    db.commit()
    chat_hub.broadcast({'type': 'clear'})
    return jsonify({'ok': True})


@app.route('/mural')
@login_required
def mural_panel():
    db = get_db()
    mural_images = db.execute('SELECT * FROM mural_images ORDER BY id DESC').fetchall()
    return render_template('mural.html', mural_images=mural_images, mural_categories=MURAL_CATEGORIES)


@app.route('/mural/add', methods=['POST'])
@login_required
def mural_add():
    caption = request.form.get('caption', '').strip()
    category = request.form.get('category', '').strip()
    if category not in MURAL_CATEGORIES:
        category = 'Outro'
    filename = save_uploaded_image(request.files.get('image'), MURAL_IMAGES_DIR, max_size=MURAL_IMAGE_MAX_SIZE)

    if filename:
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        db.execute(
            'INSERT INTO mural_images (image, caption, category, user_id, username) VALUES (?, ?, ?, ?, ?)',
            (filename, caption, category, session['user_id'], get_display_name(user))
        )
        _log_action(db, user, f'publicou uma imagem no mural ({category})' + (f': "{caption}"' if caption else ''))
        db.commit()

    return redirect(url_for('mural_panel'))


@app.route('/mural/delete/<int:image_id>', methods=['POST'])
@login_required
def mural_delete(image_id):
    db = get_db()
    old = db.execute('SELECT image, user_id, caption FROM mural_images WHERE id = ?', (image_id,)).fetchone()

    if not old:
        return redirect(url_for('mural_panel'))

    is_admin = bool(session.get('is_admin'))
    is_owner = old['user_id'] is not None and old['user_id'] == session.get('user_id')
    if not is_admin and not is_owner:
        return redirect(url_for('mural_panel'))

    db.execute('DELETE FROM mural_images WHERE id = ?', (image_id,))
    if is_owner and not is_admin:
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, 'removeu uma imagem do mural' + (f': "{old["caption"]}"' if old['caption'] else ''))
    db.commit()

    if old and old['image']:
        old_path = os.path.join(MURAL_IMAGES_DIR, old['image'])
        if os.path.exists(old_path):
            os.remove(old_path)

    return redirect(url_for('mural_panel'))


@app.route('/player/set_character', methods=['POST'])
@login_required
def player_set_character():
    db = get_db()
    user = db.execute('SELECT character_locked FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if user and not user['character_locked']:
        name = request.form.get('character_name', '').strip()
        history = request.form.get('character_history', '').strip()
        physical = request.form.get('character_physical', '').strip()
        description = request.form.get('character_description', '').strip()

        db.execute(
            '''UPDATE users
               SET character_name = ?, character_history = ?, character_physical = ?,
                   character_description = ?, character_locked = 1
               WHERE id = ?''',
            (name, history, physical, description, session['user_id'])
        )
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, f'preencheu e trancou a ficha do personagem "{name}"')
        db.commit()

    return redirect(url_for('player_panel'))


@app.route('/admin/api/update_character/<int:user_id>', methods=['POST'])
@admin_required
def api_update_character(user_id):
    data = request.get_json(force=True) or {}

    fields = ['character_name', 'character_history', 'character_physical', 'character_description', 'character_origin', 'character_class']
    updates = {}
    for f in fields:
        if f in data:
            updates[f] = str(data[f])[:4000]

    if 'character_nex' in data:
        try:
            nex = int(data['character_nex'])
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'NEX inválido'}), 400
        if nex not in CHARACTER_NEX_OPTIONS:
            return jsonify({'ok': False, 'error': 'NEX inválido'}), 400
        updates['character_nex'] = nex

    if 'character_locked' in data:
        updates['character_locked'] = 1 if data['character_locked'] else 0

    if not updates:
        return jsonify({'ok': False, 'error': 'Nada para atualizar'}), 400

    db = get_db()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    db.execute(f'UPDATE users SET {set_clause} WHERE id = ?', (*updates.values(), user_id))
    db.commit()

    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

    if user is None:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    user_data = dict(user)
    user_data.pop('password_hash', None)
    return jsonify({'ok': True, 'user': user_data})


@app.route('/anotacoes')
@login_required
def anotacoes_panel():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    other_notes = []
    if session.get('is_admin'):
        other_notes = db.execute(
            "SELECT id, username, character_name, character_notes FROM users "
            "WHERE is_admin = 0 ORDER BY username ASC"
        ).fetchall()

    return render_template('anotacoes.html', user=user, other_notes=other_notes)


@app.route('/anotacoes/save', methods=['POST'])
@login_required
def anotacoes_save():
    notes = request.form.get('character_notes', '')[:8000]

    db = get_db()
    db.execute('UPDATE users SET character_notes = ? WHERE id = ?', (notes, session['user_id']))
    actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    _log_action(db, actor, 'salvou suas anotações')
    db.commit()

    return redirect(url_for('anotacoes_panel'))


# ---------------------------------------------------------------------------
# Aba Portrait: o próprio site gera o portrait do agente (foto, nome e barras
# de PV / Sanidade / PE) numa página com fundo transparente, para ser usada no
# OBS como "Fonte de Navegador". O OBS não tem login, então a página pública
# é protegida por um token secreto e único de cada jogador (dá para trocar).
# ---------------------------------------------------------------------------
import secrets as _secrets


def _portrait_token_for(db, user_id):
    """Devolve o token de portrait do usuário, criando um se ainda não existir."""
    row = db.execute('SELECT portrait_token FROM users WHERE id = ?', (user_id,)).fetchone()
    token = (row['portrait_token'] if row else '') or ''
    if not token:
        token = _secrets.token_urlsafe(16)
        db.execute('UPDATE users SET portrait_token = ? WHERE id = ?', (token, user_id))
        db.commit()
    return token


def _portrait_user_by_token(db, token):
    token = (token or '').strip()
    if len(token) < 8:
        return None
    return db.execute(
        'SELECT * FROM users WHERE portrait_token = ? AND is_admin = 0', (token,)
    ).fetchone()


# Opções do portrait salvas no servidor (botão "Salvar" da aba Portrait). O OBS
# lê estas opções a cada atualização, então o link colado no OBS nunca muda.
_PORTRAIT_OPTS_DEFAULT = {'f': '1', 'n': '1', 't': '1', 's': '1', 'pv': '1', 'x': '0', 'e': '1', 'l': 'h'}   # e = símbolo da afinidade


def _portrait_opts_for(row):
    """Opções salvas do usuário, completando o que faltar com os padrões."""
    opts = dict(_PORTRAIT_OPTS_DEFAULT)
    try:
        saved = json.loads((row['portrait_opts'] if row else '') or '{}')
    except (ValueError, TypeError):
        saved = {}
    if isinstance(saved, dict):
        for k in opts:
            if k in saved and str(saved[k]) in (('h', 'v') if k == 'l' else ('0', '1')):
                opts[k] = str(saved[k])
    return opts


def _portrait_opts_from_form():
    """Lê e valida as opções de exibição enviadas pelo formulário da aba Portrait."""
    opts = {}
    for k, default in _PORTRAIT_OPTS_DEFAULT.items():
        v = (request.form.get(k) or default).strip()
        if k == 'l':
            opts[k] = 'v' if v == 'v' else 'h'
        else:
            opts[k] = '0' if v == '0' else '1'
    return opts


@app.route('/portrait/save', methods=['POST'])
@login_required
def portrait_save():
    """Salva as opções de exibição do portrait do próprio jogador."""
    if session.get('is_admin'):
        return jsonify({'ok': False}), 403
    opts = _portrait_opts_from_form()
    db = get_db()
    db.execute('UPDATE users SET portrait_opts = ? WHERE id = ?',
               (json.dumps(opts), session['user_id']))
    db.commit()
    return jsonify({'ok': True, 'opts': opts})


@app.route('/portrait/save_all', methods=['POST'])
@login_required
def portrait_save_all():
    """Mestre: aplica as mesmas opções de exibição (e o layout) ao portrait de
    TODOS os agentes. Substitui o que cada jogador tinha salvo; o jogador ainda
    pode mudar o dele depois. As opções ficam guardadas também no registro do
    mestre, só para a tela lembrar o último layout aplicado."""
    if not session.get('is_admin'):
        return jsonify({'ok': False}), 403
    opts = _portrait_opts_from_form()
    payload = json.dumps(opts)
    db = get_db()
    cur = db.execute('UPDATE users SET portrait_opts = ? WHERE is_admin = 0', (payload,))
    updated = cur.rowcount
    db.execute('UPDATE users SET portrait_opts = ? WHERE id = ?', (payload, session['user_id']))
    db.commit()
    return jsonify({'ok': True, 'opts': opts, 'updated': updated})


@app.route('/portrait')
@login_required
def portrait_panel():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    is_admin = bool(session.get('is_admin'))

    my_token = '' if is_admin else _portrait_token_for(db, user['id'])

    agents = []
    if is_admin:
        for u in db.execute(
            "SELECT id, username, character_name FROM users WHERE is_admin = 0 ORDER BY username ASC"
        ).fetchall():
            agents.append({
                'id': u['id'],
                'name': u['character_name'] or u['username'],
                'username': u['username'],
                'token': _portrait_token_for(db, u['id']),
            })

    return render_template('portrait.html', user=user, my_token=my_token, agents=agents,
                           my_opts=_portrait_opts_for(user),
                           preview_token=agents[0]['token'] if agents else '')


@app.route('/portrait/regenerate', methods=['POST'])
@login_required
def portrait_regenerate():
    """Gera um link novo (o antigo deixa de funcionar). Jogador troca o seu;
    o mestre pode trocar o de qualquer agente."""
    db = get_db()
    target_id = session['user_id']
    if session.get('is_admin'):
        try:
            target_id = int(request.form.get('user_id', ''))
        except ValueError:
            return redirect(url_for('portrait_panel'))
    target = db.execute('SELECT * FROM users WHERE id = ? AND is_admin = 0', (target_id,)).fetchone()
    if target:
        db.execute('UPDATE users SET portrait_token = ? WHERE id = ?',
                   (_secrets.token_urlsafe(16), target['id']))
        actor = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        _log_action(db, actor, 'gerou um novo link de portrait' if target['id'] == actor['id']
                    else f'gerou um novo link de portrait para {display_name_or_username(target)}')
        db.commit()
    return redirect(url_for('portrait_panel'))


@app.route('/portrait/stream/<token>')
def portrait_stream(token):
    """Página pública (sem login) usada como Fonte de Navegador no OBS."""
    db = get_db()
    u = _portrait_user_by_token(db, token)
    if not u:
        return ('Portrait não encontrado.', 404)
    resp = app.make_response(render_template('portrait_stream.html', token=token,
                                            element_icons=RITUAL_ELEMENT_ICONS))
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@app.route('/portrait/stream/<token>/data')
def portrait_stream_data(token):
    db = get_db()
    u = _portrait_user_by_token(db, token)
    if not u:
        return jsonify({'ok': False}), 404

    row = db.execute('SELECT sem_sanidade FROM campaign WHERE id = 1').fetchone()
    sem_sanidade = bool(row and row['sem_sanidade'])
    state = u['state'] or ''
    avatar = u['avatar']
    affinity = ''
    if u['character_element_affinity'] and (u['character_nex'] or 0) >= ELEMENT_AFFINITY_NEX_THRESHOLD \
            and u['character_element_affinity'].lower() in RITUAL_ELEMENT_ICONS:
        affinity = u['character_element_affinity'].lower()
    resp = jsonify({
        'ok': True,
        'name': display_name_or_username(u),
        'title': u['character_title'] or '',
        'title_color': u['character_title_color'] or '#f5f5f5',
        'nex': u['character_nex'] or 0,
        'color': u['color'] or '#c62f27',
        'avatar_url': url_for('static', filename='avatar_images/' + avatar) if avatar else '',
        'pv': u['pv'], 'pv_max': u['pv_max'],
        'pe': u['pe'], 'pe_max': u['pe_max'],
        'sanidade': u['sanidade'], 'sanidade_max': u['sanidade_max'],
        'sem_sanidade': sem_sanidade,
        'pe_label': 'PD' if sem_sanidade else 'PE',
        'state': state,
        'state_color': NPC_STATE_COLORS.get(state, '') if state else '',
        # Afinidade elemental: mesma regra da aba Agentes (só a partir de 50% de NEX)
        'element': affinity,
        'element_color': RITUAL_ELEMENT_COLORS.get(u['character_element_affinity'], '') if affinity else '',
        'opts': _portrait_opts_for(u),
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/portrait/stream/<token>/dice')
def portrait_stream_dice(token):
    """Consulta leve (feita a cada ~0,8 s pelo portrait do OBS) das rolagens de
    dado feitas por comando no chat. Sem ?after devolve só o último id (ponto de
    partida: rolagens antigas não são reproduzidas ao abrir/recarregar o OBS);
    com ?after=<id> devolve as rolagens novas dos últimos 20 s."""
    db = get_db()
    u = _portrait_user_by_token(db, token)
    if not u:
        return jsonify({'ok': False}), 404

    row = db.execute('SELECT COALESCE(MAX(id), 0) AS last_id FROM portrait_dice WHERE user_id = ?',
                     (u['id'],)).fetchone()
    srow = db.execute('SELECT COALESCE(MAX(id), 0) AS last_id FROM portrait_skill WHERE user_id = ?',
                      (u['id'],)).fetchone()
    skills = []
    sk_after = request.args.get('sk_after', type=int)
    if sk_after is not None:
        skills = [
            {'id': r['id'], 'name': r['name'], 'die': r['die']}
            for r in db.execute(
                """SELECT id, name, die FROM portrait_skill
                   WHERE user_id = ? AND id > ?
                     AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-20 seconds')
                   ORDER BY id ASC LIMIT 5""",
                (u['id'], sk_after)
            ).fetchall()
        ]
    events = []
    after = request.args.get('after', type=int)
    if after is not None:
        events = [
            {'id': r['id'], 'total': r['total'], 'label': r['label']}
            for r in db.execute(
                """SELECT id, total, label FROM portrait_dice
                   WHERE user_id = ? AND id > ?
                     AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-20 seconds')
                   ORDER BY id ASC LIMIT 5""",
                (u['id'], after)
            ).fetchall()
        ]
    resp = jsonify({'ok': True, 'last_id': row['last_id'], 'events': events,
                    'last_skill_id': srow['last_id'], 'skills': skills})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/player')
@login_required
def player_panel():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    items = db.execute('SELECT * FROM items WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    rituals = db.execute('SELECT * FROM rituals WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    abilities = db.execute('SELECT * FROM class_abilities WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    money_requests = db.execute(
        'SELECT * FROM money_requests WHERE user_id = ? ORDER BY id DESC LIMIT 20', (session['user_id'],)
    ).fetchall()
    money_transactions = db.execute(
        'SELECT * FROM money_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50', (session['user_id'],)
    ).fetchall()

    used_spaces = sum(i['quantity'] * i['spaces'] for i in items)
    capacidade_max = _capacidade_max(user, _carga_bonus_from_items(items))
    overloaded = used_spaces > capacidade_max
    ritual_error = request.args.get('ritual_error')
    defesa = _defesa_valor(user, overloaded)
    point_pcts = {
        'pv': _stat_pct(user['pv'], user['pv_max']),
        'pe': _stat_pct(user['pe'], user['pe_max']),
        'sanidade': _stat_pct(user['sanidade'], user['sanidade_max']),
    }
    campaign = db.execute('SELECT financeiro_visible FROM campaign WHERE id = 1').fetchone()
    return render_template(
        'player.html',
        user=user,
        npc_states=NPC_STATES,
        npc_state_colors=NPC_STATE_COLORS,
        financeiro_visible=bool(campaign['financeiro_visible']) if campaign else True,
        items=items,
        used_spaces=used_spaces,
        capacidade_max=capacidade_max,
        rituals=rituals,
        abilities=abilities,
        money_requests=money_requests,
        money_transactions=money_transactions,
        defesa=defesa,
        ritual_elements=RITUAL_ELEMENTS,
        ritual_element_icons=RITUAL_ELEMENT_ICONS,
        ritual_circle_costs=RITUAL_CIRCLE_PE_COSTS,
        ritual_error=ritual_error,
        point_pcts=point_pcts,
        point_defaults=POINT_DEFAULTS,
        character_origins=CHARACTER_ORIGINS,
        character_classes=CHARACTER_CLASSES,
        character_nex_options=CHARACTER_NEX_OPTIONS,
        item_levels=ITEM_LEVELS,
        item_types=ITEM_TYPES,
        affinity_elements=AFFINITY_ELEMENTS,
        element_affinity_nex_threshold=ELEMENT_AFFINITY_NEX_THRESHOLD
    )


@app.route('/player/api/status')
@login_required
def api_player_status():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    items = db.execute('SELECT * FROM items WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    rituals = db.execute('SELECT * FROM rituals WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    abilities = db.execute('SELECT * FROM class_abilities WHERE user_id = ? ORDER BY sort_order ASC, id ASC', (session['user_id'],)).fetchall()
    money_requests = db.execute(
        'SELECT * FROM money_requests WHERE user_id = ? ORDER BY id DESC LIMIT 20', (session['user_id'],)
    ).fetchall()
    money_transactions = db.execute(
        'SELECT * FROM money_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50', (session['user_id'],)
    ).fetchall()

    used_spaces = sum(i['quantity'] * i['spaces'] for i in items)
    capacidade_max = _capacidade_max(user, _carga_bonus_from_items(items))
    overloaded = used_spaces > capacidade_max

    user_data = dict(user)
    user_data.pop('password_hash', None)
    user_data['items'] = [dict(i) for i in items]
    user_data['rituals'] = [dict(r) for r in rituals]
    user_data['abilities'] = [dict(a) for a in abilities]
    user_data['money_requests'] = [dict(r) for r in money_requests]
    user_data['money_transactions'] = [dict(t) for t in money_transactions]
    user_data['capacidade_max'] = capacidade_max
    user_data['defesa'] = _defesa_valor(user, overloaded)
    return jsonify(user_data)


@app.route('/pericias')
@login_required
def pericias_panel():
    db = get_db()
    me = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if not me:
        session.clear()
        return redirect(url_for('login'))

    # O admin pode visualizar (e rolar) as perícias de qualquer jogador
    # escolhendo-o no seletor da página (?user_id=N). Para os demais
    # usuários o parâmetro é ignorado: cada um vê só as próprias perícias.
    user = me
    player_options = []
    if me['is_admin']:
        player_options = db.execute(
            'SELECT id, username, character_name FROM users WHERE is_admin = 0 ORDER BY username COLLATE NOCASE'
        ).fetchall()
        target_id = request.args.get('user_id', type=int)
        if target_id and target_id != me['id']:
            target = db.execute(
                'SELECT * FROM users WHERE id = ? AND is_admin = 0', (target_id,)
            ).fetchone()
            if target:
                user = target

    viewing_other = user['id'] != me['id']
    rows = db.execute('SELECT * FROM skills WHERE user_id = ?', (user['id'],)).fetchall()

    skills_data = {
        r['skill_key']: {
            'level': r['level'],
            'custom_name': r['custom_name'],
            'bonus': r['bonus'],
            'attr_override': r['attr_override'] if r['attr_override'] in ATTRIBUTE_LABELS else '',
            'favorite': bool(r['favorite'])
        }
        for r in rows
    }
    attributes = {k: user[k] for k in ATTRIBUTE_LABELS}

    # Favoritas primeiro; dentro de cada grupo vale a ordem original da lista.
    # 'order' guarda a posição original para a página reordenar sem recarregar.
    ordered_defs = [dict(s, order=i) for i, s in enumerate(SKILL_DEFS)]
    ordered_defs.sort(key=lambda s: (0 if skills_data.get(s['key'], {}).get('favorite') else 1, s['order']))

    return render_template(
        'pericias.html',
        skill_defs=ordered_defs,
        skills_data=skills_data,
        attributes=attributes,
        attribute_labels=ATTRIBUTE_LABELS,
        player_options=player_options,
        viewing_other=viewing_other,
        target_user_id=user['id'],
        target_name=(user['character_name'] or user['username']) if viewing_other else ''
    )


@app.route('/pericias/api/save', methods=['POST'])
@login_required
def api_save_skill():
    data = request.get_json(force=True) or {}
    skill_key = data.get('skill_key', '')
    custom_name = str(data.get('custom_name', '')).strip()[:80]
    bonus = str(data.get('bonus', '')).strip()[:20]
    attr_override = str(data.get('attr_override', '')).strip()

    if skill_key not in SKILL_KEYS:
        return jsonify({'ok': False, 'error': 'Perícia inválida'}), 400

    try:
        level = int(data.get('level', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Nível inválido'}), 400

    if level not in SKILL_LEVELS:
        return jsonify({'ok': False, 'error': 'Nível inválido'}), 400

    # Atributo alternativo escolhido pelo jogador para rolar essa perícia.
    # Vazio (ou igual ao atributo primário) significa "usar o padrão da perícia".
    if attr_override not in ATTRIBUTE_LABELS:
        attr_override = ''
    if attr_override == SKILL_ATTR_BY_KEY[skill_key]:
        attr_override = ''

    db = get_db()
    existing = db.execute(
        'SELECT id FROM skills WHERE user_id = ? AND skill_key = ?',
        (session['user_id'], skill_key)
    ).fetchone()

    if existing:
        db.execute(
            'UPDATE skills SET level = ?, custom_name = ?, bonus = ?, attr_override = ? WHERE id = ?',
            (level, custom_name, bonus, attr_override, existing['id'])
        )
    else:
        db.execute(
            'INSERT INTO skills (user_id, skill_key, level, custom_name, bonus, attr_override) VALUES (?, ?, ?, ?, ?, ?)',
            (session['user_id'], skill_key, level, custom_name, bonus, attr_override)
        )
    db.commit()

    return jsonify({
        'ok': True,
        'skill_key': skill_key,
        'level': level,
        'custom_name': custom_name,
        'bonus': bonus,
        'attr_override': attr_override
    })


@app.route('/pericias/api/favorite', methods=['POST'])
@login_required
def api_favorite_skill():
    """Marca/desmarca uma perícia como favorita (as favoritas sobem na lista).
    Cada usuário só altera as próprias; não mexe em nível, bônus nem nome."""
    data = request.get_json(force=True) or {}
    skill_key = data.get('skill_key', '')
    if skill_key not in SKILL_KEYS:
        return jsonify({'ok': False, 'error': 'Perícia inválida'}), 400
    favorite = 1 if data.get('favorite') else 0

    db = get_db()
    existing = db.execute(
        'SELECT id FROM skills WHERE user_id = ? AND skill_key = ?',
        (session['user_id'], skill_key)
    ).fetchone()
    if existing:
        db.execute('UPDATE skills SET favorite = ? WHERE id = ?', (favorite, existing['id']))
    else:
        db.execute(
            'INSERT INTO skills (user_id, skill_key, level, custom_name, bonus, attr_override, favorite) VALUES (?, ?, 0, \'\', \'\', \'\', ?)',
            (session['user_id'], skill_key, favorite)
        )
    db.commit()
    return jsonify({'ok': True, 'skill_key': skill_key, 'favorite': bool(favorite)})


@app.route('/pericias/api/roll', methods=['POST'])
@login_required
def api_roll_skill():
    data = request.get_json(force=True) or {}
    skill_key = data.get('skill_key', '')
    requested_attr = str(data.get('attr_key', '') or '').strip()

    if skill_key not in SKILL_KEYS:
        return jsonify({'ok': False, 'error': 'Perícia inválida'}), 400

    default_attr_key = SKILL_ATTR_BY_KEY[skill_key]

    db = get_db()
    me = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not me:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    # O admin pode rolar as perícias de um jogador (user_id no corpo); a
    # rolagem usa os atributos/treino dele e sai no chat em nome dele.
    user = me
    try:
        target_id = int(data.get('user_id') or 0)
    except (TypeError, ValueError):
        target_id = 0
    if target_id and target_id != me['id']:
        if not me['is_admin']:
            return jsonify({'ok': False, 'error': 'Sem permissão'}), 403
        user = db.execute(
            'SELECT * FROM users WHERE id = ? AND is_admin = 0', (target_id,)
        ).fetchone()
        if not user:
            return jsonify({'ok': False, 'error': 'Jogador não encontrado'}), 404

    skill_row = db.execute(
        'SELECT * FROM skills WHERE user_id = ? AND skill_key = ?',
        (user['id'], skill_key)
    ).fetchone()

    # Ordem de prioridade para o atributo usado na rolagem:
    # 1) atributo enviado explicitamente nesta rolagem (dropdown do jogador)
    # 2) atributo alternativo salvo anteriormente para essa perícia
    # 3) atributo primário padrão da perícia
    if requested_attr in ATTRIBUTE_LABELS:
        attr_key = requested_attr
    elif skill_row and skill_row['attr_override'] in ATTRIBUTE_LABELS:
        attr_key = skill_row['attr_override']
    else:
        attr_key = default_attr_key

    is_alt_attr = attr_key != default_attr_key
    attr_value = user[attr_key]
    training = skill_row['level'] if skill_row else 0
    if training not in SKILL_LEVELS:
        training = 0

    rolls, chosen, dice_desc, natural_20 = _roll_attribute_dice(attr_value)

    # Perícias marcadas com "+" (Acrobacia, Crime, Furtividade) sofrem -5
    # quando o personagem está carregando mais espaços do que sua
    # capacidade de carga permite (sobrecarregado).
    load_penalty = 0
    if skill_key in SKILL_LOAD_PENALTY_KEYS and _is_overloaded(db, user):
        load_penalty = 5

    total = chosen + training - load_penalty
    rolls_str = ', '.join(str(r) for r in rolls)

    skill_def_label = SKILL_LABEL_BY_KEY[skill_key]
    custom_name = skill_row['custom_name'] if skill_row else ''
    display_label = f'{skill_def_label} ({custom_name})' if custom_name else skill_def_label

    attr_label = ATTRIBUTE_LABELS[attr_key]
    training_label = TRAINING_LABELS[training]

    penalty_str = ' - 5 (sobrecarregado)' if load_penalty else ''
    attr_note = ' *' if is_alt_attr else ''
    natural_20_str = ' — 20 NATURAL!' if natural_20 else ''
    content = (
        f'rolou {display_label} [{attr_label}{attr_note} {attr_value}]: '
        f'{dice_desc} [{rolls_str}] = {chosen} + {training} ({training_label}){penalty_str} = {total}{natural_20_str}'
    )

    cur = db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color, rolled_by_admin, rolled_by)
           VALUES (?, ?, ?, ?, 'roll', ?, ?, ?, ?)''',
        (user['id'], get_display_name(user), '' if user['is_admin'] else user['character_name'], user['is_admin'], content, user['color'],
         1 if user['id'] != me['id'] else 0,
         get_display_name(me) if user['id'] != me['id'] else '')
    )
    new_id = cur.lastrowid

    # O nome da perícia aparece abaixo da foto no portrait do OBS do jogador
    # (também quando o mestre rola a perícia em nome dele).
    if not user['is_admin']:
        # 'die' guarda o TOTAL da perícia (d20 + treinamento - penalidade): é o
        # número que aparece no meio do dado no portrait.
        db.execute('INSERT INTO portrait_skill (user_id, name, die) VALUES (?, ?, ?)',
                   (user['id'], display_label, total))
        db.execute("DELETE FROM portrait_skill WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')")
    db.commit()
    message = db.execute(CHAT_SELECT + ' WHERE chat_messages.id = ?', (new_id,)).fetchone()

    return jsonify({
        'ok': True,
        'message': dict(message),
        'total': total,
        'dice_desc': dice_desc,
        'rolls': rolls,
        'chosen': chosen,
        'training': training,
        'training_label': training_label,
        'attr_key': attr_key,
        'attr_label': attr_label,
        'attr_value': attr_value,
        'is_alt_attr': is_alt_attr,
        'load_penalty': load_penalty,
        'natural_20': natural_20
    })


def _do_attribute_roll(user, attr_key):
    """Rola um teste de atributo puro (sem perícia/treinamento) para `user`
    e registra o resultado no chat. Retorna o payload de resposta da API."""
    attr_value = user[attr_key]
    rolls, chosen, dice_desc, natural_20 = _roll_attribute_dice(attr_value)
    attr_label = ATTRIBUTE_LABELS[attr_key]
    rolls_str = ', '.join(str(r) for r in rolls)

    natural_20_str = ' — 20 NATURAL!' if natural_20 else ''
    content = (
        f'rolou {attr_label} (teste de atributo): '
        f'{dice_desc} [{rolls_str}] = {chosen}{natural_20_str}'
    )

    db = get_db()
    cur = db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color)
           VALUES (?, ?, ?, ?, 'roll', ?, ?)''',
        (user['id'], get_display_name(user), '' if user['is_admin'] else user['character_name'], user['is_admin'], content, user['color'])
    )
    db.commit()
    new_id = cur.lastrowid
    message = db.execute(CHAT_SELECT + ' WHERE chat_messages.id = ?', (new_id,)).fetchone()

    return {
        'ok': True,
        'message': dict(message),
        'total': chosen,
        'dice_desc': dice_desc,
        'rolls': rolls,
        'chosen': chosen,
        'attr_key': attr_key,
        'attr_label': attr_label,
        'attr_value': attr_value,
        'natural_20': natural_20
    }


@app.route('/player/api/roll_attribute', methods=['POST'])
@login_required
def api_roll_attribute():
    """Teste de atributo puro para o próprio jogador (ou o admin, para si
    mesmo), sem nenhuma perícia/treinamento envolvido."""
    data = request.get_json(force=True) or {}
    attr_key = str(data.get('attr_key', '') or '').strip()

    if attr_key not in ATTRIBUTE_LABELS:
        return jsonify({'ok': False, 'error': 'Atributo inválido'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    return jsonify(_do_attribute_roll(user, attr_key))


@app.route('/admin/api/roll_attribute/<int:user_id>', methods=['POST'])
@admin_required
def api_admin_roll_attribute(user_id):
    """Permite ao mestre rodar o teste de atributo puro em nome de um
    jogador específico (o resultado é enviado ao chat como se fosse a
    rolagem do próprio personagem)."""
    data = request.get_json(force=True) or {}
    attr_key = str(data.get('attr_key', '') or '').strip()

    if attr_key not in ATTRIBUTE_LABELS:
        return jsonify({'ok': False, 'error': 'Atributo inválido'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'ok': False, 'error': 'Usuário não encontrado'}), 404

    return jsonify(_do_attribute_roll(user, attr_key))


@app.route('/player/api/roll_item_damage/<int:item_id>', methods=['POST'])
@login_required
def api_roll_item_damage(item_id):
    db = get_db()
    item = db.execute(
        'SELECT * FROM items WHERE id = ? AND user_id = ?', (item_id, session['user_id'])
    ).fetchone()

    if item is None:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404
    if item['item_type'] != 'Arma':
        return jsonify({'ok': False, 'error': 'Este item não é uma arma'}), 400

    result = _roll_dice_expression(item['damage'])
    if result is None:
        return jsonify({'ok': False, 'error': 'Esta arma não tem um dano válido definido'}), 400

    base_total, dice_detail = result
    threshold, multiplier = _parse_critical(item['critical'])

    attack_roll = random.randint(1, 20)
    is_critical = attack_roll >= threshold
    natural_20 = attack_roll == 20
    final_total = base_total * multiplier if is_critical else base_total

    threat_desc = f'ameaça {threshold}-20' if threshold < 20 else 'ameaça 20'
    natural_20_str = ' — 20 NATURAL!' if natural_20 else ''

    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if is_critical:
        content = (
            f'rolou dano de {item["name"]}: ataque 1d20 = {attack_roll} ({threat_desc}) — '
            f'CRÍTICO x{multiplier}!{natural_20_str} dano {dice_detail} = {base_total} × {multiplier} = {final_total}'
        )
    else:
        content = (
            f'rolou dano de {item["name"]}: ataque 1d20 = {attack_roll} ({threat_desc}, sem crítico) — '
            f'dano {dice_detail} = {final_total}'
        )

    cur = db.execute(
        '''INSERT INTO chat_messages (user_id, username, character_name, is_admin, kind, content, color)
           VALUES (?, ?, ?, ?, 'roll', ?, ?)''',
        (user['id'], get_display_name(user), '' if user['is_admin'] else user['character_name'], user['is_admin'], content, user['color'])
    )
    db.commit()
    new_id = cur.lastrowid
    message = db.execute(CHAT_SELECT + ' WHERE chat_messages.id = ?', (new_id,)).fetchone()

    return jsonify({
        'ok': True,
        'message': dict(message),
        'total': final_total,
        'critical': is_critical,
        'natural_20': natural_20,
        'attack_roll': attack_roll,
        'dice_detail': dice_detail,
        'base_total': base_total,
        'multiplier': multiplier
    })


if __name__ == '__main__':
    # Modo debug do Flask expõe um console de execução de código a quem
    # acessar o site pela rede; por isso fica DESLIGADO por padrão.
    # Para ligar (só em desenvolvimento local): defina RPG_DEBUG=1.
    if os.environ.get('RPG_DEBUG') == '1':
        app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)
    elif Sock is not None and os.environ.get('RPG_SERVER') != 'waitress':
        # WebSocket do chat: o waitress não suporta, então usamos o servidor
        # do Flask em modo multithread (uma thread por conexão).
        print('Servidor rodando em http://0.0.0.0:5000 (chat em tempo real via WebSocket)')
        app.run(debug=False, use_reloader=False, threaded=True, host='0.0.0.0', port=5000)
    else:
        if Sock is None:
            print('Dica: \"pip install flask-sock\" liga o chat em tempo real (WebSocket).')
        try:
            from waitress import serve  # servidor de produção, atende várias abas em paralelo
        except ImportError:
            print('Dica: \"pip install waitress\" deixa o site mais rápido com vários jogadores.')
            app.run(debug=False, use_reloader=False, threaded=True, host='0.0.0.0', port=5000)
        else:
            print('Servidor rodando em http://0.0.0.0:5000 (waitress, chat por polling)')
            serve(app, host='0.0.0.0', port=5000, threads=16)
