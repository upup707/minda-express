"""
民大速递帮 · 云端版 — Flask 后端
校园快递代取平台，支持多用户注册登录、发单、接单、实时推送
"""
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = secrets.token_hex(32)
CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minda.db")


def get_db():
    """获取数据库连接（每个线程独立）"""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    """初始化数据库表（含自动迁移）"""
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            student_id TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            dorm TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publisher_id INTEGER NOT NULL,
            taker_id INTEGER,
            station TEXT NOT NULL,
            code TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT '小件',
            address TEXT NOT NULL,
            price REAL DEFAULT 2.0,
            note TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            urgent INTEGER DEFAULT 0,
            address_verified INTEGER DEFAULT 1,
            rating INTEGER,
            rating_comment TEXT,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (publisher_id) REFERENCES users(id),
            FOREIGN KEY (taker_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_publisher ON orders(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_orders_taker ON orders(taker_id);
        """
    )

    # 自动迁移：为已有数据库添加新列
    try:
        db.execute("ALTER TABLE users ADD COLUMN payment_qr TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在

    try:
        db.execute("ALTER TABLE orders ADD COLUMN paid INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在

    db.commit()
    db.close()


# 应用启动时初始化数据库
with app.app_context():
    init_db()

# ---------------------------------------------------------------------------
# 静态文件服务（前端 HTML）
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ---------------------------------------------------------------------------
# 用户注册
# ---------------------------------------------------------------------------
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    nickname = data.get("nickname", "").strip()
    student_id = data.get("student_id", "").strip()
    phone = data.get("phone", "").strip()
    dorm = data.get("dorm", "").strip()
    password = data.get("password", "").strip()

    if not all([nickname, student_id, phone, password]):
        return jsonify({"error": "请填写完整信息"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少4位"}), 400

    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM users WHERE student_id = ? OR phone = ?",
            (student_id, phone),
        ).fetchone()
        if existing:
            db.close()
            return jsonify({"error": "该学号或手机号已被注册"}), 409

        password_hash = generate_password_hash(password)
        db.execute(
            "INSERT INTO users (nickname, student_id, phone, dorm, password_hash) VALUES (?,?,?,?,?)",
            (nickname, student_id, phone, dorm, password_hash),
        )
        db.commit()
        db.close()
        return jsonify({"message": "注册成功"}), 201
    except Exception as e:
        db.close()
        return jsonify({"error": f"注册失败: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# 用户登录
# ---------------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()  # 学号或手机号
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "请输入学号/手机号和密码"}), 400

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE student_id = ? OR phone = ?",
        (username, username),
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        db.close()
        return jsonify({"error": "用户名或密码错误"}), 401

    has_qr = bool(user["payment_qr"]) if "payment_qr" in user.keys() else False
    db.close()
    # 返回用户信息（不含密码）
    return jsonify(
        {
            "id": user["id"],
            "nickname": user["nickname"],
            "student_id": user["student_id"],
            "phone": user["phone"],
            "dorm": user["dorm"],
            "has_payment_qr": has_qr,
            "payment_qr": user["payment_qr"] if has_qr else "",
        }
    )


# ---------------------------------------------------------------------------
# 获取所有订单
# ---------------------------------------------------------------------------
@app.route("/api/orders", methods=["GET"])
def get_all_orders():
    db = get_db()
    orders = db.execute(
        """
        SELECT o.*, u.nickname AS publisher_name
        FROM orders o
        LEFT JOIN users u ON o.publisher_id = u.id
        ORDER BY o.create_time DESC
        """
    ).fetchall()
    db.close()
    return jsonify(
        [
            {
                "id": row["id"],
                "publisher_id": row["publisher_id"],
                "publisher_name": row["publisher_name"] or "匿名",
                "taker_id": row["taker_id"],
                "station": row["station"],
                "code": row["code"],
                "type": row["type"],
                "address": row["address"],
                "price": row["price"],
                "note": row["note"],
                "status": row["status"],
                "urgent": row["urgent"],
                "address_verified": row["address_verified"],
                "rating": row["rating"],
                "rating_comment": row["rating_comment"],
                "paid": row["paid"] if "paid" in row.keys() else 0,
                "create_time": row["create_time"],
                "update_time": row["update_time"],
            }
            for row in orders
        ]
    )


# ---------------------------------------------------------------------------
# 获取我的订单（发布的 + 接单的）
# ---------------------------------------------------------------------------
@app.route("/api/my-orders/<int:user_id>", methods=["GET"])
def get_my_orders(user_id):
    db = get_db()
    orders = db.execute(
        """
        SELECT o.*, u.nickname AS publisher_name
        FROM orders o
        LEFT JOIN users u ON o.publisher_id = u.id
        WHERE o.publisher_id = ? OR o.taker_id = ?
        ORDER BY o.create_time DESC
        """,
        (user_id, user_id),
    ).fetchall()
    db.close()
    return jsonify(
        [
            {
                "id": row["id"],
                "publisher_id": row["publisher_id"],
                "publisher_name": row["publisher_name"] or "匿名",
                "taker_id": row["taker_id"],
                "station": row["station"],
                "code": row["code"],
                "type": row["type"],
                "address": row["address"],
                "price": row["price"],
                "note": row["note"],
                "status": row["status"],
                "urgent": row["urgent"],
                "address_verified": row["address_verified"],
                "rating": row["rating"],
                "rating_comment": row["rating_comment"],
                "paid": row["paid"] if "paid" in row.keys() else 0,
                "create_time": row["create_time"],
                "update_time": row["update_time"],
            }
            for row in orders
        ]
    )


# ---------------------------------------------------------------------------
# 发布新订单
# ---------------------------------------------------------------------------
@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(force=True)
    required = ["publisher_id", "station", "code", "type", "address"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"缺少必填字段: {field}"}), 400

    # 验证发布者存在
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (data["publisher_id"],)).fetchone()
    if not user:
        db.close()
        return jsonify({"error": "用户不存在，请重新登录"}), 404

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """
        INSERT INTO orders (publisher_id, station, code, type, address, price, note, urgent, address_verified, create_time, update_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["publisher_id"],
            data["station"],
            data["code"],
            data["type"],
            data["address"],
            float(data.get("price", 2)),
            data.get("note", ""),
            int(data.get("urgent", 0)),
            int(data.get("address_verified", 1)),
            now,
            now,
        ),
    )
    db.commit()
    db.close()

    # 实时通知所有客户端新订单
    socketio.emit("new_order", {})

    return jsonify({"message": "发布成功"}), 201


# ---------------------------------------------------------------------------
# 接单
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/accept", methods=["PUT"])
def accept_order(order_id):
    data = request.get_json(force=True)
    taker_id = data.get("taker_id")
    if not taker_id:
        return jsonify({"error": "缺少接单人ID"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404
    if order["status"] != "pending":
        db.close()
        return jsonify({"error": "该订单已被接单或已完成"}), 400
    if order["publisher_id"] == taker_id:
        db.close()
        return jsonify({"error": "不能接自己的订单"}), 400

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET taker_id = ?, status = 'accepted', update_time = ? WHERE id = ?",
        (taker_id, now, order_id),
    )
    db.commit()
    db.close()

    socketio.emit("order_updated", {"order_id": order_id, "status": "accepted"})

    return jsonify({"message": "接单成功"}), 200


# ---------------------------------------------------------------------------
# 更新订单状态
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/status", methods=["PUT"])
def update_order_status(order_id):
    data = request.get_json(force=True)
    status = data.get("status")
    allowed = ["pending", "accepted", "taken", "delivered", "done", "cancelled"]
    if status not in allowed:
        return jsonify({"error": f"无效状态，允许: {allowed}"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET status = ?, update_time = ? WHERE id = ?",
        (status, now, order_id),
    )
    db.commit()
    db.close()

    socketio.emit("order_updated", {"order_id": order_id, "status": status})

    return jsonify({"message": "状态更新成功"}), 200


# ---------------------------------------------------------------------------
# 确认送达（完成订单）
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/done", methods=["PUT"])
def finish_order(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET status = 'done', update_time = ? WHERE id = ?",
        (now, order_id),
    )
    db.commit()
    db.close()

    socketio.emit("order_updated", {"order_id": order_id, "status": "done"})

    return jsonify({"message": "已确认送达"}), 200


# ---------------------------------------------------------------------------
# 评价订单
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/rate", methods=["PUT"])
def rate_order(order_id):
    data = request.get_json(force=True)
    rating = data.get("rating")
    comment = data.get("comment", "")

    if not rating or rating < 1 or rating > 5:
        return jsonify({"error": "评分需在1-5之间"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET rating = ?, rating_comment = ?, update_time = ? WHERE id = ?",
        (rating, comment, now, order_id),
    )
    db.commit()
    db.close()

    return jsonify({"message": "评价成功"}), 200


# ---------------------------------------------------------------------------
# 平台统计数据
# ---------------------------------------------------------------------------
@app.route("/api/stats", methods=["GET"])
def get_stats():
    db = get_db()
    total_users = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
    total_orders = db.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()["cnt"]
    pending = db.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE status = 'pending'"
    ).fetchone()["cnt"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_orders = db.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE date(create_time) = ?", (today,)
    ).fetchone()["cnt"]
    db.close()

    return jsonify(
        {
            "total_users": total_users,
            "total_orders": total_orders,
            "pending_orders": pending,
            "today_orders": today_orders,
        }
    )


# ---------------------------------------------------------------------------
# 收款码：上传 / 更新
# ---------------------------------------------------------------------------
@app.route("/api/users/<int:user_id>/payment-qr", methods=["PUT"])
def save_payment_qr(user_id):
    data = request.get_json(force=True)
    qr_data = data.get("payment_qr", "")
    if not qr_data:
        return jsonify({"error": "请上传收款码"}), 400

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        db.close()
        return jsonify({"error": "用户不存在"}), 404

    db.execute("UPDATE users SET payment_qr = ? WHERE id = ?", (qr_data, user_id))
    db.commit()
    db.close()
    return jsonify({"message": "收款码已保存"}), 200


# ---------------------------------------------------------------------------
# 收款码：获取接单人的收款码（仅订单发布者可查看）
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/payment-qr", methods=["GET"])
def get_payment_qr(order_id):
    publisher_id = request.args.get("user_id", type=int)
    if not publisher_id:
        return jsonify({"error": "缺少用户ID"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404
    if order["publisher_id"] != publisher_id:
        db.close()
        return jsonify({"error": "仅发单人可查看收款码"}), 403
    if not order["taker_id"]:
        db.close()
        return jsonify({"error": "该订单暂无接单人"}), 400

    taker = db.execute(
        "SELECT id, nickname, payment_qr FROM users WHERE id = ?",
        (order["taker_id"],),
    ).fetchone()
    db.close()

    if not taker or not taker["payment_qr"]:
        return jsonify({"error": "接单人尚未设置收款码"}), 404

    return jsonify(
        {
            "taker_id": taker["id"],
            "taker_name": taker["nickname"],
            "payment_qr": taker["payment_qr"],
            "price": order["price"],
        }
    )


# ---------------------------------------------------------------------------
# 确认付款（发单人付款后点击）
# ---------------------------------------------------------------------------
@app.route("/api/orders/<int:order_id>/pay", methods=["PUT"])
def confirm_payment(order_id):
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "缺少用户ID"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return jsonify({"error": "订单不存在"}), 404
    if order["publisher_id"] != user_id:
        db.close()
        return jsonify({"error": "仅发单人可确认付款"}), 403

    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE orders SET paid = 1, status = 'done', update_time = ? WHERE id = ?",
        (now, order_id),
    )
    db.commit()
    db.close()

    socketio.emit("order_updated", {"order_id": order_id, "status": "done", "paid": 1})

    return jsonify({"message": "付款已确认，订单完成"}), 200


# ---------------------------------------------------------------------------
# Socket.IO 事件
# ---------------------------------------------------------------------------
@socketio.on("connect")
def on_connect():
    print(f"[Socket.IO] 客户端已连接")


@socketio.on("disconnect")
def on_disconnect():
    print(f"[Socket.IO] 客户端已断开")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print("=" * 50)
    print("  民大速递帮 · 云端版 后端服务")
    print("  校园快递代取平台")
    print("=" * 50)
    print(f"  数据库: {DATABASE}")

    # 启动 ngrok 隧道（仅本地开发时）
    public_url = None
    if "RENDER" not in os.environ:
        try:
            from pyngrok import ngrok, conf
            conf.get_default().log_level = "ERROR"
            tunnel = ngrok.connect(port, "http")
            public_url = tunnel.public_url
            print(f"  ngrok 公网地址: {public_url}")
            print(f"  同学们通过这个链接即可访问!")
        except ImportError:
            print("  [提示] pyngrok 未安装，pip install pyngrok")
        except Exception as e:
            print(f"  [警告] ngrok 启动失败: {e}")

    print(f"  访问: http://localhost:{port}")
    print("=" * 50)
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
