import sqlite3
import os
from werkzeug.security import generate_password_hash

# O banco de dados fica fora desta pasta, em "preservar/", para que esta
# pasta (atualizavel/) possa ser sobrescrita em atualizações sem apagar os
# dados salvos.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'preservar', 'rpg.db')


def get_db():
    # timeout maior + WAL: evita "database is locked" e travas/lentidão
    # quando várias abas ficam fazendo polling do chat ao mesmo tempo.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    # O modo WAL é gravado no próprio arquivo do banco (init_db ativa uma vez);
    # "synchronous = NORMAL" é o par recomendado do WAL e deixa cada gravação
    # bem mais rápida, sem risco de corromper o banco.
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def init_db():
    conn = get_db()
    conn.execute('PRAGMA journal_mode = WAL')
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,

            agilidade INTEGER NOT NULL DEFAULT 1,
            forca INTEGER NOT NULL DEFAULT 1,
            intelecto INTEGER NOT NULL DEFAULT 1,
            presenca INTEGER NOT NULL DEFAULT 1,
            vigor INTEGER NOT NULL DEFAULT 1,
            attributes_locked INTEGER NOT NULL DEFAULT 0,
            attribute_increases_used INTEGER NOT NULL DEFAULT 0,

            color TEXT NOT NULL DEFAULT '#c62f27',

            pv INTEGER NOT NULL DEFAULT 10,
            pv_max INTEGER NOT NULL DEFAULT 10,
            pe INTEGER NOT NULL DEFAULT 2,
            pe_max INTEGER NOT NULL DEFAULT 2,
            sanidade INTEGER NOT NULL DEFAULT 10,
            sanidade_max INTEGER NOT NULL DEFAULT 10,
            points_locked INTEGER NOT NULL DEFAULT 0,

            defesa_equip_bonus INTEGER NOT NULL DEFAULT 0,
            defesa_outros_bonus INTEGER NOT NULL DEFAULT 0,
            defesa_reduction INTEGER NOT NULL DEFAULT 0,

            capacidade_max INTEGER NOT NULL DEFAULT 10,
            capacidade_locked INTEGER NOT NULL DEFAULT 0,

            avatar TEXT,
            avatar_size INTEGER NOT NULL DEFAULT 160,
            is_online INTEGER NOT NULL DEFAULT 0,

            character_name TEXT NOT NULL DEFAULT '',
            character_title TEXT NOT NULL DEFAULT '',
            character_title_color TEXT NOT NULL DEFAULT '#f5f5f5',
            character_history TEXT NOT NULL DEFAULT '',
            character_physical TEXT NOT NULL DEFAULT '',
            character_description TEXT NOT NULL DEFAULT '',
            character_origin TEXT NOT NULL DEFAULT '',
            character_class TEXT NOT NULL DEFAULT '',
            character_patente TEXT NOT NULL DEFAULT '',
            character_class_track TEXT NOT NULL DEFAULT '',
            character_money INTEGER NOT NULL DEFAULT 0,
            character_nex INTEGER NOT NULL DEFAULT 0,
            character_notes TEXT NOT NULL DEFAULT '',
            character_locked INTEGER NOT NULL DEFAULT 0,
            character_element_affinity TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            image TEXT,
            quantity INTEGER NOT NULL DEFAULT 1,
            spaces INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL DEFAULT '',
            level TEXT NOT NULL DEFAULT '0',
            item_type TEXT NOT NULL DEFAULT '',
            damage TEXT NOT NULL DEFAULT '',
            critical TEXT NOT NULL DEFAULT '',
            defense INTEGER NOT NULL DEFAULT 0,
            carga_bonus INTEGER NOT NULL DEFAULT 0,
            element TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS rituals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            element TEXT NOT NULL,
            circle INTEGER NOT NULL DEFAULT 1,
            pe_cost INTEGER NOT NULL DEFAULT 0,
            pe_cost_discente INTEGER NOT NULL DEFAULT 0,
            pe_cost_verdadeira INTEGER NOT NULL DEFAULT 0,
            pe_cost_normal INTEGER NOT NULL DEFAULT 0,
            execution TEXT NOT NULL DEFAULT '',
            range_ TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            resistance TEXT NOT NULL DEFAULT '',
            effect_summary TEXT NOT NULL DEFAULT '',
            description_discente TEXT NOT NULL DEFAULT '',
            description_verdadeira TEXT NOT NULL DEFAULT '',
            symbol TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            skill_key TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            custom_name TEXT NOT NULL DEFAULT '',
            bonus TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, skill_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS class_abilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pe_cost INTEGER NOT NULL DEFAULT 0,
            element TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'habilidade',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS campaign (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            banner TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            character_name TEXT NOT NULL DEFAULT '',
            is_admin INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL DEFAULT '',
            ritual_element TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS mural_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            user_id INTEGER,
            username TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS money_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            resolved_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS money_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL DEFAULT '',
            character_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS npcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            avatar TEXT,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#c62f27',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    ''')

    # Participantes do combate atual (aba "Combate"): tanto agentes (jogadores
    # já cadastrados, referenciados por user_id) quanto criaturas avulsas
    # (monstros/inimigos cadastrados na hora pelo mestre, com nome/avatar
    # próprios). A iniciativa rolada fica fixa até o mestre encerrar o
    # combate, que apaga todas as linhas desta tabela.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS combat_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'agente',
            user_id INTEGER,
            name TEXT NOT NULL DEFAULT '',
            avatar TEXT,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#c62f27',
            initiative_bonus INTEGER NOT NULL DEFAULT 0,
            initiative INTEGER,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    # Estado do sistema de turnos do combate (uma única linha, id = 1):
    # se os turnos estão ativos, qual a rodada e de quem é a vez.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS combat_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active INTEGER NOT NULL DEFAULT 0,
            round INTEGER NOT NULL DEFAULT 0,
            current_id INTEGER
        )
    ''')
    cur.execute('INSERT OR IGNORE INTO combat_state (id) VALUES (1)')

    # Rolagens feitas por comando no chat (/d20, /2d6+3...) que disparam o dado
    # animado no portrait do OBS. Só guarda o resultado final de cada rolagem.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS portrait_dice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            total INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    # Perícias rolladas (nome mostrado abaixo da foto no portrait do OBS).
    cur.execute('''
        CREATE TABLE IF NOT EXISTS portrait_skill (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            die INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')

    conn.commit()

    # Índices para as colunas mais usadas em WHERE/JOIN. Sem eles, o SQLite
    # varre a tabela inteira a cada consulta (ex.: buscar os itens de um
    # jogador, ou as mensagens de chat mais recentes) — o que só fica lento
    # perceptível quando as tabelas crescem, mas não custa nada evitar.
    cur.executescript('''
        CREATE INDEX IF NOT EXISTS idx_items_user_id ON items(user_id);
        CREATE INDEX IF NOT EXISTS idx_rituals_user_id ON rituals(user_id);
        CREATE INDEX IF NOT EXISTS idx_skills_user_id ON skills(user_id);
        CREATE INDEX IF NOT EXISTS idx_class_abilities_user_id ON class_abilities(user_id);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_mural_images_user_id ON mural_images(user_id);
        CREATE INDEX IF NOT EXISTS idx_money_requests_user_id ON money_requests(user_id);
        CREATE INDEX IF NOT EXISTS idx_money_requests_status ON money_requests(status);
        CREATE INDEX IF NOT EXISTS idx_money_transactions_user_id ON money_transactions(user_id);
        CREATE INDEX IF NOT EXISTS idx_action_logs_created_at ON action_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_action_logs_user_id ON action_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_combat_participants_user_id ON combat_participants(user_id);
        CREATE INDEX IF NOT EXISTS idx_portrait_dice_user_id ON portrait_dice(user_id, id);
        CREATE INDEX IF NOT EXISTS idx_portrait_skill_user_id ON portrait_skill(user_id, id);
    ''')
    conn.commit()

    # Migração leve para bancos já existentes que foram criados antes
    # dos campos de avatar/símbolo existirem.
    for table, column, coldef in (
        ('users', 'avatar', 'avatar TEXT'),
        ('users', 'avatar_size', 'avatar_size INTEGER NOT NULL DEFAULT 160'),
        ('users', 'is_online', 'is_online INTEGER NOT NULL DEFAULT 0'),
        ('users', 'character_name', "character_name TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_history', "character_history TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_physical', "character_physical TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_description', "character_description TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_locked', 'character_locked INTEGER NOT NULL DEFAULT 0'),
        ('rituals', 'symbol', 'symbol TEXT'),
        ('portrait_skill', 'die', 'die INTEGER NOT NULL DEFAULT 0'),
        ('portrait_dice', 'label', "label TEXT NOT NULL DEFAULT ''"),
        ('skills', 'bonus', "bonus TEXT NOT NULL DEFAULT ''"),
        ('users', 'attributes_locked', 'attributes_locked INTEGER NOT NULL DEFAULT 0'),
        ('users', 'color', "color TEXT NOT NULL DEFAULT '#c62f27'"),
        ('users', 'points_locked', 'points_locked INTEGER NOT NULL DEFAULT 0'),
        ('users', 'character_origin', "character_origin TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_class', "character_class TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_patente', "character_patente TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_class_track', "character_class_track TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_money', 'character_money INTEGER NOT NULL DEFAULT 0'),
        ('users', 'character_nex', 'character_nex INTEGER NOT NULL DEFAULT 0'),
        ('users', 'character_notes', "character_notes TEXT NOT NULL DEFAULT ''"),
        ('users', 'portrait_token', "portrait_token TEXT NOT NULL DEFAULT ''"),
        ('users', 'portrait_opts', "portrait_opts TEXT NOT NULL DEFAULT ''"),
        ('mural_images', 'user_id', 'user_id INTEGER'),
        ('mural_images', 'username', "username TEXT NOT NULL DEFAULT ''"),
        ('mural_images', 'category', "category TEXT NOT NULL DEFAULT ''"),
        ('users', 'capacidade_locked', 'capacidade_locked INTEGER NOT NULL DEFAULT 0'),
        ('items', 'level', "level TEXT NOT NULL DEFAULT '0'"),
        ('items', 'item_type', "item_type TEXT NOT NULL DEFAULT ''"),
        ('items', 'damage', "damage TEXT NOT NULL DEFAULT ''"),
        ('items', 'critical', "critical TEXT NOT NULL DEFAULT ''"),
        ('items', 'defense', 'defense INTEGER NOT NULL DEFAULT 0'),
        ('items', 'carga_bonus', 'carga_bonus INTEGER NOT NULL DEFAULT 0'),
        ('items', 'element', "element TEXT NOT NULL DEFAULT ''"),
        ('items', 'sort_order', 'sort_order INTEGER NOT NULL DEFAULT 0'),
        ('users', 'defesa_equip_bonus', 'defesa_equip_bonus INTEGER NOT NULL DEFAULT 0'),
        ('users', 'defesa_outros_bonus', 'defesa_outros_bonus INTEGER NOT NULL DEFAULT 0'),
        ('users', 'defesa_reduction', 'defesa_reduction INTEGER NOT NULL DEFAULT 0'),
        ('chat_messages', 'color', 'color TEXT'),
        ('class_abilities', 'pe_cost', 'pe_cost INTEGER NOT NULL DEFAULT 0'),
        ('users', 'last_read_chat_id', 'last_read_chat_id INTEGER NOT NULL DEFAULT 0'),
        ('skills', 'attr_override', "attr_override TEXT NOT NULL DEFAULT ''"),
        ('skills', 'favorite', 'favorite INTEGER NOT NULL DEFAULT 0'),
        ('rituals', 'resistance', "resistance TEXT NOT NULL DEFAULT ''"),
        ('rituals', 'circle', 'circle INTEGER NOT NULL DEFAULT 1'),
        ('chat_messages', 'ritual_element', 'ritual_element TEXT'),
        ('chat_messages', 'rolled_by_admin', 'rolled_by_admin INTEGER NOT NULL DEFAULT 0'),
        ('chat_messages', 'rolled_by', "rolled_by TEXT NOT NULL DEFAULT ''"),
        ('class_abilities', 'element', "element TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_title', "character_title TEXT NOT NULL DEFAULT ''"),
        ('users', 'character_title_color', "character_title_color TEXT NOT NULL DEFAULT '#f5f5f5'"),
        ('class_abilities', 'kind', "kind TEXT NOT NULL DEFAULT 'habilidade'"),
        ('class_abilities', 'sort_order', 'sort_order INTEGER NOT NULL DEFAULT 0'),
        ('rituals', 'sort_order', 'sort_order INTEGER NOT NULL DEFAULT 0'),
        ('npcs', 'state', "state TEXT NOT NULL DEFAULT ''"),
        ('npcs', 'sort_order', 'sort_order INTEGER NOT NULL DEFAULT 0'),
        ('users', 'character_element_affinity', "character_element_affinity TEXT NOT NULL DEFAULT ''"),
        # Regra geral "Aumento de Atributo": conta quantos aumentos de +1 o
        # jogador já gastou (ganhos automaticamente em NEX 20%, 50%, 80% e
        # 95%, para qualquer classe — veja ATTRIBUTE_INCREASE_NEX_THRESHOLDS).
        ('users', 'attribute_increases_used', 'attribute_increases_used INTEGER NOT NULL DEFAULT 0'),
        ('campaign', 'financeiro_visible', 'financeiro_visible INTEGER NOT NULL DEFAULT 1'),
        # Modo "Jogar sem Sanidade": quando ligado pelo mestre, a Sanidade some
        # das telas (Painel, Agentes, Combate e Admin). Os valores continuam
        # guardados, então desligar o modo traz tudo de volta.
        ('campaign', 'sem_sanidade', 'sem_sanidade INTEGER NOT NULL DEFAULT 0'),
        # PD (Pontos de Determinação) reaproveita as colunas pe/pe_max no modo
        # "Jogar sem Sanidade". Ao ligar o modo, o PE original é guardado aqui
        # e devolvido quando o modo é desligado; pd_key lembra com quais
        # classe/NEX/Presença o PD máximo foi calculado da última vez.
        ('users', 'pe_backup', 'pe_backup INTEGER'),
        ('users', 'pe_max_backup', 'pe_max_backup INTEGER'),
        ('users', 'pd_key', 'pd_key TEXT'),
        # Rituais agora têm dois custos em PE separados: um para a versão
        # "discente" (mais barata) e outro para a versão "verdadeira" (mais
        # cara / mais poderosa) do mesmo ritual.
        ('rituals', 'pe_cost_discente', 'pe_cost_discente INTEGER NOT NULL DEFAULT 0'),
        ('rituals', 'pe_cost_verdadeira', 'pe_cost_verdadeira INTEGER NOT NULL DEFAULT 0'),
        # Custo de PE "normal": um terceiro custo, independente das versões
        # discente/verdadeira, para rituais que não seguem essa divisão.
        ('rituals', 'pe_cost_normal', 'pe_cost_normal INTEGER NOT NULL DEFAULT 0'),
        # Descrições específicas de cada versão do ritual — exibidas no
        # lugar do resumo do efeito conforme a versão escolhida no menu
        # suspenso ("Normal" continua usando effect_summary).
        ('rituals', 'description_discente', "description_discente TEXT NOT NULL DEFAULT ''"),
        ('rituals', 'description_verdadeira', "description_verdadeira TEXT NOT NULL DEFAULT ''"),
        # Anotação privada do mestre sobre um participante do combate (agente
        # ou criatura) — nunca enviada para a visão dos jogadores.
        ('combat_participants', 'admin_notes', "admin_notes TEXT NOT NULL DEFAULT ''"),
        # Tipo do inimigo cadastrado no combate: 'monstro', 'criatura' ou
        # 'comum' (inimigo comum). Criaturas antigas ficam como 'criatura'.
        ('combat_participants', 'enemy_type', "enemy_type TEXT NOT NULL DEFAULT 'criatura'"),
        # NPC da Equipe de Suporte que está no combate (kind = 'npc').
        ('combat_participants', 'npc_id', 'npc_id INTEGER'),
        # Dados de iniciativa das criaturas/ameaças, ex: '6d20+35'. Vazio =
        # comportamento antigo (1d20 + initiative_bonus).
        ('combat_participants', 'initiative_dice', "initiative_dice TEXT NOT NULL DEFAULT ''"),
        # VD (Valor de Desafio) da criatura/ameaça no combate. NULL = sem VD.
        ('combat_participants', 'challenge', 'challenge INTEGER'),
        # 1 = a iniciativa deste inimigo (criatura ou NPC) fica oculta para os
        # jogadores; só o mestre vê o valor. O mestre pode revelar a qualquer momento.
        ('combat_participants', 'initiative_hidden', 'initiative_hidden INTEGER NOT NULL DEFAULT 0'),
        # Estado do agente (Ferido, Inconsciente etc.), mesma lista dos NPCs.
        ('users', 'state', "state TEXT NOT NULL DEFAULT ''"),
    ):
        existing_cols = [row[1] for row in cur.execute(f'PRAGMA table_info({table})').fetchall()]
        if column not in existing_cols:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN {coldef}')
            if table == 'rituals' and column in ('pe_cost_discente', 'pe_cost_verdadeira'):
                # Rituais cadastrados antes de existirem os dois custos
                # separados: usa o custo único que já tinham como ponto de
                # partida para as duas versões (o mestre pode ajustar depois).
                cur.execute(f'UPDATE rituals SET {column} = pe_cost WHERE {column} = 0')
            if table == 'rituals' and column == 'circle':
                # Rituais já cadastrados antes do campo "círculo" existir:
                # estima o círculo de cada um a partir do custo em PE que já
                # tinham, usando a tabela círculo -> PE (1º=1, 2º=3, 3º=6, 4º=10).
                circle_pe_costs = {1: 1, 2: 3, 3: 6, 4: 10}
                cur.execute('SELECT id, pe_cost FROM rituals')
                for ritual_id, pe_cost in cur.fetchall():
                    best_circle = min(
                        circle_pe_costs,
                        key=lambda c: abs(circle_pe_costs[c] - (pe_cost or 0))
                    )
                    cur.execute('UPDATE rituals SET circle = ? WHERE id = ?', (best_circle, ritual_id))
    conn.commit()

    # Preenche a cor "congelada" das mensagens antigas (que ainda não tinham
    # esse campo) com a cor atual do autor, para não quebrar a exibição.
    # A partir de agora, cada nova mensagem já grava a cor do autor no
    # momento do envio, então trocar de cor depois não afeta mensagens antigas.
    cur.execute('''
        UPDATE chat_messages
        SET color = (SELECT color FROM users WHERE users.id = chat_messages.user_id)
        WHERE color IS NULL
    ''')
    conn.commit()

    # Preenche a ordem de itens já existentes (criados antes do recurso de
    # arrastar-e-organizar) usando o próprio id, para manter a ordem atual.
    cur.execute('UPDATE items SET sort_order = id WHERE sort_order = 0')
    conn.commit()

    # Mesma ideia para rituais já cadastrados antes do recurso de
    # arrastar-e-organizar existir, para manter a ordem atual.
    cur.execute('UPDATE rituals SET sort_order = id WHERE sort_order = 0')
    conn.commit()

    # Usuários que ainda não têm um marcador de "última mensagem lida"
    # (recurso novo) começam já em dia com o chat, em vez de verem todo o
    # histórico existente como não lido de uma vez.
    cur.execute('''
        UPDATE users
        SET last_read_chat_id = (SELECT COALESCE(MAX(id), 0) FROM chat_messages)
        WHERE last_read_chat_id = 0
    ''')
    conn.commit()

    cur.execute('SELECT COUNT(*) FROM users WHERE is_admin = 1')
    if cur.fetchone()[0] == 0:
        cur.execute(
            'INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)',
            ('admin', generate_password_hash('admin123'))
        )
        conn.commit()
        print("Conta admin criada -> usuario: admin | senha: admin123")

    cur.execute('SELECT COUNT(*) FROM campaign WHERE id = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO campaign (id, name, description, banner) VALUES (1, ?, ?, NULL)', ('', ''))
        conn.commit()

    conn.close()
