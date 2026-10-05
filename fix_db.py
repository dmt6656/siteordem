"""
Script de correção pontual do banco de dados.

Use este script se aparecer um erro do tipo:
    sqlite3.OperationalError: no such column: character_origin
    (ou qualquer outra coluna faltando)

Ele adiciona, no seu rpg.db JÁ EXISTENTE, qualquer coluna que ainda
esteja faltando — sem apagar nenhum dado (personagens, itens, rituais,
mensagens de chat, etc. continuam intactos).

Como usar:
1. Coloque este arquivo (fix_db.py) na mesma pasta onde está o rpg.db
   (a mesma pasta do app.py).
2. Feche o servidor Flask (Ctrl+C no terminal onde ele está rodando).
3. Rode:  python fix_db.py
4. Rode o app normalmente de novo:  python app.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'preservar', 'rpg.db')

# Lista de todas as colunas que o sistema espera existir, e a definição
# usada para criá-las caso estejam faltando.
COLUMNS = [
    ('users', 'avatar', 'avatar TEXT'),
    ('users', 'avatar_size', 'avatar_size INTEGER NOT NULL DEFAULT 160'),
    ('users', 'is_online', 'is_online INTEGER NOT NULL DEFAULT 0'),
    ('users', 'character_name', "character_name TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_history', "character_history TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_physical', "character_physical TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_description', "character_description TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_locked', 'character_locked INTEGER NOT NULL DEFAULT 0'),
    ('rituals', 'symbol', 'symbol TEXT'),
    ('skills', 'bonus', "bonus TEXT NOT NULL DEFAULT ''"),
    ('users', 'attributes_locked', 'attributes_locked INTEGER NOT NULL DEFAULT 0'),
    ('users', 'color', "color TEXT NOT NULL DEFAULT '#c62f27'"),
    ('users', 'points_locked', 'points_locked INTEGER NOT NULL DEFAULT 0'),
    ('users', 'character_origin', "character_origin TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_class', "character_class TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_patente', "character_patente TEXT NOT NULL DEFAULT ''"),
    ('users', 'character_money', 'character_money INTEGER NOT NULL DEFAULT 0'),
    ('users', 'character_nex', 'character_nex INTEGER NOT NULL DEFAULT 0'),
    ('users', 'character_notes', "character_notes TEXT NOT NULL DEFAULT ''"),
    ('mural_images', 'user_id', 'user_id INTEGER'),
    ('mural_images', 'username', "username TEXT NOT NULL DEFAULT ''"),
    ('mural_images', 'category', "category TEXT NOT NULL DEFAULT ''"),
    ('users', 'capacidade_locked', 'capacidade_locked INTEGER NOT NULL DEFAULT 0'),
    ('items', 'level', "level TEXT NOT NULL DEFAULT '0'"),
    ('items', 'item_type', "item_type TEXT NOT NULL DEFAULT ''"),
    ('items', 'damage', "damage TEXT NOT NULL DEFAULT ''"),
    ('items', 'critical', "critical TEXT NOT NULL DEFAULT ''"),
    ('items', 'defense', 'defense INTEGER NOT NULL DEFAULT 0'),
    ('class_abilities', 'pe_cost', 'pe_cost INTEGER NOT NULL DEFAULT 0'),
    ('skills', 'attr_override', "attr_override TEXT NOT NULL DEFAULT ''"),
]


def main():
    if not os.path.exists(DB_PATH):
        print(f"Nenhum rpg.db encontrado em: {DB_PATH}")
        print("Verifique se este script está na mesma pasta do app.py.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    tables = {row[0] for row in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    added = []
    for table, column, coldef in COLUMNS:
        if table not in tables:
            continue
        existing_cols = [row[1] for row in cur.execute(f'PRAGMA table_info({table})').fetchall()]
        if column not in existing_cols:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN {coldef}')
            added.append(f'{table}.{column}')

    conn.commit()
    conn.close()

    if added:
        print("Colunas adicionadas com sucesso:")
        for col in added:
            print(f'  - {col}')
    else:
        print("Nenhuma coluna faltando. O banco já está atualizado.")


if __name__ == '__main__':
    main()
