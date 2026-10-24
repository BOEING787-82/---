import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog, scrolledtext
import sqlite3
import re
from datetime import datetime, timedelta
import random
import os
import threading
import time
import feedback_common as fc
import transfer_executor as te

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# 用户角色常量
ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"

# 数据库路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK_DB_PATH = os.path.join(BASE_DIR, 'bank_system.db')
HOUSEHOLD_DB_PATH = os.path.join(BASE_DIR, 'household_registration.db')
STUDENT_DB_PATH = os.path.join(BASE_DIR, 'student_registration.db')
COURT_DB_PATH = os.path.join(BASE_DIR, 'court_system.db')
PHONE_DB_PATH = os.path.join(BASE_DIR, 'phone_system.db')
ENTERPRISE_DB_PATH = os.path.join(BASE_DIR, 'enterprise_system.db')


class BankSystem:
    def __init__(self):
        self.current_user = None
        self.current_role = None
        self.current_customer_id = None

        self.root = tk.Tk()
        self.root.title("蛋国银行系统")
        self.root.geometry("1400x800")

        self.large_amount_limits = {
            'deposit': 500000,
            'withdraw': 500000,
            'transfer': 500000,
            'loan': 10000000
        }
        self.stocks = {}
        # 验证码存储：{phone: (code, expire_time)}
        self.verification_codes = {}

        self.init_database()
        self.init_stocks()

        # 后台线程：轮询执行到期的定时转账计划（客户端批量/定时转账）
        threading.Thread(target=self._plan_executor_loop, daemon=True).start()

        self.show_login()

    def _plan_executor_loop(self):
        """后台线程：轮询执行到期的定时转账计划（客户端批量转账/定时转账）"""
        while True:
            try:
                te.execute_ready_plans()
            except Exception:
                pass
            time.sleep(15)

    def init_database(self):
        self.conn = sqlite3.connect(BANK_DB_PATH)
        self.cursor = self.conn.cursor()

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user', 'manager')),
                created_at TEXT NOT NULL
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number TEXT UNIQUE NOT NULL,
                id_number TEXT NOT NULL,
                name TEXT NOT NULL,
                password TEXT NOT NULL,
                balance REAL DEFAULT 0.0,
                credit_score INTEGER DEFAULT 100,
                account_type TEXT DEFAULT '储蓄账户',
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                address TEXT DEFAULT '',
                create_time TEXT NOT NULL,
                last_update_time TEXT NOT NULL,
                is_frozen INTEGER DEFAULT 0,
                failed_attempts INTEGER DEFAULT 0
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                loan_type TEXT DEFAULT '普通贷款',
                loan_amount REAL NOT NULL,
                interest_rate REAL NOT NULL,
                term_months INTEGER NOT NULL,
                remaining_amount REAL NOT NULL,
                monthly_payment REAL NOT NULL,
                status TEXT DEFAULT 'active',
                start_date TEXT NOT NULL,
                next_payment_date TEXT NOT NULL,
                student_id TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER NOT NULL,
                payment_amount REAL NOT NULL,
                payment_date TEXT NOT NULL,
                FOREIGN KEY (loan_id) REFERENCES loans (id)
            )
        ''')

        # 转账计划表：客户端批量转账 / 定时转账
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS transfer_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                from_card TEXT NOT NULL,
                plan_name TEXT DEFAULT '',
                item_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                execute_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_time TEXT NOT NULL,
                executed_time TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS transfer_plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                to_card TEXT NOT NULL,
                to_name TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                execute_time TEXT,
                error_msg TEXT DEFAULT '',
                FOREIGN KEY (plan_id) REFERENCES transfer_plans (id)
            )
        ''')
        self.conn.commit()

        # 信用卡账户：授信额度字段（信用卡=20000，其他账户=0）
        try:
            self.cursor.execute("ALTER TABLE customers ADD COLUMN credit_limit REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # 企业账户：企业卡字段（开设企业卡片 / 变更法人专用）
        enterprise_col_alter = [
            ("enterprise_id", "TEXT DEFAULT ''"),
            ("enterprise_name", "TEXT DEFAULT ''"),
            ("unified_credit_code", "TEXT DEFAULT ''"),
            ("legal_rep_name", "TEXT DEFAULT ''"),
            ("legal_rep_id_number", "TEXT DEFAULT ''"),
            ("is_enterprise", "INTEGER DEFAULT 0"),
        ]
        for col, decl in enterprise_col_alter:
            try:
                self.cursor.execute(f"ALTER TABLE customers ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

        # ========== 贷款模块升级（真实银行功能） ==========
        # 贷款产品表：可配置利率、额度区间、期限区间
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT UNIQUE NOT NULL,
                annual_rate REAL NOT NULL,
                min_amount REAL NOT NULL,
                max_amount REAL NOT NULL,
                min_term INTEGER NOT NULL,
                max_term INTEGER NOT NULL,
                description TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1
            )
        ''')
        # 还款计划表：每期本金/利息/本息合计/剩余本金
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER NOT NULL,
                period_no INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                principal_part REAL NOT NULL,
                interest_part REAL NOT NULL,
                total_due REAL NOT NULL,
                balance_after REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                paid_date TEXT,
                paid_amount REAL DEFAULT 0,
                FOREIGN KEY (loan_id) REFERENCES loans (id)
            )
        ''')
        # loans 表升级：合同编号/还款方式/用途/利息台账/逾期台账
        loan_col_alter = [
            ("contract_no", "TEXT"),
            ("repayment_method", "TEXT DEFAULT '等额本息'"),
            ("purpose", "TEXT DEFAULT ''"),
            ("total_interest", "REAL DEFAULT 0"),
            ("paid_principal", "REAL DEFAULT 0"),
            ("paid_interest", "REAL DEFAULT 0"),
            ("penalty_interest", "REAL DEFAULT 0"),
            ("is_overdue", "INTEGER DEFAULT 0"),
            ("overdue_days", "INTEGER DEFAULT 0"),
        ]
        for col, decl in loan_col_alter:
            try:
                self.cursor.execute(f"ALTER TABLE loans ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        # loan_payments 表升级：本金/利息/罚息拆分
        pay_col_alter = [
            ("principal_part", "REAL DEFAULT 0"),
            ("interest_part", "REAL DEFAULT 0"),
            ("penalty", "REAL DEFAULT 0"),
            ("period_no", "INTEGER"),
            ("payment_type", "TEXT DEFAULT '定期还款'"),
        ]
        for col, decl in pay_col_alter:
            try:
                self.cursor.execute(f"ALTER TABLE loan_payments ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        # loan_approvals 表升级：还款方式/用途
        app_col_alter = [
            ("repayment_method", "TEXT DEFAULT '等额本息'"),
            ("purpose", "TEXT DEFAULT ''"),
        ]
        for col, decl in app_col_alter:
            try:
                self.cursor.execute(f"ALTER TABLE loan_approvals ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        # 初始化贷款产品目录
        self.cursor.execute("SELECT COUNT(*) FROM loan_products")
        if self.cursor.fetchone()[0] == 0:
            products = [
                ("住房贷款", 3.45, 100000, 10000000, 12, 360, "用于购买自住住房，期限最长30年"),
                ("汽车贷款", 4.2, 30000, 5000000, 6, 60, "用于购买汽车，期限最长5年"),
                ("装修贷款", 4.9, 10000, 1000000, 6, 60, "用于房屋装修，期限最长5年"),
                ("消费贷款", 7.0, 1000, 500000, 3, 36, "用于日常消费，无需抵押"),
                ("经营贷款", 6.2, 50000, 10000000, 6, 120, "用于企业经营周转，需提供经营证明"),
                ("助学贷款", 3.85, 1000, 80000, 12, 120, "面向在校学生，国家贴息优惠利率，需学籍核验"),
                ("信用贷款", 8.5, 5000, 300000, 3, 36, "纯信用贷款，额度由信用评分决定"),
            ]
            for p in products:
                self.cursor.execute('''
                    INSERT INTO loan_products (product_name, annual_rate, min_amount, max_amount, min_term, max_term, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', p)
        self.conn.commit()

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_card TEXT NOT NULL,
                to_card TEXT NOT NULL,
                amount REAL NOT NULL,
                transaction_type TEXT NOT NULL,
                transaction_time TEXT NOT NULL
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_type TEXT NOT NULL,
                operator_id INTEGER NOT NULL,
                operation_type TEXT NOT NULL,
                details TEXT,
                operation_time TEXT NOT NULL,
                ip_address TEXT DEFAULT '127.0.0.1'
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                stock_symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                purchase_price REAL NOT NULL,
                purchase_date TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                stock_symbol TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                total_amount REAL NOT NULL,
                transaction_time TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                change REAL DEFAULT 0,
                volume INTEGER DEFAULT 0
            )
        ''')
        self.conn.commit()

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS large_amount_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                amount REAL NOT NULL,
                target_account TEXT,
                apply_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                review_time TEXT,
                reviewer_id INTEGER,
                processed INTEGER DEFAULT 0,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                loan_type TEXT DEFAULT '普通贷款',
                loan_amount REAL NOT NULL,
                interest_rate REAL NOT NULL,
                term_months INTEGER NOT NULL,
                monthly_payment REAL NOT NULL,
                student_id TEXT,
                apply_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                review_time TEXT,
                reviewer_id INTEGER,
                review_note TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        self.cursor.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if self.cursor.fetchone()[0] == 0:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute(
                "INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)",
                ('admin', 'admin123', 'admin', now)
            )

        self.cursor.execute("SELECT COUNT(*) FROM users WHERE username='user'")
        if self.cursor.fetchone()[0] == 0:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute(
                "INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)",
                ('user', '123456', 'user', now)
            )

        # 数据库升级：为旧版本users表添加manager角色支持（重建CHECK约束）
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute(
                "INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)",
                ('manager', 'manager123', 'manager', now)
            )
        except sqlite3.IntegrityError:
            # CHECK约束不包含manager，需要重建表升级
            try:
                self.cursor.execute("PRAGMA table_info(users)")
                columns = self.cursor.fetchall()
                col_defs = ", ".join([f"{col[1]} {col[2]}" for col in columns])

                # 备份数据
                self.cursor.execute("SELECT * FROM users")
                old_users = self.cursor.fetchall()

                # 删除旧表，创建新表
                self.cursor.execute("DROP TABLE users")
                self.cursor.execute('''
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('admin', 'user', 'manager')),
                        created_at TEXT NOT NULL
                    )
                ''')

                # 恢复数据
                for user in old_users:
                    self.cursor.execute(
                        "INSERT INTO users (id, username, password, role, created_at) VALUES (?, ?, ?, ?, ?)",
                        user
                    )

                # 插入经理账号
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.cursor.execute(
                    "INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)",
                    ('manager', 'manager123', 'manager', now)
                )
            except Exception:
                pass  # 升级失败则跳过

        # 信用分补给申请表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS credit_supply_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                card_number TEXT NOT NULL,
                id_number TEXT NOT NULL,
                name TEXT NOT NULL,
                old_credit_score INTEGER NOT NULL,
                new_credit_score INTEGER DEFAULT 80,
                agreement_signed INTEGER DEFAULT 0,
                face_verify_status TEXT DEFAULT 'pending',
                captured_face_path TEXT,
                captured_face_blob BLOB,
                db_face_path TEXT,
                apply_time TEXT NOT NULL,
                supply_time TEXT,
                review_status TEXT DEFAULT 'pending',
                review_time TEXT,
                reviewer TEXT,
                review_note TEXT,
                is_reverted INTEGER DEFAULT 0,
                revert_time TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')

        # 数据库升级：为旧版本表添加新字段
        try:
            self.cursor.execute("PRAGMA table_info(credit_supply_applications)")
            existing_cols = [col[1] for col in self.cursor.fetchall()]
            if 'new_credit_score' not in existing_cols:
                self.cursor.execute("ALTER TABLE credit_supply_applications ADD COLUMN new_credit_score INTEGER DEFAULT 80")
            if 'captured_face_blob' not in existing_cols:
                self.cursor.execute("ALTER TABLE credit_supply_applications ADD COLUMN captured_face_blob BLOB")
            if 'supply_time' not in existing_cols:
                self.cursor.execute("ALTER TABLE credit_supply_applications ADD COLUMN supply_time TEXT")
            if 'is_reverted' not in existing_cols:
                self.cursor.execute("ALTER TABLE credit_supply_applications ADD COLUMN is_reverted INTEGER DEFAULT 0")
            if 'revert_time' not in existing_cols:
                self.cursor.execute("ALTER TABLE credit_supply_applications ADD COLUMN revert_time TEXT")
        except Exception:
            pass

        self.conn.commit()

    def init_stocks(self):
        self.stocks = {}
        # 从数据库加载股票
        self.cursor.execute("SELECT symbol, name, price, change, volume FROM stocks")
        rows = self.cursor.fetchall()
        if rows:
            for symbol, name, price, change, volume in rows:
                self.stocks[symbol] = {
                    'name': name,
                    'price': price,
                    'change': change,
                    'volume': volume,
                    'history': []
                }
                # 为每只股票生成初始历史价格数据（30个数据点）
                base_price = price
                history = []
                p = base_price * 0.9
                for _ in range(30):
                    p = p * (1 + random.uniform(-0.03, 0.03))
                    history.append(round(p, 2))
                history.append(base_price)
                self.stocks[symbol]['history'] = history
        else:
            # 数据库为空，初始化默认股票并写入数据库
            default_stocks = {
                'AAPL': {'name': '苹果公司', 'price': 150.25, 'change': 2.5, 'volume': 1000000},
                'GOOGL': {'name': '谷歌', 'price': 2750.80, 'change': -1.2, 'volume': 500000},
                'MSFT': {'name': '微软', 'price': 305.50, 'change': 0.8, 'volume': 750000},
                'TSLA': {'name': '特斯拉', 'price': 850.75, 'change': -3.1, 'volume': 1200000},
                'AMZN': {'name': '亚马逊', 'price': 3300.50, 'change': 1.5, 'volume': 600000}
            }
            for symbol, data in default_stocks.items():
                self.stocks[symbol] = {
                    'name': data['name'],
                    'price': data['price'],
                    'change': data['change'],
                    'volume': data['volume'],
                    'history': []
                }
                # 生成历史数据
                base_price = data['price']
                history = []
                p = base_price * 0.9
                for _ in range(30):
                    p = p * (1 + random.uniform(-0.03, 0.03))
                    history.append(round(p, 2))
                history.append(base_price)
                self.stocks[symbol]['history'] = history
                # 写入数据库
                self.cursor.execute('''
                    INSERT OR REPLACE INTO stocks (symbol, name, price, change, volume)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol, data['name'], data['price'], data['change'], data['volume']))
            self.conn.commit()

    def update_stock_prices(self):
        for symbol in self.stocks:
            change_pct = random.uniform(-5, 5)
            old_price = self.stocks[symbol]['price']
            new_price = old_price * (1 + change_pct / 100)
            self.stocks[symbol]['price'] = round(new_price, 2)
            self.stocks[symbol]['change'] = round(change_pct, 2)
            self.stocks[symbol]['volume'] = random.randint(100000, 2000000)
            # 记录历史价格，最多保留100个点（兼容缺少history字段的旧数据）
            if 'history' not in self.stocks[symbol]:
                self.stocks[symbol]['history'] = [round(old_price, 2)]
            self.stocks[symbol]['history'].append(round(new_price, 2))
            if len(self.stocks[symbol]['history']) > 100:
                self.stocks[symbol]['history'] = self.stocks[symbol]['history'][-100:]
            # 同步写入数据库
            try:
                self.cursor.execute('''
                    UPDATE stocks SET price = ?, change = ?, volume = ? WHERE symbol = ?
                ''', (round(new_price, 2), round(change_pct, 2), self.stocks[symbol]['volume'], symbol))
            except Exception:
                pass
        self.conn.commit()

    # ==================== 电话系统消息接口 ====================

    def send_phone_message(self, phone_number, content, msg_type='system'):
        """向电话系统用户发送消息（验证码/交易通知等）"""
        if not phone_number:
            return False
        try:
            phone_conn = sqlite3.connect(PHONE_DB_PATH)
            phone_cursor = phone_conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            phone_cursor.execute('''
                INSERT INTO messages (from_phone, to_phone, content, msg_type, sent_at, is_read)
                VALUES ('BANK', ?, ?, ?, ?, 0)
            ''', (phone_number, content, msg_type, now))
            phone_conn.commit()
            phone_conn.close()
            return True
        except Exception as e:
            print(f"发送电话消息失败: {e}")
            return False

    def generate_verification_code(self, phone_number):
        """生成6位随机验证码并发送到电话"""
        code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
        expire_time = datetime.now() + timedelta(minutes=5)
        self.verification_codes[phone_number] = (code, expire_time)

        content = f"【蛋国银行】您的验证码为 {code}，5分钟内有效，请勿泄露给他人。"
        success = self.send_phone_message(phone_number, content)
        return success, code

    def verify_code(self, phone_number, input_code):
        """验证验证码是否正确"""
        if phone_number not in self.verification_codes:
            return False, "请先获取验证码"
        code, expire_time = self.verification_codes[phone_number]
        if datetime.now() > expire_time:
            del self.verification_codes[phone_number]
            return False, "验证码已过期，请重新获取"
        if input_code.strip() != code:
            return False, "验证码不正确"
        # 验证成功后删除
        del self.verification_codes[phone_number]
        return True, "验证成功"

    def send_open_card_code(self, phone_entry, status_label):
        """开户时发送验证码"""
        phone = phone_entry.get().strip()
        if not phone:
            messagebox.showwarning("提示", "请先输入手机号！")
            return
        if not re.match(r'^1[3-9]\d{9}$', phone):
            messagebox.showerror("错误", "手机号格式不正确！")
            return
        success, code = self.generate_verification_code(phone)
        if success:
            status_label.config(text=f"✓ 验证码已发送至 {phone}（5分钟内有效）", fg='#27ae60')
            messagebox.showinfo("发送成功", f"验证码已发送至 {phone}\n请注意查收短信（在电话系统中查看）")
        else:
            status_label.config(text="✗ 验证码发送失败，请检查电话系统是否正常", fg='#e74c3c')
            messagebox.showerror("发送失败", "验证码发送失败，请确保电话系统已初始化数据库。")

    def send_modify_phone_code(self, phone_entry, original_phone, status_label):
        """修改手机号时发送验证码"""
        phone = phone_entry.get().strip()
        if not phone:
            messagebox.showwarning("提示", "请先输入新手机号！")
            return
        if not re.match(r'^1[3-9]\d{9}$', phone):
            messagebox.showerror("错误", "手机号格式不正确！")
            return
        if phone == original_phone[0]:
            messagebox.showinfo("提示", "新手机号与原手机号相同，无需验证。")
            return
        success, code = self.generate_verification_code(phone)
        if success:
            status_label.config(text=f"✓ 验证码已发送至新手机号 {phone}", fg='#27ae60')
            messagebox.showinfo("发送成功", f"验证码已发送至新手机号 {phone}\n请注意查收短信（在电话系统中查看）")
        else:
            status_label.config(text="✗ 验证码发送失败，请检查电话系统是否正常", fg='#e74c3c')
            messagebox.showerror("发送失败", "验证码发送失败，请确保电话系统已初始化数据库。")

    def notify_customer(self, card_number, title, content):
        """给持卡人绑定的手机号发送交易通知"""
        try:
            self.cursor.execute("SELECT phone, name FROM customers WHERE card_number = ?", (card_number,))
            result = self.cursor.fetchone()
            if result and result[0]:
                phone = result[0]
                name = result[1] or ''
                full_msg = f"【蛋国银行】{title}\n{content}"
                self.send_phone_message(phone, full_msg)
        except Exception:
            pass

    def verify_household_identity(self, id_number, name):
        """核实人员身份 - 用户籍系统数据库"""
        try:
            hh_conn = sqlite3.connect(HOUSEHOLD_DB_PATH)
            hh_cursor = hh_conn.cursor()
            hh_cursor.execute(
                "SELECT name, status FROM residents WHERE id_number = ?",
                (id_number,)
            )
            result = hh_cursor.fetchone()
            hh_conn.close()

            if result:
                db_name, status = result
                if db_name == name and status == '正常':
                    return True, "身份核验通过"
                elif db_name != name:
                    return False, "姓名与户籍登记不符"
                else:
                    return False, f"户籍状态异常：{status}"
            else:
                return False, "未在户籍系统中找到该身份证号"
        except Exception as e:
            return False, f"户籍系统连接失败：{str(e)}"

    def verify_student_identity(self, id_number, student_id=None):
        """核实学生身份 - 用学籍系统数据库（助学贷款专用）"""
        try:
            stu_conn = sqlite3.connect(STUDENT_DB_PATH)
            stu_cursor = stu_conn.cursor()

            if student_id:
                stu_cursor.execute(
                    "SELECT name, status FROM students WHERE id_number = ? AND student_id = ?",
                    (id_number, student_id)
                )
            else:
                stu_cursor.execute(
                    "SELECT name, student_id, status FROM students WHERE id_number = ?",
                    (id_number,)
                )

            result = stu_cursor.fetchone()
            stu_conn.close()

            if result:
                if student_id:
                    name, status = result
                else:
                    name, student_id, status = result

                if status == '在读':
                    return True, f"学籍核验通过，学号：{student_id}", student_id
                else:
                    return False, f"学籍状态异常：{status}", None
            else:
                return False, "未在学籍系统中找到该学生信息", None
        except Exception as e:
            return False, f"学籍系统连接失败：{str(e)}", None

    def verify_enterprise_legal_rep(self, unified_credit_code, name, id_number):
        """核实企业法人身份 - 用企业系统数据库。
        要求：统一社会信用代码存在、企业状态正常、法人姓名与身份证号必须与企业登记法人完全一致。
        返回 (ok, msg, info)；info 为核验通过后的企业信息字典或 None。"""
        try:
            ent_conn = sqlite3.connect(ENTERPRISE_DB_PATH)
            ent_cursor = ent_conn.cursor()
            ent_cursor.execute(
                "SELECT enterprise_id, enterprise_name, legal_representative, legal_rep_id_number, "
                "status, enterprise_type FROM enterprises WHERE unified_credit_code = ?",
                (unified_credit_code,)
            )
            result = ent_cursor.fetchone()
            ent_conn.close()

            if result:
                ent_id, ent_name, db_legal, db_legal_id, status, ent_type = result
                if status != '正常':
                    return False, f"企业状态异常：{status}", None
                if db_legal != name:
                    return False, f"法人姓名与企业登记法人（{db_legal}）不一致", None
                if db_legal_id != id_number:
                    return False, "法人身份证号与企业登记信息不一致", None
                return True, "企业法人核验通过", {
                    'enterprise_id': ent_id,
                    'enterprise_name': ent_name,
                    'enterprise_type': ent_type,
                    'unified_credit_code': unified_credit_code,
                    'legal_rep_name': name,
                    'legal_rep_id_number': id_number,
                }
            else:
                return False, "未在企业系统中找到该统一社会信用代码", None
        except Exception as e:
            return False, f"企业系统连接失败：{str(e)}", None

    def add_operation_log(self, operation_type, details=""):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        operator_id = self.current_customer_id if self.current_customer_id else 0
        operator_type = self.current_role if self.current_role else 'guest'

        self.cursor.execute('''
            INSERT INTO operation_logs (operator_type, operator_id, operation_type, details, operation_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (operator_type, operator_id, operation_type, details, now))
        self.conn.commit()

    def clear_main_frame(self):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def generate_card_number(self):
        prefix = "6222"
        number = ''.join([str(random.randint(0, 9)) for _ in range(12)])
        return prefix + number

    # ==================== 登录系统 ====================

    def show_login(self):
        for widget in self.root.winfo_children():
            widget.destroy()

        login_frame = tk.Frame(self.root, bg='white')
        login_frame.pack(expand=True)

        title_label = tk.Label(login_frame, text="蛋国银行系统登录", font=("Arial", 24, "bold"),
                               bg='white', fg='#2c3e50')
        title_label.pack(pady=40)

        form_frame = tk.Frame(login_frame, bg='white')
        form_frame.pack(pady=20)

        tk.Label(form_frame, text="用户名：", font=("Arial", 12), bg='white').grid(row=0, column=0,
                                                                                  padx=15, pady=15, sticky=tk.E)
        self.username_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        self.username_entry.grid(row=0, column=1, padx=15, pady=15)
        self.username_entry.insert(0, "admin")

        tk.Label(form_frame, text="密码：", font=("Arial", 12), bg='white').grid(row=1, column=0,
                                                                                padx=15, pady=15, sticky=tk.E)
        self.password_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2,
                                       relief=tk.GROOVE, show="*")
        self.password_entry.grid(row=1, column=1, padx=15, pady=15)
        self.password_entry.insert(0, "admin123")

        login_btn = tk.Button(login_frame, text="登录", command=self.login,
                              width=20, height=2, bg='#3498db', fg='white',
                              font=("Arial", 12), relief=tk.FLAT)
        login_btn.pack(pady=20)
        login_btn.bind("<Enter>", lambda e: login_btn.config(bg='#2980b9'))
        login_btn.bind("<Leave>", lambda e: login_btn.config(bg='#3498db'))

        tip_label = tk.Label(login_frame, text="管理员账号: admin / admin123\n银行经理: manager / manager123\n普通用户: user / 123456",
                             font=("Arial", 9), bg='white', fg='#7f8c8d')
        tip_label.pack(pady=10)

    def login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()

        if not username or not password:
            messagebox.showerror("错误", "用户名和密码不能为空！")
            return

        self.cursor.execute('''
            SELECT id, username, role FROM users 
            WHERE username=? AND password=?
        ''', (username, password))

        user = self.cursor.fetchone()
        if not user:
            messagebox.showerror("错误", "用户名或密码不正确！")
            return

        self.current_user = user[1]
        self.current_role = user[2]
        self.current_customer_id = None

        self.add_operation_log("用户登录", f"用户 {username} 登录系统")
        self.create_widgets()

    def logout(self):
        self.add_operation_log("退出登录", f"用户 {self.current_user} 退出系统")
        self.current_user = None
        self.current_role = None
        self.current_customer_id = None
        self.show_login()

    # ==================== 主界面 ====================

    def create_widgets(self):
        for widget in self.root.winfo_children():
            widget.destroy()

        # 顶部状态栏
        status_bar = tk.Frame(self.root, height=30, bg='#34495e')
        status_bar.pack(side=tk.TOP, fill=tk.X)

        role_text = "管理员" if self.current_role == ROLE_ADMIN else ("银行经理" if self.current_role == ROLE_MANAGER else "普通用户")
        user_label = tk.Label(status_bar, text=f"当前用户：{self.current_user}（{role_text}）",
                              bg='#34495e', fg='white', font=("Arial", 10))
        user_label.pack(side=tk.LEFT, padx=20)

        logout_btn = tk.Button(status_bar, text="退出登录", command=self.logout,
                               bg='#e74c3c', fg='white', font=("Arial", 10), relief=tk.FLAT)
        logout_btn.pack(side=tk.RIGHT, padx=20, pady=5)
        logout_btn.bind("<Enter>", lambda e: logout_btn.config(bg='#c0392b'))
        logout_btn.bind("<Leave>", lambda e: logout_btn.config(bg='#e74c3c'))

        # 左侧菜单栏
        menu_container = tk.Frame(self.root, width=200, bg='#f0f0f0')
        menu_container.pack(side=tk.LEFT, fill=tk.Y)
        menu_container.pack_propagate(False)

        scrollbar = tk.Scrollbar(menu_container)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        canvas = tk.Canvas(menu_container, yscrollcommand=scrollbar.set, width=180, bg='#f0f0f0')
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=canvas.yview)

        self.menu_frame = tk.Frame(canvas, bg='#f0f0f0')
        canvas.create_window((0, 0), window=self.menu_frame, anchor="nw")

        menu_items = [
            ("🏠 首页展示", self.show_welcome),
            ("💳 开卡开户", self.show_open_card),
            ("💰 查询余额", self.show_query_balance),
            ("📋 账户信息", self.show_account_info),
            ("💵 存款业务", self.show_deposit),
            ("💴 取款业务", self.show_withdraw),
            ("🔄 转账业务", self.show_transfer),
            ("🔐 修改密码", self.show_change_password),
            ("✏️ 修改个人信息", self.show_modify_personal_info),
            ("🔒 锁卡/解锁", self.show_lock_unlock),
            ("📝 申请贷款", self.show_apply_loan),
            ("📊 我的贷款", self.show_my_loans),
            ("💸 贷款还款", self.show_repayment),
            ("🔁 提前还款", self.show_early_repayment),
            ("📜 交易记录", self.show_transaction_history),
            ("❌ 账户注销", self.show_close_account),
            ("🔍 查询名下账户", self.show_query_accounts_by_id),
            ("💳 信用分补给", self.show_credit_supply),
            ("📜 历史信用分补给", self.show_credit_supply_history),
            ("📢 问题反馈", self.show_submit_feedback),
            ("📨 我的反馈", self.show_my_feedback),
        ]

        if self.current_role == ROLE_ADMIN:
            menu_items.extend([
                ("🏢 开设企业卡片", self.show_open_enterprise_card),
                ("👥 变更法人", self.show_change_legal_rep),
                ("⚖️ 大额资金审核", self.show_large_amount_review),
                ("🚨 强行冻结账户", self.show_force_freeze),
                ("📝 操作日志查询", self.show_operation_logs),
                ("📈 股票市场", self.show_stock_market),
                ("📊 我的股票", self.show_my_stocks),
                ("➕ 添加股票", self.show_add_stock),
                ("🗑️ 下架股票", self.show_remove_stock),
                ("✏️ 修改股价", self.show_edit_stock_price),
                ("📤 导出客户信息", self.show_export_customers),
                ("⚠️ 逾期信用分处理", self.show_overdue_credit_score),
                ("⚖️ 起诉用户", self.show_sue_user),
                ("🔨 执行法院判决", self.show_execute_judgment),
                ("👤 人脸审核", self.show_face_review),
            ])

        if self.current_role == ROLE_MANAGER:
            menu_items.extend([
                ("🏦 大额贷款审核", self.show_loan_approval),
            ])

        for text, command in menu_items:
            btn = tk.Button(self.menu_frame, text=text, command=command,
                            width=18, height=2, bg='#e1e1e1', relief=tk.FLAT, font=("Arial", 10))
            btn.pack(pady=6, padx=8)
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg='#d0d0d0'))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg='#e1e1e1'))

        def configure_scroll_region(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        self.menu_frame.bind("<Configure>", configure_scroll_region)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<MouseWheel>", on_mousewheel)

        # 右侧主内容区域
        self.main_frame = tk.Frame(self.root, bg='white')
        self.main_frame.pack(side=tk.RIGHT, expand=True, fill=tk.BOTH)

        self.show_welcome()

    # ==================== 欢迎页 ====================

    def show_welcome(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🏦 蛋国银行系统 - 客户总览",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        stats_frame = tk.Frame(self.main_frame, bg='white')
        stats_frame.pack(fill=tk.X, padx=30, pady=10)

        self.cursor.execute("SELECT COUNT(*) FROM customers")
        total_customers = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT SUM(balance) FROM customers")
        total_balance = self.cursor.fetchone()[0] or 0

        self.cursor.execute("SELECT COUNT(*) FROM loans WHERE status='active'")
        active_loans = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM large_amount_transactions WHERE status='pending'")
        pending_review = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM loan_approvals WHERE status='pending'")
        pending_loans = self.cursor.fetchone()[0]

        if self.current_role == ROLE_MANAGER:
            card_configs = [
                ("客户总数", str(total_customers), "#3498db", "👥"),
                ("总存款额", f"¥{total_balance:,.2f}", "#27ae60", "💰"),
                ("活跃贷款", str(active_loans), "#e67e22", "📝"),
                ("待审贷款", str(pending_loans), "#9b59b6", "🏦"),
            ]
        else:
            card_configs = [
                ("客户总数", str(total_customers), "#3498db", "👥"),
                ("总存款额", f"¥{total_balance:,.2f}", "#27ae60", "💰"),
                ("活跃贷款", str(active_loans), "#e67e22", "📝"),
                ("待审核", str(pending_review), "#e74c3c", "⏳"),
            ]

        for i, (title, value, color, icon) in enumerate(card_configs):
            card = tk.Frame(stats_frame, relief=tk.RAISED, bd=2, bg='white', width=200, height=100)
            card.grid(row=0, column=i, padx=15, pady=10)
            card.grid_propagate(False)

            icon_label = tk.Label(card, text=icon, font=("Arial", 24), bg='white')
            icon_label.pack(pady=5)

            tk.Label(card, text=title, font=("Arial", 10), bg='white', fg='#7f8c8d').pack()
            tk.Label(card, text=value, font=("Arial", 14, "bold"), bg='white', fg=color).pack()

        list_frame = tk.LabelFrame(self.main_frame, text="客户列表", font=("Arial", 12, "bold"),
                                   bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=20)

        columns = ("卡号", "姓名", "身份证号", "账户类型", "余额", "状态", "开户时间")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=120, anchor=tk.CENTER)

        tree.column("余额", width=100)
        tree.column("开户时间", width=150)

        self.cursor.execute('''
            SELECT card_number, name, id_number, account_type, balance, 
                   CASE WHEN is_frozen=1 THEN '冻结' ELSE '正常' END, create_time
            FROM customers ORDER BY create_time DESC
        ''')
        for row in self.cursor.fetchall():
            tree.insert("", "end", values=(
                row[0], row[1], row[2], row[3], f"¥{row[4]:,.2f}", row[5], row[6]
            ))

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

    # ==================== 开卡开户 ====================

    def show_open_card(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="💳 开卡开户",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="开户信息填写", font=("Arial", 12, "bold"),
                                   bg='white', padx=20, pady=20)
        form_frame.pack(pady=10, padx=50, fill=tk.X)

        fields = [
            ("姓名", "name_entry"),
            ("身份证号", "id_entry"),
            ("设置密码", "pwd_entry"),
            ("确认密码", "pwd2_entry"),
            ("邮箱", "email_entry"),
            ("住址", "addr_entry"),
            ("账户类型", "type_entry"),
        ]

        entries = {}
        for i, (label_text, var_name) in enumerate(fields):
            tk.Label(form_frame, text=f"{label_text}：", font=("Arial", 11), bg='white').grid(
                row=i // 2, column=(i % 2) * 2, padx=10, pady=12, sticky=tk.E)

            if var_name == "type_entry":
                combo = ttk.Combobox(form_frame, values=["储蓄账户", "活期账户", "定期账户", "信用卡"],
                                     width=27, state="readonly")
                combo.current(0)
                combo.grid(row=i // 2, column=(i % 2) * 2 + 1, padx=10, pady=12)
                entries[var_name] = combo
            else:
                show = "*" if "pwd" in var_name else ""
                entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
                entry.grid(row=i // 2, column=(i % 2) * 2 + 1, padx=10, pady=12)
                entries[var_name] = entry

        # 手机号 + 验证码（单独一行）
        phone_row = 4
        tk.Label(form_frame, text="手机号：", font=("Arial", 11), bg='white').grid(
            row=phone_row, column=0, padx=10, pady=12, sticky=tk.E)
        phone_entry = tk.Entry(form_frame, width=20, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        phone_entry.grid(row=phone_row, column=1, padx=10, pady=12, sticky=tk.W)
        entries["phone_entry"] = phone_entry

        code_btn = tk.Button(form_frame, text="发送验证码", command=lambda: self.send_open_card_code(phone_entry, code_status_label),
                             bg='#3498db', fg='white', font=("Arial", 10), relief=tk.FLAT)
        code_btn.grid(row=phone_row, column=2, padx=5, pady=12, sticky=tk.W)

        tk.Label(form_frame, text="验证码：", font=("Arial", 11), bg='white').grid(
            row=phone_row, column=3, padx=10, pady=12, sticky=tk.E)
        code_entry = tk.Entry(form_frame, width=12, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        code_entry.grid(row=phone_row, column=4, padx=10, pady=12, sticky=tk.W)
        entries["code_entry"] = code_entry

        code_status_label = tk.Label(form_frame, text="", font=("Arial", 9), bg='white', fg='gray')
        code_status_label.grid(row=phone_row + 1, column=0, columnspan=5, pady=2)

        tip_label = tk.Label(form_frame,
                             text="⚠️ 开户需通过户籍系统身份核验，且手机号需通过短信验证码验证",
                             font=("Arial", 9), bg='white', fg='#e67e22')
        tip_label.grid(row=phone_row + 2, column=0, columnspan=5, pady=10)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        def open_account():
            name = entries["name_entry"].get().strip()
            id_number = entries["id_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            pwd2 = entries["pwd2_entry"].get()
            phone = entries["phone_entry"].get().strip()
            code = entries["code_entry"].get().strip()
            email = entries["email_entry"].get().strip()
            address = entries["addr_entry"].get().strip()
            account_type = entries["type_entry"].get()

            if not all([name, id_number, pwd, pwd2, phone]):
                messagebox.showerror("错误", "姓名、身份证号、密码、手机号为必填项！")
                return

            if pwd != pwd2:
                messagebox.showerror("错误", "两次输入的密码不一致！")
                return

            if len(pwd) < 6:
                messagebox.showerror("错误", "密码长度不能少于6位！")
                return

            if not re.match(r'^\d{17}[\dXx]$', id_number):
                messagebox.showerror("错误", "身份证号格式不正确！")
                return

            if not re.match(r'^1[3-9]\d{9}$', phone):
                messagebox.showerror("错误", "手机号格式不正确！")
                return

            # 验证手机号验证码
            if not code:
                messagebox.showerror("错误", "请输入手机号验证码！")
                return
            code_ok, code_msg = self.verify_code(phone, code)
            if not code_ok:
                messagebox.showerror("验证码错误", code_msg)
                return

            verified, msg = self.verify_household_identity(id_number, name)
            if not verified:
                messagebox.showerror("身份核验失败", f"户籍系统核验未通过：{msg}\n\n无法办理开户业务！")
                return

            card_number = self.generate_card_number()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            credit_limit = self._get_credit_limit(account_type)

            self.cursor.execute('''
                INSERT INTO customers (card_number, id_number, name, password, account_type,
                                       phone, email, address, create_time, last_update_time, credit_limit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (card_number, id_number, name, pwd, account_type, phone, email, address, now, now, credit_limit))
            self.conn.commit()

            self.add_operation_log("开卡开户", f"客户 {name} 开户成功，卡号：{card_number}")

            # 发送开户成功通知
            credit_tip = f"，授信额度 ¥{credit_limit:,.0f}（可在额度内透支消费）" if account_type == '信用卡' else ""
            self.notify_customer(card_number, "开户成功通知",
                                 f"尊敬的{name}，您的银行卡 {card_number} 已成功开户，账户类型：{account_type}{credit_tip}。")

            credit_line = f"\n信用额度：¥ {credit_limit:,.0f}" if account_type == '信用卡' else ""
            messagebox.showinfo("开户成功",
                                f"🎉 恭喜开户成功！\n\n"
                                f"持卡人：{name}\n"
                                f"银行卡号：{card_number}\n"
                                f"账户类型：{account_type}{credit_line}\n"
                                f"开户时间：{now}\n\n"
                                f"（身份已通过户籍系统核验，手机号已通过验证码验证）")
            self.show_welcome()

        submit_btn = tk.Button(btn_frame, text="✅ 确认开户", command=open_account,
                               width=18, height=2, bg='#27ae60', fg='white',
                               font=("Arial", 11), relief=tk.FLAT)
        submit_btn.pack(side=tk.LEFT, padx=20)
        submit_btn.bind("<Enter>", lambda e: submit_btn.config(bg='#229954'))
        submit_btn.bind("<Leave>", lambda e: submit_btn.config(bg='#27ae60'))

        reset_btn = tk.Button(btn_frame, text="重置", command=self.show_open_card,
                              width=12, height=2, bg='#95a5a6', fg='white',
                              font=("Arial", 11), relief=tk.FLAT)
        reset_btn.pack(side=tk.LEFT, padx=20)

    # ==================== 开设企业卡片（管理后台） ====================

    def show_open_enterprise_card(self):
        """管理后台：开设企业卡片。
        流程：输入 统一社会信用代码+法人姓名+法人身份证号 → 企业系统核验（法人必须与企业登记法人一致）
        → 核验通过后自动开户（生成唯一卡号）→ 验证手机号 → 开户成功。"""
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🏢 开设企业卡片（企业账户）",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        # ---------- 第 1 步：企业系统核验 ----------
        verify_frame = tk.LabelFrame(self.main_frame, text="第 1 步 · 企业系统核验（法人必须与企业登记法人一致）",
                                     font=("Arial", 12, "bold"), bg='white', padx=20, pady=15)
        verify_frame.pack(pady=10, padx=50, fill=tk.X)

        fields = [
            ("统一社会信用代码", "credit_entry"),
            ("法人姓名", "legal_name_entry"),
            ("法人身份证号", "legal_id_entry"),
        ]
        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(verify_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=10, sticky=tk.E)
            entry = tk.Entry(verify_frame, width=34, font=("Arial", 11), bd=2, relief=tk.GROOVE)
            entry.grid(row=i, column=1, padx=10, pady=10, sticky=tk.W)
            entries[var] = entry

        verify_status = tk.Label(verify_frame, text="等待企业系统核验……", font=("Arial", 10),
                                 bg='white', fg='#7f8c8d', justify=tk.LEFT)
        verify_status.grid(row=3, column=0, columnspan=3, pady=8, sticky=tk.W, padx=10)

        # 核验通过后的企业信息与自动生成的卡号
        ent_info_labels = {}
        info_fields = [("企业编号", "enterprise_id"), ("企业名称", "enterprise_name"),
                       ("企业类型", "enterprise_type"), ("法人姓名", "legal_rep_name"),
                       ("法人身份证号", "legal_rep_id_number")]
        for j, (label, key) in enumerate(info_fields):
            tk.Label(verify_frame, text=f"{label}：", font=("Arial", 10), bg='white',
                     fg='#7f8c8d').grid(row=4 + j, column=0, padx=10, pady=4, sticky=tk.E)
            lbl = tk.Label(verify_frame, text="-", font=("Arial", 10, "bold"), bg='white', fg='#2c3e50')
            lbl.grid(row=4 + j, column=1, padx=10, pady=4, sticky=tk.W)
            ent_info_labels[key] = lbl

        card_number_var = tk.StringVar()
        card_no_label = tk.Label(verify_frame, text="自动生成卡号：-", font=("Arial", 12, "bold"),
                                 bg='white', fg='#27ae60')
        card_no_label.grid(row=9, column=0, columnspan=3, pady=8, sticky=tk.W, padx=10)

        verified_data = {}

        def do_verify_enterprise():
            credit_code = entries["credit_entry"].get().strip()
            legal_name = entries["legal_name_entry"].get().strip()
            legal_id = entries["legal_id_entry"].get().strip()

            if not all([credit_code, legal_name, legal_id]):
                messagebox.showerror("错误", "请填写统一社会信用代码、法人姓名和法人身份证号！")
                return
            if not re.match(r'^[0-9A-HJ-NPQRTUWXY]{18}$', credit_code):
                messagebox.showerror("错误", "统一社会信用代码格式不正确（应为18位）！")
                return
            if not re.match(r'^\d{17}[\dXx]$', legal_id):
                messagebox.showerror("错误", "法人身份证号格式不正确！")
                return

            verified, msg, info = self.verify_enterprise_legal_rep(credit_code, legal_name, legal_id)
            if not verified:
                verified_data.clear()
                verify_status.config(text=f"✗ 核验未通过：{msg}", fg='#e74c3c')
                card_no_label.config(text="自动生成卡号：-", fg='#27ae60')
                messagebox.showerror("企业核验失败", f"企业系统核验未通过：{msg}\n\n无法办理开户业务！")
                return

            verified_data.update(info)
            for key, lbl in ent_info_labels.items():
                lbl.config(text=verified_data.get(key, '-'))
            verify_status.config(text="✓ 企业法人核验通过，已自动开户（生成唯一卡号），请完成第 2 步手机号验证。",
                                 fg='#27ae60')

            # 自动开户：生成唯一卡号
            while True:
                new_no = self.generate_card_number()
                self.cursor.execute("SELECT 1 FROM customers WHERE card_number = ?", (new_no,))
                if not self.cursor.fetchone():
                    break
            card_number_var.set(new_no)
            card_no_label.config(text=f"自动生成卡号：{new_no}", fg='#27ae60')

        verify_btn = tk.Button(verify_frame, text="🔍 企业系统核验", command=do_verify_enterprise,
                               width=16, height=1, bg='#3498db', fg='white',
                               font=("Arial", 11), relief=tk.FLAT)
        verify_btn.grid(row=9, column=1, columnspan=2, pady=8, sticky=tk.W, padx=10)

        # ---------- 第 2 步：手机号验证 + 初始密码 ----------
        phone_frame = tk.LabelFrame(self.main_frame, text="第 2 步 · 手机号验证（完成手机号验证后开户成功）",
                                    font=("Arial", 12, "bold"), bg='white', padx=20, pady=15)
        phone_frame.pack(pady=10, padx=50, fill=tk.X)

        tk.Label(phone_frame, text="手机号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        phone_entry = tk.Entry(phone_frame, width=22, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        phone_entry.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)

        phone_status = tk.Label(phone_frame, text="", font=("Arial", 9), bg='white', fg='gray')
        phone_status.grid(row=1, column=0, columnspan=4, pady=2, sticky=tk.W, padx=10)

        send_btn = tk.Button(phone_frame, text="发送验证码", command=lambda: self.send_open_card_code(phone_entry, phone_status),
                             bg='#3498db', fg='white', font=("Arial", 10), relief=tk.FLAT)
        send_btn.grid(row=0, column=2, padx=5, pady=10, sticky=tk.W)

        tk.Label(phone_frame, text="验证码：", font=("Arial", 11), bg='white').grid(
            row=0, column=3, padx=10, pady=10, sticky=tk.E)
        code_entry = tk.Entry(phone_frame, width=14, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        code_entry.grid(row=0, column=4, padx=10, pady=10, sticky=tk.W)

        tk.Label(phone_frame, text="初始密码（企业端登录使用，至少6位）：", font=("Arial", 11),
                 bg='white').grid(row=2, column=0, padx=10, pady=10, sticky=tk.E)
        pwd_entry = tk.Entry(phone_frame, width=22, font=("Arial", 11), bd=2, relief=tk.GROOVE, show="*")
        pwd_entry.grid(row=2, column=1, padx=10, pady=10, sticky=tk.W)

        tk.Label(phone_frame, text="确认密码：", font=("Arial", 11), bg='white').grid(
            row=2, column=3, padx=10, pady=10, sticky=tk.E)
        pwd2_entry = tk.Entry(phone_frame, width=14, font=("Arial", 11), bd=2, relief=tk.GROOVE, show="*")
        pwd2_entry.grid(row=2, column=4, padx=10, pady=10, sticky=tk.W)

        def do_open_enterprise():
            if not verified_data:
                messagebox.showerror("错误", "请先完成第 1 步企业系统核验！")
                return
            phone = phone_entry.get().strip()
            code = code_entry.get().strip()
            pwd = pwd_entry.get()
            pwd2 = pwd2_entry.get()

            if not phone or not code or not pwd:
                messagebox.showerror("错误", "手机号、验证码、初始密码为必填项！")
                return
            if not re.match(r'^1[3-9]\d{9}$', phone):
                messagebox.showerror("错误", "手机号格式不正确！")
                return
            if pwd != pwd2:
                messagebox.showerror("错误", "两次输入的密码不一致！")
                return
            if len(pwd) < 6:
                messagebox.showerror("错误", "密码长度不能少于6位！")
                return

            code_ok, code_msg = self.verify_code(phone, code)
            if not code_ok:
                messagebox.showerror("验证码错误", code_msg)
                return

            card_number = card_number_var.get()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute('''
                INSERT INTO customers (card_number, id_number, name, password, account_type,
                                       phone, email, address, create_time, last_update_time,
                                       credit_limit, enterprise_id, enterprise_name,
                                       unified_credit_code, legal_rep_name, legal_rep_id_number,
                                       is_enterprise)
                VALUES (?, ?, ?, ?, '企业账户', ?, '', '', ?, ?, 0, ?, ?, ?, ?, ?, 1)
            ''', (card_number, verified_data['legal_rep_id_number'], verified_data['legal_rep_name'],
                  pwd, phone, now, now,
                  verified_data['enterprise_id'], verified_data['enterprise_name'],
                  verified_data['unified_credit_code'], verified_data['legal_rep_name'],
                  verified_data['legal_rep_id_number']))
            self.conn.commit()

            self.add_operation_log("开设企业卡片",
                                   f"企业 {verified_data['enterprise_name']}（{verified_data['unified_credit_code']}）"
                                   f"开户成功，卡号：{card_number}，法人：{verified_data['legal_rep_name']}")

            self.notify_customer(card_number, "企业开户成功通知",
                                 f"尊敬的{verified_data['legal_rep_name']}，贵企业 {verified_data['enterprise_name']} "
                                 f"的企业账户卡 {card_number} 已成功开户（企业账户），初始密码请咨询开户网点。")

            messagebox.showinfo("开户成功",
                                f"🎉 企业开户成功！\n\n"
                                f"企业名称：{verified_data['enterprise_name']}\n"
                                f"企业编号：{verified_data['enterprise_id']}\n"
                                f"统一社会信用代码：{verified_data['unified_credit_code']}\n"
                                f"法定代表人：{verified_data['legal_rep_name']}\n"
                                f"银行卡号：{card_number}\n"
                                f"账户类型：企业账户\n"
                                f"绑定手机号：{phone}\n"
                                f"开户时间：{now}\n\n"
                                f"（企业法人已通过企业系统核验，手机号已通过验证码验证）")
            self.show_open_enterprise_card()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)
        tk.Button(btn_frame, text="✅ 确认开户（完成手机号验证）", command=do_open_enterprise,
                  width=26, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=20)
        tk.Button(btn_frame, text="重置", command=self.show_open_enterprise_card,
                  width=12, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=20)

    # ==================== 变更法人（管理后台） ====================

    def show_change_legal_rep(self):
        """管理后台：变更企业卡法人。
        流程：输入卡号 → 查询（识别企业卡并自动加载信息）→ 输入新法人姓名+身份证号
        → 企业系统核验（新法人必须与企业登记法人一致）→ 修改成功（企业卡在法人名下）。"""
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="👥 变更企业卡法人",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        # ---------- 第 1 步：查询卡号 ----------
        query_frame = tk.LabelFrame(self.main_frame, text="第 1 步 · 查询企业卡（输入卡号后点击查询）",
                                    font=("Arial", 12, "bold"), bg='white', padx=20, pady=15)
        query_frame.pack(pady=10, padx=50, fill=tk.X)

        tk.Label(query_frame, text="银行卡号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        card_entry = tk.Entry(query_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)

        query_status = tk.Label(query_frame, text="请输入企业卡卡号，查询后将自动加载企业信息。",
                                font=("Arial", 10), bg='white', fg='#7f8c8d', justify=tk.LEFT)
        query_status.grid(row=2, column=0, columnspan=4, pady=6, sticky=tk.W, padx=10)

        # 加载出的企业卡信息（只读展示）
        card_info_labels = {}
        info_fields = [("企业名称", "enterprise_name"), ("统一社会信用代码", "unified_credit_code"),
                       ("当前法人姓名", "legal_rep_name"), ("当前法人身份证号", "legal_rep_id_number"),
                       ("账户状态", "status"), ("开户时间", "create_time")]
        for j, (label, key) in enumerate(info_fields):
            tk.Label(query_frame, text=f"{label}：", font=("Arial", 10), bg='white',
                     fg='#7f8c8d').grid(row=3 + j, column=0, padx=10, pady=4, sticky=tk.E)
            lbl = tk.Label(query_frame, text="-", font=("Arial", 10, "bold"), bg='white', fg='#2c3e50')
            lbl.grid(row=3 + j, column=1, padx=10, pady=4, sticky=tk.W)
            card_info_labels[key] = lbl

        loaded_card = {}

        def do_query_card():
            card = card_entry.get().strip()
            if not card:
                messagebox.showerror("错误", "请输入卡号！")
                return
            self.cursor.execute(
                "SELECT id, card_number, name, id_number, account_type, is_enterprise, enterprise_id, "
                "enterprise_name, unified_credit_code, legal_rep_name, legal_rep_id_number, "
                "is_frozen, create_time FROM customers WHERE card_number = ?", (card,))
            row = self.cursor.fetchone()
            if not row:
                loaded_card.clear()
                query_status.config(text="✗ 未找到该卡号！", fg='#e74c3c')
                for lbl in card_info_labels.values():
                    lbl.config(text="-")
                messagebox.showerror("错误", "未找到该卡号！")
                return
            (cid, cno, name, idn, acc_type, is_ent, ent_id, ent_name, credit_code,
             legal_name, legal_id, frozen, create_time) = row
            if not is_ent:
                loaded_card.clear()
                query_status.config(text="✗ 该卡不是企业卡（账户类型：" + acc_type + "），无法办理法人变更！", fg='#e74c3c')
                for lbl in card_info_labels.values():
                    lbl.config(text="-")
                messagebox.showerror("错误", f"该卡不是企业卡（账户类型：{acc_type}），无法办理法人变更！")
                return
            loaded_card.update({
                'cid': cid, 'card_number': cno, 'enterprise_name': ent_name or '-',
                'unified_credit_code': credit_code or '-', 'legal_rep_name': legal_name or name,
                'legal_rep_id_number': legal_id or idn, 'is_frozen': frozen, 'create_time': create_time,
            })
            card_info_labels['enterprise_name'].config(text=loaded_card['enterprise_name'])
            card_info_labels['unified_credit_code'].config(text=loaded_card['unified_credit_code'])
            card_info_labels['legal_rep_name'].config(text=loaded_card['legal_rep_name'])
            card_info_labels['legal_rep_id_number'].config(text=loaded_card['legal_rep_id_number'])
            card_info_labels['status'].config(text="🔒 已冻结" if frozen else "✅ 正常",
                                              fg='#e74c3c' if frozen else '#27ae60')
            card_info_labels['create_time'].config(text=loaded_card['create_time'])
            query_status.config(text="✓ 已识别为企业卡，企业信息已自动加载。请填写新法人信息进行企业系统核验。",
                                fg='#27ae60')

        tk.Button(query_frame, text="🔍 查询", command=do_query_card,
                  width=10, height=1, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).grid(row=0, column=2, padx=15, pady=10, sticky=tk.W)

        # ---------- 第 2 步：新法人核验 ----------
        change_frame = tk.LabelFrame(self.main_frame, text="第 2 步 · 新法人企业系统核验（新法人必须与企业登记法人一致）",
                                     font=("Arial", 12, "bold"), bg='white', padx=20, pady=15)
        change_frame.pack(pady=10, padx=50, fill=tk.X)

        tk.Label(change_frame, text="新法人姓名：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        new_name_entry = tk.Entry(change_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        new_name_entry.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)

        tk.Label(change_frame, text="新法人身份证号：", font=("Arial", 11), bg='white').grid(
            row=1, column=0, padx=10, pady=10, sticky=tk.E)
        new_id_entry = tk.Entry(change_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        new_id_entry.grid(row=1, column=1, padx=10, pady=10, sticky=tk.W)

        change_status = tk.Label(change_frame, text="（企业卡在法人名下，变更后企业卡户名同步为新法人）",
                                 font=("Arial", 9), bg='white', fg='#7f8c8d', justify=tk.LEFT)
        change_status.grid(row=2, column=0, columnspan=3, pady=4, sticky=tk.W, padx=10)

        def do_change_legal():
            if not loaded_card:
                messagebox.showerror("错误", "请先完成第 1 步查询企业卡！")
                return
            new_name = new_name_entry.get().strip()
            new_id = new_id_entry.get().strip()
            if not all([new_name, new_id]):
                messagebox.showerror("错误", "请填写新法人姓名和身份证号！")
                return
            if not re.match(r'^\d{17}[\dXx]$', new_id):
                messagebox.showerror("错误", "新法人身份证号格式不正确！")
                return
            if loaded_card['is_frozen']:
                messagebox.showerror("错误", "该企业账户已被冻结，无法办理法人变更！")
                return

            credit_code = loaded_card.get('unified_credit_code', '')
            verified, msg, info = self.verify_enterprise_legal_rep(credit_code, new_name, new_id)
            if not verified:
                change_status.config(text=f"✗ 企业系统核验未通过：{msg}", fg='#e74c3c')
                messagebox.showerror("企业核验失败", f"企业系统核验未通过：{msg}\n\n无法办理法人变更！")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute('''
                UPDATE customers SET name = ?, id_number = ?, legal_rep_name = ?, legal_rep_id_number = ?,
                                      last_update_time = ? WHERE id = ?
            ''', (new_name, new_id, new_name, new_id, now, loaded_card['cid']))
            self.conn.commit()

            self.add_operation_log("变更法人",
                                   f"企业卡 {loaded_card['card_number']}（{loaded_card['enterprise_name']}）"
                                   f"法人由 {loaded_card['legal_rep_name']} 变更为 {new_name}")

            self.notify_customer(loaded_card['card_number'], "企业法人变更通知",
                                 f"贵企业 {loaded_card['enterprise_name']} 的企业账户法人已由 "
                                 f"{loaded_card['legal_rep_name']} 变更为 {new_name}，企业卡已更新至新法人名下。")

            messagebox.showinfo("变更成功",
                                f"✅ 企业法人变更成功！\n\n"
                                f"企业名称：{loaded_card['enterprise_name']}\n"
                                f"银行卡号：{loaded_card['card_number']}\n"
                                f"原法人：{loaded_card['legal_rep_name']}（{loaded_card['legal_rep_id_number']}）\n"
                                f"新法人：{new_name}（{new_id}）\n"
                                f"变更时间：{now}\n\n"
                                f"（新法人已通过企业系统核验，企业卡已更新至新法人名下）")
            self.show_change_legal_rep()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)
        tk.Button(btn_frame, text="✅ 确认变更法人", command=do_change_legal,
                  width=18, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=20)
        tk.Button(btn_frame, text="重置", command=self.show_change_legal_rep,
                  width=12, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=20)

    # ==================== 查询余额 ====================

    def show_query_balance(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="💰 余额查询",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.LabelFrame(self.main_frame, text="输入查询信息", font=("Arial", 12, "bold"),
                                    bg='white', padx=20, pady=20)
        query_frame.pack(pady=10, padx=50, fill=tk.X)

        tk.Label(query_frame, text="银行卡号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        card_entry = tk.Entry(query_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=10)

        tk.Label(query_frame, text="查询密码：", font=("Arial", 11), bg='white').grid(
            row=0, column=2, padx=10, pady=10, sticky=tk.E)
        pwd_entry = tk.Entry(query_frame, width=20, font=("Arial", 11), bd=2, relief=tk.GROOVE, show="*")
        pwd_entry.grid(row=0, column=3, padx=10, pady=10)

        result_frame = tk.LabelFrame(self.main_frame, text="查询结果", font=("Arial", 12, "bold"),
                                     bg='white', padx=20, pady=20)
        result_frame.pack(pady=20, padx=50, fill=tk.BOTH, expand=True)

        result_label = tk.Label(result_frame, text="请输入卡号和密码进行查询",
                                font=("Arial", 14), bg='white', fg='#7f8c8d')
        result_label.pack(pady=30)

        def do_query():
            card = card_entry.get().strip()
            pwd = pwd_entry.get()

            if not card or not pwd:
                messagebox.showerror("错误", "请输入卡号和密码！")
                return

            self.cursor.execute('''
                SELECT name, balance, account_type, credit_limit, is_frozen, create_time
                FROM customers WHERE card_number = ? AND password = ?
            ''', (card, pwd))
            result = self.cursor.fetchone()

            if not result:
                result_label.config(text="❌ 卡号或密码错误！", fg='#e74c3c')
                return

            name, balance, acc_type, credit_limit, frozen, create_time = result
            status = "🔒 已冻结" if frozen else "✅ 正常"
            status_color = '#e74c3c' if frozen else '#27ae60'

            for widget in result_frame.winfo_children():
                widget.destroy()

            info_frame = tk.Frame(result_frame, bg='white')
            info_frame.pack(pady=20)

            tk.Label(info_frame, text=f"持卡人：{name}", font=("Arial", 14), bg='white').grid(
                row=0, column=0, padx=30, pady=10, sticky=tk.W)
            tk.Label(info_frame, text=f"账户类型：{acc_type}", font=("Arial", 14), bg='white').grid(
                row=0, column=1, padx=30, pady=10, sticky=tk.W)
            tk.Label(info_frame, text=f"账户状态：{status}", font=("Arial", 14),
                     bg='white', fg=status_color).grid(row=1, column=0, padx=30, pady=10, sticky=tk.W)
            tk.Label(info_frame, text=f"开户时间：{create_time}", font=("Arial", 14), bg='white').grid(
                row=1, column=1, padx=30, pady=10, sticky=tk.W)

            balance_frame = tk.Frame(result_frame, bg='#ecf0f1', bd=2, relief=tk.GROOVE)
            balance_frame.pack(pady=20, padx=50, fill=tk.X)

            tk.Label(balance_frame, text="账户余额", font=("Arial", 14), bg='#ecf0f1', fg='#7f8c8d').pack(pady=10)
            balance_color = '#e74c3c' if (acc_type == '信用卡' and balance < 0) else '#27ae60'
            tk.Label(balance_frame, text=f"¥ {balance:,.2f}", font=("Arial", 32, "bold"),
                     bg='#ecf0f1', fg=balance_color).pack(pady=10)
            if acc_type == '信用卡':
                tk.Label(balance_frame, text=f"信用额度：¥ {credit_limit:,.0f}", font=("Arial", 12),
                         bg='#ecf0f1', fg='#7f8c8d').pack(pady=2)
                tk.Label(balance_frame,
                         text=f"可用额度：¥ {self._get_available_amount(balance, acc_type, credit_limit):,.2f}",
                         font=("Arial", 14, "bold"), bg='#ecf0f1', fg='#2980b9').pack(pady=2)
                if balance < 0:
                    tk.Label(balance_frame, text=f"当前欠款：¥ {-balance:,.2f}（请及时存款还款）",
                             font=("Arial", 12, "bold"), bg='#ecf0f1', fg='#e74c3c').pack(pady=2)

            self.cursor.execute("SELECT id FROM customers WHERE card_number = ?", (card,))
            cid = self.cursor.fetchone()
            if cid:
                self.current_customer_id = cid[0]

            self.add_operation_log("余额查询", f"查询卡号 {card} 余额，余额：¥{balance:,.2f}")

        query_btn = tk.Button(query_frame, text="🔍 查询", command=do_query,
                              width=10, height=1, bg='#3498db', fg='white',
                              font=("Arial", 11), relief=tk.FLAT)
        query_btn.grid(row=0, column=4, padx=20)
        query_btn.bind("<Enter>", lambda e: query_btn.config(bg='#2980b9'))
        query_btn.bind("<Leave>", lambda e: query_btn.config(bg='#3498db'))

    # ==================== 账户信息 ====================

    def show_account_info(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📋 账户信息查询",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.Frame(self.main_frame, bg='white')
        query_frame.pack(pady=10)

        tk.Label(query_frame, text="输入卡号：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        card_entry = tk.Entry(query_frame, width=25, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.pack(side=tk.LEFT, padx=5)

        result_frame = tk.LabelFrame(self.main_frame, text="账户详细信息", font=("Arial", 12, "bold"),
                                     bg='white', padx=20, pady=20)
        result_frame.pack(pady=20, padx=50, fill=tk.BOTH, expand=True)

        info_label = tk.Label(result_frame, text="请输入卡号查询", font=("Arial", 12), bg='white', fg='#7f8c8d')
        info_label.pack(pady=30)

        def query_info():
            card = card_entry.get().strip()
            if not card:
                messagebox.showerror("错误", "请输入卡号！")
                return

            self.cursor.execute('''
                SELECT id, card_number, name, id_number, balance, account_type, credit_limit,
                       phone, email, address, credit_score, is_frozen, create_time
                FROM customers WHERE card_number = ?
            ''', (card,))
            result = self.cursor.fetchone()

            if not result:
                info_label.config(text="❌ 未找到该账户！", fg='#e74c3c')
                return

            for widget in result_frame.winfo_children():
                widget.destroy()

            cid, card_num, name, id_num, balance, acc_type, credit_limit, phone, email, addr, credit, frozen, create_t = result
            self.current_customer_id = cid

            basic_frame = tk.LabelFrame(result_frame, text="基本信息", font=("Arial", 11, "bold"),
                                        bg='white', padx=15, pady=10)
            basic_frame.pack(fill=tk.X, pady=5)

            info_items = [
                ("银行卡号", card_num),
                ("持卡人姓名", name),
                ("身份证号", id_num),
                ("账户类型", acc_type),
                ("信用评分", f"{credit} 分"),
                ("信用额度", f"¥ {credit_limit:,.0f}" if acc_type == '信用卡' else "-"),
            ]

            for i, (label, value) in enumerate(info_items):
                tk.Label(basic_frame, text=f"{label}：", font=("Arial", 10), bg='white', fg='#7f8c8d').grid(
                    row=i, column=0, padx=20, pady=8, sticky=tk.E)
                tk.Label(basic_frame, text=value, font=("Arial", 10, "bold"), bg='white').grid(
                    row=i, column=1, padx=10, pady=8, sticky=tk.W)

            status_text = "🔒 已冻结" if frozen else "✅ 正常"
            status_color = '#e74c3c' if frozen else '#27ae60'
            tk.Label(basic_frame, text="账户状态：", font=("Arial", 10), bg='white', fg='#7f8c8d').grid(
                row=6, column=0, padx=20, pady=8, sticky=tk.E)
            tk.Label(basic_frame, text=status_text, font=("Arial", 10, "bold"),
                     bg='white', fg=status_color).grid(row=6, column=1, padx=10, pady=8, sticky=tk.W)

            contact_frame = tk.LabelFrame(result_frame, text="联系信息", font=("Arial", 11, "bold"),
                                          bg='white', padx=15, pady=10)
            contact_frame.pack(fill=tk.X, pady=5)

            contact_items = [
                ("手机号码", phone or "未填写"),
                ("电子邮箱", email or "未填写"),
                ("联系地址", addr or "未填写"),
                ("开户时间", create_t),
            ]

            for i, (label, value) in enumerate(contact_items):
                tk.Label(contact_frame, text=f"{label}：", font=("Arial", 10), bg='white', fg='#7f8c8d').grid(
                    row=i, column=0, padx=20, pady=8, sticky=tk.E)
                tk.Label(contact_frame, text=value, font=("Arial", 10), bg='white').grid(
                    row=i, column=1, padx=10, pady=8, sticky=tk.W)

            asset_frame = tk.LabelFrame(result_frame, text="资产概览", font=("Arial", 11, "bold"),
                                        bg='white', padx=15, pady=10)
            asset_frame.pack(fill=tk.X, pady=5)

            self.cursor.execute("SELECT COUNT(*), SUM(remaining_amount) FROM loans "
                                "WHERE customer_id = ? AND status = 'active'", (cid,))
            loan_count, loan_total = self.cursor.fetchone()
            loan_total = loan_total or 0

            self.cursor.execute("SELECT COUNT(*) FROM stock_portfolio WHERE customer_id = ?", (cid,))
            stock_count = self.cursor.fetchone()[0]

            asset_items = [
                ("账户余额", f"¥ {balance:,.2f}"),
            ]
            if acc_type == '信用卡':
                asset_items.append(("信用额度", f"¥ {credit_limit:,.0f}"))
                asset_items.append(("可用额度", f"¥ {self._get_available_amount(balance, acc_type, credit_limit):,.2f}"))
            asset_items.extend([
                ("未结清贷款数", f"{loan_count} 笔"),
                ("未还贷款总额", f"¥ {loan_total:,.2f}"),
                ("持仓股票种类", f"{stock_count} 种"),
            ])

            for i, (label, value) in enumerate(asset_items):
                tk.Label(asset_frame, text=f"{label}：", font=("Arial", 10), bg='white', fg='#7f8c8d').grid(
                    row=i // 2, column=(i % 2) * 2, padx=20, pady=8, sticky=tk.E)
                tk.Label(asset_frame, text=value, font=("Arial", 10, "bold"),
                         bg='white', fg='#27ae60').grid(row=i // 2, column=(i % 2) * 2 + 1, padx=10, pady=8, sticky=tk.W)

            self.add_operation_log("账户信息查询", f"查询账户 {card} 的详细信息")

        query_btn = tk.Button(query_frame, text="查询", command=query_info,
                              width=8, bg='#3498db', fg='white', relief=tk.FLAT)
        query_btn.pack(side=tk.LEFT, padx=10)

    # ==================== 存款 ====================

    def show_deposit(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="💵 存款业务",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="存款信息", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        tk.Label(form_frame, text="目标卡号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=15, sticky=tk.E)
        card_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=15)

        tk.Label(form_frame, text="存款金额：", font=("Arial", 11), bg='white').grid(
            row=1, column=0, padx=10, pady=15, sticky=tk.E)
        amount_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        amount_entry.grid(row=1, column=1, padx=10, pady=15)

        tk.Label(form_frame, text=f"（单笔超过 ¥{self.large_amount_limits['deposit']:,.0f} 需人工审核）",
                 font=("Arial", 9), bg='white', fg='#e67e22').grid(row=2, column=0, columnspan=2, pady=5)

        def do_deposit():
            card = card_entry.get().strip()
            amount_str = amount_entry.get().strip()

            if not card or not amount_str:
                messagebox.showerror("错误", "请填写完整信息！")
                return

            try:
                amount = float(amount_str)
            except ValueError:
                messagebox.showerror("错误", "金额格式不正确！")
                return

            if amount <= 0:
                messagebox.showerror("错误", "存款金额必须大于0！")
                return

            self.cursor.execute("SELECT id, name, is_frozen, account_type, balance FROM customers WHERE card_number = ?", (card,))
            customer = self.cursor.fetchone()
            if not customer:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, frozen, acc_type, old_balance = customer
            if frozen:
                messagebox.showerror("错误", "该账户已被冻结，无法存款！")
                return

            if amount > self.large_amount_limits['deposit']:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.cursor.execute('''
                    INSERT INTO large_amount_transactions (customer_id, transaction_type, amount, apply_time)
                    VALUES (?, 'deposit', ?, ?)
                ''', (cid, amount, now))
                self.conn.commit()
                messagebox.showinfo("大额审核", f"存款金额 ¥{amount:,.2f} 超过限额，已提交审核，请等待管理员审批！")
                self.add_operation_log("大额存款申请", f"卡号 {card} 申请存款 ¥{amount:,.2f}，待审核")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("UPDATE customers SET balance = balance + ?, last_update_time = ? WHERE id = ?",
                                (amount, now, cid))
            self.cursor.execute('''
                INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                VALUES ('CASH', ?, ?, 'deposit', ?)
            ''', (card, amount, now))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("存款", f"向卡号 {card} 存款 ¥{amount:,.2f}")

            # 发送存款通知
            repay_note = "，已用于偿还信用卡欠款" if (acc_type == '信用卡' and old_balance < 0) else ""
            self.notify_customer(card, "存款到账通知",
                                 f"您的账户 {card} 存入 ¥{amount:,.2f}{repay_note}，交易时间：{now}。")

            repay_msg = ""
            if acc_type == '信用卡' and old_balance < 0:
                repay_msg = f"其中 ¥ {min(amount, -old_balance):,.2f} 已用于偿还信用卡欠款\n"
            messagebox.showinfo("存款成功",
                                f"✅ 存款成功！\n\n"
                                f"持卡人：{name}\n"
                                f"存款金额：¥ {amount:,.2f}\n"
                                f"{repay_msg}"
                                f"交易时间：{now}")
            self.show_deposit()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        deposit_btn = tk.Button(btn_frame, text="💰 确认存款", command=do_deposit,
                                width=15, height=2, bg='#27ae60', fg='white',
                                font=("Arial", 11), relief=tk.FLAT)
        deposit_btn.pack()
        deposit_btn.bind("<Enter>", lambda e: deposit_btn.config(bg='#229954'))
        deposit_btn.bind("<Leave>", lambda e: deposit_btn.config(bg='#27ae60'))

    # ==================== 取款 ====================

    def show_withdraw(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="💴 取款业务",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="取款信息", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        fields = [
            ("银行卡号", "card_entry"),
            ("取款密码", "pwd_entry"),
            ("取款金额", "amount_entry"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=15, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=15)
            entries[var] = entry

        tk.Label(form_frame, text=f"（单笔超过 ¥{self.large_amount_limits['withdraw']:,.0f} 需人工审核）",
                 font=("Arial", 9), bg='white', fg='#e67e22').grid(row=3, column=0, columnspan=2, pady=5)

        def do_withdraw():
            card = entries["card_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            amount_str = entries["amount_entry"].get().strip()

            if not all([card, pwd, amount_str]):
                messagebox.showerror("错误", "请填写完整信息！")
                return

            try:
                amount = float(amount_str)
            except ValueError:
                messagebox.showerror("错误", "金额格式不正确！")
                return

            if amount <= 0:
                messagebox.showerror("错误", "取款金额必须大于0！")
                return

            self.cursor.execute("SELECT id, name, password, balance, is_frozen, failed_attempts, "
                                "account_type, credit_limit "
                                "FROM customers WHERE card_number = ?", (card,))
            customer = self.cursor.fetchone()
            if not customer:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, db_pwd, balance, frozen, failed, acc_type, credit_limit = customer

            if frozen:
                messagebox.showerror("错误", "该账户已被冻结！")
                return

            if pwd != db_pwd:
                failed += 1
                self.cursor.execute("UPDATE customers SET failed_attempts = ? WHERE id = ?", (failed, cid))
                if failed >= 3:
                    self.cursor.execute("UPDATE customers SET is_frozen = 1 WHERE id = ?", (cid,))
                    self.conn.commit()
                    messagebox.showerror("账户冻结", "密码错误3次，账户已被冻结！")
                    return
                self.conn.commit()
                messagebox.showerror("错误", f"密码错误！还剩 {3 - failed} 次机会")
                return

            available = self._get_available_amount(balance, acc_type, credit_limit)
            if available < amount:
                if acc_type == '信用卡':
                    messagebox.showerror("错误",
                                         f"可用额度不足！当前余额 ¥{balance:,.2f} + 信用额度 ¥{credit_limit:,.0f} = ¥{available:,.2f}，本次需 ¥{amount:,.2f}")
                else:
                    messagebox.showerror("错误", "余额不足！")
                return

            self.cursor.execute("UPDATE customers SET failed_attempts = 0 WHERE id = ?", (cid,))

            if amount > self.large_amount_limits['withdraw']:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.cursor.execute('''
                    INSERT INTO large_amount_transactions (customer_id, transaction_type, amount, apply_time)
                    VALUES (?, 'withdraw', ?, ?)
                ''', (cid, amount, now))
                self.conn.commit()
                messagebox.showinfo("大额审核", f"取款金额 ¥{amount:,.2f} 超过限额，已提交审核！")
                self.add_operation_log("大额取款申请", f"卡号 {card} 申请取款 ¥{amount:,.2f}，待审核")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("UPDATE customers SET balance = balance - ?, last_update_time = ? WHERE id = ?",
                                (amount, now, cid))
            self.cursor.execute('''
                INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                VALUES (?, 'CASH', ?, 'withdraw', ?)
            ''', (card, amount, now))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("取款", f"从卡号 {card} 取款 ¥{amount:,.2f}")

            # 发送取款通知
            new_balance = balance - amount
            if acc_type == '信用卡' and new_balance < 0:
                self.notify_customer(card, "取款通知",
                                     f"您的信用卡 {card} 取出 ¥{amount:,.2f}，剩余余额 ¥{new_balance:,.2f}"
                                     f"（已使用信用额度 ¥{-new_balance:,.2f}，"
                                     f"可用额度 ¥{self._get_available_amount(new_balance, acc_type, credit_limit):,.2f}），交易时间：{now}。")
            else:
                self.notify_customer(card, "取款通知",
                                     f"您的账户 {card} 取出 ¥{amount:,.2f}，剩余余额 ¥{new_balance:,.2f}，交易时间：{now}。")

            credit_msg = ""
            if acc_type == '信用卡':
                if new_balance < 0:
                    credit_msg += f"本次使用信用额度：¥ {-new_balance:,.2f}\n"
                credit_msg += f"信用额度：¥ {credit_limit:,.0f}    可用额度：¥ {self._get_available_amount(new_balance, acc_type, credit_limit):,.2f}\n"
            messagebox.showinfo("取款成功",
                                f"✅ 取款成功！\n\n"
                                f"持卡人：{name}\n"
                                f"取款金额：¥ {amount:,.2f}\n"
                                f"剩余余额：¥ {new_balance:,.2f}\n"
                                f"{credit_msg}"
                                f"交易时间：{now}")
            self.show_withdraw()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="💴 确认取款", command=do_withdraw,
                  width=15, height=2, bg='#e67e22', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 转账 ====================

    def show_transfer(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🔄 转账业务",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="转账信息", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        fields = [
            ("付款卡号", "from_entry"),
            ("付款密码", "pwd_entry"),
            ("收款卡号", "to_entry"),
            ("转账金额", "amount_entry"),
            ("转账备注", "remark_entry"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=12, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=12)
            entries[var] = entry

        tk.Label(form_frame, text=f"（单笔超过 ¥{self.large_amount_limits['transfer']:,.0f} 需人工审核）",
                 font=("Arial", 9), bg='white', fg='#e67e22').grid(row=5, column=0, columnspan=2, pady=5)

        def do_transfer():
            from_card = entries["from_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            to_card = entries["to_entry"].get().strip()
            amount_str = entries["amount_entry"].get().strip()
            remark = entries["remark_entry"].get().strip()

            if not all([from_card, pwd, to_card, amount_str]):
                messagebox.showerror("错误", "请填写必填信息！")
                return

            if from_card == to_card:
                messagebox.showerror("错误", "不能给自己转账！")
                return

            try:
                amount = float(amount_str)
            except ValueError:
                messagebox.showerror("错误", "金额格式不正确！")
                return

            if amount <= 0:
                messagebox.showerror("错误", "转账金额必须大于0！")
                return

            self.cursor.execute("SELECT id, name, password, balance, is_frozen, account_type, credit_limit "
                                "FROM customers WHERE card_number = ?", (from_card,))
            from_cust = self.cursor.fetchone()
            if not from_cust:
                messagebox.showerror("错误", "付款账户不存在！")
                return

            from_id, from_name, db_pwd, from_balance, from_frozen, from_acc_type, from_credit_limit = from_cust

            if from_frozen:
                messagebox.showerror("错误", "付款账户已被冻结！")
                return

            if pwd != db_pwd:
                messagebox.showerror("错误", "付款密码错误！")
                return

            available = self._get_available_amount(from_balance, from_acc_type, from_credit_limit)
            if available < amount:
                if from_acc_type == '信用卡':
                    messagebox.showerror("错误",
                                         f"付款账户可用额度不足！余额 ¥{from_balance:,.2f} + 信用额度 ¥{from_credit_limit:,.0f} = ¥{available:,.2f}，本次需 ¥{amount:,.2f}")
                else:
                    messagebox.showerror("错误", "付款账户余额不足！")
                return

            self.cursor.execute("SELECT id, name, is_frozen FROM customers WHERE card_number = ?", (to_card,))
            to_cust = self.cursor.fetchone()
            if not to_cust:
                messagebox.showerror("错误", "收款账户不存在！")
                return

            to_id, to_name, to_frozen = to_cust
            if to_frozen:
                messagebox.showerror("错误", "收款账户已被冻结！")
                return

            if amount > self.large_amount_limits['transfer']:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.cursor.execute('''
                    INSERT INTO large_amount_transactions (customer_id, transaction_type, amount, target_account, apply_time)
                    VALUES (?, 'transfer', ?, ?, ?)
                ''', (from_id, amount, to_card, now))
                self.conn.commit()
                messagebox.showinfo("大额审核", f"转账金额 ¥{amount:,.2f} 超过限额，已提交审核！")
                self.add_operation_log("大额转账申请",
                                       f"{from_card} 向 {to_card} 转账 ¥{amount:,.2f}，待审核")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("UPDATE customers SET balance = balance - ?, last_update_time = ? WHERE id = ?",
                                (amount, now, from_id))
            self.cursor.execute("UPDATE customers SET balance = balance + ?, last_update_time = ? WHERE id = ?",
                                (amount, now, to_id))

            self.cursor.execute('''
                INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                VALUES (?, ?, ?, 'transfer', ?)
            ''', (from_card, to_card, amount, now))
            self.conn.commit()

            self.current_customer_id = from_id
            self.add_operation_log("转账", f"{from_card} → {to_card}，金额 ¥{amount:,.2f}")

            # 发送转账通知（付款人）
            self.notify_customer(from_card, "转账支出通知",
                                 f"您的账户 {from_card} 向 {to_card}（{to_name}）转出 ¥{amount:,.2f}，交易时间：{now}。")
            # 发送收款通知（收款人）
            self.notify_customer(to_card, "转账到账通知",
                                 f"您的账户 {to_card} 收到 {from_card}（{from_name}）转入 ¥{amount:,.2f}，交易时间：{now}。")

            from_credit_msg = ""
            if from_acc_type == '信用卡':
                from_new_balance = from_balance - amount
                if from_new_balance < 0:
                    from_credit_msg += f"本次使用信用额度：¥ {-from_new_balance:,.2f}\n"
                from_credit_msg += f"信用额度：¥ {from_credit_limit:,.0f}    可用额度：¥ {self._get_available_amount(from_new_balance, from_acc_type, from_credit_limit):,.2f}\n"
            messagebox.showinfo("转账成功",
                                f"✅ 转账成功！\n\n"
                                f"付款人：{from_name}\n"
                                f"收款人：{to_name}\n"
                                f"转账金额：¥ {amount:,.2f}\n"
                                f"{from_credit_msg}"
                                f"交易时间：{now}")
            self.show_transfer()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="🔄 确认转账", command=do_transfer,
                  width=15, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 修改密码 ====================

    def show_change_password(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🔐 修改密码",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="密码修改", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=150, fill=tk.X)

        fields = [
            ("银行卡号", "card_entry"),
            ("原密码", "old_pwd"),
            ("新密码", "new_pwd"),
            ("确认新密码", "confirm_pwd"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=15, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=15)
            entries[var] = entry

        def change_pwd():
            card = entries["card_entry"].get().strip()
            old = entries["old_pwd"].get()
            new = entries["new_pwd"].get()
            confirm = entries["confirm_pwd"].get()

            if not all([card, old, new, confirm]):
                messagebox.showerror("错误", "请填写完整信息！")
                return

            if len(new) < 6:
                messagebox.showerror("错误", "新密码长度不能少于6位！")
                return

            if new != confirm:
                messagebox.showerror("错误", "两次输入的新密码不一致！")
                return

            self.cursor.execute("SELECT id, password FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, db_pwd = result
            if old != db_pwd:
                messagebox.showerror("错误", "原密码错误！")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("UPDATE customers SET password = ?, last_update_time = ?, failed_attempts = 0 "
                                "WHERE id = ?", (new, now, cid))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("修改密码", f"卡号 {card} 修改密码成功")

            messagebox.showinfo("成功", "✅ 密码修改成功！")
            self.show_change_password()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="确认修改", command=change_pwd,
                  width=15, height=2, bg='#9b59b6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 修改个人信息 ====================

    def show_modify_personal_info(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="✏️ 修改个人信息",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.Frame(self.main_frame, bg='white')
        query_frame.pack(pady=10)

        tk.Label(query_frame, text="输入卡号查询：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        card_entry = tk.Entry(query_frame, width=25, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.pack(side=tk.LEFT, padx=5)

        form_frame = tk.LabelFrame(self.main_frame, text="个人信息", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        fields = [
            ("姓名", "name_entry", True),
            ("身份证号", "id_entry", True),
            ("邮箱", "email_entry", False),
            ("住址", "addr_entry", False),
        ]

        entries = {}
        for i, (label, var, readonly) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=12, sticky=tk.E)
            entry = tk.Entry(form_frame, width=35, font=("Arial", 11), bd=2, relief=tk.GROOVE)
            entry.grid(row=i, column=1, padx=10, pady=12)
            if readonly:
                entry.config(state='readonly', readonlybackground='#ecf0f1')
            entries[var] = entry

        # 手机号行（带验证码）
        tk.Label(form_frame, text="手机号：", font=("Arial", 11), bg='white').grid(
            row=4, column=0, padx=10, pady=12, sticky=tk.E)
        phone_entry = tk.Entry(form_frame, width=22, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        phone_entry.grid(row=4, column=1, padx=10, pady=12, sticky=tk.W)
        entries["phone_entry"] = phone_entry

        send_code_btn = tk.Button(form_frame, text="发送验证码",
                                  command=lambda: self.send_modify_phone_code(phone_entry, original_phone, modify_code_status),
                                  bg='#3498db', fg='white', font=("Arial", 10), relief=tk.FLAT)
        send_code_btn.grid(row=4, column=2, padx=5, pady=12, sticky=tk.W)

        tk.Label(form_frame, text="验证码：", font=("Arial", 11), bg='white').grid(
            row=5, column=0, padx=10, pady=12, sticky=tk.E)
        modify_code_entry = tk.Entry(form_frame, width=15, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        modify_code_entry.grid(row=5, column=1, padx=10, pady=12, sticky=tk.W)
        entries["modify_code_entry"] = modify_code_entry

        modify_code_status = tk.Label(form_frame, text="（修改手机号需重新验证）", font=("Arial", 9), bg='white', fg='gray')
        modify_code_status.grid(row=5, column=2, columnspan=2, padx=5, pady=12, sticky=tk.W)

        original_phone = [None]
        current_cid = [None]

        def load_info():
            card = card_entry.get().strip()
            if not card:
                messagebox.showerror("错误", "请输入卡号！")
                return

            self.cursor.execute("SELECT id, name, id_number, phone, email, address "
                                "FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, id_num, phone, email, addr = result
            current_cid[0] = cid
            original_phone[0] = phone

            for var, val in [("name_entry", name), ("id_entry", id_num),
                             ("phone_entry", phone), ("email_entry", email), ("addr_entry", addr)]:
                entries[var].config(state='normal')
                entries[var].delete(0, tk.END)
                entries[var].insert(0, val or "")
                if var in ["name_entry", "id_entry"]:
                    entries[var].config(state='readonly')

            modify_code_status.config(text="（修改手机号需重新验证）", fg='gray')

        def save_info():
            if not current_cid[0]:
                messagebox.showerror("错误", "请先查询账户！")
                return

            phone = entries["phone_entry"].get().strip()
            email = entries["email_entry"].get().strip()
            addr = entries["addr_entry"].get().strip()
            code = entries["modify_code_entry"].get().strip()

            # 如果手机号变更，需要验证
            if phone != original_phone[0]:
                if not phone:
                    messagebox.showerror("错误", "手机号不能为空！")
                    return
                if not re.match(r'^1[3-9]\d{9}$', phone):
                    messagebox.showerror("错误", "手机号格式不正确！")
                    return
                if not code:
                    messagebox.showerror("错误", "手机号已变更，请输入新手机号的验证码！")
                    return
                code_ok, code_msg = self.verify_code(phone, code)
                if not code_ok:
                    messagebox.showerror("验证码错误", code_msg)
                    return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute('''
                UPDATE customers SET phone = ?, email = ?, address = ?, last_update_time = ?
                WHERE id = ?
            ''', (phone, email, addr, now, current_cid[0]))
            self.conn.commit()

            self.current_customer_id = current_cid[0]
            self.add_operation_log("修改个人信息", f"客户ID {current_cid[0]} 更新个人信息")

            # 发送修改通知
            card_num = card_entry.get().strip()
            if phone != original_phone[0]:
                self.notify_customer(card_num, "个人信息变更通知",
                                     f"您的绑定手机号已由 {original_phone[0] or '无'} 变更为 {phone}。")
            else:
                self.notify_customer(card_num, "个人信息变更通知",
                                     "您的个人信息已更新。")

            messagebox.showinfo("成功", "✅ 个人信息更新成功！")

        tk.Button(query_frame, text="查询", command=load_info,
                  width=8, bg='#3498db', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="💾 保存修改", command=save_info,
                  width=15, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 锁卡/解锁 ====================

    def show_lock_unlock(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🔒 锁卡 / 解锁",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="账户操作", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=150, fill=tk.X)

        tk.Label(form_frame, text="银行卡号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=15, sticky=tk.E)
        card_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=15)

        tk.Label(form_frame, text="账户密码：", font=("Arial", 11), bg='white').grid(
            row=1, column=0, padx=10, pady=15, sticky=tk.E)
        pwd_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show="*")
        pwd_entry.grid(row=1, column=1, padx=10, pady=15)

        def toggle_lock():
            card = card_entry.get().strip()
            pwd = pwd_entry.get()

            if not card or not pwd:
                messagebox.showerror("错误", "请填写卡号和密码！")
                return

            self.cursor.execute("SELECT id, name, password, is_frozen FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, db_pwd, frozen = result
            if pwd != db_pwd:
                messagebox.showerror("错误", "密码错误！")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            new_status = 0 if frozen else 1
            action = "解锁" if frozen else "冻结"

            self.cursor.execute("UPDATE customers SET is_frozen = ?, failed_attempts = 0, last_update_time = ? "
                                "WHERE id = ?", (new_status, now, cid))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log(f"账户{action}", f"卡号 {card} 被{action}")

            messagebox.showinfo("操作成功", f"✅ 账户已{action}！")
            self.show_lock_unlock()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="🔓 冻结 / 解锁", command=toggle_lock,
                  width=15, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 申请贷款 ====================

    # ---- 贷款核心算法（真实银行标准） ----

    @staticmethod
    def _add_months(d, months):
        """日期加N个月（自动处理月末，如1月31日+1月=2月28/29日）"""
        year = d.year + (d.month - 1 + months) // 12
        month = (d.month - 1 + months) % 12 + 1
        days_in_month = [31, 29 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        day = min(d.day, days_in_month[month - 1])
        return datetime(year, month, day)

    def _norm_monthly_rate(self, annual_rate):
        """统一年利率为月利率（兼容百分比5.5与小数0.055两种写法）"""
        if annual_rate > 1:
            return annual_rate / 12 / 100
        return annual_rate / 12

    def _build_loan_schedule(self, amount, annual_rate, term_months, method, start_date):
        """生成完整还款计划：[(期数, 应还日, 本金, 利息, 本息合计, 剩余本金), ...]
        支持：等额本息 / 等额本金 / 先息后本 / 到期一次还本付息"""
        monthly_rate = self._norm_monthly_rate(annual_rate)
        schedule = []
        balance = amount
        for k in range(1, term_months + 1):
            due = self._add_months(start_date, k)
            if method == "等额本息":
                if monthly_rate > 0:
                    pay = amount * monthly_rate * (1 + monthly_rate) ** term_months / \
                          ((1 + monthly_rate) ** term_months - 1)
                else:
                    pay = amount / term_months
                interest = balance * monthly_rate
                principal = pay - interest
                if k == term_months:
                    principal = balance
                pay = principal + interest
            elif method == "等额本金":
                principal = amount / term_months
                if k == term_months:
                    principal = balance
                interest = balance * monthly_rate
                pay = principal + interest
            elif method == "先息后本":
                interest = balance * monthly_rate
                principal = balance if k == term_months else 0.0
                pay = principal + interest
            else:  # 到期一次还本付息（单利）
                interest = amount * monthly_rate * term_months
                principal = amount if k == term_months else 0.0
                pay = principal + interest if k == term_months else 0.0
            if method == '到期一次还本付息' and k != term_months:
                pay = 0.0
            else:
                pay = principal + interest
            principal = round(principal, 2)
            interest = round(interest, 2)
            pay = round(pay, 2)
            balance = round(balance - principal, 2)
            if k == term_months:
                balance = 0.0
            schedule.append((k, due.strftime('%Y-%m-%d'), principal, interest, pay, balance))
        return schedule

    def _generate_loan_schedule(self, loan_id, amount, annual_rate, term_months, method, start_date,
                                keep_paid=False):
        """写入还款计划表；keep_paid=True 用于提前还款后重建剩余计划（保留已还历史）"""
        if keep_paid:
            self.cursor.execute("DELETE FROM loan_schedule WHERE loan_id = ? AND status != 'paid'", (loan_id,))
            self.cursor.execute("SELECT COUNT(*) FROM loan_schedule WHERE loan_id = ? AND status = 'paid'", (loan_id,))
            start_period = (self.cursor.fetchone()[0] or 0) + 1
        else:
            self.cursor.execute("DELETE FROM loan_schedule WHERE loan_id = ?", (loan_id,))
            start_period = 1
        schedule = self._build_loan_schedule(amount, annual_rate, term_months, method, start_date)
        total_interest = round(sum(s[3] for s in schedule), 2)
        if method == "到期一次还本付息":
            first_payment = round((amount + total_interest) / term_months, 2) if term_months else amount
        else:
            first_payment = schedule[0][4] if schedule else 0.0
        for k, due, p, i, total, bal in schedule:
            self.cursor.execute('''
                INSERT INTO loan_schedule (loan_id, period_no, due_date, principal_part, interest_part,
                                           total_due, balance_after, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (loan_id, start_period + k - 1, due, p, i, total, bal))
        return first_payment, total_interest, schedule

    def _first_payment(self, amount, annual_rate, term_months, method):
        """计算首期月供（等额本息固定；其余为展示口径）"""
        sched = self._build_loan_schedule(amount, annual_rate, term_months, method, datetime.now())
        if not sched:
            return 0.0
        if method == "到期一次还本付息":
            return round((amount + sum(s[3] for s in sched)) / term_months, 2)
        return sched[0][4]

    def _make_contract_no(self):
        """贷款合同编号：DK + 日期 + 4位流水"""
        return "DK" + datetime.now().strftime('%Y%m%d') + str(random.randint(1000, 9999))

    @staticmethod
    def _get_credit_loan_limit(credit_score):
        """信用评分对应的可贷总额上限（银行风控口径）"""
        if credit_score >= 90:
            return 1000000
        if credit_score >= 80:
            return 500000
        if credit_score >= 70:
            return 100000
        return 0

    def _backfill_schedule(self, loan_id):
        """为历史贷款（无还款计划）按剩余本金重建未来还款计划"""
        self.cursor.execute("SELECT COUNT(*) FROM loan_schedule WHERE loan_id = ?", (loan_id,))
        if self.cursor.fetchone()[0] > 0:
            return
        self.cursor.execute('''
            SELECT loan_amount, interest_rate, term_months, remaining_amount, start_date, repayment_method
            FROM loans WHERE id = ?
        ''', (loan_id,))
        loan = self.cursor.fetchone()
        if not loan:
            return
        amount, rate, term, remaining, start, method = loan
        method = method or '等额本息'
        try:
            start_dt = datetime.strptime(start[:10], '%Y-%m-%d')
        except Exception:
            start_dt = datetime.now()
        elapsed = (datetime.now().year - start_dt.year) * 12 + (datetime.now().month - start_dt.month)
        elapsed = max(0, min(elapsed, term - 1))
        remain_periods = max(1, term - elapsed)
        self._generate_loan_schedule(loan_id, remaining, rate, remain_periods, method, datetime.now())

    def _refresh_loan_overdue(self, loan_id):
        """按还款计划刷新逾期状态与罚息（罚息利率 = 合同利率×1.5，日罚息 = 年罚息利率/360）"""
        self._backfill_schedule(loan_id)
        today = datetime.now().strftime('%Y-%m-%d')
        self.cursor.execute('''
            SELECT due_date, total_due, status FROM loan_schedule
            WHERE loan_id = ? AND status IN ('pending', 'overdue') ORDER BY period_no
        ''', (loan_id,))
        rows = self.cursor.fetchall()
        self.cursor.execute("SELECT interest_rate FROM loans WHERE id = ?", (loan_id,))
        rate_row = self.cursor.fetchone()
        if not rate_row:
            return
        rate = rate_row[0] or 0
        daily_penalty_rate = rate * 1.5 / 100 / 360
        today_dt = datetime.now().date()
        overdue_days = 0
        penalty = 0.0
        next_due = None
        for due_date, total_due, status in rows:
            try:
                due = datetime.strptime(due_date, '%Y-%m-%d').date()
            except Exception:
                continue
            days = (today_dt - due).days
            if days > 0:
                if status == 'pending':
                    self.cursor.execute("UPDATE loan_schedule SET status = 'overdue' WHERE loan_id = ? AND due_date = ?",
                                        (loan_id, due_date))
                penalty += total_due * daily_penalty_rate * days
                overdue_days = max(overdue_days, days)
            if next_due is None or due_date < next_due:
                next_due = due_date
        if next_due is None:
            next_due = today
        self.cursor.execute('''
            UPDATE loans SET is_overdue = ?, overdue_days = ?, penalty_interest = ?, next_payment_date = ?
            WHERE id = ?
        ''', (1 if overdue_days > 0 else 0, overdue_days, round(penalty, 2), next_due, loan_id))
        self.conn.commit()

    def _refresh_all_overdue(self):
        """刷新全部在贷贷款的逾期状态"""
        self.cursor.execute("SELECT id FROM loans WHERE status = 'active'")
        for (lid,) in self.cursor.fetchall():
            try:
                self._refresh_loan_overdue(lid)
            except Exception as e:
                print(f"刷新贷款 {lid} 逾期状态出错: {e}")

    def _get_loan_summary(self, loan_id):
        """贷款概要（含客户卡号姓名）"""
        self.cursor.execute('''
            SELECT l.id, l.contract_no, l.loan_type, l.loan_amount, l.interest_rate, l.term_months,
                   l.remaining_amount, l.monthly_payment, l.status, l.start_date, l.next_payment_date,
                   l.repayment_method, l.purpose, l.paid_principal, l.paid_interest, l.penalty_interest,
                   l.is_overdue, l.overdue_days, l.student_id, l.total_interest,
                   c.card_number, c.name
            FROM loans l JOIN customers c ON l.customer_id = c.id WHERE l.id = ?
        ''', (loan_id,))
        return self.cursor.fetchone()

    def _get_active_loan_total(self, customer_id):
        """客户当前未结清贷款总额"""
        self.cursor.execute("SELECT COALESCE(SUM(remaining_amount), 0) FROM loans "
                            "WHERE customer_id = ? AND status = 'active'", (customer_id,))
        return self.cursor.fetchone()[0] or 0

    def _ask_prepay_mode(self, parent):
        """弹出提前还款调整方式选择，返回 'reduce' / 'shorten' / None"""
        mode_dialog = tk.Toplevel(parent)
        mode_dialog.title("选择还款调整方式")
        mode_dialog.geometry("420x200")
        mode_dialog.configure(bg='white')
        mode_dialog.transient(parent)
        mode_var = tk.StringVar(value="reduce")
        tk.Label(mode_dialog, text="提前还款后如何调整？", font=("Arial", 11, "bold"),
                 bg='white').pack(pady=12)
        tk.Radiobutton(mode_dialog, text="减少月供（贷款期限不变）", variable=mode_var, value="reduce",
                       bg='white', font=("Arial", 10)).pack(anchor=tk.W, padx=30)
        tk.Radiobutton(mode_dialog, text="缩短期限（月供基本不变）", variable=mode_var, value="shorten",
                       bg='white', font=("Arial", 10)).pack(anchor=tk.W, padx=30)
        result = {}

        def confirm():
            result['mode'] = mode_var.get()
            mode_dialog.destroy()

        tk.Button(mode_dialog, text="确定", command=confirm, bg='#3498db', fg='white',
                  relief=tk.FLAT, width=10).pack(pady=12)
        parent.wait_window(mode_dialog)
        return result.get('mode')

    @staticmethod
    def _get_credit_limit(account_type):
        """信用卡授信额度（当前固定 20000 元），其他账户类型为 0"""
        return 20000.0 if account_type == '信用卡' else 0.0

    def _get_available_amount(self, balance, account_type, credit_limit=None):
        """可用金额：信用卡 = 余额 + 信用额度（可在额度内透支消费），其他账户 = 余额"""
        if account_type == '信用卡':
            limit = credit_limit if credit_limit is not None else self._get_credit_limit(account_type)
            return balance + (limit or 0)
        return balance

    def show_apply_loan(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📝 申请贷款",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="贷款申请（真实银行信贷流程）", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=10, padx=60, fill=tk.X)

        # 贷款产品选择
        tk.Label(form_frame, text="贷款产品：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        self.cursor.execute("SELECT product_name, annual_rate, min_amount, max_amount, min_term, max_term, description "
                            "FROM loan_products WHERE enabled = 1 ORDER BY id")
        products = self.cursor.fetchall()
        product_names = [p[0] for p in products]
        product_map = {p[0]: p for p in products}
        product_var = tk.StringVar(value=product_names[0] if product_names else "普通贷款")
        product_box = ttk.Combobox(form_frame, textvariable=product_var, values=product_names,
                                   state="readonly", width=27, font=("Arial", 11))
        product_box.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)

        product_info = tk.Label(form_frame, text="", font=("Arial", 9), bg='white', fg='#7f8c8d', justify=tk.LEFT)
        product_info.grid(row=1, column=0, columnspan=2, pady=2, sticky=tk.W, padx=20)

        # 还款方式
        tk.Label(form_frame, text="还款方式：", font=("Arial", 11), bg='white').grid(
            row=2, column=0, padx=10, pady=10, sticky=tk.E)
        method_frame = tk.Frame(form_frame, bg='white')
        method_frame.grid(row=2, column=1, padx=10, pady=10, sticky=tk.W)
        method_var = tk.StringVar(value="等额本息")
        for m in ["等额本息", "等额本金", "先息后本", "到期一次还本付息"]:
            tk.Radiobutton(method_frame, text=m, variable=method_var, value=m,
                           bg='white', font=("Arial", 10)).pack(side=tk.LEFT, padx=5)

        fields = [
            ("银行卡号", "card_entry", ""),
            ("取款密码", "pwd_entry", "*"),
            ("贷款金额(元)", "amount_entry", ""),
            ("贷款期限(月)", "term_entry", ""),
            ("贷款用途", "purpose_entry", ""),
        ]
        entries = {}
        for i, (label, var, show) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i + 3, column=0, padx=10, pady=10, sticky=tk.E)
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i + 3, column=1, padx=10, pady=10, sticky=tk.W)
            entries[var] = entry

        student_frame = tk.LabelFrame(form_frame, text="助学贷款专属（仅助学贷款需填写）",
                                      font=("Arial", 10, "bold"), bg='#fff9e6', padx=15, pady=10)
        student_frame.grid(row=8, column=0, columnspan=2, padx=10, pady=10, sticky=tk.EW)
        tk.Label(student_frame, text="学号：", font=("Arial", 10), bg='#fff9e6').grid(
            row=0, column=0, padx=10, pady=8, sticky=tk.E)
        student_id_entry = tk.Entry(student_frame, width=25, font=("Arial", 10), bd=2, relief=tk.GROOVE)
        student_id_entry.grid(row=0, column=1, padx=10, pady=8, sticky=tk.W)
        tk.Label(student_frame, text="（助学贷款需通过学籍系统核验在读身份）",
                 font=("Arial", 9), bg='#fff9e6', fg='#e67e22').grid(row=1, column=0, columnspan=2, pady=5)

        rate_label = tk.Label(form_frame, text="", font=("Arial", 10), bg='white', fg='#27ae60')
        rate_label.grid(row=9, column=0, columnspan=2, pady=5)

        def update_product(*args):
            pname = product_var.get()
            p = product_map.get(pname)
            if not p:
                return
            _, rate, min_amt, max_amt, min_term, max_term, desc = p
            product_info.config(text=f"{desc}\n额度区间：¥{min_amt:,.0f} ~ ¥{max_amt:,.0f}    期限区间：{min_term} ~ {max_term} 个月")
            rate_label.config(text=f"年利率：{rate}%  |  {pname}专属利率")
            if pname == "助学贷款":
                student_frame.grid()
            else:
                student_frame.grid_remove()

        product_var.trace_add("write", update_product)
        update_product()

        def apply_loan():
            card = entries["card_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            amount_str = entries["amount_entry"].get().strip()
            term_str = entries["term_entry"].get().strip()
            purpose = entries["purpose_entry"].get().strip()
            loan_type = product_var.get()
            method = method_var.get()
            student_id = student_id_entry.get().strip()

            if not all([card, pwd, amount_str, term_str]):
                messagebox.showerror("错误", "请填写必填信息！")
                return

            try:
                amount = float(amount_str)
                term = int(term_str)
            except ValueError:
                messagebox.showerror("错误", "金额或期限格式不正确！")
                return

            if amount <= 0 or term <= 0:
                messagebox.showerror("错误", "金额和期限必须大于0！")
                return

            p = product_map.get(loan_type)
            if not p:
                messagebox.showerror("错误", "请选择有效的贷款产品！")
                return
            _, rate, min_amt, max_amt, min_term, max_term, _ = p
            if amount < min_amt or amount > max_amt:
                messagebox.showerror("错误", f"该产品贷款金额须在 ¥{min_amt:,.0f} ~ ¥{max_amt:,.0f} 之间！")
                return
            if term < min_term or term > max_term:
                messagebox.showerror("错误", f"该产品贷款期限须在 {min_term} ~ {max_term} 个月之间！")
                return

            self.cursor.execute("SELECT id, name, id_number, password, balance, is_frozen, credit_score "
                                "FROM customers WHERE card_number = ?", (card,))
            customer = self.cursor.fetchone()
            if not customer:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, id_number, db_pwd, balance, frozen, credit = customer

            if frozen:
                messagebox.showerror("错误", "账户已被冻结，无法申请贷款！")
                return

            if pwd != db_pwd:
                messagebox.showerror("错误", "密码错误！")
                return

            if credit < 90:
                messagebox.showerror("错误", f"信用评分过低（当前{credit}分，需90分以上），无法申请贷款！")
                return

            # 信用额度风控：在贷余额 + 本次申请 <= 信用可贷上限
            credit_limit = self._get_credit_loan_limit(credit)
            existing_total = self._get_active_loan_total(cid)
            if existing_total + amount > credit_limit:
                messagebox.showerror("错误",
                                     f"信用额度不足！您的可贷总额上限为 ¥{credit_limit:,.2f}，"
                                     f"当前未还贷款 ¥{existing_total:,.2f}，本次申请 ¥{amount:,.2f} 将超出额度。")
                return

            if loan_type == "助学贷款":
                if not student_id:
                    messagebox.showerror("错误", "助学贷款必须填写学号！")
                    return
                verified, msg, verified_sid = self.verify_student_identity(id_number, student_id)
                if not verified:
                    messagebox.showerror("学籍核验失败", f"学籍系统核验未通过：{msg}\n\n无法办理助学贷款！")
                    return
                student_id = verified_sid
            else:
                student_id = None

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 大额贷款（单笔超过阈值）提交经理审批
            if amount > self.large_amount_limits['loan']:
                monthly_payment = self._first_payment(amount, rate, term, method)
                self.cursor.execute('''
                    INSERT INTO loan_approvals (customer_id, loan_type, loan_amount, interest_rate,
                                                term_months, monthly_payment, student_id, apply_time, status,
                                                repayment_method, purpose)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ''', (cid, loan_type, amount, rate, term, monthly_payment, student_id, now, method, purpose))
                self.conn.commit()

                self.current_customer_id = cid
                self.add_operation_log("大额贷款申请",
                                       f"{name} 申请大额{loan_type} ¥{amount:,.2f}，期限{term}个月，"
                                       f"还款方式{method}，已提交经理审核")

                self.notify_customer(card, "贷款申请提交通知",
                                     f"您的{loan_type}申请 ¥{amount:,.2f}（期限{term}个月，{method}）已提交，等待银行经理审核。")

                msg = f"⏳ 大额贷款申请已提交，等待银行经理审核！\n\n"
                msg += f"借款人：{name}\n贷款类型：{loan_type}\n申请金额：¥ {amount:,.2f}\n"
                msg += f"贷款期限：{term} 个月\n还款方式：{method}\n预计月供：¥ {monthly_payment:,.2f}\n"
                msg += f"\n请耐心等待银行经理审核，审核通过后将生成贷款合同并自动放款。"
                messagebox.showinfo("申请已提交", msg)
                self.show_apply_loan()
                return

            # 小额贷款：系统自动审批放款，生成合同与还款计划
            contract_no = self._make_contract_no()
            next_payment = self._add_months(datetime.now(), 1).strftime('%Y-%m-%d')

            self.cursor.execute('''
                INSERT INTO loans (customer_id, loan_type, loan_amount, interest_rate, term_months,
                                   remaining_amount, monthly_payment, status, start_date,
                                   next_payment_date, student_id, contract_no, repayment_method,
                                   purpose, total_interest)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            ''', (cid, loan_type, amount, rate, term, amount, 0.0, now, next_payment,
                  student_id, contract_no, method, purpose, 0.0))
            self.conn.commit()
            loan_id = self.cursor.lastrowid
            monthly_payment, total_interest, _ = self._generate_loan_schedule(
                loan_id, amount, rate, term, method, datetime.now())
            self.cursor.execute("UPDATE loans SET monthly_payment = ?, total_interest = ? WHERE id = ?",
                                (monthly_payment, total_interest, loan_id))
            self.cursor.execute("UPDATE customers SET balance = balance + ?, last_update_time = ? WHERE id = ?",
                                (amount, now, cid))
            self.cursor.execute('''
                INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                VALUES ('BANK', ?, ?, 'loan_disbursement', ?)
            ''', (card, amount, now))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("贷款申请",
                                   f"{name} 申请{loan_type} ¥{amount:,.2f}，期限{term}个月，"
                                   f"还款方式{method}，合同号{contract_no}")

            self.notify_customer(card, "贷款放款通知",
                                 f"您的{loan_type} ¥{amount:,.2f}已放款至账户，合同号 {contract_no}，"
                                 f"期限{term}个月，{method}，首期还款 ¥{monthly_payment:,.2f}，首次还款日 {next_payment}。")

            msg = f"✅ 贷款申请成功并已放款！\n\n"
            msg += f"借款人：{name}\n合同编号：{contract_no}\n贷款类型：{loan_type}\n"
            if loan_type == "助学贷款":
                msg += f"关联学号：{student_id}（已通过学籍核验）\n"
            msg += f"贷款金额：¥ {amount:,.2f}\n"
            msg += f"年利率：{rate}%\n还款方式：{method}\n贷款期限：{term} 个月\n"
            msg += f"首期月供：¥ {monthly_payment:,.2f}\n应还总利息：¥ {total_interest:,.2f}\n首次还款日：{next_payment}"

            messagebox.showinfo("贷款成功", msg)
            self.show_apply_loan()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="📝 提交申请", command=apply_loan,
                  width=15, height=2, bg='#9b59b6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 我的贷款 ====================

    def show_my_loans(self):
        self.clear_main_frame()
        self._refresh_all_overdue()

        title_label = tk.Label(self.main_frame, text="📊 我的贷款",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.Frame(self.main_frame, bg='white')
        query_frame.pack(pady=10)

        tk.Label(query_frame, text="输入卡号：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        card_entry = tk.Entry(query_frame, width=25, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.pack(side=tk.LEFT, padx=5)

        list_frame = tk.LabelFrame(self.main_frame, text="贷款记录（双击查看还款计划）", font=("Arial", 12, "bold"),
                                   bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=20)

        columns = ("合同编号", "贷款类型", "贷款金额", "年利率", "还款方式", "期限", "剩余本金",
                   "首期月供", "状态", "逾期", "下次还款日")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=100, anchor=tk.CENTER)
        tree.column("合同编号", width=120)
        tree.column("贷款金额", width=100)
        tree.column("剩余本金", width=100)
        tree.column("下次还款日", width=100)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        def query_loans():
            card = card_entry.get().strip()
            if not card:
                messagebox.showerror("错误", "请输入卡号！")
                return
            self.cursor.execute("SELECT id FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                messagebox.showerror("错误", "账户不存在！")
                return
            cid = result[0]
            self.current_customer_id = cid

            self.cursor.execute('''
                SELECT id, contract_no, loan_type, loan_amount, interest_rate, repayment_method,
                       term_months, remaining_amount, monthly_payment, status, is_overdue, overdue_days,
                       next_payment_date
                FROM loans WHERE customer_id = ? ORDER BY start_date DESC
            ''', (cid,))

            for item in tree.get_children():
                tree.delete(item)

            loans = self.cursor.fetchall()
            if not loans:
                messagebox.showinfo("提示", "该客户暂无贷款记录")
                return

            for loan in loans:
                (lid, contract_no, ltype, amount, rate, method, term, remain, monthly,
                 status, is_overdue, overdue_days, next_date) = loan
                status_text = "还款中" if status == 'active' else "已结清"
                if status == 'active' and is_overdue:
                    overdue_text = f"逾期{overdue_days}天"
                elif status == 'active':
                    overdue_text = "正常"
                else:
                    overdue_text = "-"
                tree.insert("", "end", values=(
                    contract_no or f"L{lid:04d}", ltype, f"¥{amount:,.2f}", f"{rate}%",
                    method or "等额本息", f"{term}月", f"¥{remain:,.2f}",
                    f"¥{monthly:,.2f}", status_text, overdue_text, next_date
                ), tags=(str(lid),))

            self.add_operation_log("查询贷款", f"查询卡号 {card} 的贷款记录")

        def on_double_click(event):
            selected = tree.selection()
            if not selected:
                return
            item = tree.item(selected[0])
            tags = item['tags']
            if tags:
                try:
                    self.show_loan_detail(int(tags[0]))
                except ValueError:
                    pass

        tree.bind("<Double-1>", on_double_click)

        tk.Button(query_frame, text="查询", command=query_loans,
                  width=8, bg='#3498db', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    def show_loan_detail(self, loan_id):
        """贷款详情：合同信息 + 还款计划表 + 还款明细（支持提前还款）"""
        self._refresh_loan_overdue(loan_id)
        loan = self._get_loan_summary(loan_id)
        if not loan:
            messagebox.showerror("错误", "未找到该贷款！")
            return
        (lid, contract_no, ltype, amount, rate, term, remaining, monthly, status, start_date,
         next_date, method, purpose, paid_principal, paid_interest, penalty_interest,
         is_overdue, overdue_days, sid, total_interest, card, name) = loan

        dialog = tk.Toplevel(self.root)
        dialog.title(f"贷款详情 - 合同号 {contract_no or lid}")
        dialog.geometry("980x720")
        dialog.configure(bg='white')
        dialog.transient(self.root)

        info_frame = tk.LabelFrame(dialog, text="贷款合同信息", font=("Arial", 11, "bold"),
                                   bg='white', padx=15, pady=8)
        info_frame.pack(fill=tk.X, padx=15, pady=8)

        status_text = "还款中" if status == 'active' else "已结清"
        overdue_text = f"逾期 {overdue_days} 天" if is_overdue and status == 'active' else "正常"
        info_lines = [
            ("合同编号", contract_no or f"L{lid:04d}"), ("借款人", f"{name}（卡号 {card}）"),
            ("贷款类型", ltype), ("贷款金额", f"¥{amount:,.2f}"),
            ("年利率", f"{rate}%"), ("还款方式", method or "等额本息"),
            ("贷款期限", f"{term} 个月"), ("贷款用途", purpose or "-"),
            ("已还本金", f"¥{paid_principal:,.2f}"), ("已还利息", f"¥{paid_interest:,.2f}"),
            ("剩余本金", f"¥{remaining:,.2f}"), ("应还总利息", f"¥{total_interest:,.2f}"),
            ("当前罚息", f"¥{penalty_interest:,.2f}"), ("状态", f"{status_text}（{overdue_text}）"),
            ("放款日期", start_date), ("下次还款日", next_date),
        ]
        for i, (k, v) in enumerate(info_lines):
            r, c = i // 4, (i % 4) * 2
            tk.Label(info_frame, text=f"{k}：", font=("Arial", 9), bg='white', fg='#666').grid(
                row=r, column=c, sticky=tk.E, padx=4, pady=3)
            tk.Label(info_frame, text=str(v), font=("Arial", 9, "bold"), bg='white', fg='#2c3e50').grid(
                row=r, column=c + 1, sticky=tk.W, padx=4, pady=3)

        nb = ttk.Notebook(dialog)
        nb.pack(fill=tk.BOTH, expand=True, padx=15, pady=8)

        tab1 = tk.Frame(nb, bg='white')
        tab2 = tk.Frame(nb, bg='white')
        nb.add(tab1, text="📅 还款计划表")
        nb.add(tab2, text="📜 还款明细")

        sched_columns = ("期数", "应还日期", "应还本金", "应还利息", "本息合计", "剩余本金", "状态")
        sched_tree = ttk.Treeview(tab1, columns=sched_columns, show="headings", height=16)
        for col in sched_columns:
            sched_tree.heading(col, text=col)
            sched_tree.column(col, width=110, anchor=tk.CENTER)
        sched_scroll = ttk.Scrollbar(tab1, orient=tk.VERTICAL, command=sched_tree.yview)
        sched_tree.configure(yscrollcommand=sched_scroll.set)
        sched_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        sched_tree.pack(fill=tk.BOTH, expand=True)

        pay_columns = ("时间", "还款类型", "本金", "利息", "罚息", "还款金额")
        pay_tree = ttk.Treeview(tab2, columns=pay_columns, show="headings", height=16)
        for col in pay_columns:
            pay_tree.heading(col, text=col)
            pay_tree.column(col, width=120, anchor=tk.CENTER)
        pay_scroll = ttk.Scrollbar(tab2, orient=tk.VERTICAL, command=pay_tree.yview)
        pay_tree.configure(yscrollcommand=pay_scroll.set)
        pay_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        pay_tree.pack(fill=tk.BOTH, expand=True)

        def load_schedule():
            for item in sched_tree.get_children():
                sched_tree.delete(item)
            self.cursor.execute('''
                SELECT period_no, due_date, principal_part, interest_part, total_due, balance_after, status
                FROM loan_schedule WHERE loan_id = ? ORDER BY period_no
            ''', (loan_id,))
            for row in self.cursor.fetchall():
                pno, due, pp, ip, total, bal, st = row
                st_map = {'pending': '待还', 'paid': '已还', 'overdue': '逾期'}
                sched_tree.insert("", "end", values=(
                    f"第{pno}期", due, f"¥{pp:,.2f}", f"¥{ip:,.2f}", f"¥{total:,.2f}",
                    f"¥{bal:,.2f}", st_map.get(st, st)
                ), tags=('overdue' if st == 'overdue' else '',))
            sched_tree.tag_configure('overdue', foreground='#e74c3c')

        def load_payments():
            for item in pay_tree.get_children():
                pay_tree.delete(item)
            self.cursor.execute('''
                SELECT payment_date, payment_type, principal_part, interest_part, penalty, payment_amount
                FROM loan_payments WHERE loan_id = ? ORDER BY payment_date
            ''', (loan_id,))
            for row in self.cursor.fetchall():
                pdate, ptype, pp, ip, pen, total = row
                pay_tree.insert("", "end", values=(
                    pdate, ptype, f"¥{pp:,.2f}", f"¥{ip:,.2f}", f"¥{pen:,.2f}", f"¥{total:,.2f}"
                ))

        load_schedule()
        load_payments()

        def refresh_all():
            self._refresh_loan_overdue(loan_id)
            load_schedule()
            load_payments()

        btn_frame = tk.Frame(dialog, bg='white')
        btn_frame.pack(pady=10)

        def do_partial():
            if status != 'active':
                messagebox.showinfo("提示", "该贷款已结清！")
                return
            pwd = simpledialog.askstring("取款密码", "请输入取款密码确认身份：", show="*", parent=dialog)
            if pwd is None:
                return
            amount_str = simpledialog.askstring("部分提前还款",
                                                f"当前剩余本金：¥{remaining:,.2f}\n请输入提前还款金额（至少 ¥1,000）：",
                                                parent=dialog)
            if not amount_str:
                return
            mode = self._ask_prepay_mode(dialog)
            if not mode:
                return
            ok, msg = self._partial_repay(loan_id, amount_str, mode, pwd)
            if ok:
                messagebox.showinfo("提前还款成功", msg, parent=dialog)
                refresh_all()
            else:
                messagebox.showerror("操作失败", msg, parent=dialog)

        def do_settle():
            if status != 'active':
                messagebox.showinfo("提示", "该贷款已结清！")
                return
            pwd = simpledialog.askstring("取款密码", "请输入取款密码确认身份：", show="*", parent=dialog)
            if pwd is None:
                return
            if not messagebox.askyesno("确认结清",
                                       "提前结清将一次性归还剩余本金及未还利息、罚息，并收取结清违约金（剩余本金×0.5%）。是否继续？",
                                       parent=dialog):
                return
            ok, msg = self._settle_loan(loan_id, pwd)
            if ok:
                messagebox.showinfo("结清成功", msg, parent=dialog)
                refresh_all()
            else:
                messagebox.showerror("操作失败", msg, parent=dialog)

        if status == 'active':
            tk.Button(btn_frame, text="🔁 部分提前还款", command=do_partial,
                      width=16, bg='#f39c12', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)
            tk.Button(btn_frame, text="💯 提前结清", command=do_settle,
                      width=16, bg='#e74c3c', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="🔄 刷新", command=refresh_all,
                  width=12, bg='#3498db', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)

    # ---- 提前还款业务逻辑 ----

    def _partial_repay(self, loan_id, amount_str, mode, password):
        """部分提前还款：mode='reduce' 减少月供（期限不变）；mode='shorten' 缩短期限（月供基本不变）"""
        try:
            amount = float(str(amount_str).strip())
        except (ValueError, TypeError):
            return False, "还款金额格式不正确！"
        loan = self._get_loan_summary(loan_id)
        if not loan:
            return False, "贷款不存在！"
        (lid, contract_no, ltype, amount_orig, rate, term, remaining, monthly, status, start_date,
         next_date, method, purpose, paid_principal, paid_interest, penalty_interest,
         is_overdue, overdue_days, sid, total_interest, card, name) = loan
        if status != 'active':
            return False, "该贷款已结清！"
        if amount < 1000:
            return False, "部分提前还款金额至少为 ¥1,000！"
        if amount > remaining - 1:
            return False, f"提前还款金额不得超过剩余本金（当前剩余 ¥{remaining:,.2f}），如需结清请使用提前结清。"
        self.cursor.execute("SELECT customer_id FROM loans WHERE id = ?", (loan_id,))
        cid = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT card_number, password, balance, is_frozen FROM customers WHERE id = ?", (cid,))
        card2, db_pwd, balance, frozen = self.cursor.fetchone()
        if frozen:
            return False, "账户已被冻结，无法操作！"
        if password != db_pwd:
            return False, "取款密码错误！"
        if balance < amount:
            return False, f"账户余额不足！当前余额 ¥{balance:,.2f}，需还款 ¥{amount:,.2f}。"
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        new_remaining = round(remaining - amount, 2)
        # 扣款并记账
        self.cursor.execute("UPDATE customers SET balance = balance - ?, last_update_time = ? WHERE id = ?",
                            (amount, now, cid))
        self.cursor.execute('''
            INSERT INTO loan_payments (loan_id, payment_amount, payment_date, principal_part,
                                       interest_part, penalty, period_no, payment_type)
            VALUES (?, ?, ?, ?, 0, 0, 0, '提前部分还款')
        ''', (loan_id, amount, now, amount))
        self.cursor.execute('''
            UPDATE loans SET remaining_amount = ?, paid_principal = paid_principal + ? WHERE id = ?
        ''', (new_remaining, amount, loan_id))
        self.conn.commit()
        # 重建剩余还款计划
        self.cursor.execute("SELECT COUNT(*) FROM loan_schedule WHERE loan_id = ? AND status = 'paid'", (loan_id,))
        paid_periods = self.cursor.fetchone()[0] or 0
        pending_periods = max(1, term - paid_periods)
        method = method or '等额本息'
        old_monthly = monthly or self._first_payment(amount_orig, rate, pending_periods, method)
        new_term = pending_periods
        if mode == 'shorten' and pending_periods > 1:
            best_n = pending_periods
            for n in range(pending_periods, 0, -1):
                sched = self._build_loan_schedule(new_remaining, rate, n, method, datetime.now())
                first = sched[0][4] if sched else 0
                if method == "到期一次还本付息" and n > 0:
                    first = (new_remaining + sum(s[3] for s in sched)) / n
                if first <= old_monthly * 1.0001:
                    best_n = n
                    break
            new_term = best_n
        first_payment, new_total_interest, _ = self._generate_loan_schedule(
            loan_id, new_remaining, rate, new_term, method, datetime.now(), keep_paid=True)
        self.cursor.execute('''
            UPDATE loans SET monthly_payment = ?, total_interest = ?, term_months = ? WHERE id = ?
        ''', (first_payment, round(paid_interest + new_total_interest, 2), new_term, loan_id))
        self.conn.commit()
        self._refresh_loan_overdue(loan_id)
        self.add_operation_log("提前部分还款",
                               f"贷款ID {loan_id}（合同 {contract_no}）提前还款 ¥{amount:,.2f}，"
                               f"方式：{'缩短期限' if mode == 'shorten' else '减少月供'}")
        self.notify_customer(card2, "提前还款通知",
                             f"您的贷款（合同 {contract_no}）提前还款 ¥{amount:,.2f} 成功，"
                             f"剩余本金 ¥{new_remaining:,.2f}，新还款计划已生成。")
        return True, (f"✅ 提前还款成功！\n本次偿还本金：¥{amount:,.2f}\n剩余本金：¥{new_remaining:,.2f}\n"
                      f"还款调整：{'缩短期限至 ' + str(new_term) + ' 个月' if mode == 'shorten' else '月供减少'}，"
                      f"新首期月供：¥{first_payment:,.2f}")

    def _settle_loan(self, loan_id, password):
        """提前结清：剩余本金 + 未还利息 + 未付罚息 + 结清违约金（剩余本金×0.5%）"""
        loan = self._get_loan_summary(loan_id)
        if not loan:
            return False, "贷款不存在！"
        (lid, contract_no, ltype, amount, rate, term, remaining, monthly, status, start_date,
         next_date, method, purpose, paid_principal, paid_interest, penalty_interest,
         is_overdue, overdue_days, sid, total_interest, card, name) = loan
        if status != 'active':
            return False, "该贷款已结清！"
        self.cursor.execute("SELECT customer_id FROM loans WHERE id = ?", (loan_id,))
        cid = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT card_number, password, balance, is_frozen FROM customers WHERE id = ?", (cid,))
        card2, db_pwd, balance, frozen = self.cursor.fetchone()
        if frozen:
            return False, "账户已被冻结，无法操作！"
        if password != db_pwd:
            return False, "取款密码错误！"
        self._refresh_loan_overdue(loan_id)
        loan = self._get_loan_summary(loan_id)
        remaining = loan[6]
        penalty_interest = loan[15]
        self.cursor.execute('''
            SELECT COALESCE(SUM(interest_part), 0) FROM loan_schedule
            WHERE loan_id = ? AND status IN ('pending', 'overdue')
        ''', (loan_id,))
        pending_interest = self.cursor.fetchone()[0] or 0
        settle_fee = round(remaining * 0.005, 2)  # 结清违约金 0.5%
        total = round(remaining + pending_interest + penalty_interest + settle_fee, 2)
        if balance < total:
            return False, (f"账户余额不足！本次结清共需 ¥{total:,.2f}\n"
                           f"（剩余本金 ¥{remaining:,.2f} + 未还利息 ¥{pending_interest:,.2f} + "
                           f"罚息 ¥{penalty_interest:,.2f} + 违约金 ¥{settle_fee:,.2f}）\n当前余额 ¥{balance:,.2f}")
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.cursor.execute("UPDATE customers SET balance = balance - ?, last_update_time = ? WHERE id = ?",
                            (total, now, cid))
        self.cursor.execute('''
            INSERT INTO loan_payments (loan_id, payment_amount, payment_date, principal_part,
                                       interest_part, penalty, period_no, payment_type)
            VALUES (?, ?, ?, ?, ?, ?, 0, '提前结清')
        ''', (loan_id, total, now, remaining, pending_interest, penalty_interest + settle_fee))
        self.cursor.execute("UPDATE loan_schedule SET status = 'paid', paid_date = ?, paid_amount = total_due "
                            "WHERE loan_id = ? AND status IN ('pending', 'overdue')", (now, loan_id))
        self.cursor.execute('''
            UPDATE loans SET remaining_amount = 0, status = 'paid', paid_principal = paid_principal + ?,
                             paid_interest = paid_interest + ?, penalty_interest = 0,
                             is_overdue = 0, overdue_days = 0 WHERE id = ?
        ''', (remaining, pending_interest, loan_id))
        self.cursor.execute('''
            INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
            VALUES (?, 'BANK', ?, 'loan_settlement', ?)
        ''', (card2, total, now))
        self.conn.commit()
        self.add_operation_log("贷款提前结清",
                               f"贷款ID {loan_id}（合同 {contract_no}）提前结清，结清金额 ¥{total:,.2f}（含违约金 ¥{settle_fee:,.2f}）")
        self.notify_customer(card2, "贷款结清通知",
                             f"您的贷款（合同 {contract_no}）已提前结清，结清金额 ¥{total:,.2f}。感谢您选择本行！")
        return True, (f"🎉 贷款已全部结清！\n\n结清金额：¥{total:,.2f}\n"
                      f"其中：剩余本金 ¥{remaining:,.2f}、未还利息 ¥{pending_interest:,.2f}、"
                      f"罚息 ¥{penalty_interest:,.2f}、违约金 ¥{settle_fee:,.2f}\n贷款合同 {contract_no} 已结清。")

    # ==================== 还款 ====================

    def show_repayment(self):
        self.clear_main_frame()
        self._refresh_all_overdue()

        title_label = tk.Label(self.main_frame, text="💸 贷款还款（按期）",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="按期还款（偿还当期本息）", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        fields = [
            ("银行卡号", "card_entry"),
            ("取款密码", "pwd_entry"),
            ("贷款ID", "loan_id_entry"),
        ]
        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=12, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=12)
            entries[var] = entry

        due_label = tk.Label(form_frame, text="", font=("Arial", 11), bg='white', fg='#2c3e50', justify=tk.LEFT)
        due_label.grid(row=3, column=0, columnspan=2, pady=5)

        tk.Label(form_frame, text="还款金额(元)：", font=("Arial", 11), bg='white').grid(
            row=4, column=0, padx=10, pady=12, sticky=tk.E)
        amount_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        amount_entry.grid(row=4, column=1, padx=10, pady=12)

        tk.Label(form_frame, text="（自动带出当期应还金额；多还部分将自动用于提前偿还本金）",
                 font=("Arial", 9), bg='white', fg='#7f8c8d').grid(row=5, column=0, columnspan=2, pady=5)

        loan_status = {}

        def load_due(*args):
            card = entries["card_entry"].get().strip()
            loan_id_str = entries["loan_id_entry"].get().strip()
            loan_status.clear()
            due_label.config(text="")
            amount_entry.delete(0, tk.END)
            if not card or not loan_id_str:
                return
            try:
                loan_id = int(loan_id_str)
            except ValueError:
                return
            self.cursor.execute("SELECT id FROM customers WHERE card_number = ?", (card,))
            cust = self.cursor.fetchone()
            if not cust:
                due_label.config(text="账户不存在！", fg='#e74c3c')
                return
            cid = cust[0]
            self.cursor.execute("SELECT status FROM loans WHERE id = ? AND customer_id = ?", (loan_id, cid))
            lrow = self.cursor.fetchone()
            if not lrow:
                due_label.config(text="未找到该贷款！", fg='#e74c3c')
                return
            if lrow[0] != 'active':
                due_label.config(text="该贷款已结清！", fg='#e74c3c')
                return
            self._refresh_loan_overdue(loan_id)
            self.cursor.execute('''
                SELECT period_no, due_date, principal_part, interest_part, total_due, status
                FROM loan_schedule WHERE loan_id = ? AND status IN ('pending', 'overdue')
                ORDER BY period_no LIMIT 1
            ''', (loan_id,))
            row = self.cursor.fetchone()
            if not row:
                due_label.config(text="该贷款已无待还期次（可办理提前结清）", fg='#e67e22')
                return
            pno, due_date, pp, ip, total, st = row
            days = 0
            try:
                due_dt = datetime.strptime(due_date, '%Y-%m-%d').date()
                days = max(0, (datetime.now().date() - due_dt).days)
            except Exception:
                days = 0
            self.cursor.execute("SELECT interest_rate FROM loans WHERE id = ?", (loan_id,))
            rate = self.cursor.fetchone()[0] or 0
            daily_penalty_rate = rate * 1.5 / 100 / 360
            penalty = round(total * daily_penalty_rate * days, 2) if days > 0 else 0.0
            due_total = round(total + penalty, 2)
            loan_status['loan_id'] = loan_id
            loan_status['cid'] = cid
            loan_status['pno'] = pno
            loan_status['pp'] = pp
            loan_status['ip'] = ip
            loan_status['penalty'] = penalty
            loan_status['due_total'] = due_total
            due_label.config(
                text=(f"第 {pno} 期应还：本金 ¥{pp:,.2f} + 利息 ¥{ip:,.2f} = ¥{total:,.2f}\n"
                      f"应还日期：{due_date}" +
                      (f"（已逾期 {days} 天，罚息 ¥{penalty:,.2f}）" if days > 0 else "（未逾期）")),
                fg='#2c3e50' if days == 0 else '#e74c3c')
            amount_entry.insert(0, f"{due_total:.2f}")

        for entry in entries.values():
            entry.bind("<KeyRelease>", load_due)

        def do_repayment():
            card = entries["card_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            amount_str = amount_entry.get().strip()
            if not loan_status:
                load_due()
            if not loan_status:
                messagebox.showerror("错误", "请先填写卡号和贷款ID以加载当期应还信息！")
                return
            loan_id = loan_status['loan_id']
            cid = loan_status['cid']
            try:
                pay_amount = float(amount_str)
            except ValueError:
                messagebox.showerror("错误", "还款金额格式不正确！")
                return
            due_total = loan_status['due_total']
            if pay_amount < due_total:
                messagebox.showerror("错误", f"还款金额不得低于当期应还合计 ¥{due_total:,.2f}！")
                return
            self.cursor.execute("SELECT password, balance, is_frozen FROM customers WHERE id = ?", (cid,))
            db_pwd, balance, frozen = self.cursor.fetchone()
            if frozen:
                messagebox.showerror("错误", "账户已被冻结！")
                return
            if pwd != db_pwd:
                messagebox.showerror("错误", "密码错误！")
                return
            if balance < pay_amount:
                messagebox.showerror("错误", f"账户余额不足！当前余额 ¥{balance:,.2f}，需还款 ¥{pay_amount:,.2f}")
                return
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            pp = loan_status['pp']
            ip = loan_status['ip']
            penalty = loan_status['penalty']
            # 1) 偿还当期本息（含逾期罚息）
            self.cursor.execute("UPDATE customers SET balance = balance - ?, last_update_time = ? WHERE id = ?",
                                (due_total, now, cid))
            self.cursor.execute('''
                INSERT INTO loan_payments (loan_id, payment_amount, payment_date, principal_part,
                                           interest_part, penalty, period_no, payment_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, '定期还款')
            ''', (loan_id, due_total, now, pp, ip, penalty, loan_status['pno']))
            self.cursor.execute('''
                UPDATE loan_schedule SET status = 'paid', paid_date = ?, paid_amount = ? WHERE loan_id = ? AND period_no = ?
            ''', (now, due_total, loan_id, loan_status['pno']))
            self.cursor.execute('''
                UPDATE loans SET paid_principal = paid_principal + ?, paid_interest = paid_interest + ?
                WHERE id = ?
            ''', (pp, ip, loan_id))
            self.cursor.execute('''
                INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                VALUES (?, 'BANK', ?, 'loan_repayment', ?)
            ''', (card, due_total, now))
            # 2) 超额部分：自动用于提前偿还本金
            extra = round(pay_amount - due_total, 2)
            extra_msg = ""
            if extra > 0:
                self.cursor.execute("SELECT remaining_amount, interest_rate, repayment_method, term_months FROM loans WHERE id = ?", (loan_id,))
                rem, rate, method, term = self.cursor.fetchone()
                self.cursor.execute("UPDATE loans SET remaining_amount = remaining_amount - ?, paid_principal = paid_principal + ? WHERE id = ?",
                                    (extra, extra, loan_id))
                self.cursor.execute('''
                    INSERT INTO loan_payments (loan_id, payment_amount, payment_date, principal_part,
                                               interest_part, penalty, period_no, payment_type)
                    VALUES (?, ?, ?, ?, 0, 0, 0, '提前部分还款')
                ''', (loan_id, extra, now, extra))
                new_remaining = round(rem - extra, 2)
                if new_remaining <= 1:
                    self.cursor.execute("UPDATE loans SET remaining_amount = 0, status = 'paid' WHERE id = ?", (loan_id,))
                    self.cursor.execute("UPDATE loan_schedule SET status = 'paid' WHERE loan_id = ? AND status != 'paid'", (loan_id,))
                    extra_msg = f"\n本次超额部分已将贷款全部还清！"
                else:
                    self.cursor.execute("SELECT COUNT(*) FROM loan_schedule WHERE loan_id = ? AND status = 'paid'", (loan_id,))
                    paid_periods = self.cursor.fetchone()[0] or 0
                    pending_periods = max(1, term - paid_periods)
                    method = method or '等额本息'
                    first_payment, _, _ = self._generate_loan_schedule(
                        loan_id, new_remaining, rate, pending_periods, method, datetime.now(), keep_paid=True)
                    self.cursor.execute("UPDATE loans SET monthly_payment = ? WHERE id = ?", (first_payment, loan_id))
                    extra_msg = f"\n超额部分 ¥{extra:,.2f} 已用于提前偿还本金，还款计划已重新生成。"
            self.conn.commit()
            self._refresh_loan_overdue(loan_id)
            self.add_operation_log("贷款还款",
                                   f"贷款ID {loan_id} 按期还款 ¥{due_total:,.2f}"
                                   + (f"，其中本金 ¥{pp:,.2f}、利息 ¥{ip:,.2f}、罚息 ¥{penalty:,.2f}" if penalty else ""))
            messagebox.showinfo("还款成功", f"✅ 还款成功！\n\n本期还款：¥ {due_total:,.2f}\n"
                                            f"其中本金：¥ {pp:,.2f}，利息：¥ {ip:,.2f}"
                                            + (f"，罚息：¥ {penalty:,.2f}" if penalty else "")
                                            + extra_msg)
            self.show_repayment()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="💸 确认还款", command=do_repayment,
                  width=15, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 提前还款 ====================

    def show_early_repayment(self):
        self.clear_main_frame()
        self._refresh_all_overdue()

        title_label = tk.Label(self.main_frame, text="🔁 提前还款（部分提前还款 / 提前结清）",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="选择贷款", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=15, padx=100, fill=tk.X)

        tk.Label(form_frame, text="银行卡号：", font=("Arial", 11), bg='white').grid(
            row=0, column=0, padx=10, pady=12, sticky=tk.E)
        card_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=12)

        tk.Label(form_frame, text="取款密码：", font=("Arial", 11), bg='white').grid(
            row=1, column=0, padx=10, pady=12, sticky=tk.E)
        pwd_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show="*")
        pwd_entry.grid(row=1, column=1, padx=10, pady=12)

        tk.Label(form_frame, text="贷款ID：", font=("Arial", 11), bg='white').grid(
            row=2, column=0, padx=10, pady=12, sticky=tk.E)
        loan_id_entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        loan_id_entry.grid(row=2, column=1, padx=10, pady=12)

        summary_label = tk.Label(form_frame, text="", font=("Arial", 10), bg='white', fg='#2c3e50',
                                 justify=tk.LEFT)
        summary_label.grid(row=3, column=0, columnspan=2, pady=8)

        current = {}

        def load_summary():
            card = card_entry.get().strip()
            pwd = pwd_entry.get()
            loan_id_str = loan_id_entry.get().strip()
            current.clear()
            summary_label.config(text="")
            if not all([card, pwd, loan_id_str]):
                messagebox.showerror("错误", "请填写卡号、取款密码和贷款ID！")
                return
            try:
                loan_id = int(loan_id_str)
            except ValueError:
                messagebox.showerror("错误", "贷款ID格式不正确！")
                return
            self.cursor.execute("SELECT id, password, is_frozen FROM customers WHERE card_number = ?", (card,))
            cust = self.cursor.fetchone()
            if not cust:
                messagebox.showerror("错误", "账户不存在！")
                return
            cid, db_pwd, frozen = cust
            if pwd != db_pwd:
                messagebox.showerror("错误", "取款密码错误！")
                return
            if frozen:
                messagebox.showerror("错误", "账户已被冻结！")
                return
            self.cursor.execute("SELECT status FROM loans WHERE id = ? AND customer_id = ?", (loan_id, cid))
            lrow = self.cursor.fetchone()
            if not lrow:
                messagebox.showerror("错误", "未找到该贷款！")
                return
            if lrow[0] != 'active':
                messagebox.showerror("错误", "该贷款已结清！")
                return
            self._refresh_loan_overdue(loan_id)
            loan = self._get_loan_summary(loan_id)
            (lid, contract_no, ltype, amount, rate, term, remaining, monthly, status, start_date,
             next_date, method, purpose, paid_principal, paid_interest, penalty_interest,
             is_overdue, overdue_days, sid, total_interest, card2, name) = loan
            current['loan_id'] = loan_id
            current['password'] = pwd
            overdue_text = f"（逾期 {overdue_days} 天，罚息 ¥{penalty_interest:,.2f}）" if is_overdue else ""
            summary_label.config(text=(
                f"合同编号：{contract_no or lid}    贷款类型：{ltype}    金额：¥{amount:,.2f}\n"
                f"年利率：{rate}%    还款方式：{method or '等额本息'}    期限：{term} 个月\n"
                f"剩余本金：¥{remaining:,.2f}    首期月供：¥{monthly:,.2f}    已还利息：¥{paid_interest:,.2f}\n"
                f"状态：还款中{overdue_text}    下次还款日：{next_date}"))

        btn_frame2 = tk.Frame(form_frame, bg='white')
        btn_frame2.grid(row=4, column=0, columnspan=2, pady=10)
        tk.Button(btn_frame2, text="🔍 查询贷款信息", command=load_summary,
                  width=16, bg='#3498db', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=8)

        def do_partial():
            if not current:
                messagebox.showerror("错误", "请先查询贷款信息！")
                return
            loan_id = current['loan_id']
            pwd = current['password']
            amount_str = simpledialog.askstring("部分提前还款", "请输入提前还款金额（至少 ¥1,000）：", parent=self.root)
            if not amount_str:
                return
            mode = self._ask_prepay_mode(self.root)
            if not mode:
                return
            ok, msg = self._partial_repay(loan_id, amount_str, mode, pwd)
            if ok:
                messagebox.showinfo("提前还款成功", msg)
                load_summary()
            else:
                messagebox.showerror("操作失败", msg)

        def do_settle():
            if not current:
                messagebox.showerror("错误", "请先查询贷款信息！")
                return
            loan_id = current['loan_id']
            pwd = current['password']
            if not messagebox.askyesno("确认结清",
                                       "提前结清将一次性归还剩余本金及未还利息、罚息，并收取违约金（剩余本金×0.5%）。是否继续？"):
                return
            ok, msg = self._settle_loan(loan_id, pwd)
            if ok:
                messagebox.showinfo("结清成功", msg)
                load_summary()
            else:
                messagebox.showerror("操作失败", msg)

        tk.Button(btn_frame2, text="🔁 部分提前还款", command=do_partial,
                  width=16, bg='#f39c12', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame2, text="💯 提前结清", command=do_settle,
                  width=16, bg='#e74c3c', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=8)

    # ==================== 交易记录 ====================

    def show_transaction_history(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📜 交易记录查询",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.LabelFrame(self.main_frame, text="查询条件", font=("Arial", 12, "bold"),
                                    bg='white', padx=20, pady=10)
        query_frame.pack(pady=10, padx=30, fill=tk.X)

        tk.Label(query_frame, text="卡号：", font=("Arial", 10), bg='white').grid(row=0, column=0, padx=5, pady=5)
        card_entry = tk.Entry(query_frame, width=20, font=("Arial", 10))
        card_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(query_frame, text="交易类型：", font=("Arial", 10), bg='white').grid(row=0, column=2, padx=5, pady=5)
        type_combo = ttk.Combobox(query_frame, values=["全部", "存款", "取款", "转账", "贷款放款", "贷款还款"],
                                  width=12, state="readonly")
        type_combo.current(0)
        type_combo.grid(row=0, column=3, padx=5, pady=5)

        list_frame = tk.LabelFrame(self.main_frame, text="交易明细", font=("Arial", 12, "bold"),
                                   bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("交易ID", "交易类型", "付款账户", "收款账户", "金额", "交易时间")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)

        type_map = {
            'deposit': '存款', 'withdraw': '取款', 'transfer': '转账',
            'loan_disbursement': '贷款放款', 'loan_repayment': '贷款还款'
        }

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=130, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        def query_transactions():
            card = card_entry.get().strip()
            tx_type = type_combo.get()

            query = "SELECT id, transaction_type, from_card, to_card, amount, transaction_time " \
                    "FROM transactions WHERE 1=1"
            params = []

            if card:
                query += " AND (from_card = ? OR to_card = ?)"
                params.extend([card, card])

            if tx_type != "全部":
                type_reverse = {v: k for k, v in type_map.items()}
                if tx_type in type_reverse:
                    query += " AND transaction_type = ?"
                    params.append(type_reverse[tx_type])

            query += " ORDER BY transaction_time DESC LIMIT 100"

            self.cursor.execute(query, params)

            for item in tree.get_children():
                tree.delete(item)

            for row in self.cursor.fetchall():
                tid, ttype, from_c, to_c, amount, time = row
                tree.insert("", "end", values=(
                    tid, type_map.get(ttype, ttype), from_c, to_c, f"¥{amount:,.2f}", time
                ))

            self.add_operation_log("查询交易记录", f"查询卡号 {card or '全部'} 的交易记录")

        tk.Button(query_frame, text="🔍 查询", command=query_transactions,
                  width=8, bg='#3498db', fg='white', relief=tk.FLAT).grid(
            row=0, column=4, padx=20)

    # ==================== 账户注销 ====================

    def show_close_account(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="❌ 账户注销",
                               font=("Arial", 18, "bold"), bg='white', fg='#e74c3c')
        title_label.pack(pady=20)

        warn_frame = tk.Frame(self.main_frame, bg='#fdf2f2', bd=2, relief=tk.GROOVE)
        warn_frame.pack(pady=10, padx=100, fill=tk.X)
        tk.Label(warn_frame, text="⚠️ 警告：账户注销后不可恢复，请确保账户余额为0且无未结清贷款！",
                 font=("Arial", 11), bg='#fdf2f2', fg='#e74c3c').pack(pady=15)

        form_frame = tk.LabelFrame(self.main_frame, text="注销确认", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=100, fill=tk.X)

        fields = [
            ("银行卡号", "card_entry"),
            ("账户密码", "pwd_entry"),
            ("注销原因", "reason_entry"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=15, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=35, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=15)
            entries[var] = entry

        def close_account():
            card = entries["card_entry"].get().strip()
            pwd = entries["pwd_entry"].get()
            reason = entries["reason_entry"].get().strip()

            if not all([card, pwd]):
                messagebox.showerror("错误", "请填写卡号和密码！")
                return

            self.cursor.execute("SELECT id, name, password, balance, is_frozen, account_type FROM customers WHERE card_number = ?",
                                (card,))
            customer = self.cursor.fetchone()
            if not customer:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, db_pwd, balance, frozen, acc_type = customer

            if pwd != db_pwd:
                messagebox.showerror("错误", "密码错误！")
                return

            if balance > 0:
                messagebox.showerror("错误", f"账户还有余额 ¥{balance:,.2f}，请先取出再注销！")
                return

            if acc_type == '信用卡' and balance < 0:
                messagebox.showerror("错误", f"信用卡尚有欠款 ¥{-balance:,.2f}，请先存款还清欠款再注销！")
                return

            self.cursor.execute("SELECT COUNT(*) FROM loans WHERE customer_id = ? AND status = 'active'", (cid,))
            if self.cursor.fetchone()[0] > 0:
                messagebox.showerror("错误", "存在未结清贷款，无法注销！")
                return

            if not messagebox.askyesno("确认注销", f"确定要注销 {name} 的账户吗？\n此操作不可恢复！"):
                return

            self.cursor.execute("DELETE FROM customers WHERE id = ?", (cid,))
            self.cursor.execute("DELETE FROM stock_portfolio WHERE customer_id = ?", (cid,))
            self.conn.commit()

            self.add_operation_log("账户注销", f"卡号 {card} 账户注销，原因：{reason}")

            messagebox.showinfo("注销成功", "✅ 账户已成功注销！")
            self.show_close_account()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="❌ 确认注销", command=close_account,
                  width=15, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 强行冻结账户（管理员） ====================

    def show_force_freeze(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🚨 强行冻结账户",
                               font=("Arial", 18, "bold"), bg='white', fg='#e74c3c')
        title_label.pack(pady=20)

        tip_label = tk.Label(self.main_frame, text="管理员特权：输入卡号和管理员密码，可直接冻结/解冻任意账户",
                             font=("Arial", 10), bg='white', fg='#e67e22')
        tip_label.pack(pady=5)

        form_frame = tk.LabelFrame(self.main_frame, text="强行冻结/解锁", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=80, fill=tk.X)

        fields = [
            ("银行卡号", "card_entry"),
            ("管理员密码", "admin_pwd_entry"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=15, sticky=tk.E)
            show = "*" if "pwd" in var else ""
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE, show=show)
            entry.grid(row=i, column=1, padx=10, pady=15, sticky=tk.W)
            entries[var] = entry

        status_frame = tk.Frame(self.main_frame, bg='white')
        status_frame.pack(pady=10)
        status_label = tk.Label(status_frame, text="", font=("Arial", 11), bg='white')
        status_label.pack()

        def check_status():
            card = entries["card_entry"].get().strip()
            if not card:
                status_label.config(text="请输入卡号查询状态", fg='#7f8c8d')
                return

            self.cursor.execute("SELECT name, is_frozen FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                status_label.config(text="❌ 该账户不存在", fg='#e74c3c')
            else:
                name, frozen = result
                status_text = "🔒 已冻结" if frozen else "✅ 正常状态"
                status_color = '#e74c3c' if frozen else '#27ae60'
                status_label.config(text=f"账户：{name} | 当前状态：{status_text}", fg=status_color)

        def force_toggle(action_type):
            card = entries["card_entry"].get().strip()
            admin_pwd = entries["admin_pwd_entry"].get()

            if not card or not admin_pwd:
                messagebox.showerror("错误", "请填写卡号和管理员密码！")
                return

            # 验证管理员密码
            self.cursor.execute("SELECT password FROM users WHERE username = ? AND role = ?",
                                (self.current_user, ROLE_ADMIN))
            admin_result = self.cursor.fetchone()
            if not admin_result or admin_result[0] != admin_pwd:
                messagebox.showerror("错误", "管理员密码验证失败！")
                return

            self.cursor.execute("SELECT id, name, is_frozen FROM customers WHERE card_number = ?", (card,))
            result = self.cursor.fetchone()
            if not result:
                messagebox.showerror("错误", "账户不存在！")
                return

            cid, name, frozen = result

            if action_type == 'freeze' and frozen:
                messagebox.showinfo("提示", "该账户已经是冻结状态！")
                return
            if action_type == 'unfreeze' and not frozen:
                messagebox.showinfo("提示", "该账户已经是正常状态！")
                return

            action = "冻结" if action_type == 'freeze' else "解冻"
            new_status = 1 if action_type == 'freeze' else 0

            if not messagebox.askyesno("确认操作", f"确定要强行{action}账户【{name}】吗？"):
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.cursor.execute("UPDATE customers SET is_frozen = ?, failed_attempts = 0, last_update_time = ? "
                                "WHERE id = ?", (new_status, now, cid))
            self.conn.commit()

            self.add_operation_log(f"强行{action}账户",
                                   f"管理员 {self.current_user} 强行{action}卡号 {card}（账户名：{name}）")

            messagebox.showinfo("操作成功", f"✅ 账户【{name}】已强行{action}！")
            check_status()
            entries["admin_pwd_entry"].delete(0, tk.END)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="🔍 查询状态", command=check_status,
                  width=12, bg='#3498db', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="🔒 强行冻结", command=lambda: force_toggle('freeze'),
                  width=12, bg='#e74c3c', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="🔓 强行解冻", command=lambda: force_toggle('unfreeze'),
                  width=12, bg='#27ae60', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=8)

    # ==================== 大额资金审核（管理员） ====================

    def show_large_amount_review(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="⚖️ 大额资金审核",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        pending_frame = tk.LabelFrame(self.main_frame, text="待审核申请", font=("Arial", 12, "bold"),
                                      bg='white', padx=10, pady=10)
        pending_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("申请ID", "客户ID", "交易类型", "金额", "目标账户", "申请时间", "状态")
        tree = ttk.Treeview(pending_frame, columns=columns, show="headings", height=8)

        type_map = {'deposit': '大额存款', 'withdraw': '大额取款', 'transfer': '大额转账'}

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=110, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(pending_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        def refresh_list():
            self.cursor.execute('''
                SELECT id, customer_id, transaction_type, amount, target_account, apply_time, status
                FROM large_amount_transactions WHERE status = 'pending' ORDER BY apply_time DESC
            ''')
            for item in tree.get_children():
                tree.delete(item)
            for row in self.cursor.fetchall():
                tid, cid, ttype, amount, target, apply_t, status = row
                tree.insert("", "end", values=(
                    tid, cid, type_map.get(ttype, ttype), f"¥{amount:,.2f}",
                    target or '-', apply_t, status
                ))

        refresh_list()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        def review(approve):
            selected = tree.selection()
            if not selected:
                messagebox.showerror("错误", "请选择一条审核记录！")
                return

            item = tree.item(selected[0])
            tx_id = item['values'][0]
            action = "通过" if approve else "拒绝"

            if not messagebox.askyesno("确认", f"确定要{action}该申请吗？"):
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            self.cursor.execute('''
                SELECT customer_id, transaction_type, amount, target_account
                FROM large_amount_transactions WHERE id = ?
            ''', (tx_id,))
            tx = self.cursor.fetchone()
            cid, ttype, amount, target = tx

            if approve:
                self.cursor.execute("SELECT card_number, name, balance FROM customers WHERE id = ?", (cid,))
                cust = self.cursor.fetchone()
                card, name, balance = cust

                if ttype == 'deposit':
                    self.cursor.execute("UPDATE customers SET balance = balance + ? WHERE id = ?", (amount, cid))
                    self.cursor.execute('''
                        INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                        VALUES ('CASH', ?, ?, 'deposit', ?)
                    ''', (card, amount, now))

                elif ttype == 'withdraw':
                    if balance < amount:
                        messagebox.showerror("错误", "余额不足，无法通过！")
                        return
                    self.cursor.execute("UPDATE customers SET balance = balance - ? WHERE id = ?", (amount, cid))
                    self.cursor.execute('''
                        INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                        VALUES (?, 'CASH', ?, 'withdraw', ?)
                    ''', (card, amount, now))

                elif ttype == 'transfer':
                    if balance < amount:
                        messagebox.showerror("错误", "付款方余额不足！")
                        return
                    self.cursor.execute("SELECT id FROM customers WHERE card_number = ?", (target,))
                    target_cust = self.cursor.fetchone()
                    if not target_cust:
                        messagebox.showerror("错误", "收款账户不存在！")
                        return
                    self.cursor.execute("UPDATE customers SET balance = balance - ? WHERE id = ?", (amount, cid))
                    self.cursor.execute("UPDATE customers SET balance = balance + ? WHERE id = ?",
                                        (amount, target_cust[0]))
                    self.cursor.execute('''
                        INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                        VALUES (?, ?, ?, 'transfer', ?)
                    ''', (card, target, amount, now))

                    # 转账通知
                    self.notify_customer(card, "转账支出通知",
                                         f"您的大额转账申请已通过审核，账户 {card} 向 {target} 转出 ¥{amount:,.2f}，交易时间：{now}。")
                    self.notify_customer(target, "转账到账通知",
                                         f"您的账户 {target} 收到 {card} 转入 ¥{amount:,.2f}，交易时间：{now}。")

                if ttype == 'deposit':
                    self.notify_customer(card, "存款到账通知",
                                         f"您的大额存款申请已通过审核，账户 {card} 存入 ¥{amount:,.2f}，交易时间：{now}。")
                elif ttype == 'withdraw':
                    self.notify_customer(card, "取款通知",
                                         f"您的大额取款申请已通过审核，账户 {card} 取出 ¥{amount:,.2f}，交易时间：{now}。")

                status = 'approved'
            else:
                status = 'rejected'

            self.cursor.execute('''
                UPDATE large_amount_transactions SET status = ?, review_time = ?, processed = 1 WHERE id = ?
            ''', (status, now, tx_id))
            self.conn.commit()

            self.add_operation_log("大额审核", f"申请ID {tx_id} 审核{action}")
            messagebox.showinfo("成功", f"✅ 已{action}该申请！")
            refresh_list()

        tk.Button(btn_frame, text="✅ 审核通过", command=lambda: review(True),
                  width=12, bg='#27ae60', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="❌ 审核拒绝", command=lambda: review(False),
                  width=12, bg='#e74c3c', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="🔄 刷新列表", command=refresh_list,
                  width=12, bg='#3498db', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 大额贷款审核（经理） ====================

    def show_loan_approval(self):
        self.clear_main_frame()
        self._refresh_all_overdue()

        title_label = tk.Label(self.main_frame, text="🏦 贷款审批",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        # 统计信息
        stats_frame = tk.Frame(self.main_frame, bg='white')
        stats_frame.pack(fill=tk.X, padx=30, pady=5)

        self.cursor.execute("SELECT COUNT(*) FROM loan_approvals WHERE status='pending'")
        pending_count = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM loan_approvals WHERE status='approved'")
        approved_count = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM loan_approvals WHERE status='rejected'")
        rejected_count = self.cursor.fetchone()[0]

        info_text = f"待审核：{pending_count} 笔  |  已通过：{approved_count} 笔  |  已拒绝：{rejected_count} 笔"
        tk.Label(stats_frame, text=info_text, font=("Arial", 11), bg='white', fg='#7f8c8d').pack()

        # 待审核列表
        pending_frame = tk.LabelFrame(self.main_frame, text="待审核贷款申请", font=("Arial", 12, "bold"),
                                      bg='white', padx=10, pady=10)
        pending_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("申请ID", "客户ID", "贷款类型", "申请金额", "年利率", "还款方式", "期限(月)", "月供", "申请时间", "状态")
        tree = ttk.Treeview(pending_frame, columns=columns, show="headings", height=10)
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=100, anchor=tk.CENTER)
        tree.column("申请金额", width=120)
        tree.column("申请时间", width=150)

        scrollbar = ttk.Scrollbar(pending_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        def refresh_list():
            self.cursor.execute('''
                SELECT la.id, la.customer_id, la.loan_type, la.loan_amount, la.interest_rate,
                       la.repayment_method, la.term_months, la.monthly_payment, la.apply_time, la.status
                FROM loan_approvals la
                ORDER BY 
                    CASE la.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                    la.apply_time DESC
            ''')
            for item in tree.get_children():
                tree.delete(item)
            for row in self.cursor.fetchall():
                lid, cid, ltype, amount, rate, method, term, monthly, apply_t, status = row
                status_map = {'pending': '待审核', 'approved': '已通过', 'rejected': '已拒绝'}
                tree.insert("", "end", values=(
                    lid, cid, ltype, f"¥{amount:,.2f}", f"{rate}%", method or "等额本息", term,
                    f"¥{monthly:,.2f}", apply_t, status_map.get(status, status)
                ))

        refresh_list()

        # 申请详情
        detail_frame = tk.LabelFrame(self.main_frame, text="申请详情", font=("Arial", 11, "bold"),
                                     bg='white', padx=15, pady=10)
        detail_frame.pack(fill=tk.X, padx=30, pady=5)

        detail_text = tk.Label(detail_frame, text="请选择一条申请记录查看详情",
                               font=("Arial", 10), bg='white', fg='#7f8c8d', justify=tk.LEFT)
        detail_text.pack(anchor=tk.W)

        def show_detail(event):
            selected = tree.selection()
            if not selected:
                return
            item = tree.item(selected[0])
            app_id = item['values'][0]

            self.cursor.execute('''
                SELECT la.*, c.name, c.card_number, c.credit_score
                FROM loan_approvals la
                JOIN customers c ON la.customer_id = c.id
                WHERE la.id = ?
            ''', (app_id,))
            app = self.cursor.fetchone()
            if app:
                (lid, cid, ltype, amount, rate, term, monthly, sid, apply_t,
                 status, review_t, reviewer, note, method, purpose, name, card, credit) = app
                active_total = self._get_active_loan_total(cid)
                credit_limit = self._get_credit_loan_limit(credit)
                detail = f"申请人：{name}    卡号：{card}    信用评分：{credit}分（可贷上限 ¥{credit_limit:,.2f}，当前在贷 ¥{active_total:,.2f}）\n"
                detail += f"贷款类型：{ltype}    申请金额：¥{amount:,.2f}    用途：{purpose or '-'}\n"
                detail += f"年利率：{rate}%    期限：{term}个月    还款方式：{method or '等额本息'}    月供：¥{monthly:,.2f}\n"
                detail += f"申请时间：{apply_t}"
                if sid:
                    detail += f"\n关联学号：{sid}"
                if note:
                    detail += f"\n审核备注：{note}"
                detail_text.config(text=detail, fg='#2c3e50')

        tree.bind("<<TreeviewSelect>>", show_detail)

        # 审核按钮
        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        def review_loan(approve):
            selected = tree.selection()
            if not selected:
                messagebox.showerror("错误", "请选择一条审核记录！")
                return

            item = tree.item(selected[0])
            app_id = item['values'][0]
            action = "通过" if approve else "拒绝"

            self.cursor.execute("SELECT status FROM loan_approvals WHERE id = ?", (app_id,))
            current_status = self.cursor.fetchone()[0]
            if current_status != 'pending':
                messagebox.showinfo("提示", "该申请已审核，无法重复操作！")
                return

            note = ""
            if not approve:
                note = simpledialog.askstring("审核备注", "请输入拒绝原因：", parent=self.root)
                if note is None:
                    return

            if not messagebox.askyesno("确认审核", f"确定要{action}该贷款申请吗？"):
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if approve:
                # 获取申请详情
                self.cursor.execute('''
                    SELECT customer_id, loan_type, loan_amount, interest_rate, term_months,
                           monthly_payment, student_id, repayment_method, purpose
                    FROM loan_approvals WHERE id = ?
                ''', (app_id,))
                app = self.cursor.fetchone()
                cid, ltype, amount, rate, term, monthly, sid, method, purpose = app
                method = method or '等额本息'
                purpose = purpose or ''

                # 审批时复核信用评分与信用额度
                self.cursor.execute("SELECT credit_score FROM customers WHERE id = ?", (cid,))
                credit_row = self.cursor.fetchone()
                if credit_row and credit_row[0] < 90:
                    messagebox.showerror("审核拒绝", f"申请人信用评分过低（当前{credit_row[0]}分），低于90分无法发放贷款！")
                    self.cursor.execute('''
                        UPDATE loan_approvals SET status = 'rejected', review_time = ?, reviewer_id = ?, review_note = ?
                        WHERE id = ?
                    ''', (now, self.current_user, f"信用分不足{credit_row[0]}分，低于90分标准", app_id))
                    self.conn.commit()
                    refresh_list()
                    return
                credit_limit = self._get_credit_loan_limit(credit_row[0])
                active_total = self._get_active_loan_total(cid)
                if active_total + amount > credit_limit:
                    messagebox.showerror("审核拒绝",
                                         f"申请人信用额度不足：可贷上限 ¥{credit_limit:,.2f}，"
                                         f"当前在贷 ¥{active_total:,.2f}，申请 ¥{amount:,.2f} 超出额度！")
                    self.cursor.execute('''
                        UPDATE loan_approvals SET status = 'rejected', review_time = ?, reviewer_id = ?, review_note = ?
                        WHERE id = ?
                    ''', (now, self.current_user, f"信用额度不足（上限{credit_limit:.0f}元）", app_id))
                    self.conn.commit()
                    refresh_list()
                    return

                # 创建贷款合同并生成还款计划
                contract_no = self._make_contract_no()
                next_payment = self._add_months(datetime.now(), 1).strftime('%Y-%m-%d')
                self.cursor.execute('''
                    INSERT INTO loans (customer_id, loan_type, loan_amount, interest_rate, term_months,
                                       remaining_amount, monthly_payment, status, start_date,
                                       next_payment_date, student_id, contract_no, repayment_method, purpose, total_interest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                ''', (cid, ltype, amount, rate, term, amount, 0.0, now, next_payment,
                      sid, contract_no, method, purpose, 0.0))
                self.conn.commit()
                loan_id = self.cursor.lastrowid
                monthly_payment, total_interest, _ = self._generate_loan_schedule(
                    loan_id, amount, rate, term, method, datetime.now())
                self.cursor.execute("UPDATE loans SET monthly_payment = ?, total_interest = ? WHERE id = ?",
                                    (monthly_payment, total_interest, loan_id))

                # 放款到账户
                self.cursor.execute("UPDATE customers SET balance = balance + ?, last_update_time = ? WHERE id = ?",
                                    (amount, now, cid))

                # 交易记录
                self.cursor.execute("SELECT card_number FROM customers WHERE id = ?", (cid,))
                card = self.cursor.fetchone()[0]
                self.cursor.execute('''
                    INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                    VALUES ('BANK', ?, ?, 'loan_disbursement', ?)
                ''', (card, amount, now))

                # 发送贷款放款通知
                self.notify_customer(card, "贷款放款通知",
                                     f"您的{ltype}申请已审核通过，合同号 {contract_no}，¥{amount:,.2f}已放款至账户，"
                                     f"{method}，首期月供 ¥{monthly_payment:,.2f}，首次还款日 {next_payment}。")

                status = 'approved'
                self.add_operation_log("大额贷款审核",
                                       f"经理 {self.current_user} 通过贷款申请ID {app_id}，合同号 {contract_no}")
            else:
                status = 'rejected'
                self.add_operation_log("大额贷款审核",
                                       f"经理 {self.current_user} 拒绝贷款申请ID {app_id}，原因：{note or '未说明'}")

            # 更新审核状态
            self.cursor.execute('''
                UPDATE loan_approvals SET status = ?, review_time = ?, reviewer_id = ?, review_note = ?
                WHERE id = ?
            ''', (status, now, self.current_user, note, app_id))
            self.conn.commit()

            messagebox.showinfo("审核完成", f"✅ 已{action}该贷款申请！")
            refresh_list()
            detail_text.config(text="请选择一条申请记录查看详情", fg='#7f8c8d')

        tk.Button(btn_frame, text="✅ 审核通过", command=lambda: review_loan(True),
                  width=14, bg='#27ae60', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="❌ 审核拒绝", command=lambda: review_loan(False),
                  width=14, bg='#e74c3c', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="🔄 刷新列表", command=refresh_list,
                  width=14, bg='#3498db', fg='white', relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=10)
    # ==================== 操作日志（管理员） ====================

    def show_operation_logs(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📝 操作日志查询",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        list_frame = tk.LabelFrame(self.main_frame, text="操作日志（最近100条）", font=("Arial", 12, "bold"),
                                   bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=20)

        columns = ("日志ID", "操作者类型", "操作者ID", "操作类型", "详情", "操作时间", "IP地址")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=18)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=100, anchor=tk.CENTER)

        tree.column("详情", width=250)
        tree.column("操作时间", width=150)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        self.cursor.execute('''
            SELECT id, operator_type, operator_id, operation_type, details, operation_time, ip_address
            FROM operation_logs ORDER BY operation_time DESC LIMIT 100
        ''')
        for row in self.cursor.fetchall():
            tree.insert("", "end", values=row)

    # ==================== 股票市场（管理员） ====================

    def show_stock_market(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📈 股票市场",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=10)

        self.update_stock_prices()

        stock_frame = tk.Frame(self.main_frame, bg='white')
        stock_frame.pack(pady=10, fill=tk.BOTH, expand=True, padx=30)

        columns = ("股票代码", "股票名称", "当前价格", "涨跌幅", "成交量")
        tree = ttk.Treeview(stock_frame, columns=columns, show="headings", height=12)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=150, anchor=tk.CENTER)

        for symbol, data in self.stocks.items():
            change_text = f"+{data['change']}%" if data['change'] >= 0 else f"{data['change']}%"
            tree.insert("", "end", values=(
                symbol, data['name'], f"${data['price']:.2f}", change_text, f"{data['volume']:,}"
            ))

        tree.pack(fill=tk.BOTH, expand=True)

        # 双击股票查看趋势图
        def on_double_click(event):
            selected = tree.selection()
            if selected:
                item = tree.item(selected[0])
                symbol = item['values'][0]
                self.show_stock_trend(symbol)

        tree.bind("<Double-1>", on_double_click)

        tk.Label(self.main_frame, text="💡 提示：双击股票行可查看实时趋势图",
                 font=("Arial", 9), bg='white', fg='#7f8c8d').pack(pady=2)

        action_frame = tk.LabelFrame(self.main_frame, text="股票交易", font=("Arial", 11, "bold"),
                                     bg='white', padx=20, pady=15)
        action_frame.pack(pady=10, padx=30, fill=tk.X)

        tk.Label(action_frame, text="客户卡号：", font=("Arial", 10), bg='white').grid(row=0, column=0, padx=5, pady=5)
        card_entry = tk.Entry(action_frame, width=20)
        card_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(action_frame, text="股票代码：", font=("Arial", 10), bg='white').grid(row=0, column=2, padx=5, pady=5)
        symbol_entry = tk.Entry(action_frame, width=10)
        symbol_entry.grid(row=0, column=3, padx=5, pady=5)

        tk.Label(action_frame, text="数量：", font=("Arial", 10), bg='white').grid(row=0, column=4, padx=5, pady=5)
        qty_entry = tk.Entry(action_frame, width=10)
        qty_entry.grid(row=0, column=5, padx=5, pady=5)

        def buy_stock():
            card = card_entry.get().strip()
            symbol = symbol_entry.get().strip().upper()
            qty_str = qty_entry.get().strip()

            if not all([card, symbol, qty_str]):
                messagebox.showerror("错误", "请填写完整信息！")
                return

            try:
                qty = int(qty_str)
            except ValueError:
                messagebox.showerror("错误", "数量格式不正确！")
                return

            if symbol not in self.stocks:
                messagebox.showerror("错误", "股票不存在！")
                return

            self.cursor.execute("SELECT id, name, balance FROM customers WHERE card_number = ?", (card,))
            cust = self.cursor.fetchone()
            if not cust:
                messagebox.showerror("错误", "客户不存在！")
                return

            cid, name, balance = cust
            price = self.stocks[symbol]['price']
            total = price * qty

            if balance < total:
                messagebox.showerror("错误", "余额不足！")
                return

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            self.cursor.execute("UPDATE customers SET balance = balance - ? WHERE id = ?", (total, cid))
            self.cursor.execute('''
                INSERT INTO stock_portfolio (customer_id, stock_symbol, quantity, purchase_price, purchase_date)
                VALUES (?, ?, ?, ?, ?)
            ''', (cid, symbol, qty, price, now))
            self.cursor.execute('''
                INSERT INTO stock_transactions (customer_id, stock_symbol, transaction_type, quantity, price, total_amount, transaction_time)
                VALUES (?, ?, 'buy', ?, ?, ?, ?)
            ''', (cid, symbol, qty, price, total, now))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("买入股票", f"{name} 买入 {symbol} {qty}股，总价 ¥{total:,.2f}")

            # 发送买入股票通知
            self.notify_customer(card, "股票买入通知",
                                 f"您已买入 {symbol} {qty}股，成交价 ¥{price:.2f}，总金额 ¥{total:,.2f}。")

            messagebox.showinfo("成功", f"✅ 买入成功！\n\n{symbol} {qty}股\n总价：¥{total:,.2f}")

        def sell_stock():
            card = card_entry.get().strip()
            symbol = symbol_entry.get().strip().upper()
            qty_str = qty_entry.get().strip()

            if not all([card, symbol, qty_str]):
                messagebox.showerror("错误", "请填写完整信息！")
                return

            try:
                qty = int(qty_str)
            except ValueError:
                messagebox.showerror("错误", "数量格式不正确！")
                return

            self.cursor.execute("SELECT id, name, balance FROM customers WHERE card_number = ?", (card,))
            cust = self.cursor.fetchone()
            if not cust:
                messagebox.showerror("错误", "客户不存在！")
                return

            cid, name, balance = cust

            self.cursor.execute('''
                SELECT id, quantity, purchase_price FROM stock_portfolio
                WHERE customer_id = ? AND stock_symbol = ?
            ''', (cid, symbol))
            holding = self.cursor.fetchone()
            if not holding or holding[1] < qty:
                messagebox.showerror("错误", "持仓不足！")
                return

            pid, held_qty, avg_price = holding
            price = self.stocks.get(symbol, {}).get('price', avg_price)
            total = price * qty

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            new_qty = held_qty - qty
            if new_qty == 0:
                self.cursor.execute("DELETE FROM stock_portfolio WHERE id = ?", (pid,))
            else:
                self.cursor.execute("UPDATE stock_portfolio SET quantity = ? WHERE id = ?", (new_qty, pid))

            self.cursor.execute("UPDATE customers SET balance = balance + ? WHERE id = ?", (total, cid))
            self.cursor.execute('''
                INSERT INTO stock_transactions (customer_id, stock_symbol, transaction_type, quantity, price, total_amount, transaction_time)
                VALUES (?, ?, 'sell', ?, ?, ?, ?)
            ''', (cid, symbol, qty, price, total, now))
            self.conn.commit()

            self.current_customer_id = cid
            self.add_operation_log("卖出股票", f"{name} 卖出 {symbol} {qty}股，总价 ¥{total:,.2f}")

            # 发送卖出股票通知
            self.notify_customer(card, "股票卖出通知",
                                 f"您已卖出 {symbol} {qty}股，成交价 ¥{price:.2f}，总金额 ¥{total:,.2f}，资金已到账。")

            messagebox.showinfo("成功", f"✅ 卖出成功！\n\n{symbol} {qty}股\n总价：¥{total:,.2f}")

        btn_frame = tk.Frame(action_frame, bg='white')
        btn_frame.grid(row=1, column=0, columnspan=6, pady=10)

        tk.Button(btn_frame, text="📈 买入", command=buy_stock,
                  width=12, bg='#e74c3c', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="📉 卖出", command=sell_stock,
                  width=12, bg='#27ae60', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 股票趋势图（双击查看） ====================

    def show_stock_trend(self, symbol):
        if symbol not in self.stocks:
            messagebox.showerror("错误", "股票不存在！")
            return

        stock = self.stocks[symbol]
        trend_window = tk.Toplevel(self.root)
        trend_window.title(f"📈 {symbol} - {stock['name']} 实时趋势图")
        trend_window.geometry("800x550")
        trend_window.configure(bg='white')
        trend_window.resizable(False, False)

        # 顶部信息栏
        info_frame = tk.Frame(trend_window, bg='#2c3e50', height=60)
        info_frame.pack(fill=tk.X)
        info_frame.pack_propagate(False)

        name_label = tk.Label(info_frame, text=f"{symbol}  {stock['name']}",
                              font=("Arial", 16, "bold"), bg='#2c3e50', fg='white')
        name_label.pack(side=tk.LEFT, padx=20)

        price_label = tk.Label(info_frame, text="", font=("Arial", 18, "bold"), bg='#2c3e50', fg='white')
        price_label.pack(side=tk.LEFT, padx=10)

        change_label = tk.Label(info_frame, text="", font=("Arial", 12), bg='#2c3e50')
        change_label.pack(side=tk.LEFT, padx=10)

        volume_label = tk.Label(info_frame, text="", font=("Arial", 10), bg='#2c3e50', fg='#bdc3c7')
        volume_label.pack(side=tk.RIGHT, padx=20)

        # 趋势图画布
        canvas_width = 760
        canvas_height = 380
        canvas = tk.Canvas(trend_window, width=canvas_width, height=canvas_height, bg='white', highlightthickness=0)
        canvas.pack(pady=10)

        # 底部状态栏
        status_frame = tk.Frame(trend_window, bg='#ecf0f1', height=30)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        status_frame.pack_propagate(False)
        status_label = tk.Label(status_frame, text="🔄 行情自动更新中（每2秒刷新）",
                                font=("Arial", 9), bg='#ecf0f1', fg='#7f8c8d')
        status_label.pack(side=tk.LEFT, padx=10)

        update_job = [None]

        def draw_trend():
            if symbol not in self.stocks:
                return
            stock = self.stocks[symbol]
            history = stock['history']
            if len(history) < 2:
                return

            canvas.delete("all")

            # 边距
            margin_left = 60
            margin_right = 20
            margin_top = 20
            margin_bottom = 40
            plot_width = canvas_width - margin_left - margin_right
            plot_height = canvas_height - margin_top - margin_bottom

            # 计算价格范围
            min_price = min(history)
            max_price = max(history)
            price_range = max_price - min_price
            if price_range == 0:
                price_range = max_price * 0.01
            padding = price_range * 0.1
            min_price -= padding
            max_price += padding
            price_range = max_price - min_price

            # 绘制网格线和Y轴刻度
            for i in range(5):
                y = margin_top + plot_height * i / 4
                price = max_price - price_range * i / 4
                canvas.create_line(margin_left, y, canvas_width - margin_right, y,
                                   fill='#ecf0f1', dash=(2, 2))
                canvas.create_text(margin_left - 8, y, text=f"${price:.2f}",
                                   font=("Arial", 8), fill='#7f8c8d', anchor=tk.E)

            # 绘制X轴
            canvas.create_line(margin_left, margin_top + plot_height,
                               canvas_width - margin_right, margin_top + plot_height,
                               fill='#bdc3c7', width=1)
            # 绘制Y轴
            canvas.create_line(margin_left, margin_top, margin_left, margin_top + plot_height,
                               fill='#bdc3c7', width=1)

            # 计算数据点坐标
            n = len(history)
            points = []
            for i, price in enumerate(history):
                x = margin_left + plot_width * i / (n - 1) if n > 1 else margin_left
                y = margin_top + plot_height * (1 - (price - min_price) / price_range)
                points.append((x, y))

            # 绘制填充区域（渐变效果用纯色模拟）
            if len(points) >= 2:
                # 填充区域
                fill_points = [points[0][0], margin_top + plot_height]
                for px, py in points:
                    fill_points.extend([px, py])
                fill_points.extend([points[-1][0], margin_top + plot_height])

                # 根据涨跌决定颜色
                first_price = history[0]
                last_price = history[-1]
                is_up = last_price >= first_price
                fill_color = '#ffebee' if not is_up else '#e8f5e9'
                line_color = '#e74c3c' if not is_up else '#27ae60'

                canvas.create_polygon(fill_points, fill=fill_color, outline='')

                # 绘制趋势线
                for i in range(len(points) - 1):
                    x1, y1 = points[i]
                    x2, y2 = points[i + 1]
                    canvas.create_line(x1, y1, x2, y2, fill=line_color, width=2)

                # 绘制最新价格点（高亮）
                last_x, last_y = points[-1]
                canvas.create_oval(last_x - 5, last_y - 5, last_x + 5, last_y + 5,
                                   fill=line_color, outline='white', width=2)
                # 最新价格标签
                canvas.create_text(last_x, last_y - 12, text=f"${last_price:.2f}",
                                   font=("Arial", 10, "bold"), fill=line_color)

            # X轴标签
            canvas.create_text(margin_left, margin_top + plot_height + 20,
                               text="最早", font=("Arial", 8), fill='#7f8c8d', anchor=tk.W)
            canvas.create_text(canvas_width - margin_right, margin_top + plot_height + 20,
                               text="最新", font=("Arial", 8), fill='#7f8c8d', anchor=tk.E)
            canvas.create_text(canvas_width / 2, margin_top + plot_height + 20,
                               text=f"共 {n} 个数据点", font=("Arial", 8), fill='#7f8c8d')

            # 更新顶部信息
            current_price = stock['price']
            change = stock['change']
            price_label.config(text=f"${current_price:.2f}")
            change_text = f"+{change}%" if change >= 0 else f"{change}%"
            change_color = '#2ecc71' if change >= 0 else '#e74c3c'
            change_label.config(text=change_text, fg=change_color)
            volume_label.config(text=f"成交量: {stock['volume']:,}")

        def auto_update():
            if symbol not in self.stocks:
                return
            # 更新该股票价格
            change_pct = random.uniform(-5, 5)
            old_price = self.stocks[symbol]['price']
            new_price = old_price * (1 + change_pct / 100)
            self.stocks[symbol]['price'] = round(new_price, 2)
            self.stocks[symbol]['change'] = round(change_pct, 2)
            self.stocks[symbol]['volume'] = random.randint(100000, 2000000)
            self.stocks[symbol]['history'].append(round(new_price, 2))
            if len(self.stocks[symbol]['history']) > 100:
                self.stocks[symbol]['history'] = self.stocks[symbol]['history'][-100:]
            # 同步写入数据库
            try:
                self.cursor.execute('''
                    UPDATE stocks SET price = ?, change = ?, volume = ? WHERE symbol = ?
                ''', (round(new_price, 2), round(change_pct, 2), self.stocks[symbol]['volume'], symbol))
                self.conn.commit()
            except Exception:
                pass

            draw_trend()
            update_job[0] = trend_window.after(2000, auto_update)

        def on_close():
            if update_job[0]:
                trend_window.after_cancel(update_job[0])
            trend_window.destroy()

        trend_window.protocol("WM_DELETE_WINDOW", on_close)

        # 初始绘制并启动自动更新
        draw_trend()
        update_job[0] = trend_window.after(2000, auto_update)

    # ==================== 我的股票（管理员） ====================

    def show_my_stocks(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📊 股票持仓查询",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        query_frame = tk.Frame(self.main_frame, bg='white')
        query_frame.pack(pady=10)

        tk.Label(query_frame, text="客户卡号：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        card_entry = tk.Entry(query_frame, width=25, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        card_entry.pack(side=tk.LEFT, padx=5)

        list_frame = tk.LabelFrame(self.main_frame, text="持仓明细", font=("Arial", 12, "bold"),
                                   bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=20)

        columns = ("股票代码", "股票名称", "持仓数量", "买入均价", "现价", "市值", "盈亏")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=100, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        def query_stocks():
            card = card_entry.get().strip()
            if not card:
                messagebox.showerror("错误", "请输入卡号！")
                return

            self.cursor.execute("SELECT id, name FROM customers WHERE card_number = ?", (card,))
            cust = self.cursor.fetchone()
            if not cust:
                messagebox.showerror("错误", "客户不存在！")
                return

            cid, name = cust
            self.current_customer_id = cid

            self.cursor.execute('''
                SELECT stock_symbol, quantity, purchase_price FROM stock_portfolio
                WHERE customer_id = ?
            ''', (cid,))

            for item in tree.get_children():
                tree.delete(item)

            holdings = self.cursor.fetchall()
            if not holdings:
                messagebox.showinfo("提示", "该客户暂无股票持仓")
                return

            for symbol, qty, avg_price in holdings:
                stock_info = self.stocks.get(symbol, {})
                stock_name = stock_info.get('name', symbol)
                current_price = stock_info.get('price', avg_price)
                market_value = current_price * qty
                profit = (current_price - avg_price) * qty
                profit_pct = (profit / (avg_price * qty)) * 100 if avg_price > 0 else 0

                profit_text = f"+¥{profit:,.2f} (+{profit_pct:.2f}%)" if profit >= 0 else f"¥{profit:,.2f} ({profit_pct:.2f}%)"

                tree.insert("", "end", values=(
                    symbol, stock_name, f"{qty}股", f"¥{avg_price:.2f}",
                    f"¥{current_price:.2f}", f"¥{market_value:,.2f}", profit_text
                ))

            self.add_operation_log("查询股票持仓", f"查询客户 {name} 的股票持仓")

        tk.Button(query_frame, text="查询", command=query_stocks,
                  width=8, bg='#3498db', fg='white', relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 添加股票（管理员） ====================

    def show_add_stock(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="➕ 添加股票",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        form_frame = tk.LabelFrame(self.main_frame, text="新增股票信息", font=("Arial", 12, "bold"),
                                   bg='white', padx=30, pady=20)
        form_frame.pack(pady=20, padx=150, fill=tk.X)

        fields = [
            ("股票代码", "symbol_entry"),
            ("股票名称", "name_entry"),
            ("初始价格", "price_entry"),
        ]

        entries = {}
        for i, (label, var) in enumerate(fields):
            tk.Label(form_frame, text=f"{label}：", font=("Arial", 11), bg='white').grid(
                row=i, column=0, padx=10, pady=15, sticky=tk.E)
            entry = tk.Entry(form_frame, width=30, font=("Arial", 11), bd=2, relief=tk.GROOVE)
            entry.grid(row=i, column=1, padx=10, pady=15)
            entries[var] = entry

        def add_stock():
            symbol = entries["symbol_entry"].get().strip().upper()
            name = entries["name_entry"].get().strip()
            price_str = entries["price_entry"].get().strip()

            if not all([symbol, name, price_str]):
                messagebox.showerror("错误", "请填写完整信息！")
                return

            try:
                price = float(price_str)
            except ValueError:
                messagebox.showerror("错误", "价格格式不正确！")
                return

            if symbol in self.stocks:
                messagebox.showerror("错误", "该股票代码已存在！")
                return

            self.stocks[symbol] = {
                'name': name,
                'price': price,
                'change': 0.0,
                'volume': random.randint(100000, 1000000),
                'history': []
            }
            # 为新股票生成初始历史价格数据
            base_price = price
            history = []
            p = base_price * 0.9
            for _ in range(30):
                p = p * (1 + random.uniform(-0.03, 0.03))
                history.append(round(p, 2))
            history.append(base_price)
            self.stocks[symbol]['history'] = history
            # 写入数据库
            self.cursor.execute('''
                INSERT OR REPLACE INTO stocks (symbol, name, price, change, volume)
                VALUES (?, ?, ?, ?, ?)
            ''', (symbol, name, price, 0.0, self.stocks[symbol]['volume']))
            self.conn.commit()

            self.add_operation_log("添加股票", f"新增股票 {symbol} - {name}")
            messagebox.showinfo("成功", f"✅ 股票 {symbol} {name} 添加成功！\n即将跳转到股票市场查看。")
            self.show_stock_market()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="➕ 添加股票", command=add_stock,
                  width=15, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 下架股票（管理员） ====================

    def show_remove_stock(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="🗑️ 下架股票",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        list_frame = tk.LabelFrame(self.main_frame, text="当前上市股票列表（选中后点击下架）",
                                   font=("Arial", 12, "bold"), bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=50, pady=10)

        columns = ("股票代码", "股票名称", "当前价格", "涨跌幅", "成交量")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=150, anchor=tk.CENTER)

        for symbol, data in self.stocks.items():
            change_text = f"+{data['change']}%" if data['change'] >= 0 else f"{data['change']}%"
            tree.insert("", "end", values=(
                symbol, data['name'], f"${data['price']:.2f}", change_text, f"{data['volume']:,}"
            ))

        tree.pack(fill=tk.BOTH, expand=True)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        def do_remove():
            selected = tree.selection()
            if not selected:
                messagebox.showerror("错误", "请先选择要下架的股票！")
                return

            item = tree.item(selected[0])
            symbol = item['values'][0]
            name = item['values'][1]

            if not messagebox.askyesno("确认下架", f"确定要下架股票 {symbol} - {name} 吗？\n\n下架后该股票将从市场中移除，用户将无法再交易。"):
                return

            if symbol in self.stocks:
                del self.stocks[symbol]
                # 从数据库删除
                self.cursor.execute("DELETE FROM stocks WHERE symbol = ?", (symbol,))
                self.conn.commit()
                self.add_operation_log("下架股票", f"下架股票 {symbol} - {name}")
                messagebox.showinfo("成功", f"✅ 股票 {symbol} {name} 已成功下架！")
                self.show_stock_market()

        tk.Button(btn_frame, text="🗑️ 下架选中股票", command=do_remove,
                  width=18, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="🔄 刷新列表", command=self.show_remove_stock,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 修改股价（管理员） ====================

    def show_edit_stock_price(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="✏️ 修改股价",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        list_frame = tk.LabelFrame(self.main_frame, text="当前上市股票列表（选中后修改价格）",
                                   font=("Arial", 12, "bold"), bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=50, pady=10)

        columns = ("股票代码", "股票名称", "当前价格", "涨跌幅", "成交量")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=150, anchor=tk.CENTER)

        for symbol, data in self.stocks.items():
            change_text = f"+{data['change']}%" if data['change'] >= 0 else f"{data['change']}%"
            tree.insert("", "end", values=(
                symbol, data['name'], f"${data['price']:.2f}", change_text, f"{data['volume']:,}"
            ))

        tree.pack(fill=tk.BOTH, expand=True)

        edit_frame = tk.LabelFrame(self.main_frame, text="修改股价", font=("Arial", 12, "bold"),
                                   bg='white', padx=20, pady=15)
        edit_frame.pack(pady=10, padx=150, fill=tk.X)

        tk.Label(edit_frame, text="股票代码：", font=("Arial", 11), bg='white').grid(row=0, column=0, padx=10, pady=10, sticky=tk.E)
        symbol_label = tk.Label(edit_frame, text="（请在上方列表选择）", font=("Arial", 11), bg='white', fg='#999')
        symbol_label.grid(row=0, column=1, padx=10, pady=10, sticky=tk.W)

        tk.Label(edit_frame, text="股票名称：", font=("Arial", 11), bg='white').grid(row=1, column=0, padx=10, pady=10, sticky=tk.E)
        name_label = tk.Label(edit_frame, text="-", font=("Arial", 11), bg='white')
        name_label.grid(row=1, column=1, padx=10, pady=10, sticky=tk.W)

        tk.Label(edit_frame, text="当前价格：", font=("Arial", 11), bg='white').grid(row=2, column=0, padx=10, pady=10, sticky=tk.E)
        current_price_label = tk.Label(edit_frame, text="-", font=("Arial", 11), bg='white')
        current_price_label.grid(row=2, column=1, padx=10, pady=10, sticky=tk.W)

        tk.Label(edit_frame, text="新价格：", font=("Arial", 11), bg='white').grid(row=3, column=0, padx=10, pady=10, sticky=tk.E)
        new_price_entry = tk.Entry(edit_frame, width=20, font=("Arial", 11), bd=2, relief=tk.GROOVE)
        new_price_entry.grid(row=3, column=1, padx=10, pady=10, sticky=tk.W)

        selected_symbol = [None]

        def on_select(event):
            selected = tree.selection()
            if selected:
                item = tree.item(selected[0])
                symbol = item['values'][0]
                selected_symbol[0] = symbol
                symbol_label.config(text=symbol, fg='#2c3e50')
                name_label.config(text=item['values'][1])
                current_price_label.config(text=item['values'][2])
                new_price_entry.delete(0, tk.END)
                new_price_entry.insert(0, str(self.stocks[symbol]['price']))

        tree.bind("<<TreeviewSelect>>", on_select)

        def do_edit_price():
            symbol = selected_symbol[0]
            if not symbol:
                messagebox.showerror("错误", "请先在列表中选择要修改的股票！")
                return

            new_price_str = new_price_entry.get().strip()
            if not new_price_str:
                messagebox.showerror("错误", "请输入新价格！")
                return

            try:
                new_price = float(new_price_str)
            except ValueError:
                messagebox.showerror("错误", "价格格式不正确！")
                return

            if new_price <= 0:
                messagebox.showerror("错误", "价格必须大于0！")
                return

            old_price = self.stocks[symbol]['price']
            change_pct = round((new_price - old_price) / old_price * 100, 2)
            self.stocks[symbol]['price'] = round(new_price, 2)
            self.stocks[symbol]['change'] = change_pct
            self.stocks[symbol]['history'].append(round(new_price, 2))
            if len(self.stocks[symbol]['history']) > 100:
                self.stocks[symbol]['history'] = self.stocks[symbol]['history'][-100:]
            # 同步更新数据库
            self.cursor.execute('''
                UPDATE stocks SET price = ?, change = ? WHERE symbol = ?
            ''', (round(new_price, 2), change_pct, symbol))
            self.conn.commit()

            self.add_operation_log("修改股价", f"修改 {symbol} 股价：${old_price:.2f} → ${new_price:.2f}")
            messagebox.showinfo("成功", f"✅ 股价修改成功！\n\n{symbol} {self.stocks[symbol]['name']}\n旧价格：${old_price:.2f}\n新价格：${new_price:.2f}\n涨跌幅：{change_pct:+.2f}%")
            self.show_stock_market()

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        tk.Button(btn_frame, text="✏️ 确认修改", command=do_edit_price,
                  width=15, height=2, bg='#f39c12', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="🔄 刷新列表", command=self.show_edit_stock_price,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 导出客户信息（管理员） ====================

    def show_export_customers(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="📤 导出客户信息",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        info_frame = tk.LabelFrame(self.main_frame, text="导出说明", font=("Arial", 12, "bold"),
                                   bg='white', padx=20, pady=20)
        info_frame.pack(pady=20, padx=100, fill=tk.X)

        tk.Label(info_frame, text="将导出所有客户的基本信息为Excel文件（.xlsx格式）",
                 font=("Arial", 11), bg='white').pack(pady=10)

        self.cursor.execute("SELECT COUNT(*) FROM customers")
        count = self.cursor.fetchone()[0]
        tk.Label(info_frame, text=f"当前客户总数：{count} 人",
                 font=("Arial", 11, "bold"), bg='white', fg='#3498db').pack(pady=5)

        def do_export():
            if not HAS_PANDAS:
                messagebox.showerror("错误", "缺少pandas库，无法导出Excel！\n请先安装：pip install pandas openpyxl")
                return

            filepath = filedialog.asksaveasfilename(
                defaultextension=".xlsx",
                filetypes=[("Excel文件", "*.xlsx"), ("CSV文件", "*.csv")],
                initialfile=f"客户信息导出_{datetime.now().strftime('%Y%m%d')}.xlsx"
            )

            if not filepath:
                return

            try:
                self.cursor.execute('''
                    SELECT card_number as 卡号, name as 姓名, id_number as 身份证号,
                           account_type as 账户类型, balance as 余额, credit_score as 信用评分,
                           phone as 手机号, email as 邮箱, address as 地址,
                           CASE WHEN is_frozen=1 THEN '冻结' ELSE '正常' END as 状态,
                           create_time as 开户时间
                    FROM customers ORDER BY create_time DESC
                ''')
                columns = [desc[0] for desc in self.cursor.description]
                data = self.cursor.fetchall()

                df = pd.DataFrame(data, columns=columns)
                df.to_excel(filepath, index=False, engine='openpyxl')

                self.add_operation_log("导出客户信息", f"导出 {len(data)} 条客户信息到 {filepath}")
                messagebox.showinfo("成功", f"✅ 导出成功！\n共导出 {len(data)} 条记录\n文件：{filepath}")

            except Exception as e:
                messagebox.showerror("错误", f"导出失败：{str(e)}")

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="📤 开始导出", command=do_export,
                  width=15, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack()

    # ==================== 逾期信用分处理（管理员） ====================

    def init_overdue_table(self):
        """初始化逾期处理日志表"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS overdue_credit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                next_payment_date TEXT NOT NULL,
                deducted_score INTEGER DEFAULT 10,
                process_time TEXT NOT NULL,
                FOREIGN KEY (loan_id) REFERENCES loans (id),
                FOREIGN KEY (customer_id) REFERENCES customers (id)
            )
        ''')
        self.conn.commit()

    def show_overdue_credit_score(self):
        self.clear_main_frame()
        self.init_overdue_table()

        title_label = tk.Label(self.main_frame, text="⚠️ 逾期信用分处理",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        # 统计信息
        stats_frame = tk.Frame(self.main_frame, bg='white')
        stats_frame.pack(fill=tk.X, padx=30, pady=10)

        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')

        # 查询所有已逾期的active贷款（过了下次还款日即算逾期）
        self.cursor.execute('''
            SELECT l.id, l.customer_id, c.name, c.card_number, c.credit_score,
                   l.loan_type, l.remaining_amount, l.next_payment_date, l.monthly_payment
            FROM loans l
            JOIN customers c ON l.customer_id = c.id
            WHERE l.status = 'active' AND l.next_payment_date < ?
            ORDER BY l.next_payment_date ASC
        ''', (today_str,))
        all_overdue = self.cursor.fetchall()

        # 筛选出所有已逾期的（过了还款日就算）
        overdue_list = []
        for row in all_overdue:
            try:
                due_date = datetime.strptime(row[7], '%Y-%m-%d')
                days_overdue = (now - due_date).days
                if days_overdue > 0:
                    overdue_list.append(row + (days_overdue,))
            except Exception:
                continue

        # 查询已处理的
        self.cursor.execute('''
            SELECT COUNT(DISTINCT loan_id) FROM overdue_credit_logs
        ''')
        processed_count = self.cursor.fetchone()[0]

        # 统计待处理的（已逾期且未针对该next_payment_date处理过）
        pending = []
        for row in overdue_list:
            loan_id = row[0]
            next_pay_date = row[7]
            self.cursor.execute('''
                SELECT id FROM overdue_credit_logs WHERE loan_id = ? AND next_payment_date = ?
            ''', (loan_id, next_pay_date))
            if not self.cursor.fetchone():
                pending.append(row)

        stat_labels = [
            ("逾期贷款总数", str(len(overdue_list)), "#e67e22", "⏰"),
            ("已逾期", str(len(overdue_list)), "#e74c3c", "🚨"),
            ("待处理扣分", str(len(pending)), "#9b59b6", "📋"),
            ("历史处理记录", str(processed_count), "#27ae60", "✅"),
        ]

        for i, (title, value, color, icon) in enumerate(stat_labels):
            card = tk.Frame(stats_frame, relief=tk.RAISED, bd=2, bg='white', width=180, height=90)
            card.grid(row=0, column=i, padx=10, pady=10)
            card.grid_propagate(False)
            tk.Label(card, text=icon, font=("Arial", 20), bg='white').pack(pady=3)
            tk.Label(card, text=title, font=("Arial", 9), bg='white', fg='#7f8c8d').pack()
            tk.Label(card, text=value, font=("Arial", 14, "bold"), bg='white', fg=color).pack()

        # 逾期用户列表
        list_frame = tk.LabelFrame(self.main_frame, text="已逾期贷款用户列表（过了还款日即显示）",
                                   font=("Arial", 12, "bold"), bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("贷款ID", "客户姓名", "卡号", "信用分", "贷款类型", "剩余本金", "月供", "应还款日", "逾期天数", "处理状态")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=90, anchor=tk.CENTER)
        tree.column("客户姓名", width=80)
        tree.column("卡号", width=130)
        tree.column("贷款类型", width=90)
        tree.column("剩余本金", width=100)
        tree.column("处理状态", width=80)

        for row in overdue_list:
            loan_id, cust_id, name, card, credit, loan_type, remaining, monthly, next_pay, days_overdue = row
            try:
                remaining = float(remaining)
            except (ValueError, TypeError):
                remaining = 0.0
            try:
                monthly = float(monthly)
            except (ValueError, TypeError):
                monthly = 0.0
            self.cursor.execute('''
                SELECT id FROM overdue_credit_logs WHERE loan_id = ? AND next_payment_date = ?
            ''', (loan_id, next_pay))
            is_processed = "已处理" if self.cursor.fetchone() else "待处理"
            tree.insert("", "end", values=(
                loan_id, name, card, credit, loan_type,
                f"¥{remaining:,.2f}", f"¥{monthly:,.2f}", next_pay,
                f"{days_overdue}天", is_processed
            ))

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        # 操作按钮
        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        def do_process_all():
            if not pending:
                messagebox.showinfo("提示", "没有需要处理的逾期记录！")
                return

            if not messagebox.askyesno("确认", f"将对 {len(pending)} 名已逾期用户进行信用分-10处理，是否继续？"):
                return

            success_count = 0
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for row in pending:
                loan_id = row[0]
                cust_id = row[1]
                next_pay = row[7]
                try:
                    self.cursor.execute("UPDATE customers SET credit_score = credit_score - 10 WHERE id = ?", (cust_id,))
                    self.cursor.execute('''
                        INSERT INTO overdue_credit_logs (loan_id, customer_id, next_payment_date, deducted_score, process_time)
                        VALUES (?, ?, ?, 10, ?)
                    ''', (loan_id, cust_id, next_pay, now_str))
                    success_count += 1
                except Exception as e:
                    print(f"处理贷款ID {loan_id} 时出错: {e}")

            self.conn.commit()
            self.add_operation_log("逾期信用分处理", f"批量处理 {success_count} 条逾期记录，每人信用分-10")
            messagebox.showinfo("处理完成", f"✅ 处理完成！\n\n共处理 {success_count} 名逾期用户，每人信用分已扣除10分。")
            self.show_overdue_credit_score()

        tk.Button(btn_frame, text="⚠️ 批量处理逾期扣分（-10分）", command=do_process_all,
                  width=25, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="🔄 刷新列表", command=self.show_overdue_credit_score,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 起诉用户（管理员） ====================

    def show_sue_user(self):
        self.clear_main_frame()

        title_label = tk.Label(self.main_frame, text="⚖️ 起诉用户 - 贷款纠纷诉讼",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        info_frame = tk.LabelFrame(self.main_frame, text="操作说明", font=("Arial", 12, "bold"),
                                   bg='white', padx=20, pady=10)
        info_frame.pack(fill=tk.X, padx=30, pady=5)
        tk.Label(info_frame, text="选择下方有贷款的用户，双击或选中后点击\"起诉该用户\"按钮，填写诉讼信息后将提交至法院系统。",
                 font=("Arial", 10), bg='white', fg='#555').pack(anchor=tk.W)

        # 贷款用户列表
        list_frame = tk.LabelFrame(self.main_frame, text="贷款用户列表（选中后可起诉）",
                                   font=("Arial", 12, "bold"), bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("贷款ID", "客户姓名", "身份证号", "卡号", "手机号", "贷款类型", "贷款金额", "剩余本金", "贷款状态", "应还款日")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=90, anchor=tk.CENTER)
        tree.column("客户姓名", width=80)
        tree.column("身份证号", width=140)
        tree.column("卡号", width=140)
        tree.column("手机号", width=100)
        tree.column("贷款类型", width=90)
        tree.column("贷款金额", width=100)
        tree.column("剩余本金", width=100)
        tree.column("贷款状态", width=70)

        self.cursor.execute('''
            SELECT l.id, c.name, c.id_number, c.card_number, c.phone,
                   l.loan_type, l.loan_amount, l.remaining_amount, l.status, l.next_payment_date
            FROM loans l
            JOIN customers c ON l.customer_id = c.id
            ORDER BY l.status ASC, l.next_payment_date ASC
        ''')
        for row in self.cursor.fetchall():
            status_text = "还款中" if row[8] == 'active' else "已结清"
            tree.insert("", "end", values=(
                row[0], row[1], row[2], row[3], row[4], row[5],
                f"¥{row[6]:,.2f}", f"¥{row[7]:,.2f}", status_text, row[9]
            ))

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        def open_sue_dialog():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("提示", "请先选择要起诉的用户！")
                return
            item = tree.item(selected[0])
            values = item['values']
            loan_id = values[0]
            customer_name = values[1]
            id_number = values[2]
            card_number = values[3]
            phone = values[4]
            loan_type = values[5]
            loan_amount = values[6]
            remaining = values[7]

            # 获取客户详细信息
            self.cursor.execute('''
                SELECT id, name, id_number, phone, address, email FROM customers WHERE card_number = ?
            ''', (card_number,))
            cust = self.cursor.fetchone()
            if not cust:
                messagebox.showerror("错误", "未找到该客户信息！")
                return
            cust_id, c_name, c_id, c_phone, c_address, c_email = cust

            self.cursor.execute('''
                SELECT loan_amount, remaining_amount, interest_rate, term_months, start_date, next_payment_date
                FROM loans WHERE id = ?
            ''', (loan_id,))
            loan = self.cursor.fetchone()
            if not loan:
                messagebox.showerror("错误", "未找到该贷款信息！")
                return
            l_amount, l_remaining, l_rate, l_term, l_start, l_next = loan

            # 创建诉讼信息填写弹窗
            dialog = tk.Toplevel(self.root)
            dialog.title(f"起诉用户 - {customer_name}")
            dialog.geometry("650x720")
            dialog.configure(bg='white')
            dialog.transient(self.root)
            dialog.grab_set()

            tk.Label(dialog, text="📝 诉讼信息填写", font=("Arial", 16, "bold"),
                     bg='white', fg='#2c3e50').pack(pady=15)

            # 被告信息（自动填充，只读）
            defendant_frame = tk.LabelFrame(dialog, text="被告信息（自动填充）",
                                            font=("Arial", 11, "bold"), bg='white', padx=15, pady=10)
            defendant_frame.pack(fill=tk.X, padx=20, pady=5)

            info_grid = [
                ("被告姓名", c_name),
                ("身份证号", c_id),
                ("联系电话", c_phone or "未知"),
                ("住址", c_address or "未知"),
                ("贷款ID", str(loan_id)),
                ("贷款类型", loan_type),
                ("贷款金额", f"¥{l_amount:,.2f}"),
                ("剩余未还", f"¥{l_remaining:,.2f}"),
                ("贷款利率", f"{l_rate}%"),
                ("贷款期限", f"{l_term}个月"),
                ("放款日期", l_start),
                ("应还款日", l_next),
            ]
            for i, (label, val) in enumerate(info_grid):
                r, c = i // 2, (i % 2) * 2
                tk.Label(defendant_frame, text=f"{label}：", font=("Arial", 9),
                         bg='white', fg='#666').grid(row=r, column=c, sticky=tk.E, padx=3, pady=3)
                tk.Label(defendant_frame, text=str(val), font=("Arial", 9, "bold"),
                         bg='white', fg='#333').grid(row=r, column=c+1, sticky=tk.W, padx=3, pady=3)

            # 诉讼信息填写
            sue_frame = tk.LabelFrame(dialog, text="诉讼信息（请填写）",
                                      font=("Arial", 11, "bold"), bg='white', padx=15, pady=10)
            sue_frame.pack(fill=tk.X, padx=20, pady=10)

            fields = {}

            def add_field(row, label, default="", width=40):
                tk.Label(sue_frame, text=f"{label}：", font=("Arial", 10),
                         bg='white').grid(row=row, column=0, sticky=tk.E, padx=5, pady=8)
                entry = tk.Entry(sue_frame, width=width, font=("Arial", 10), bd=2, relief=tk.GROOVE)
                entry.insert(0, default)
                entry.grid(row=row, column=1, sticky=tk.W, padx=5, pady=8)
                fields[label] = entry
                return entry

            add_field(0, "原告名称", "蛋国银行")
            add_field(1, "原告联系方式", "400-888-8888")
            add_field(2, "案件类型", "金融借款合同纠纷")

            tk.Label(sue_frame, text="诉讼请求：", font=("Arial", 10),
                     bg='white').grid(row=3, column=0, sticky=tk.NE, padx=5, pady=8)
            claim_text = tk.Text(sue_frame, width=40, height=3, font=("Arial", 10),
                                 bd=2, relief=tk.GROOVE)
            claim_text.insert("1.0", f"1. 判令被告立即偿还贷款本金人民币 {l_remaining:,.2f} 元；\n2. 判令被告支付逾期利息及违约金；\n3. 本案诉讼费用由被告承担。")
            claim_text.grid(row=3, column=1, sticky=tk.W, padx=5, pady=8)
            fields["诉讼请求"] = claim_text

            tk.Label(sue_frame, text="事实与理由：", font=("Arial", 10),
                     bg='white').grid(row=4, column=0, sticky=tk.NE, padx=5, pady=8)
            fact_text = tk.Text(sue_frame, width=40, height=4, font=("Arial", 10),
                                bd=2, relief=tk.GROOVE)
            fact_text.insert("1.0", f"被告 {c_name}（身份证号：{c_id}）于 {l_start} 向原告申请{loan_type}，"
                                    f"贷款金额人民币 {l_amount:,.2f} 元，期限 {l_term} 个月，年利率 {l_rate}%。"
                                    f"截至起诉之日，被告尚欠贷款本金 {l_remaining:,.2f} 元未予偿还，"
                                    f"且已多期逾期，经原告多次催收仍拒不履行还款义务，已构成严重违约。"
                                    f"为维护原告合法权益，特向贵院提起诉讼，请求依法判决。")
            fact_text.grid(row=4, column=1, sticky=tk.W, padx=5, pady=8)
            fields["事实与理由"] = fact_text

            add_field(5, "起诉法院", "蛋国人民法院")

            # 按钮
            dialog_btn_frame = tk.Frame(dialog, bg='white')
            dialog_btn_frame.pack(pady=15)

            def submit_to_court():
                plaintiff_name = fields["原告名称"].get().strip()
                plaintiff_phone = fields["原告联系方式"].get().strip()
                case_type = fields["案件类型"].get().strip()
                lawsuit_request = fields["诉讼请求"].get("1.0", tk.END).strip()
                case_description = fields["事实与理由"].get("1.0", tk.END).strip()
                court_name = fields["起诉法院"].get().strip()

                if not all([plaintiff_name, case_type, lawsuit_request, case_description]):
                    messagebox.showwarning("提示", "请填写完整的诉讼信息！", parent=dialog)
                    return

                if not messagebox.askyesno("确认提交",
                                           f"确认将对被告 {c_name} 的诉讼信息提交至法院系统？\n\n"
                                           f"案件类型：{case_type}\n"
                                           f"诉讼标的：¥{l_remaining:,.2f}\n"
                                           f"受理法院：{court_name}",
                                           parent=dialog):
                    return

                try:
                    # 连接法院数据库
                    court_conn = sqlite3.connect(COURT_DB_PATH)
                    court_cursor = court_conn.cursor()

                    # 生成案号
                    now = datetime.now()
                    case_number = f"（{now.year}）蛋民初字第{random.randint(10000, 99999)}号"
                    file_time = now.strftime('%Y-%m-%d %H:%M:%S')

                    # 插入案件表
                    court_cursor.execute('''
                        INSERT INTO cases (
                            case_number, case_type, case_title,
                            plaintiff_name, plaintiff_id, plaintiff_phone,
                            defendant_name, defendant_id, defendant_phone,
                            case_description, lawsuit_request,
                            status, file_time, fee, created_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ''', (
                        case_number, case_type,
                        f"{plaintiff_name}诉{c_name}金融借款合同纠纷案",
                        plaintiff_name, "BANK_001", plaintiff_phone,
                        c_name, c_id, c_phone,
                        case_description, lawsuit_request,
                        file_time, 0, 1
                    ))
                    court_conn.commit()
                    court_conn.close()

                    self.add_operation_log("起诉用户",
                                           f"对客户 {c_name}（贷款ID:{loan_id}）提起诉讼，案号：{case_number}")

                    messagebox.showinfo("提交成功",
                                        f"✅ 诉讼信息已成功提交至法院系统！\n\n"
                                        f"案号：{case_number}\n"
                                        f"被告：{c_name}\n"
                                        f"案件类型：{case_type}\n"
                                        f"受理法院：{court_name}\n"
                                        f"提交时间：{file_time}\n\n"
                                        f"请等待法院立案审理。",
                                        parent=dialog)
                    dialog.destroy()
                    self.show_sue_user()

                except Exception as e:
                    messagebox.showerror("提交失败", f"提交至法院系统时出错：{str(e)}", parent=dialog)

            tk.Button(dialog_btn_frame, text="✅ 确认并提交至法院", command=submit_to_court,
                      width=20, height=2, bg='#27ae60', fg='white',
                      font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

            tk.Button(dialog_btn_frame, text="取消", command=dialog.destroy,
                      width=10, height=2, bg='#95a5a6', fg='white',
                      font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="⚖️ 起诉该用户", command=open_sue_dialog,
                  width=18, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tree.bind("<Double-1>", lambda e: open_sue_dialog())

    # ==================== 执行法院判决（管理员） ====================

    def init_judgment_execution_table(self):
        """初始化判决执行记录表"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS judgment_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_number TEXT NOT NULL UNIQUE,
                loan_id INTEGER,
                customer_id INTEGER,
                defendant_name TEXT,
                defendant_id_card TEXT,
                execute_amount REAL,
                execute_status TEXT DEFAULT 'pending',
                execute_time TEXT,
                result_note TEXT
            )
        ''')
        self.conn.commit()

    def show_execute_judgment(self):
        self.clear_main_frame()
        self.init_judgment_execution_table()

        title_label = tk.Label(self.main_frame, text="🔨 执行法院判决",
                               font=("Arial", 18, "bold"), bg='white', fg='#2c3e50')
        title_label.pack(pady=20)

        info_frame = tk.LabelFrame(self.main_frame, text="操作说明", font=("Arial", 12, "bold"),
                                   bg='white', padx=20, pady=10)
        info_frame.pack(fill=tk.X, padx=30, pady=5)
        tk.Label(info_frame,
                 text="列出法院已判决「原告胜诉」的案件。选中案件后点击「执行判决」，将从被告银行卡扣除贷款连本带利金额；"
                      "扣款成功则贷款标记为已结清，余额不足则被告信用分直接 -10。",
                 font=("Arial", 10), bg='white', fg='#555', wraplength=900, justify=tk.LEFT).pack(anchor=tk.W)

        # 案件列表
        list_frame = tk.LabelFrame(self.main_frame, text="原告胜诉案件列表",
                                   font=("Arial", 12, "bold"), bg='white', padx=10, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ("案号", "被告姓名", "身份证号", "贷款ID", "贷款类型", "剩余本金", "执行金额(连本带利)", "判决日期", "执行状态")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)

        col_widths = [160, 80, 150, 60, 90, 100, 130, 100, 90]
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=col_widths[i], anchor=tk.CENTER)

        # 从法院系统查询原告胜诉且已判决的案件
        try:
            court_conn = sqlite3.connect(COURT_DB_PATH)
            court_cursor = court_conn.cursor()
            court_cursor.execute('''
                SELECT case_number, case_title, defendant_name, defendant_id,
                       verdict, verdict_date, lawsuit_request
                FROM cases
                WHERE verdict = '原告胜诉' AND status = 'judged'
                ORDER BY verdict_date DESC
            ''')
            court_cases = court_cursor.fetchall()
            court_conn.close()
        except Exception as e:
            tk.Label(list_frame, text=f"无法连接法院系统：{str(e)}",
                     font=("Arial", 12), bg='white', fg='#e74c3c').pack(pady=20)
            court_cases = []

        # 关联银行系统的贷款信息
        case_data = []
        for case in court_cases:
            case_number, case_title, def_name, def_id, verdict, verdict_date, lawsuit_req = case

            # 查询执行记录
            self.cursor.execute('''
                SELECT execute_status, execute_amount, result_note FROM judgment_executions WHERE case_number = ?
            ''', (case_number,))
            exec_record = self.cursor.fetchone()

            # 通过身份证号查询客户及其active贷款
            self.cursor.execute('''
                SELECT c.id, c.name, c.card_number, c.balance, c.credit_score,
                       l.id, l.loan_type, l.loan_amount, l.remaining_amount,
                       l.interest_rate, l.term_months, l.status, l.next_payment_date
                FROM customers c
                LEFT JOIN loans l ON c.id = l.customer_id AND l.status = 'active'
                WHERE c.id_number = ?
                ORDER BY l.remaining_amount DESC
            ''', (def_id,))
            loans = self.cursor.fetchall()

            if exec_record:
                exec_status = exec_record[0]
                exec_amount = exec_record[1]
                status_text = "执行成功" if exec_status == 'success' else ("执行失败(余额不足)" if exec_status == 'failed' else "待执行")
                # 已执行的案件也展示
                loan_id = ""
                loan_type = ""
                remaining = ""
                if loans:
                    loan_id = loans[0][5] or ""
                    loan_type = loans[0][6] or ""
                    remaining = loans[0][8]
                case_data.append((case_number, def_name, def_id, loan_id, loan_type,
                                  remaining, exec_amount or 0, verdict_date or "", status_text))
            else:
                if not loans or not loans[0][5]:
                    # 没有找到active贷款，跳过或显示无贷款
                    case_data.append((case_number, def_name, def_id, "无", "无", "无", "—", verdict_date or "", "无待执行贷款"))
                    continue
                # 取第一笔active贷款（剩余本金最多的）
                cust_id, c_name, card, balance, credit, loan_id, loan_type, loan_amt, remaining, rate, term, l_status, next_pay = loans[0]
                # 计算连本带利执行金额：剩余本金 + 按贷款利率计算的未还利息（简化为剩余本金 * 年利率 * 0.5，即半年利息作为违约补偿）
                interest = remaining * (rate / 100) * 0.5
                execute_amount = round(remaining + interest, 2)
                case_data.append((case_number, def_name, def_id, loan_id, loan_type,
                                  remaining, execute_amount, verdict_date or "", "待执行"))

        for row in case_data:
            tree.insert("", "end", values=row)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        # 统计
        stats_frame = tk.Frame(self.main_frame, bg='white')
        stats_frame.pack(fill=tk.X, padx=30, pady=5)
        total = len(case_data)
        pending = sum(1 for r in case_data if r[8] == "待执行")
        success = sum(1 for r in case_data if r[8] == "执行成功")
        failed = sum(1 for r in case_data if "失败" in r[8])
        tk.Label(stats_frame, text=f"案件总数：{total}  |  待执行：{pending}  |  执行成功：{success}  |  执行失败：{failed}",
                 font=("Arial", 10), bg='white', fg='#7f8c8d').pack(side=tk.LEFT)

        # 操作按钮
        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)

        def do_execute():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("提示", "请先选择要执行的案件！")
                return
            item = tree.item(selected[0])
            values = item['values']
            case_number = values[0]
            def_name = values[1]
            def_id = values[2]
            loan_id = values[3]
            execute_amount = values[6]
            current_status = values[8]

            if current_status != "待执行":
                messagebox.showinfo("提示", f"该案件状态为「{current_status}」，无需重复执行！")
                return

            if loan_id in ("无", "", None):
                messagebox.showerror("错误", "该被告无有效贷款，无法执行！")
                return

            try:
                loan_id_int = int(loan_id)
                execute_amt = float(execute_amount)
            except (ValueError, TypeError):
                messagebox.showerror("错误", "贷款ID或执行金额格式不正确！")
                return

            # 查询客户和贷款详情
            self.cursor.execute('''
                SELECT c.id, c.name, c.card_number, c.balance, c.credit_score, c.is_frozen,
                       l.remaining_amount, l.loan_type, l.interest_rate
                FROM customers c
                JOIN loans l ON c.id = l.customer_id
                WHERE l.id = ?
            ''', (loan_id_int,))
            info = self.cursor.fetchone()
            if not info:
                messagebox.showerror("错误", "未找到对应的贷款或客户信息！")
                return

            cust_id, c_name, card, balance, credit, frozen, remaining, loan_type, rate = info

            if frozen:
                messagebox.showerror("错误", f"被告账户已被冻结，无法执行扣款！\n当前余额：¥{balance:,.2f}")
                return

            # 确认执行
            confirm = messagebox.askyesno("确认执行判决",
                                          f"案号：{case_number}\n"
                                          f"被告：{c_name}（{def_id}）\n"
                                          f"贷款类型：{loan_type}\n"
                                          f"剩余本金：¥{remaining:,.2f}\n"
                                          f"执行金额（连本带利）：¥{execute_amt:,.2f}\n"
                                          f"被告当前余额：¥{balance:,.2f}\n\n"
                                          f"确认从被告银行卡（{card}）扣除 ¥{execute_amt:,.2f} 吗？")
            if not confirm:
                return

            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if balance >= execute_amt:
                # 余额充足，扣款成功
                new_balance = balance - execute_amt
                self.cursor.execute('''
                    UPDATE customers SET balance = ?, last_update_time = ? WHERE id = ?
                ''', (new_balance, now_str, cust_id))
                self.cursor.execute('''
                    UPDATE loans SET remaining_amount = 0, status = 'paid' WHERE id = ?
                ''', (loan_id_int,))
                self.cursor.execute('''
                    INSERT INTO loan_payments (loan_id, payment_amount, payment_date)
                    VALUES (?, ?, ?)
                ''', (loan_id_int, execute_amt, now_str))
                self.cursor.execute('''
                    INSERT INTO transactions (from_card, to_card, amount, transaction_type, transaction_time)
                    VALUES (?, 'COURT_EXECUTION', ?, 'court_judgment_execution', ?)
                ''', (card, execute_amt, now_str))
                self.cursor.execute('''
                    INSERT INTO judgment_executions (case_number, loan_id, customer_id, defendant_name,
                                                     defendant_id_card, execute_amount, execute_status,
                                                     execute_time, result_note)
                    VALUES (?, ?, ?, ?, ?, ?, 'success', ?, ?)
                ''', (case_number, loan_id_int, cust_id, c_name, def_id, execute_amt, now_str,
                      f"法院判决执行成功，扣款¥{execute_amt:,.2f}，贷款已结清"))
                self.conn.commit()
                self.add_operation_log("执行法院判决",
                                       f"案件{case_number}执行成功，从{c_name}账户扣款¥{execute_amt:,.2f}，贷款{loan_id_int}已结清")
                messagebox.showinfo("执行成功",
                                    f"✅ 法院判决执行成功！\n\n"
                                    f"案号：{case_number}\n"
                                    f"被告：{c_name}\n"
                                    f"扣款金额：¥{execute_amt:,.2f}\n"
                                    f"扣款后余额：¥{new_balance:,.2f}\n"
                                    f"贷款状态：已结清")
            else:
                # 余额不足，信用分-10
                new_credit = credit - 10
                self.cursor.execute('''
                    UPDATE customers SET credit_score = ?, last_update_time = ? WHERE id = ?
                ''', (new_credit, now_str, cust_id))
                self.cursor.execute('''
                    INSERT INTO judgment_executions (case_number, loan_id, customer_id, defendant_name,
                                                     defendant_id_card, execute_amount, execute_status,
                                                     execute_time, result_note)
                    VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?)
                ''', (case_number, loan_id_int, cust_id, c_name, def_id, execute_amt, now_str,
                      f"余额不足执行失败，被告信用分-10（当前余额¥{balance:,.2f}，需执行¥{execute_amt:,.2f}）"))
                self.conn.commit()
                self.add_operation_log("执行法院判决",
                                       f"案件{case_number}执行失败，{c_name}余额不足（¥{balance:,.2f}<¥{execute_amt:,.2f}），信用分-10")
                messagebox.showwarning("执行失败 - 余额不足",
                                       f"⚠️ 法院判决执行失败！\n\n"
                                       f"案号：{case_number}\n"
                                       f"被告：{c_name}\n"
                                       f"应执行金额：¥{execute_amt:,.2f}\n"
                                       f"当前余额：¥{balance:,.2f}\n"
                                       f"差额：¥{execute_amt - balance:,.2f}\n\n"
                                       f"已对被告信用分扣除 10 分（当前信用分：{new_credit}）")

            self.show_execute_judgment()

        tk.Button(btn_frame, text="🔨 执行判决", command=do_execute,
                  width=18, height=2, bg='#e74c3c', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="🔄 刷新列表", command=self.show_execute_judgment,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 问题反馈 ====================

    def show_submit_feedback(self):
        """问题反馈界面"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="📢 问题反馈", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)
        tk.Label(self.main_frame, text="请输入您遇到的问题或建议，我们将尽快处理",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)

        form_frame = tk.Frame(self.main_frame, bg='white')
        form_frame.pack(fill=tk.X, padx=100, pady=20)

        tk.Label(form_frame, text="反馈内容：", font=("Arial", 12), bg='white').grid(
            row=0, column=0, sticky=tk.NW, padx=10, pady=10)
        feedback_text = scrolledtext.ScrolledText(form_frame, width=60, height=10,
                                                   font=("Arial", 11), wrap=tk.WORD)
        feedback_text.grid(row=0, column=1, padx=10, pady=10)

        def do_submit():
            content = feedback_text.get(1.0, tk.END).strip()
            if not content:
                messagebox.showwarning("提示", "反馈内容不能为空！")
                return
            if fc.submit_feedback("银行系统", self.current_user, content):
                messagebox.showinfo("成功", "✅ 反馈提交成功！我们会尽快处理您的问题。")
                feedback_text.delete(1.0, tk.END)
            else:
                messagebox.showerror("错误", "反馈提交失败！")

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)
        tk.Button(btn_frame, text="📤 提交反馈", command=do_submit,
                  width=15, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="📨 查看我的反馈", command=self.show_my_feedback,
                  width=15, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    def show_my_feedback(self):
        """我的反馈界面"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="📨 我的反馈", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)

        list_frame = tk.Frame(self.main_frame, bg='white')
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ('id', 'content', 'status', 'reply', 'created_at', 'replied_at')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=15)
        tree.heading('id', text='ID')
        tree.heading('content', text='反馈内容')
        tree.heading('status', text='状态')
        tree.heading('reply', text='系统回复')
        tree.heading('created_at', text='提交时间')
        tree.heading('replied_at', text='回复时间')
        tree.column('id', width=50, anchor=tk.CENTER)
        tree.column('content', width=250)
        tree.column('status', width=70, anchor=tk.CENTER)
        tree.column('reply', width=250)
        tree.column('created_at', width=140, anchor=tk.CENTER)
        tree.column('replied_at', width=140, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        feedback_list = fc.get_user_feedback(self.current_user)
        for fb in feedback_list:
            status_text = "待回复" if fb['status'] == 'pending' else "已回复"
            content_preview = fb['content'][:40] + "..." if len(fb['content']) > 40 else fb['content']
            reply_preview = (fb['reply'][:40] + "...") if fb['reply'] and len(fb['reply']) > 40 else (fb['reply'] or '-')
            tree.insert('', tk.END, values=(
                fb['id'], content_preview, status_text, reply_preview,
                fb['created_at'], fb['replied_at'] or '-'
            ))

        def on_double_click(event):
            selected = tree.selection()
            if not selected:
                return
            item = tree.item(selected[0])
            fb_id = item['values'][0]
            fb = next((f for f in feedback_list if f['id'] == fb_id), None)
            if not fb:
                return
            detail = tk.Toplevel(self.root)
            detail.title(f"反馈详情 - ID:{fb['id']}")
            detail.geometry("600x500")
            detail.configure(bg='white')

            tk.Label(detail, text="📝 反馈详情", font=("Arial", 14, "bold"),
                     bg='white', fg='#2c3e50').pack(pady=10)

            info_frame = tk.Frame(detail, bg='white')
            info_frame.pack(fill=tk.X, padx=20)
            tk.Label(info_frame, text=f"状态：{'待回复' if fb['status'] == 'pending' else '已回复'}",
                     font=("Arial", 11), bg='white').pack(anchor=tk.W)
            tk.Label(info_frame, text=f"提交时间：{fb['created_at']}",
                     font=("Arial", 11), bg='white').pack(anchor=tk.W)
            if fb['replied_at']:
                tk.Label(info_frame, text=f"回复时间：{fb['replied_at']}",
                         font=("Arial", 11), bg='white').pack(anchor=tk.W)

            tk.Label(detail, text="反馈内容：", font=("Arial", 11, "bold"),
                     bg='white', fg='#2c3e50').pack(anchor=tk.W, padx=20, pady=(10, 5))
            content_box = scrolledtext.ScrolledText(detail, width=65, height=6,
                                                     font=("Arial", 10), wrap=tk.WORD)
            content_box.pack(padx=20, pady=5)
            content_box.insert(tk.END, fb['content'])
            content_box.config(state=tk.DISABLED)

            tk.Label(detail, text="系统回复：", font=("Arial", 11, "bold"),
                     bg='white', fg='#27ae60').pack(anchor=tk.W, padx=20, pady=(10, 5))
            reply_box = scrolledtext.ScrolledText(detail, width=65, height=6,
                                                   font=("Arial", 10), wrap=tk.WORD)
            reply_box.pack(padx=20, pady=5)
            reply_box.insert(tk.END, fb['reply'] or '（暂无回复）')
            reply_box.config(state=tk.DISABLED)

        tree.bind('<Double-1>', on_double_click)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=15)
        tk.Button(btn_frame, text="🔄 刷新", command=self.show_my_feedback,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="📢 提交新反馈", command=self.show_submit_feedback,
                  width=15, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 查询名下账户 ====================

    def show_query_accounts_by_id(self):
        """根据身份证号查询名下所有账户"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="🔍 查询名下账户", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)
        tk.Label(self.main_frame, text="输入身份证号，查询该身份证名下的所有银行账户",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)

        query_frame = tk.Frame(self.main_frame, bg='white')
        query_frame.pack(fill=tk.X, padx=100, pady=20)

        tk.Label(query_frame, text="身份证号：", font=("Arial", 12), bg='white').grid(
            row=0, column=0, padx=10, pady=10)
        id_entry = tk.Entry(query_frame, width=30, font=("Arial", 12), bd=2, relief=tk.GROOVE)
        id_entry.grid(row=0, column=1, padx=10, pady=10)

        result_frame = tk.Frame(self.main_frame, bg='white')
        result_frame.pack(fill=tk.BOTH, expand=True, padx=50, pady=10)

        def do_query():
            id_number = id_entry.get().strip()
            if not id_number:
                messagebox.showwarning("提示", "请输入身份证号！")
                return
            for widget in result_frame.winfo_children():
                widget.destroy()

            self.cursor.execute(
                "SELECT id, card_number, name, balance, credit_score, account_type, "
                "phone, email, address, create_time, is_frozen FROM customers WHERE id_number=?",
                (id_number,)
            )
            accounts = self.cursor.fetchall()

            if not accounts:
                tk.Label(result_frame, text="❌ 未找到该身份证号名下的账户",
                         font=("Arial", 14), bg='white', fg='#e74c3c').pack(pady=30)
                return

            tk.Label(result_frame, text=f"✅ 找到 {len(accounts)} 个账户（双击查看详情）",
                     font=("Arial", 13, "bold"), bg='white', fg='#27ae60').pack(pady=10)

            columns = ('card_number', 'name', 'account_type', 'balance', 'credit_score', 'status')
            tree = ttk.Treeview(result_frame, columns=columns, show='headings', height=10)
            tree.heading('card_number', text='卡号')
            tree.heading('name', text='户主姓名')
            tree.heading('account_type', text='账户类型')
            tree.heading('balance', text='余额')
            tree.heading('credit_score', text='信用分')
            tree.heading('status', text='状态')
            tree.column('card_number', width=180, anchor=tk.CENTER)
            tree.column('name', width=100, anchor=tk.CENTER)
            tree.column('account_type', width=100, anchor=tk.CENTER)
            tree.column('balance', width=120, anchor=tk.CENTER)
            tree.column('credit_score', width=80, anchor=tk.CENTER)
            tree.column('status', width=80, anchor=tk.CENTER)
            tree.pack(fill=tk.BOTH, expand=True, padx=20)

            account_dict = {}
            for acc in accounts:
                acc_id, card, name, balance, credit, acc_type, phone, email, address, create_time, frozen = acc
                status = "冻结" if frozen else "正常"
                tree.insert('', tk.END, values=(card, name, acc_type, f"¥{balance:,.2f}", credit, status))
                account_dict[card] = {
                    'id': acc_id, 'card_number': card, 'name': name, 'balance': balance,
                    'credit_score': credit, 'account_type': acc_type, 'phone': phone,
                    'email': email, 'address': address, 'create_time': create_time,
                    'is_frozen': frozen
                }

            def show_account_detail(event):
                selected = tree.selection()
                if not selected:
                    return
                item = tree.item(selected[0])
                card = item['values'][0]
                acc = account_dict.get(card)
                if not acc:
                    return

                detail = tk.Toplevel(self.root)
                detail.title(f"账户详情 - {acc['name']}")
                detail.geometry("500x550")
                detail.configure(bg='white')

                tk.Label(detail, text="📋 账户详细信息", font=("Arial", 16, "bold"),
                         bg='white', fg='#2c3e50').pack(pady=15)

                info_frame = tk.Frame(detail, bg='white')
                info_frame.pack(fill=tk.BOTH, expand=True, padx=30)

                details = [
                    ("卡号：", acc['card_number']),
                    ("户主姓名：", acc['name']),
                    ("账户类型：", acc['account_type']),
                    ("账户余额：", f"¥{acc['balance']:,.2f}"),
                    ("信用分：", str(acc['credit_score'])),
                    ("账户状态：", "冻结" if acc['is_frozen'] else "正常"),
                    ("手机号：", acc['phone'] or '未设置'),
                    ("邮箱：", acc['email'] or '未设置'),
                    ("地址：", acc['address'] or '未设置'),
                    ("开户时间：", acc['create_time']),
                ]
                for i, (label, value) in enumerate(details):
                    tk.Label(info_frame, text=label, font=("Arial", 11, "bold"),
                             bg='white', fg='#7f8c8d').grid(row=i, column=0, sticky=tk.W, pady=8)
                    tk.Label(info_frame, text=value, font=("Arial", 11),
                             bg='white', fg='#2c3e50').grid(row=i, column=1, sticky=tk.W, pady=8, padx=10)

                # 查询该账户的贷款信息
                self.cursor.execute(
                    "SELECT loan_type, loan_amount, remaining_amount, status, start_date FROM loans WHERE customer_id=?",
                    (acc['id'],)
                )
                loans = self.cursor.fetchall()
                if loans:
                    tk.Label(info_frame, text="\n💳 贷款信息：", font=("Arial", 11, "bold"),
                             bg='white', fg='#e67e22').grid(row=len(details), column=0, sticky=tk.W, pady=8)
                    loan_text = ""
                    for lt, amt, rem, st, sd in loans:
                        loan_text += f"  {lt} - 金额:¥{amt:,.2f} 剩余:¥{rem:,.2f} 状态:{st}\n"
                    tk.Label(info_frame, text=loan_text, font=("Arial", 10),
                             bg='white', fg='#2c3e50', justify=tk.LEFT).grid(
                        row=len(details), column=1, sticky=tk.W, pady=8)

                tk.Button(detail, text="关闭", command=detail.destroy,
                          width=12, height=2, bg='#95a5a6', fg='white',
                          font=("Arial", 11), relief=tk.FLAT).pack(pady=15)

            tree.bind('<Double-1>', show_account_detail)

        tk.Button(query_frame, text="🔍 查询", command=do_query,
                  width=12, height=1, bg='#3498db', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).grid(row=0, column=2, padx=10)

    # ==================== 信用分补给 ====================

    def show_credit_supply(self):
        """信用分补给界面"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="💳 信用分补给", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)
        tk.Label(self.main_frame, text="信用分低于60分可申请补给，需签署协议并通过人脸验证",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)

        form_frame = tk.Frame(self.main_frame, bg='white')
        form_frame.pack(fill=tk.X, padx=100, pady=20)

        tk.Label(form_frame, text="银行卡号：", font=("Arial", 12), bg='white').grid(
            row=0, column=0, padx=10, pady=10, sticky=tk.E)
        card_entry = tk.Entry(form_frame, width=30, font=("Arial", 12), bd=2, relief=tk.GROOVE)
        card_entry.grid(row=0, column=1, padx=10, pady=10)

        info_label = tk.Label(self.main_frame, text="", font=("Arial", 12), bg='white', fg='#2c3e50')
        info_label.pack(pady=10)

        def check_credit():
            card = card_entry.get().strip()
            if not card:
                messagebox.showwarning("提示", "请输入银行卡号！")
                return
            self.cursor.execute(
                "SELECT id, name, id_number, credit_score, is_frozen FROM customers WHERE card_number=?",
                (card,)
            )
            customer = self.cursor.fetchone()
            if not customer:
                info_label.config(text="❌ 未找到该卡号对应的账户！", fg='#e74c3c')
                return
            cust_id, name, id_number, credit_score, frozen = customer
            if frozen:
                info_label.config(text=f"❌ 账户已冻结，无法申请信用分补给！", fg='#e74c3c')
                return
            if credit_score >= 60:
                info_label.config(text=f"ℹ️ 当前信用分：{credit_score}分，无需补给（≥60分）", fg='#e67e22')
                return
            info_label.config(text=f"✅ 持卡人：{name}，当前信用分：{credit_score}分，可以申请补给", fg='#27ae60')
            self._open_credit_supply_agreement(cust_id, card, name, id_number, credit_score)

        tk.Button(form_frame, text="🔍 检测信用分", command=check_credit,
                  width=15, height=1, bg='#3498db', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).grid(row=0, column=2, padx=10)

    def _open_credit_supply_agreement(self, cust_id, card, name, id_number, old_score):
        """打开信用分补给协议签署窗口"""
        agreement = tk.Toplevel(self.root)
        agreement.title("信用分补给协议")
        agreement.geometry("600x500")
        agreement.configure(bg='white')
        agreement.grab_set()

        tk.Label(agreement, text="📜 信用分补给协议", font=("Arial", 16, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=15)

        agreement_text = """
        本人（持卡人）郑重承诺：

        1. 本人自愿申请信用分补给，将信用分恢复至80分。

        2. 本人保证每一次贷款将会在规定时间内按时还完，
           不逾期、不拖欠。

        3. 若本人未能在规定时间内还完贷款，本人愿意接受
           相应的法律责任，包括但不限于：
           - 信用分再次扣除
           - 被起诉至法院
           - 承担相应的法律后果

        4. 本人理解并同意，信用分补给需通过人脸验证，
           若人脸验证失败，申请将提交人工审核。

        持卡人（签字）：_______________
        日期：_______________
        """
        text_box = scrolledtext.ScrolledText(agreement, width=65, height=18,
                                              font=("Arial", 10), wrap=tk.WORD)
        text_box.pack(padx=20, pady=10)
        text_box.insert(tk.END, agreement_text)
        text_box.config(state=tk.DISABLED)

        agree_var = tk.IntVar()
        tk.Checkbutton(agreement, text="我已阅读并同意上述协议内容",
                       variable=agree_var, font=("Arial", 11), bg='white').pack(pady=5)

        btn_frame = tk.Frame(agreement, bg='white')
        btn_frame.pack(pady=15)

        def sign_and_continue():
            if not agree_var.get():
                messagebox.showwarning("提示", "请先勾选同意协议！", parent=agreement)
                return
            agreement.destroy()
            self._start_face_verification(cust_id, card, name, id_number, old_score)

        tk.Button(btn_frame, text="✅ 签署协议并继续", command=sign_and_continue,
                  width=18, height=2, bg='#27ae60', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="取消", command=agreement.destroy,
                  width=12, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    def _start_face_verification(self, cust_id, card, name, id_number, old_score):
        """开始人脸验证"""
        if not HAS_CV2:
            messagebox.showerror("错误", "未安装opencv-python，无法进行人脸验证！")
            return
        if not HAS_PIL:
            messagebox.showerror("错误", "未安装Pillow，无法进行人脸验证！")
            return
        if not HAS_NUMPY:
            messagebox.showerror("错误", "未安装numpy，无法进行人脸验证！")
            return

        # 从户籍系统人脸数据库查询该持卡人的人脸
        household_conn = sqlite3.connect(HOUSEHOLD_DB_PATH)
        h_cursor = household_conn.cursor()
        h_cursor.execute("SELECT face_path FROM face_info WHERE id_number=?", (id_number,))
        face_record = h_cursor.fetchone()
        household_conn.close()

        if not face_record or not face_record[0]:
            messagebox.showerror("错误", f"未在户籍系统人脸数据库中找到 {name} 的人脸信息！\n请先到户籍系统录入人脸。")
            return

        db_face_path = face_record[0]
        if not os.path.exists(db_face_path):
            messagebox.showerror("错误", f"人脸数据库文件不存在：{db_face_path}")
            return

        # 打开摄像头进行人脸比对
        verify_win = tk.Toplevel(self.root)
        verify_win.title("人脸验证")
        verify_win.geometry("700x600")
        verify_win.configure(bg='white')
        verify_win.grab_set()

        tk.Label(verify_win, text="👤 人脸验证", font=("Arial", 16, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=10)
        tk.Label(verify_win, text=f"持卡人：{name}（身份证：{id_number}）",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)
        tk.Label(verify_win, text="请正对摄像头，点击【拍照验证】按钮进行人脸比对",
                 font=("Arial", 11), bg='white', fg='#e67e22').pack(pady=5)

        preview_label = tk.Label(verify_win, bg='#f0f0f0', width=80, height=25)
        preview_label.pack(pady=10)

        result_label = tk.Label(verify_win, text="", font=("Arial", 12), bg='white')
        result_label.pack(pady=10)

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("错误", "无法启动摄像头！", parent=verify_win)
            verify_win.destroy()
            return

        self._cap = cap
        self._verify_running = True

        def update_frame():
            if not self._verify_running:
                return
            ret, frame = cap.read()
            if ret:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                img.thumbnail((500, 380))
                imgtk = ImageTk.PhotoImage(image=img)
                preview_label.config(image=imgtk)
                preview_label.image = imgtk
            verify_win.after(30, update_frame)

        update_frame()

        captured_face_path = None
        captured_face_blob = None

        def capture_and_verify():
            nonlocal captured_face_path, captured_face_blob
            ret, frame = cap.read()
            if not ret:
                result_label.config(text="❌ 拍照失败！", fg='#e74c3c')
                return

            h, w = frame.shape[:2]
            face_roi = frame[int(h * 0.15):int(h * 0.85), int(w * 0.25):int(w * 0.75)]

            save_dir = os.path.join(BASE_DIR, 'credit_supply_faces')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            captured_face_path = os.path.join(save_dir, f"{id_number}_{timestamp}.jpg")
            cv2.imwrite(captured_face_path, face_roi)

            # 将照片转为BLOB存入数据库
            _, img_encoded = cv2.imencode('.jpg', face_roi)
            captured_face_blob = img_encoded.tobytes()

            # 人脸比对
            try:
                target_size = (390, 567)
                captured_img = cv2.resize(face_roi, target_size)
                db_pil = Image.open(db_face_path).convert("RGB")
                db_img = cv2.cvtColor(np.array(db_pil), cv2.COLOR_RGB2BGR)
                db_resized = cv2.resize(db_img, target_size)
                mse = ((captured_img - db_resized) ** 2).mean()
                similarity = max(0, 100 - (mse / 10))

                if similarity >= 99:
                    # 比对成功，直接补给，并将记录存入数据库
                    self.cursor.execute(
                        "UPDATE customers SET credit_score=80 WHERE id=?", (cust_id,)
                    )
                    self.conn.commit()
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.cursor.execute('''
                        INSERT INTO credit_supply_applications
                        (customer_id, card_number, id_number, name, old_credit_score,
                         new_credit_score, agreement_signed, face_verify_status,
                         captured_face_path, captured_face_blob, db_face_path,
                         apply_time, supply_time, review_status)
                        VALUES (?, ?, ?, ?, ?, 80, 1, 'success', ?, ?, ?, ?, ?, 'approved')
                    ''', (cust_id, card, id_number, name, old_score,
                          captured_face_path, captured_face_blob, db_face_path, now, now))
                    self.conn.commit()
                    self._verify_running = False
                    cap.release()
                    result_label.config(text=f"✅ 人脸比对成功！相似度：{similarity:.1f}%\n信用分已从 {old_score} 重置至 80 分", fg='#27ae60')
                    messagebox.showinfo("成功", f"✅ 人脸比对成功！\n相似度：{similarity:.1f}%\n信用分已从 {old_score} 重置至 80 分", parent=verify_win)
                    verify_win.after(2000, verify_win.destroy)
                else:
                    # 比对失败，提交人工审核，照片存入数据库
                    self._verify_running = False
                    cap.release()
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.cursor.execute('''
                        INSERT INTO credit_supply_applications
                        (customer_id, card_number, id_number, name, old_credit_score,
                         new_credit_score, agreement_signed, face_verify_status,
                         captured_face_path, captured_face_blob, db_face_path,
                         apply_time, review_status)
                        VALUES (?, ?, ?, ?, ?, 80, 1, 'failed', ?, ?, ?, ?, 'pending')
                    ''', (cust_id, card, id_number, name, old_score,
                          captured_face_path, captured_face_blob, db_face_path, now))
                    self.conn.commit()
                    result_label.config(text=f"❌ 人脸比对失败！相似度：{similarity:.1f}%\n已提交至人脸审核功能进行人工审核", fg='#e74c3c')
                    messagebox.showwarning("提示", f"❌ 人脸比对失败！相似度：{similarity:.1f}%\n\n已将本次拍摄照片和数据库人脸照片提交至【人脸审核】功能，\n请等待管理员人工审核。", parent=verify_win)
                    verify_win.after(3000, verify_win.destroy)
            except Exception as e:
                result_label.config(text=f"❌ 比对出错：{str(e)}", fg='#e74c3c')

        btn_frame = tk.Frame(verify_win, bg='white')
        btn_frame.pack(pady=15)
        tk.Button(btn_frame, text="📸 拍照验证", command=capture_and_verify,
                  width=15, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        def on_close():
            self._verify_running = False
            cap.release()
            verify_win.destroy()

        verify_win.protocol("WM_DELETE_WINDOW", on_close)
        tk.Button(btn_frame, text="取消", command=on_close,
                  width=12, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

    # ==================== 人脸审核（管理员） ====================

    def show_face_review(self):
        """人脸审核界面（管理员）"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="👤 人脸审核 - 信用分补给申请", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)
        tk.Label(self.main_frame, text="审核人脸比对失败的信用分补给申请，对比两张照片决定通过或驳回",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)

        filter_frame = tk.Frame(self.main_frame, bg='white')
        filter_frame.pack(fill=tk.X, padx=30, pady=10)
        tk.Label(filter_frame, text="状态筛选：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        status_var = tk.StringVar(value="待审核")
        status_combo = ttk.Combobox(filter_frame, textvariable=status_var, width=12, state='readonly')
        status_combo['values'] = ['全部', '待审核', '已通过', '已驳回']
        status_combo.pack(side=tk.LEFT, padx=5)

        list_frame = tk.Frame(self.main_frame, bg='white')
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ('id', 'name', 'card_number', 'id_number', 'old_score', 'apply_time', 'status')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=10)
        tree.heading('id', text='ID')
        tree.heading('name', text='姓名')
        tree.heading('card_number', text='卡号')
        tree.heading('id_number', text='身份证号')
        tree.heading('old_score', text='原信用分')
        tree.heading('apply_time', text='申请时间')
        tree.heading('status', text='状态')
        tree.column('id', width=50, anchor=tk.CENTER)
        tree.column('name', width=80, anchor=tk.CENTER)
        tree.column('card_number', width=160, anchor=tk.CENTER)
        tree.column('id_number', width=160, anchor=tk.CENTER)
        tree.column('old_score', width=80, anchor=tk.CENTER)
        tree.column('apply_time', width=140, anchor=tk.CENTER)
        tree.column('status', width=80, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True)

        def refresh_list():
            for item in tree.get_children():
                tree.delete(item)
            status_map = {'全部': None, '待审核': 'pending', '已通过': 'approved', '已驳回': 'rejected'}
            query = "SELECT id, name, card_number, id_number, old_credit_score, apply_time, review_status FROM credit_supply_applications"
            params = []
            if status_map.get(status_var.get()):
                query += " WHERE review_status=?"
                params.append(status_map[status_var.get()])
            query += " ORDER BY apply_time DESC"
            self.cursor.execute(query, params)
            rows = self.cursor.fetchall()
            status_text_map = {'pending': '待审核', 'approved': '已通过', 'rejected': '已驳回'}
            for row in rows:
                tree.insert('', tk.END, values=(
                    row[0], row[1], row[2], row[3], row[4], row[5],
                    status_text_map.get(row[6], row[6])
                ))

        def review_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("提示", "请先选择一条申请！")
                return
            item = tree.item(selected[0])
            app_id = item['values'][0]

            self.cursor.execute(
                "SELECT * FROM credit_supply_applications WHERE id=?", (app_id,)
            )
            app = self.cursor.fetchone()
            if not app:
                return

            # 解析申请数据
            cols = [d[0] for d in self.cursor.description]
            app_dict = dict(zip(cols, app))

            if app_dict['review_status'] != 'pending':
                messagebox.showinfo("提示", "该申请已审核完毕！")
                return

            review_win = tk.Toplevel(self.root)
            review_win.title(f"人脸审核 - 申请ID:{app_id}")
            review_win.geometry("900x650")
            review_win.configure(bg='white')
            review_win.grab_set()

            tk.Label(review_win, text="🔍 人脸审核", font=("Arial", 16, "bold"),
                     bg='white', fg='#2c3e50').pack(pady=10)

            info_frame = tk.Frame(review_win, bg='white')
            info_frame.pack(fill=tk.X, padx=20)
            tk.Label(info_frame, text=f"申请人：{app_dict['name']}  |  卡号：{app_dict['card_number']}  |  "
                                       f"身份证：{app_dict['id_number']}  |  原信用分：{app_dict['old_credit_score']}",
                     font=("Arial", 11), bg='white').pack(pady=5)

            # 显示两张对比照片
            photos_frame = tk.Frame(review_win, bg='white')
            photos_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

            left_frame = tk.Frame(photos_frame, bg='white')
            left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
            tk.Label(left_frame, text="📷 本次拍摄照片", font=("Arial", 12, "bold"),
                     bg='white', fg='#3498db').pack(pady=5)
            left_img_label = tk.Label(left_frame, bg='#f0f0f0', width=50, height=20)
            left_img_label.pack(pady=5)

            right_frame = tk.Frame(photos_frame, bg='white')
            right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10)
            tk.Label(right_frame, text="🗄️ 数据库人脸照片", font=("Arial", 12, "bold"),
                     bg='white', fg='#9b59b6').pack(pady=5)
            right_img_label = tk.Label(right_frame, bg='#f0f0f0', width=50, height=20)
            right_img_label.pack(pady=5)

            # 加载照片
            try:
                if app_dict['captured_face_path'] and os.path.exists(app_dict['captured_face_path']):
                    img1 = Image.open(app_dict['captured_face_path']).convert("RGB")
                    img1.thumbnail((350, 350))
                    imgtk1 = ImageTk.PhotoImage(img1)
                    left_img_label.config(image=imgtk1)
                    left_img_label.image = imgtk1
                else:
                    left_img_label.config(text="照片不存在")
            except Exception as e:
                left_img_label.config(text=f"加载失败：{e}")

            try:
                if app_dict['db_face_path'] and os.path.exists(app_dict['db_face_path']):
                    img2 = Image.open(app_dict['db_face_path']).convert("RGB")
                    img2.thumbnail((350, 350))
                    imgtk2 = ImageTk.PhotoImage(img2)
                    right_img_label.config(image=imgtk2)
                    right_img_label.image = imgtk2
                else:
                    right_img_label.config(text="照片不存在")
            except Exception as e:
                right_img_label.config(text=f"加载失败：{e}")

            tk.Label(review_win, text="审核意见：", font=("Arial", 11, "bold"), bg='white').pack(anchor=tk.W, padx=20)
            note_text = scrolledtext.ScrolledText(review_win, width=90, height=3, font=("Arial", 10))
            note_text.pack(padx=20, pady=5)

            btn_frame = tk.Frame(review_win, bg='white')
            btn_frame.pack(pady=15)

            def do_approve():
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                note = note_text.get(1.0, tk.END).strip()
                self.cursor.execute(
                    "UPDATE credit_supply_applications SET review_status='approved', review_time=?, reviewer=?, review_note=?, supply_time=? WHERE id=?",
                    (now, self.current_user, note, now, app_id)
                )
                self.cursor.execute(
                    "UPDATE customers SET credit_score=80 WHERE id=?", (app_dict['customer_id'],)
                )
                self.conn.commit()
                messagebox.showinfo("成功", f"✅ 审核通过！\n{app_dict['name']} 的信用分已重置至 80 分", parent=review_win)
                review_win.destroy()
                refresh_list()

            def do_reject():
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                note = note_text.get(1.0, tk.END).strip()
                if not note:
                    messagebox.showwarning("提示", "驳回请填写审核意见！", parent=review_win)
                    return
                self.cursor.execute(
                    "UPDATE credit_supply_applications SET review_status='rejected', review_time=?, reviewer=?, review_note=? WHERE id=?",
                    (now, self.current_user, note, app_id)
                )
                self.conn.commit()
                messagebox.showinfo("提示", f"❌ 已驳回 {app_dict['name']} 的信用分补给申请", parent=review_win)
                review_win.destroy()
                refresh_list()

            tk.Button(btn_frame, text="✅ 审核通过（补给信用分）", command=do_approve,
                      width=22, height=2, bg='#27ae60', fg='white',
                      font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=15)
            tk.Button(btn_frame, text="❌ 驳回申请", command=do_reject,
                      width=18, height=2, bg='#e74c3c', fg='white',
                      font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=15)
            tk.Button(btn_frame, text="关闭", command=review_win.destroy,
                      width=12, height=2, bg='#95a5a6', fg='white',
                      font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=15)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="🔍 审核选中", command=review_selected,
                  width=15, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="🔄 刷新列表", command=refresh_list,
                  width=12, height=2, bg='#95a5a6', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        status_combo.bind('<<ComboboxSelected>>', lambda e: refresh_list())
        refresh_list()

    # ==================== 历史信用分补给 ====================

    def show_credit_supply_history(self):
        """历史信用分补给界面"""
        self.clear_main_frame()

        tk.Label(self.main_frame, text="📜 历史信用分补给", font=("Arial", 18, "bold"),
                 bg='white', fg='#2c3e50').pack(pady=20)
        tk.Label(self.main_frame, text="查看所有信用分补给记录，双击可查看详情并撤回补给",
                 font=("Arial", 11), bg='white', fg='#7f8c8d').pack(pady=5)

        # 筛选栏
        filter_frame = tk.Frame(self.main_frame, bg='white')
        filter_frame.pack(fill=tk.X, padx=30, pady=10)
        tk.Label(filter_frame, text="状态筛选：", font=("Arial", 11), bg='white').pack(side=tk.LEFT, padx=5)
        status_var = tk.StringVar(value="全部")
        status_combo = ttk.Combobox(filter_frame, textvariable=status_var, width=12, state='readonly')
        status_combo['values'] = ['全部', '有效', '已撤回']
        status_combo.pack(side=tk.LEFT, padx=5)

        list_frame = tk.Frame(self.main_frame, bg='white')
        list_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        columns = ('id', 'name', 'card_number', 'id_number', 'old_score', 'new_score',
                   'apply_time', 'supply_time', 'verify_type', 'status')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=15)
        tree.heading('id', text='ID')
        tree.heading('name', text='姓名')
        tree.heading('card_number', text='卡号')
        tree.heading('id_number', text='身份证号')
        tree.heading('old_score', text='补给前分数')
        tree.heading('new_score', text='补给后分数')
        tree.heading('apply_time', text='申请时间')
        tree.heading('supply_time', text='补给时间')
        tree.heading('verify_type', text='验证方式')
        tree.heading('status', text='状态')
        tree.column('id', width=50, anchor=tk.CENTER)
        tree.column('name', width=80, anchor=tk.CENTER)
        tree.column('card_number', width=160, anchor=tk.CENTER)
        tree.column('id_number', width=160, anchor=tk.CENTER)
        tree.column('old_score', width=80, anchor=tk.CENTER)
        tree.column('new_score', width=80, anchor=tk.CENTER)
        tree.column('apply_time', width=140, anchor=tk.CENTER)
        tree.column('supply_time', width=140, anchor=tk.CENTER)
        tree.column('verify_type', width=90, anchor=tk.CENTER)
        tree.column('status', width=70, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def load_history():
            for item in tree.get_children():
                tree.delete(item)

            query = """
                SELECT id, name, card_number, id_number, old_credit_score, new_credit_score,
                       apply_time, supply_time, face_verify_status, is_reverted
                FROM credit_supply_applications
                WHERE review_status='approved' AND supply_time IS NOT NULL
            """
            params = []
            if status_var.get() == '有效':
                query += " AND is_reverted=0"
            elif status_var.get() == '已撤回':
                query += " AND is_reverted=1"
            query += " ORDER BY supply_time DESC"

            self.cursor.execute(query, params)
            rows = self.cursor.fetchall()

            verify_map = {'success': '自动比对', 'failed': '人工审核'}
            for row in rows:
                rid, name, card, idn, old_s, new_s, apply_t, supply_t, verify, reverted = row
                status_text = "已撤回" if reverted else "有效"
                tree.insert('', tk.END, values=(
                    rid, name, card, idn, old_s, new_s,
                    apply_t, supply_t or '-', verify_map.get(verify, verify),
                    status_text
                ))

        def on_double_click(event):
            selected = tree.selection()
            if not selected:
                return
            item = tree.item(selected[0])
            record_id = item['values'][0]

            self.cursor.execute(
                "SELECT * FROM credit_supply_applications WHERE id=?", (record_id,)
            )
            cols = [d[0] for d in self.cursor.description]
            record = dict(zip(cols, self.cursor.fetchone()))
            if not record:
                return

            detail = tk.Toplevel(self.root)
            detail.title(f"信用分补给详情 - ID:{record_id}")
            detail.geometry("750x650")
            detail.configure(bg='white')
            detail.grab_set()

            tk.Label(detail, text="📋 信用分补给详情", font=("Arial", 16, "bold"),
                     bg='white', fg='#2c3e50').pack(pady=10)

            info_frame = tk.Frame(detail, bg='white')
            info_frame.pack(fill=tk.X, padx=20)

            details = [
                ("记录ID：", str(record['id'])),
                ("持卡人：", record['name']),
                ("卡号：", record['card_number']),
                ("身份证号：", record['id_number']),
                ("补给前信用分：", str(record['old_credit_score'])),
                ("补给后信用分：", str(record['new_credit_score'])),
                ("申请时间：", record['apply_time']),
                ("补给时间：", record['supply_time'] or '-'),
                ("验证方式：", "自动比对成功" if record['face_verify_status'] == 'success' else "人工审核通过"),
                ("协议签署：", "已签署" if record['agreement_signed'] else "未签署"),
                ("当前状态：", "已撤回" if record['is_reverted'] else "有效"),
            ]
            if record['revert_time']:
                details.append(("撤回时间：", record['revert_time']))
            if record['reviewer']:
                details.append(("审核人：", record['reviewer']))
            if record['review_note']:
                details.append(("审核意见：", record['review_note']))

            for i, (label, value) in enumerate(details):
                tk.Label(info_frame, text=label, font=("Arial", 10, "bold"),
                         bg='white', fg='#7f8c8d').grid(row=i, column=0, sticky=tk.W, padx=10, pady=3)
                tk.Label(info_frame, text=value, font=("Arial", 10),
                         bg='white', fg='#2c3e50').grid(row=i, column=1, sticky=tk.W, padx=10, pady=3)

            # 显示拍摄的人脸照片
            if record['captured_face_blob']:
                tk.Label(detail, text="📷 补给时拍摄的人脸照片：", font=("Arial", 11, "bold"),
                         bg='white', fg='#2c3e50').pack(anchor=tk.W, padx=20, pady=(10, 5))
                try:
                    import io
                    img_data = np.frombuffer(record['captured_face_blob'], dtype=np.uint8)
                    img = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
                    if img is not None:
                        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                        pil_img = Image.fromarray(img_rgb)
                        pil_img.thumbnail((250, 250))
                        imgtk = ImageTk.PhotoImage(pil_img)
                        img_label = tk.Label(detail, image=imgtk, bg='white')
                        img_label.image = imgtk
                        img_label.pack(pady=5)
                except Exception as e:
                    tk.Label(detail, text=f"照片加载失败：{e}", font=("Arial", 10),
                             bg='white', fg='#e74c3c').pack(pady=5)

            btn_frame = tk.Frame(detail, bg='white')
            btn_frame.pack(pady=15)

            def do_revert():
                if record['is_reverted']:
                    messagebox.showinfo("提示", "该补给记录已被撤回！", parent=detail)
                    return
                if not messagebox.askyesno("确认撤回",
                    f"确定要撤回此补给吗？\n\n持卡人：{record['name']}\n"
                    f"补给后分数：{record['new_credit_score']}\n"
                    f"将恢复至补给前分数：{record['old_credit_score']}",
                    parent=detail):
                    return
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.cursor.execute(
                    "UPDATE credit_supply_applications SET is_reverted=1, revert_time=? WHERE id=?",
                    (now, record_id)
                )
                self.cursor.execute(
                    "UPDATE customers SET credit_score=? WHERE id=?",
                    (record['old_credit_score'], record['customer_id'])
                )
                self.conn.commit()
                messagebox.showinfo("成功",
                    f"✅ 已撤回此补给！\n{record['name']} 的信用分已从 {record['new_credit_score']} "
                    f"恢复至 {record['old_credit_score']}", parent=detail)
                detail.destroy()
                load_history()

            if not record['is_reverted']:
                tk.Button(btn_frame, text="↩️ 撤回此补给", command=do_revert,
                          width=15, height=2, bg='#e67e22', fg='white',
                          font=("Arial", 11, "bold"), relief=tk.FLAT).pack(side=tk.LEFT, padx=15)

            tk.Button(btn_frame, text="关闭窗口", command=detail.destroy,
                      width=12, height=2, bg='#95a5a6', fg='white',
                      font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=15)

        tree.bind('<Double-1>', on_double_click)

        btn_frame = tk.Frame(self.main_frame, bg='white')
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="🔄 刷新列表", command=load_history,
                  width=12, height=2, bg='#3498db', fg='white',
                  font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT, padx=10)

        status_combo.bind('<<ComboboxSelected>>', lambda e: load_history())
        load_history()

    # ==================== 运行 ====================

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = BankSystem()
    app.run()
