from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
from pathlib import Path
from datetime import datetime
import csv
import io
import click
import os
import threading

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('SQLITE_PATH') or ('/tmp/helpdesk_pro.db' if os.environ.get('VERCEL') else BASE_DIR / 'helpdesk_pro.db'))
DB_INIT_LOCK = threading.Lock()
DB_INITIALIZED = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key-for-production')

STATUS_OPTIONS = ['Нова', 'В роботі', 'Очікує користувача', 'Виконано', 'Закрито']
PRIORITY_OPTIONS = ['Низький', 'Середній', 'Високий', 'Критичний']
CATEGORY_OPTIONS = ['Обладнання', 'Програмне забезпечення', 'Мережа', 'Доступи', 'Інше']


def normalize_choice(value, options, default):
    return value if value in options else default


def parse_optional_int(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    global DB_INITIALIZED
    if DB_INITIALIZED and DB_PATH.exists():
        return

    with DB_INIT_LOCK:
        if DB_INITIALIZED and DB_PATH.exists():
            return

        init_db_unlocked()
        DB_INITIALIZED = True


def init_db_unlocked():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Нова',
            created_by INTEGER NOT NULL,
            assigned_to INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(created_by) REFERENCES users(id),
            FOREIGN KEY(assigned_to) REFERENCES users(id)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')

    # Автооновлення структури БД для нової версії застосунку.
    # Якщо база вже існує, потрібні поля додаються автоматично.
    existing_columns = {row['name'] for row in cur.execute('PRAGMA table_info(tickets)').fetchall()}
    migrations = {
        'requester_name': 'TEXT',
        'requester_email': 'TEXT',
        'requester_phone': 'TEXT',
        'department': 'TEXT',
        'location': 'TEXT',
        'inventory_number': 'TEXT',
        'due_date': 'TEXT',
        'resolution': 'TEXT'
    }
    for column, column_type in migrations.items():
        if column not in existing_columns:
            cur.execute(f'ALTER TABLE tickets ADD COLUMN {column} {column_type}')

    cur.execute('SELECT COUNT(*) AS count FROM users')
    if cur.fetchone()['count'] == 0:
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        users = [
            ('Адміністратор системи', 'admin@example.com', generate_password_hash('admin123'), 'admin', now),
            ('Користувач практики', 'user@example.com', generate_password_hash('user123'), 'user', now),
            ('IT-спеціаліст', 'tech@example.com', generate_password_hash('tech123'), 'admin', now),
        ]
        cur.executemany('''
            INSERT OR IGNORE INTO users(full_name, email, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', users)

    cur.execute('SELECT COUNT(*) AS count FROM tickets')
    if cur.fetchone()['count'] == 0:
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        admin_id = cur.execute('SELECT id FROM users WHERE email = ?', ('admin@example.com',)).fetchone()['id']
        user_id = cur.execute('SELECT id FROM users WHERE email = ?', ('user@example.com',)).fetchone()['id']
        tech_id = cur.execute('SELECT id FROM users WHERE email = ?', ('tech@example.com',)).fetchone()['id']
        sample_tickets = [
            ('Не працює Wi-Fi у кабінеті', 'Користувачі не можуть підключитися до бездротової мережі у навчальній аудиторії.', 'Мережа', 'Високий', 'В роботі', user_id, admin_id, now, now, 'Користувач практики', 'user@example.com', '+380000000000', 'Навчальний відділ', 'Кабінет 305', 'AP-305'),
            ('Потрібно встановити офісний пакет', 'На новому комп’ютері відсутнє програмне забезпечення для роботи з документами.', 'Програмне забезпечення', 'Середній', 'Нова', user_id, None, now, now, 'Користувач практики', 'user@example.com', '+380000000000', 'Бібліотека', 'Кабінет 112', 'PC-112-04'),
            ('Заблоковано обліковий запис', 'Користувач не може увійти до внутрішньої інформаційної системи.', 'Доступи', 'Критичний', 'Очікує користувача', user_id, tech_id, now, now, 'Користувач практики', 'user@example.com', '+380000000000', 'Деканат', 'Кабінет 201', ''),
        ]
        cur.executemany('''
            INSERT INTO tickets(title, description, category, priority, status, created_by, assigned_to, created_at, updated_at,
                                requester_name, requester_email, requester_phone, department, location, inventory_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', sample_tickets)
    conn.commit()
    conn.close()


def recreate_db():
    global DB_INITIALIZED
    if DB_PATH.exists():
        DB_PATH.unlink()
    DB_INITIALIZED = False
    init_db()


@app.cli.command('init-db')
def init_db_command():
    """Створити або безпечно оновити структуру бази даних."""
    init_db()
    click.echo(f'Базу даних ініціалізовано: {DB_PATH}')


@app.cli.command('reset-db')
@click.option('--yes', is_flag=True, help='Підтвердити видалення старої бази без додаткового запиту.')
def reset_db_command(yes):
    """Пересоздати базу даних із тестовими даними."""
    if not yes and not click.confirm('Видалити поточну SQLite базу та створити нову?'):
        click.echo('Операцію скасовано.')
        return
    recreate_db()
    click.echo(f'Базу даних пересоздано: {DB_PATH}')


@app.before_request
def ensure_db():
    if request.endpoint == 'static' or request.path in ('/favicon.ico', '/favicon.png'):
        return
    init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash('Увійдіть у систему, щоб продовжити.', 'warning')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Ця дія доступна лише адміністратору.', 'danger')
            return redirect(url_for('dashboard'))
        return view(*args, **kwargs)
    return wrapped


@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['full_name'] = user['full_name']
            session['email'] = user['email']
            session['role'] = user['role']
            flash('Вхід виконано успішно.', 'success')
            return redirect(url_for('dashboard'))
        flash('Невірний email або пароль.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Ви вийшли із системи.', 'info')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    search = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    priority_filter = request.args.get('priority', '').strip()
    filters = []
    params = []

    if session['role'] != 'admin':
        filters.append('t.created_by = ?')
        params.append(session['user_id'])
    if search:
        filters.append('''
            (
                t.title LIKE ? OR t.description LIKE ? OR t.requester_name LIKE ? OR
                t.department LIKE ? OR t.location LIKE ? OR t.inventory_number LIKE ?
            )
        ''')
        like = f'%{search}%'
        params.extend([like, like, like, like, like, like])
    if status_filter in STATUS_OPTIONS:
        filters.append('t.status = ?')
        params.append(status_filter)
    if priority_filter in PRIORITY_OPTIONS:
        filters.append('t.priority = ?')
        params.append(priority_filter)

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ''
    tickets = conn.execute(f'''
        SELECT t.*, u.full_name AS author_name, a.full_name AS assignee_name
        FROM tickets t
        JOIN users u ON t.created_by = u.id
        LEFT JOIN users a ON t.assigned_to = a.id
        {where_clause}
        ORDER BY t.updated_at DESC
    ''', params).fetchall()

    if session['role'] == 'admin':
        stats_scope = ''
        stats_params = []
    else:
        stats_scope = 'WHERE created_by = ?'
        stats_params = [session['user_id']]

    stats = conn.execute(f'''
        SELECT status, COUNT(*) AS count
        FROM tickets
        {stats_scope}
        GROUP BY status
    ''', stats_params).fetchall()
    total = conn.execute(f'SELECT COUNT(*) AS count FROM tickets {stats_scope}', stats_params).fetchone()['count']
    critical_params = stats_params + ['Критичний']
    critical_where = f'{stats_scope} AND priority = ?' if stats_scope else 'WHERE priority = ?'
    critical = conn.execute(f'SELECT COUNT(*) AS count FROM tickets {critical_where}', critical_params).fetchone()['count']
    conn.close()
    active_filters = {
        'q': search,
        'status': status_filter if status_filter in STATUS_OPTIONS else '',
        'priority': priority_filter if priority_filter in PRIORITY_OPTIONS else ''
    }
    return render_template('dashboard.html', tickets=tickets, stats=stats, total=total, critical=critical,
                           statuses=STATUS_OPTIONS, priorities=PRIORITY_OPTIONS, active_filters=active_filters)


@app.route('/tickets/new', methods=['GET', 'POST'])
@login_required
def new_ticket():
    conn = get_db()
    admins = conn.execute("SELECT id, full_name FROM users WHERE role = 'admin' ORDER BY full_name").fetchall()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category = normalize_choice(request.form.get('category'), CATEGORY_OPTIONS, 'Інше')
        priority = normalize_choice(request.form.get('priority'), PRIORITY_OPTIONS, 'Середній')
        requester_name = request.form.get('requester_name', '').strip()
        requester_email = request.form.get('requester_email', '').strip() or session.get('email', '')
        requester_phone = request.form.get('requester_phone', '').strip()
        department = request.form.get('department', '').strip()
        location = request.form.get('location', '').strip()
        inventory_number = request.form.get('inventory_number', '').strip()
        due_date = request.form.get('due_date', '').strip()
        status = 'Нова'
        assigned_to = None

        if not title or not description or not requester_name or not location:
            flash('Заповніть назву проблеми, ПІБ заявника, місце та опис заявки.', 'warning')
            conn.close()
            return redirect(url_for('new_ticket'))
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.execute('''
            INSERT INTO tickets(
                title, description, category, priority, status, created_by, assigned_to, created_at, updated_at,
                requester_name, requester_email, requester_phone, department, location, inventory_number, due_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (title, description, category, priority, status, session['user_id'], assigned_to, now, now,
              requester_name, requester_email, requester_phone, department, location, inventory_number, due_date))
        conn.commit()
        conn.close()
        flash('Заявку створено.', 'success')
        return redirect(url_for('dashboard'))
    conn.close()
    return render_template('ticket_form.html', categories=CATEGORY_OPTIONS, priorities=PRIORITY_OPTIONS,
                           statuses=STATUS_OPTIONS, admins=admins)

@app.route('/tickets/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def ticket_detail(ticket_id):
    conn = get_db()
    ticket = conn.execute('''
        SELECT t.*, u.full_name AS author_name, a.full_name AS assignee_name
        FROM tickets t
        JOIN users u ON t.created_by = u.id
        LEFT JOIN users a ON t.assigned_to = a.id
        WHERE t.id = ?
    ''', (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        flash('Заявку не знайдено.', 'danger')
        return redirect(url_for('dashboard'))
    if session['role'] != 'admin' and ticket['created_by'] != session['user_id']:
        conn.close()
        flash('Немає доступу до цієї заявки.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if body:
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            conn.execute('INSERT INTO comments(ticket_id, user_id, body, created_at) VALUES (?, ?, ?, ?)',
                         (ticket_id, session['user_id'], body, now))
            conn.execute('UPDATE tickets SET updated_at = ? WHERE id = ?', (now, ticket_id))
            conn.commit()
            flash('Коментар додано.', 'success')
        conn.close()
        return redirect(url_for('ticket_detail', ticket_id=ticket_id))

    comments = conn.execute('''
        SELECT c.*, u.full_name
        FROM comments c
        JOIN users u ON c.user_id = u.id
        WHERE c.ticket_id = ?
        ORDER BY c.created_at ASC
    ''', (ticket_id,)).fetchall()
    admins = conn.execute("SELECT id, full_name FROM users WHERE role = 'admin' ORDER BY full_name").fetchall()
    conn.close()
    return render_template('ticket_detail.html', ticket=ticket, comments=comments, admins=admins,
                           statuses=STATUS_OPTIONS, priorities=PRIORITY_OPTIONS)


@app.route('/tickets/<int:ticket_id>/update', methods=['POST'])
@login_required
@admin_required
def update_ticket(ticket_id):
    status = normalize_choice(request.form.get('status'), STATUS_OPTIONS, 'Нова')
    priority = normalize_choice(request.form.get('priority'), PRIORITY_OPTIONS, 'Середній')
    assigned_to = parse_optional_int(request.form.get('assigned_to'))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = get_db()
    conn.execute('''
        UPDATE tickets
        SET status = ?, priority = ?, assigned_to = ?, updated_at = ?
        WHERE id = ?
    ''', (status, priority, assigned_to, now, ticket_id))
    conn.commit()
    conn.close()
    flash('Заявку оновлено.', 'success')
    return redirect(url_for('ticket_detail', ticket_id=ticket_id))


@app.route('/tickets/<int:ticket_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_ticket(ticket_id):
    conn = get_db()
    conn.execute('DELETE FROM comments WHERE ticket_id = ?', (ticket_id,))
    conn.execute('DELETE FROM tickets WHERE id = ?', (ticket_id,))
    conn.commit()
    conn.close()
    flash('Заявку видалено.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/reports')
@login_required
@admin_required
def reports():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) AS count FROM tickets').fetchone()['count']
    by_category = conn.execute('SELECT category, COUNT(*) AS count FROM tickets GROUP BY category ORDER BY count DESC').fetchall()
    by_priority = conn.execute('SELECT priority, COUNT(*) AS count FROM tickets GROUP BY priority ORDER BY count DESC').fetchall()
    by_status = conn.execute('SELECT status, COUNT(*) AS count FROM tickets GROUP BY status ORDER BY count DESC').fetchall()
    by_assignee = conn.execute('''
        SELECT COALESCE(u.full_name, 'Не призначено') AS assignee_name, COUNT(*) AS count
        FROM tickets t
        LEFT JOIN users u ON t.assigned_to = u.id
        GROUP BY COALESCE(u.full_name, 'Не призначено')
        ORDER BY count DESC, assignee_name ASC
    ''').fetchall()
    conn.close()
    return render_template('reports.html', total=total, by_category=by_category, by_priority=by_priority,
                           by_status=by_status, by_assignee=by_assignee)


@app.route('/export.csv')
@login_required
@admin_required
def export_csv():
    conn = get_db()
    rows = conn.execute('''
        SELECT t.id, t.title, t.description, t.requester_name, t.requester_email, t.requester_phone, t.department, t.location,
               t.inventory_number, t.category, t.priority, t.status, t.created_by, u.full_name AS author_name,
               t.assigned_to, COALESCE(a.full_name, '') AS assignee_name, t.due_date, COALESCE(t.resolution, '') AS resolution,
               t.created_at, t.updated_at
        FROM tickets t
        JOIN users u ON t.created_by = u.id
        LEFT JOIN users a ON t.assigned_to = a.id
        ORDER BY t.id DESC
    ''').fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Назва проблеми', 'Детальний опис', 'ПІБ заявника', 'Email', 'Телефон',
                     'Підрозділ / група', 'Місце виникнення проблеми', 'Пристрій або інвентарний номер',
                     'Категорія', 'Пріоритет', 'Статус', 'ID автора', 'Автор запису', 'ID виконавця',
                     'Виконавець', 'Планова дата виконання', 'Результат виконання', 'Створено', 'Оновлено'])
    for row in rows:
        writer.writerow([row['id'], row['title'], row['description'], row['requester_name'], row['requester_email'],
                         row['requester_phone'], row['department'], row['location'], row['inventory_number'],
                         row['category'], row['priority'], row['status'], row['created_by'], row['author_name'],
                         row['assigned_to'], row['assignee_name'], row['due_date'], row['resolution'],
                         row['created_at'], row['updated_at']])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=helpdesk_report.csv'})


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
