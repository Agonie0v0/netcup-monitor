import os
import json
import time
import math
import sqlite3
import logging
import requests
import datetime
import secrets
import hashlib
import subprocess
import concurrent.futures
from collections import defaultdict
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from logging.handlers import TimedRotatingFileHandler

# ===================== 基础配置 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
DB_FILE = os.path.join(DATA_DIR, 'monitor.db')
LOG_FILE = os.path.join(DATA_DIR, 'nc_monitor.log')

last_notify_time = 0
login_attempts = defaultdict(list)  # ip -> [timestamps]

def setup_logging():
    logger = logging.getLogger('NC_Monitor')
    logger.setLevel(logging.INFO)
    if logger.hasHandlers(): logger.handlers.clear()
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s')
    
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    
    fh = TimedRotatingFileHandler(filename=LOG_FILE, when='midnight', interval=1, backupCount=1, encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger

logger = setup_logging()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ===================== 数据库初始化与维护 =====================
def init_db():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS traffic_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  server_name TEXT, timestamp REAL, up_total INTEGER, dl_total INTEGER, state TEXT,
                  netcup_rx_mib REAL DEFAULT 0, netcup_tx_mib REAL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS state_events
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  server_name TEXT, start_time REAL, end_time REAL, state TEXT, duration REAL)''')
    
    # 兼容历史数据库：如果缺少网卡流量字段则自动添加
    try:
        c.execute("ALTER TABLE traffic_log ADD COLUMN netcup_rx_mib REAL DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE traffic_log ADD COLUMN netcup_tx_mib REAL DEFAULT 0")
    except Exception:
        pass

    # 建立高效复合索引加速趋势查询与统计
    c.execute("CREATE INDEX IF NOT EXISTS idx_traffic_server_ts ON traffic_log(server_name, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_server_ts ON state_events(server_name, start_time)")
    conn.commit()
    conn.close()

init_db()

def cleanup_old_data(days_to_keep=60):
    """定期修剪超过保存天数的原始历史打点数据，防止 SQLite 无限膨胀"""
    try:
        cutoff = time.time() - (days_to_keep * 86400)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM traffic_log WHERE timestamp < ?", (cutoff,))
        deleted_logs = c.rowcount
        c.execute("DELETE FROM state_events WHERE end_time IS NOT NULL AND end_time < ?", (cutoff,))
        deleted_events = c.rowcount
        conn.commit()
        c.execute("VACUUM")
        conn.close()
        logger.info(f"数据库自动归档维护完成: 清理了 {deleted_logs} 条旧流量记录和 {deleted_events} 条旧事件记录")
    except Exception as e:
        logger.error(f"清理旧数据失败: {e}")

# ===================== 工具函数与安全认证 =====================
def load_config():
    try:
        if not os.path.exists(CONFIG_FILE): return {}
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return {}

def save_config_file(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("配置文件已更新并保存")
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")

def hash_password(password):
    return generate_password_hash(password)

def verify_password(stored, input_pwd):
    if not stored or not input_pwd: return False
    if stored.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
        return check_password_hash(stored, input_pwd)
    # 兼容历史 SHA-256 哈希或明文
    if hashlib.sha256(input_pwd.encode()).hexdigest() == stored:
        return True
    return stored == input_pwd

def check_login_rate_limit(ip):
    now = time.time()
    login_attempts[ip] = [t for t in login_attempts[ip] if now - t < 600]
    return len(login_attempts[ip]) < 5

def record_login_failure(ip):
    login_attempts[ip].append(time.time())

def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m}m"

def log_to_db(name, state, up_total, dl_total, netcup_rx_mib=0.0, netcup_tx_mib=0.0):
    """
    写入流量日志及状态事件
    返回: (state_changed: bool, old_state: str)
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now = time.time()
        c.execute("INSERT INTO traffic_log (server_name, timestamp, up_total, dl_total, state, netcup_rx_mib, netcup_tx_mib) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (name, now, up_total, dl_total, state, netcup_rx_mib, netcup_tx_mib))
        
        c.execute("SELECT id, state, start_time, duration FROM state_events WHERE server_name=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (name,))
        last_event = c.fetchone()
        
        state_changed = False
        old_state = None
        
        if last_event:
            last_id, last_state, start_time, last_dur = last_event
            duration = now - start_time
            # 异常停机看门狗机制：若距离上次打点超过 30 分钟，说明监控服务曾中断，截断旧事件，防止虚增限速时长
            if (now - (start_time + (last_dur or 0))) > 1800 and last_dur:
                c.execute("UPDATE state_events SET end_time=?, duration=? WHERE id=?", (start_time + last_dur, last_dur, last_id))
                c.execute("INSERT INTO state_events (server_name, start_time, state, duration) VALUES (?, ?, ?, 0)", (name, now, state))
                state_changed = (last_state != state)
                old_state = last_state
            elif last_state != state:
                logger.info(f"[{name}] 状态变更: {last_state} -> {state}")
                c.execute("UPDATE state_events SET end_time=?, duration=? WHERE id=?", (now, duration, last_id))
                c.execute("INSERT INTO state_events (server_name, start_time, state, duration) VALUES (?, ?, ?, 0)", (name, now, state))
                state_changed = True
                old_state = last_state
            else:
                c.execute("UPDATE state_events SET duration=? WHERE id=?", (duration, last_id))
        else:
            c.execute("INSERT INTO state_events (server_name, start_time, state, duration) VALUES (?, ?, ?, 0)", (name, now, state))
            
        conn.commit()
        conn.close()
        return state_changed, old_state
    except Exception as e:
        logger.error(f"数据库写入错误: {e}")
        return False, None

# ===================== 数据计算逻辑 (防穿透与双轨流量) =====================
def calculate_traffic(conn, name):
    """
    计算流量数据：
    包含防 qB 重启/重置导致的 Delta 异常穿透保护，并提供 Netcup 官方网卡月度流量
    """
    now = datetime.datetime.now()
    m_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    t_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = conn.cursor()
    c.execute("SELECT timestamp, up_total, dl_total, netcup_rx_mib, netcup_tx_mib FROM traffic_log WHERE server_name=? AND timestamp >= ? ORDER BY timestamp ASC", (name, m_start))
    rows = c.fetchall()
    
    cur_u, cur_d = 0, 0
    u_day, d_day, u_mon, d_mon = 0, 0, 0, 0
    nc_rx_mon, nc_tx_mon = 0.0, 0.0
    
    if rows:
        cur_u, cur_d = rows[-1][1], rows[-1][2]
        nc_rx_mon = rows[-1][3] or 0.0
        nc_tx_mon = rows[-1][4] or 0.0
        
        c.execute("SELECT MIN(timestamp) FROM traffic_log WHERE server_name=?", (name,))
        first_ts = c.fetchone()[0] or m_start
        eff_start = max(m_start, first_ts)
        delta_days = (now.timestamp() - eff_start) / 86400
        eff_days = max(1, math.ceil(delta_days))
        
        prev_u, prev_d = rows[0][1], rows[0][2]
        for i in range(1, len(rows)):
            ts, u, d, _, _ = rows[i]
            
            # 防重启/重置穿透保护算法：
            # 如果计数值下降（u < prev_u），判定为客户端重启
            # 若 u 处于合理周期范围（如 50GB 以内），则取 u，否则跳过该次异常跳变
            if u < prev_u:
                du = u if u < 50 * 1024 * 1024 * 1024 else 0
            else:
                du = u - prev_u

            if d < prev_d:
                dd = d if d < 50 * 1024 * 1024 * 1024 else 0
            else:
                dd = d - prev_d

            u_mon += du
            d_mon += dd
            if ts >= t_start:
                u_day += du
                d_day += dd
            prev_u, prev_d = u, d
            
        return u_day, d_day, u_mon, d_mon, cur_u, cur_d, u_mon/eff_days, d_mon/eff_days, nc_rx_mon, nc_tx_mon
    return 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0

def calculate_health(conn, name):
    now = time.time()
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = conn.cursor()
    
    c.execute("SELECT state, start_time FROM state_events WHERE server_name=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (name,))
    curr = c.fetchone()
    state = curr[0] if curr else 'unknown'
    dur = (now - curr[1]) if curr else 0
    
    c.execute("SELECT start_time, end_time FROM state_events WHERE server_name=? AND state='low' AND (end_time >= ? OR end_time IS NULL)", (name, today))
    t_day = sum([min(end or now, now) - max(start, today) for start, end in c.fetchall() if min(end or now, now) > max(start, today)])
    
    c.execute("SELECT MIN(timestamp) FROM traffic_log WHERE server_name=?", (name,))
    first = c.fetchone()
    if first and first[0]:
        days_float = (now - first[0]) / 86400
        days = max(1, math.ceil(days_float))
    else:
        days = 1
    
    c.execute("SELECT SUM(duration) FROM state_events WHERE server_name=? AND state='low' AND end_time IS NOT NULL", (name,))
    db_low = c.fetchone()[0] or 0
    all_low = db_low
    if state == 'low': all_low += dur
    avg_daily = all_low / days
    
    return state, dur, t_day, avg_daily

def get_daily_trends(conn, server_list):
    dates = []
    trends = {}
    today = datetime.date.today()
    
    for i in range(6, -1, -1):
        d = today - datetime.timedelta(days=i)
        dates.append(d.strftime('%m-%d'))
        
    start_7d = (datetime.datetime.now() - datetime.timedelta(days=7)).timestamp()
    c = conn.cursor()
    
    for s in server_list:
        name = s['name']
        trends[name] = {'health': [], 'traffic': []}
        c.execute("SELECT timestamp, up_total, dl_total FROM traffic_log WHERE server_name=? AND timestamp >= ? ORDER BY timestamp ASC", (name, start_7d))
        logs = c.fetchall()
        c.execute("SELECT start_time, end_time FROM state_events WHERE server_name=? AND state='low' AND (end_time >= ? OR end_time IS NULL)", (name, start_7d))
        events = c.fetchall()
        
        for i in range(6, -1, -1):
            target_date = today - datetime.timedelta(days=i)
            day_start = datetime.datetime.combine(target_date, datetime.time.min).timestamp()
            day_end = datetime.datetime.combine(target_date, datetime.time.max).timestamp()
            
            day_logs = [l for l in logs if day_start <= l[0] <= day_end]
            if len(day_logs) > 1:
                daily_sum = 0
                prev_u, prev_d = day_logs[0][1], day_logs[0][2]
                for j in range(1, len(day_logs)):
                    ts, u, d = day_logs[j]
                    du = u if (u < prev_u and u < 50 * 1024 * 1024 * 1024) else max(0, u - prev_u)
                    dd = d if (d < prev_d and d < 50 * 1024 * 1024 * 1024) else max(0, d - prev_d)
                    daily_sum += (du + dd)
                    prev_u, prev_d = u, d
                trends[name]['traffic'].append(round(daily_sum / 1024 / 1024 / 1024, 2))
            else:
                trends[name]['traffic'].append(0)
            
            day_throttled = 0
            now = time.time()
            for start, end in events:
                e_end = end if end else now
                overlap_start = max(start, day_start)
                overlap_end = min(e_end, day_end)
                if overlap_end > overlap_start:
                    day_throttled += (overlap_end - overlap_start)
            trends[name]['health'].append(round(day_throttled / 3600, 1))

    return dates, trends

# ===================== Netcup SCP REST 客户端 =====================
class NetcupScpClient:
    def __init__(self, config):
        self.config = config
        self.scp_conf = config.get("scp_config") or config.get("soap_config", {})
        self.token_cache = {}
        self.keycloak_url = "https://www.servercontrolpanel.de/realms/scp/protocol/openid-connect/token"
        self.base_url = "https://www.servercontrolpanel.de/scp-core/api/v1"

    def get_token(self, account):
        acc_key = str(account.get("customer_number") or account.get("username") or account.get("user") or "")
        pwd = account.get("password", "")
        refresh_tok = account.get("refresh_token") or self.token_cache.get(acc_key, {}).get("refresh_token")
        
        cached = self.token_cache.get(acc_key)
        now = time.time()
        if cached and cached.get("access_token") and cached.get("expires_at", 0) > now + 30:
            return cached.get("access_token")
            
        # 1. 尝试使用 Refresh Token 刷新
        if refresh_tok:
            try:
                r = requests.post(
                    self.keycloak_url,
                    data={"client_id": "scp", "grant_type": "refresh_token", "refresh_token": refresh_tok},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15
                )
                if r.status_code == 200:
                    data = r.json()
                    access_token = data.get("access_token")
                    new_refresh = data.get("refresh_token", refresh_tok)
                    expires_in = data.get("expires_in", 300)
                    self.token_cache[acc_key] = {
                        "access_token": access_token,
                        "refresh_token": new_refresh,
                        "expires_at": now + expires_in
                    }
                    logger.info(f"Netcup SCP Token 刷新成功 (账号: {acc_key})")
                    return access_token
                else:
                    logger.warning(f"Netcup SCP Refresh Token 刷新失败 (账号: {acc_key}): {r.status_code}")
            except Exception as e:
                logger.error(f"Netcup SCP Refresh Token 请求异常 (账号: {acc_key}): {e}")

        # 2. 尝试使用密码登录
        if acc_key and pwd:
            try:
                r = requests.post(
                    self.keycloak_url,
                    data={"client_id": "scp", "grant_type": "password", "username": acc_key, "password": pwd},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15
                )
                if r.status_code == 200:
                    data = r.json()
                    access_token = data.get("access_token")
                    refresh_token = data.get("refresh_token")
                    expires_in = data.get("expires_in", 300)
                    self.token_cache[acc_key] = {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "expires_at": now + expires_in
                    }
                    logger.info(f"Netcup SCP 登录成功，已获取 Token (账号: {acc_key})")
                    return access_token
                else:
                    err_msg = r.text
                    try:
                        err_json = r.json()
                        err_msg = err_json.get("error_description") or err_json.get("error") or err_msg
                    except Exception: pass
                    logger.error(f"Netcup SCP 认证失败 (账号: {acc_key}): HTTP {r.status_code} - {err_msg}")
                    return None
            except Exception as e:
                logger.error(f"Netcup SCP 认证请求异常 (账号: {acc_key}): {e}")
                return None
                
        logger.warning(f"Netcup SCP 账号缺少认证信息: {acc_key}")
        return None

    def get_vps_details_and_status(self, target_ips=None):
        """
        查询所有配置的 SCP 账号，返回限速状态及官方网卡流量数据
        返回: (vps_status: dict, vps_netcup_info: dict)
        """
        vps_status = {}
        vps_netcup_info = {}
        accounts = self.scp_conf.get("accounts", [])
        if not accounts:
            return vps_status, vps_netcup_info
            
        for acc in accounts:
            acc_name = acc.get("customer_number") or acc.get("username") or "未知"
            token = self.get_token(acc)
            if not token:
                continue
                
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            
            try:
                r_servers = requests.get(f"{self.base_url}/servers", headers=headers, timeout=15)
                if r_servers.status_code == 401:
                    acc_key = str(acc.get("customer_number") or acc.get("username") or "")
                    if acc_key in self.token_cache:
                        del self.token_cache[acc_key]
                    token = self.get_token(acc)
                    if not token: continue
                    headers["Authorization"] = f"Bearer {token}"
                    r_servers = requests.get(f"{self.base_url}/servers", headers=headers, timeout=15)
                    
                if r_servers.status_code != 200:
                    logger.error(f"Netcup SCP 获取服务器列表失败 ({acc_name}): HTTP {r_servers.status_code}")
                    continue
                    
                servers_data = r_servers.json()
                if not isinstance(servers_data, list): continue
                    
                for srv in servers_data:
                    srv_id = srv.get("id")
                    srv_name = srv.get("name")
                    if not srv_id: continue
                        
                    r_detail = requests.get(f"{self.base_url}/servers/{srv_id}?loadServerLiveInfo=true", headers=headers, timeout=15)
                    if r_detail.status_code != 200: continue
                        
                    detail = r_detail.json()
                    server_ips = set()
                    
                    for ip_obj in detail.get("ipv4Addresses") or []:
                        if isinstance(ip_obj, dict) and ip_obj.get("ip"):
                            server_ips.add(ip_obj.get("ip"))
                        elif isinstance(ip_obj, str):
                            server_ips.add(ip_obj)
                            
                    for ip_obj in detail.get("ipv6Addresses") or []:
                        if isinstance(ip_obj, dict):
                            if ip_obj.get("networkPrefix"): server_ips.add(ip_obj.get("networkPrefix"))
                            if ip_obj.get("ip"): server_ips.add(ip_obj.get("ip"))
                        elif isinstance(ip_obj, str):
                            server_ips.add(ip_obj)
                            
                    is_throttled = False
                    total_rx_mib = 0.0
                    total_tx_mib = 0.0
                    speed_mbits = 0
                    
                    live_info = detail.get("serverLiveInfo") or {}
                    interfaces = live_info.get("interfaces") or []
                    
                    if not interfaces:
                        try:
                            r_if = requests.get(f"{self.base_url}/servers/{srv_id}/interfaces", headers=headers, timeout=10)
                            if r_if.status_code == 200 and isinstance(r_if.json(), list):
                                interfaces = r_if.json()
                        except Exception: pass

                    for iface in interfaces:
                        if iface.get("trafficThrottled") is True:
                            is_throttled = True
                        if iface.get("rxMonthlyInMiB"):
                            total_rx_mib += float(iface.get("rxMonthlyInMiB", 0))
                        if iface.get("txMonthlyInMiB"):
                            total_tx_mib += float(iface.get("txMonthlyInMiB", 0))
                        if iface.get("speedInMBits"):
                            speed_mbits = max(speed_mbits, int(iface.get("speedInMBits", 0)))
                            
                        for ip_val in iface.get("ipv4Addresses") or []:
                            if isinstance(ip_val, str):
                                server_ips.add(ip_val)
                            elif isinstance(ip_val, dict) and ip_val.get("ip"):
                                server_ips.add(ip_val.get("ip"))
                                
                    info_payload = {
                        'throttled': is_throttled,
                        'rx_mib': total_rx_mib,
                        'tx_mib': total_tx_mib,
                        'speed_mbits': speed_mbits
                    }

                    for ip in server_ips:
                        vps_status[ip] = is_throttled
                        vps_netcup_info[ip] = info_payload
                        
                    if srv_name:
                        vps_status[srv_name] = is_throttled
                        vps_netcup_info[srv_name] = info_payload
                    if detail.get("nickname"):
                        vps_status[detail.get("nickname")] = is_throttled
                        vps_netcup_info[detail.get("nickname")] = info_payload
                    if detail.get("hostname"):
                        vps_status[detail.get("hostname")] = is_throttled
                        vps_netcup_info[detail.get("hostname")] = info_payload
                        
                    logger.info(f"Netcup 实例 [{srv_name}] (ID: {srv_id}, IPs: {list(server_ips)}): 限速={is_throttled}, 官方月流量=(下行:{total_rx_mib:.1f}MB, 上行:{total_tx_mib:.1f}MB)")

            except Exception as e:
                logger.error(f"Netcup SCP 批量查询异常 ({acc_name}): {e}")
                
        return vps_status, vps_netcup_info

    def test_account(self, account):
        token = self.get_token(account)
        acc_key = account.get("customer_number") or account.get("username") or ""
        if not token:
            return {"success": False, "message": f"账号 [{acc_key}] 认证失败，请检查客户号或密码"}
            
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            r = requests.get(f"{self.base_url}/servers", headers=headers, timeout=15)
            if r.status_code != 200:
                return {"success": False, "message": f"获取服务器列表失败: HTTP {r.status_code}"}
            servers = r.json()
            server_summaries = []
            for srv in servers:
                srv_id = srv.get("id")
                srv_name = srv.get("name")
                r_det = requests.get(f"{self.base_url}/servers/{srv_id}?loadServerLiveInfo=true", headers=headers, timeout=15)
                if r_det.status_code == 200:
                    det = r_det.json()
                    ips = [x.get("ip") for x in (det.get("ipv4Addresses") or []) if isinstance(x, dict) and x.get("ip")]
                    throttled = False
                    rx_mib, tx_mib = 0.0, 0.0
                    for iface in (det.get("serverLiveInfo") or {}).get("interfaces") or []:
                        if iface.get("trafficThrottled"): throttled = True
                        rx_mib += float(iface.get("rxMonthlyInMiB") or 0)
                        tx_mib += float(iface.get("txMonthlyInMiB") or 0)
                    total_gb = (rx_mib + tx_mib) / 1024
                    server_summaries.append(f"{srv_name} (IP: {', '.join(ips) or '无'}, 限速: {'是' if throttled else '否'}, 月流量: {total_gb:.2f}GB)")
                else:
                    server_summaries.append(f"{srv_name} (ID: {srv_id})")
            return {
                "success": True,
                "message": f"认证成功！找到 {len(servers)} 台实例",
                "servers": server_summaries
            }
        except Exception as e:
            return {"success": False, "message": f"连接异常: {str(e)}"}

# ===================== Vertex 客户端 =====================
class EnhancedVertexClient:
    def __init__(self, config):
        self.conf = config.get("vertex_config", {})
        base_url = self.conf.get("api_url", "")
        if base_url and not base_url.startswith(('http://', 'https://')): base_url = 'http://' + base_url
        self.base_url = base_url.rstrip('/')
        self.container_name = self.conf.get("container_name", "vertex")
    def get_new_sid(self):
        try:
            user = self.conf.get("api_user", "")
            pwd = self.conf.get("api_password", "")
            if not self.base_url or not user: return None
            s = requests.Session()
            try: s.get(f"{self.base_url}/login", timeout=5) 
            except Exception: pass
            payloads = [{"username": user, "password": pwd}, {"username": user, "password": hashlib.md5(pwd.encode()).hexdigest()}]
            for p in payloads:
                try:
                    r = s.post(f"{self.base_url}/api/user/login", json=p, timeout=10)
                    if r.status_code in [200, 302] and "connect.sid" in s.cookies:
                        sid = s.cookies["connect.sid"]
                        self._save_sid(sid)
                        logger.info("Vertex 重新登录成功，获取新 SID")
                        return sid
                except Exception: continue
            logger.warning("Vertex 登录失败")
            return None
        except Exception as e: 
            logger.error(f"Vertex 客户端初始化错误: {e}")
            return None
    def _save_sid(self, sid):
        try:
            full = load_config()
            if "vertex_config" not in full: full["vertex_config"] = {}
            full["vertex_config"]["connect_sid"] = sid
            save_config_file(full)
        except Exception: pass
    def list_rss_rules(self):
        sid = self.conf.get("connect_sid")
        data = self._do_list(sid)
        if data is None:
            new_sid = self.get_new_sid()
            if new_sid: return self._do_list(new_sid)
        return data
    def _do_list(self, sid):
        try:
            url = f"{self.base_url}/api/rss/list"
            r = requests.get(url, cookies={"connect.sid": sid}, timeout=10)
            if r.status_code == 200:
                res_json = r.json()
                if res_json.get("success"): return res_json.get("data", [])
            return None
        except Exception: return None
    def update_rss(self, rss_data):
        sid = self.conf.get("connect_sid")
        if not self._do_update(rss_data, sid):
            new_sid = self.get_new_sid()
            if new_sid: return self._do_update(rss_data, new_sid)
            return False
        return True
    def _do_update(self, data, sid):
        try:
            url = f"{self.base_url}/api/rss/modify"
            r = requests.post(url, json=data, cookies={"connect.sid": sid}, headers={"Content-Type": "application/json"}, timeout=10)
            success = r.status_code == 200 and "成功" in r.text
            if not success: logger.warning(f"Vertex RSS 更新响应异常: {r.text[:100]}")
            return success
        except Exception as e: 
            logger.error(f"Vertex RSS 更新请求失败: {e}")
            return False
    def restart_container(self):
        if "localhost" not in self.base_url and "127.0.0.1" not in self.base_url: return False
        try:
            subprocess.run(["docker", "restart", self.container_name], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            logger.info("Vertex 容器重启命令已执行")
            return True
        except Exception as e: 
            logger.error(f"Vertex 容器重启失败: {e}")
            return False

# ===================== 多通道通知系统 =====================
def send_instant_event_alert(config, server_name, old_state, new_state, action_summary=""):
    """突发状态变更即时告警 (秒级推送)"""
    notify_mode = config.get("notify_mode", "telegram")
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if new_state == 'low':
        icon = "🚨"
        title = f"{icon} 【限速告警】服务器进入限速状态"
        body = f"服务器 <b>{server_name}</b> 触发 Netcup 限速！\n检测时间: {now_str}\n"
        if action_summary:
            body += f"\n<b>执行流控策略:</b>\n{action_summary}\n"
    else:
        icon = "✅"
        title = f"{icon} 【恢复通知】服务器限速已解除"
        body = f"服务器 <b>{server_name}</b> 已恢复全速正常状态！\n恢复时间: {now_str}\n已自动恢复所有任务并解除速度限制。\n"
        
    plain_title = f"{icon} 限速状态突变通知"
    plain_body = f"服务器: {server_name}\n状态变更: {old_state} -> {new_state}\n时间: {now_str}\n{action_summary}"
    
    _dispatch_notification(config, notify_mode, title, body, plain_title, plain_body)

def _dispatch_notification(config, notify_mode, html_title, html_body, plain_title, plain_body):
    """底层分发通知至各通道"""
    tg_text = f"<b>{html_title}</b>\n\n{html_body}"
    wx_md = f"### {html_title}\n\n{html_body.replace('<b>', '**').replace('</b>', '**')}"
    plain_text = f"{plain_title}\n\n{plain_body}"

    if notify_mode in ['telegram', 'all']:
        tg_conf = config.get("telegram_config", {})
        token, chat_id = tg_conf.get("bot_token"), tg_conf.get("chat_id")
        if token and chat_id:
            try:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": tg_text, "parse_mode": "HTML"}, timeout=10)
                logger.info("Telegram 通知发送成功")
            except Exception as e: logger.error(f"Telegram 发送失败: {e}")

    if notify_mode in ['wechat', 'all']:
        wx_key = config.get("wechat_config", {}).get("key")
        if wx_key:
            try:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wx_key}"
                requests.post(url, json={"msgtype": "markdown", "markdown": {"content": wx_md}}, timeout=10)
                logger.info("企业微信(Webhook)通知发送成功")
            except Exception as e: logger.error(f"企业微信(Webhook)发送失败: {e}")

    if notify_mode in ['wechat_app', 'all']:
        try:
            wca = config.get("wechat_app_config", {})
            corpid, secret, agentid = wca.get("corpid"), wca.get("secret"), wca.get("agentid")
            if corpid and secret and agentid:
                token_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
                r = requests.get(token_url, timeout=10)
                token_data = r.json()
                if token_data.get("errcode") == 0:
                    access_token = token_data.get("access_token")
                    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
                    requests.post(send_url, json={"touser": "@all", "msgtype": "text", "agentid": agentid, "text": {"content": plain_text}}, timeout=10)
                    logger.info("企业微信(应用)通知发送成功")
        except Exception as e: logger.error(f"企业微信(应用)发送失败: {e}")

def send_notifications(config):
    """定时日常健康简报"""
    global last_notify_time
    # 默认 6 小时发送一次定时简报，避免频繁打扰
    report_interval = int(config.get("report_interval_hours", 6)) * 3600
    if time.time() - last_notify_time < report_interval: return

    notify_mode = config.get("notify_mode", "telegram") 
    
    try:
        servers = config.get("servers", [])
        conn = sqlite3.connect(DB_FILE)
        
        tg_lines = [f"📊 <b>服务器状态简报</b> ({datetime.datetime.now().strftime('%H:%M')})", ""]
        wx_lines = [f"### 📊 服务器状态简报 ({datetime.datetime.now().strftime('%H:%M')})"]
        plain_lines = [f"📊 服务器状态简报 ({datetime.datetime.now().strftime('%H:%M')})", ""]
        
        for s in servers:
            name = s['name']
            state, dur, t_day_throttled, _ = calculate_health(conn, name)
            now = datetime.datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            total_seconds_today = (now - start_of_day).total_seconds()
            t_day_high = max(0, total_seconds_today - t_day_throttled)
            
            status_icon = "✅ 高速" if state == 'high' else "⚠️ 限速"
            
            tg_lines.append(f"<b>{name}</b>")
            tg_lines.append(f"当前: {status_icon} (持续 {format_duration(dur)})")
            tg_lines.append(f"今日: 高速 {format_duration(t_day_high)} | 限速 {format_duration(t_day_throttled)}\n")
            
            wx_lines.append(f"**{name}**")
            wx_lines.append(f"> 当前: {status_icon} (持续 {format_duration(dur)})")
            wx_lines.append(f"> 今日: 高速 {format_duration(t_day_high)} | 限速 {format_duration(t_day_throttled)}\n")
            
            plain_lines.append(f"【{name}】")
            plain_lines.append(f"当前: {status_icon} (持续 {format_duration(dur)})")
            plain_lines.append(f"今日: 高速 {format_duration(t_day_high)} | 限速 {format_duration(t_day_throttled)}\n")
            
        conn.close()
        
        _dispatch_notification(config, notify_mode,
                               f"📊 服务器状态简报 ({datetime.datetime.now().strftime('%H:%M')})",
                               "\n".join(tg_lines[2:]),
                               f"📊 服务器状态简报 ({datetime.datetime.now().strftime('%H:%M')})",
                               "\n".join(plain_lines[2:]))
        last_notify_time = time.time()
    except Exception as e: logger.error(f"构建定时简报时出错: {e}")

# ===================== 路由与控制 API =====================
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/auth/login', methods=['POST'])
def login():
    ip = request.remote_addr
    if not check_login_rate_limit(ip):
        logger.warning(f"IP {ip} 登录尝试过于频繁，已暂时封禁")
        return jsonify({"status": "error", "message": "尝试次数过多，请10分钟后再试"}), 429

    data = request.get_json(silent=True) or {}
    pwd = data.get('password', '')
    cfg = load_config()
    tgt = cfg.get('admin_password_hash')
    
    if tgt:
        if verify_password(tgt, pwd):
            session['logged_in'] = True
            logger.info(f"管理员登录成功 (IP: {ip})")
            return jsonify({"status": "success"})
    else:
        pln = cfg.get('admin_password', 'admin')
        if verify_password(pln, pwd):
            cfg['admin_password_hash'] = hash_password(pwd)
            if 'admin_password' in cfg: del cfg['admin_password']
            save_config_file(cfg)
            session['logged_in'] = True
            logger.info(f"管理员登录成功并升级哈希算法 (IP: {ip})")
            return jsonify({"status": "success"})
    
    record_login_failure(ip)
    logger.warning(f"管理员登录失败 (IP: {ip})")
    return jsonify({"status": "error", "message": "密码错误"}), 401

@app.route('/api/auth/logout', methods=['POST'])
def logout(): 
    session.pop('logged_in', None)
    return jsonify({"status": "success"})

@app.route('/api/auth/status')
def check_status(): return jsonify({"logged_in": session.get('logged_in', False)})

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        if not session.get('logged_in'): return jsonify({"status": "error"}), 401
        new_c = request.get_json(silent=True) or {}
        if new_c.get('admin_password'):
            new_c['admin_password_hash'] = hash_password(new_c['admin_password'])
            del new_c['admin_password']
        elif 'admin_password_hash' not in new_c:
            old = load_config()
            if 'admin_password_hash' in old: new_c['admin_password_hash'] = old['admin_password_hash']
        save_config_file(new_c)
        return jsonify({"status": "success"})
    if not session.get('logged_in'): return jsonify({})
    return jsonify(load_config())

@app.route('/api/run_now', methods=['POST'])
def manual_run():
    if not session.get('logged_in'): return jsonify({"status": "error"}), 401
    logger.info(f"用户触发手动刷新 (IP: {request.remote_addr})")
    scheduler.get_job('monitor_job').modify(next_run_time=datetime.datetime.now())
    return jsonify({"status": "ok"})

@app.route('/api/logs')
def get_logs():
    if not session.get('logged_in'): return jsonify({"status": "error"}), 401
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                return jsonify({"status": "success", "data": content})
        return jsonify({"status": "success", "data": "暂无日志文件"})
    except Exception as e:
        logger.error(f"读取日志文件失败: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/stats_advanced')
def get_stats_advanced():
    cfg = load_config()
    servers = cfg.get("servers", [])
    conn = sqlite3.connect(DB_FILE)
    is_admin = session.get('logged_in', False)
    res = []
    summ = {'total': 0, 'high': 0, 'low': 0, 'offline': 0}
    
    for s in servers:
        n = s['name']
        u_d, d_d, u_m, d_m, c_u, c_d, u_avg, d_avg, nc_rx, nc_tx = calculate_traffic(conn, n)
        st, dur, t_day, t_avg = calculate_health(conn, n)
        
        summ['total'] += 1
        if st == 'unknown': summ['offline'] += 1
        else: summ[st] += 1
        
        obj = {
            'name': n, 'status': st,
            'traffic': {
                'qb_current_up': c_u, 'qb_current_dl': c_d,
                'up_today': u_d, 'dl_today': d_d,
                'up_month': u_m, 'dl_month': d_m,
                'up_daily_avg': u_avg, 'dl_daily_avg': d_avg,
                'netcup_rx_month_mib': nc_rx, 'netcup_tx_month_mib': nc_tx,
                'netcup_total_month_gb': round((nc_rx + nc_tx) / 1024, 2)
            },
            'health': { 'current_duration': dur, 'today_throttled': t_day, 'avg_daily_throttled': t_avg }
        }
        if is_admin: obj['ip'] = s['ip']
        res.append(obj)
    
    trend_dates, trends = get_daily_trends(conn, servers)
    conn.close()
    return jsonify({
        'servers': res, 
        'summary': summ, 
        'is_admin': is_admin, 
        'last_updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'trends': {'dates': trend_dates, 'data': trends}
    })

@app.route('/api/test_scp', methods=['POST'])
def test_scp():
    if not session.get('logged_in'): return jsonify({"status": "error", "message": "未登录"}), 401
    cfg = request.get_json(silent=True) or load_config()
    scp_client = NetcupScpClient(cfg)
    accounts = scp_client.scp_conf.get("accounts", [])
    if not accounts:
        return jsonify({"status": "error", "message": "未配置任何 Netcup SCP 账号"})
    
    results = []
    all_success = True
    for acc in accounts:
        res = scp_client.test_account(acc)
        acc_name = acc.get("customer_number") or acc.get("username") or "未知"
        if res["success"]:
            results.append(f"【账号 {acc_name}】{res['message']}")
            for s in res.get("servers", []):
                results.append(f"  - {s}")
        else:
            all_success = False
            results.append(f"【账号 {acc_name}】失败: {res['message']}")
            
    if all_success:
        return jsonify({"status": "success", "message": "所有 Netcup SCP 账号测试成功", "details": results})
    else:
        return jsonify({"status": "error", "message": "部分或全部 SCP 账号连接失败", "details": results})

@app.route('/api/test_qb', methods=['POST'])
def test_qb():
    """测试单个或全局 qBittorrent 连通性"""
    if not session.get('logged_in'): return jsonify({"status": "error", "message": "未登录"}), 401
    cfg = request.get_json(silent=True) or load_config()
    qb_conf = cfg.get("qb_config", {})
    servers = cfg.get("servers", [])
    
    target_ip = request.args.get("ip")
    test_list = [s for s in servers if s['ip'] == target_ip] if target_ip else servers
    
    if not test_list:
        return jsonify({"status": "error", "message": "无可用实例 IP 进行测试"})
        
    results = []
    all_ok = True
    for s in test_list:
        ip, name = s['ip'], s['name']
        base = f"http://{ip}:{qb_conf.get('port', 8080)}/api/v2"
        try:
            with requests.Session() as ses:
                r_login = ses.post(f"{base}/auth/login", data={"username": qb_conf.get("user"), "password": qb_conf.get("password")}, timeout=5)
                if r_login.status_code == 200 and r_login.text == "Ok.":
                    r_ver = ses.get(f"{base}/app/webapiVersion", timeout=5)
                    ver = r_ver.text if r_ver.status_code == 200 else "未知"
                    results.append(f"✅ [{name} ({ip})] 连接成功 (WebAPI: {ver})")
                else:
                    all_ok = False
                    results.append(f"❌ [{name} ({ip})] 登录失败 (HTTP {r_login.status_code})")
        except Exception as e:
            all_ok = False
            results.append(f"❌ [{name} ({ip})] 连接超时/异常: {e}")
            
    return jsonify({"status": "success" if all_ok else "error", "details": results})

@app.route('/api/test_notify', methods=['POST'])
def test_notify():
    """测试发送通知通道"""
    if not session.get('logged_in'): return jsonify({"status": "error", "message": "未登录"}), 401
    cfg = request.get_json(silent=True) or load_config()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _dispatch_notification(
        cfg, cfg.get("notify_mode", "telegram"),
        "🔔 【Netcup Monitor】通知测试",
        f"这是一条来自 Netcup Monitor Pro 的测试通知。\n发送时间: {now_str}\n状态: 正常连通",
        "🔔 【Netcup Monitor】通知测试",
        f"这是一条来自 Netcup Monitor Pro 的测试通知。\n发送时间: {now_str}\n状态: 正常连通"
    )
    return jsonify({"status": "success", "message": "测试通知已派发，请检查接收渠道"})

# ===================== 并发主监控巡检引擎 =====================
def run_monitor_task():
    logger.info(">>> 开始周期性并发巡检...")
    config = load_config()
    if not config: return
    SERVERS = config.get("servers", [])
    if not SERVERS:
        logger.info("未配置任何服务器实例，跳过巡检")
        return
        
    KEEP_CATS = config.get("keep_categories", [])
    HR_CONFIG = config.get("hr_config", {})
    HR_CATS = HR_CONFIG.get("categories", [])
    HR_LIMIT_KB = int(HR_CONFIG.get("upload_limit_kb", 10))
    HR_LIMIT_BYTES = HR_LIMIT_KB * 1024
    
    QB_CONF = config.get("qb_config", {})
    vertex = EnhancedVertexClient(config)
    
    def qb_req(ip, endpoint, data=None):
        base = f"http://{ip}:{QB_CONF.get('port', 8080)}/api/v2"
        try:
            with requests.Session() as s:
                r_log = s.post(f"{base}/auth/login", data={"username": QB_CONF.get("user"), "password": QB_CONF.get("password")}, timeout=5)
                if r_log.status_code != 200: return None
                url = f"{base}{endpoint}"
                res = s.post(url, data=data, timeout=10) if data else s.get(url, timeout=10)
                return res
        except Exception as e: 
            logger.error(f"QB 连接失败 ({ip}): {e}")
            return None
        
    def qb_smart_action(ip, action, hashes):
        if action == 'start': action = 'resume'
        r = qb_req(ip, f"/torrents/{action}", data={"hashes": hashes})
        if not r or r.status_code not in [200, 204]:
            fallback = {'stop': 'pause', 'resume': 'start'}
            if action in fallback: 
                logger.warning(f"QB 动作 {action} 失败，尝试 fallback: {fallback[action]}")
                qb_req(ip, f"/torrents/{fallback[action]}", data={"hashes": hashes})
            
    # 1. 批量获取 Netcup SCP 状态与官方网卡流量
    scp_client = NetcupScpClient(config)
    targets = [s['ip'] for s in SERVERS]
    vps_status, vps_metrics = scp_client.get_vps_details_and_status(target_ips=targets)
            
    good_clients = []
    
    # 2. 多线程并发巡检各台 VPS 的 qBittorrent 状态与流控
    def process_single_server(s):
        name, ip = s['name'], s['ip']
        is_unmanaged = s.get('unmanaged', False)
        up, dl = 0, 0
        
        r = qb_req(ip, "/transfer/info")
        if r and r.status_code == 200:
            d = r.json()
            up, dl = d.get('up_info_data', 0), d.get('dl_info_data', 0)
            
        is_throttled = vps_status.get(ip, vps_status.get(name, False))
        state = 'low' if is_throttled else 'high'
        
        nc_info = vps_metrics.get(ip, vps_metrics.get(name, {}))
        nc_rx = nc_info.get('rx_mib', 0.0)
        nc_tx = nc_info.get('tx_mib', 0.0)
        
        state_changed, old_state = log_to_db(name, state, up, dl, nc_rx, nc_tx)
        
        action_summary = []
        
        if not is_unmanaged and not is_throttled:
            if s.get('client_id'):
                good_clients.append(s['client_id'])
                
        if not is_unmanaged:
            torrents = []
            tr = qb_req(ip, "/torrents/info")
            if tr and tr.status_code == 200: torrents = tr.json()
            
            restore_file = os.path.join(DATA_DIR, f'restore_{ip}.json')

            if is_throttled:
                restore_data = {}
                if os.path.exists(restore_file):
                    try:
                        with open(restore_file, 'r', encoding='utf-8') as f:
                            restore_data = json.load(f)
                    except Exception: pass
                
                data_changed = False
                hr_hashes_seeding = []
                hr_hashes_downloading = []

                for t in torrents:
                    t_hash = t['hash']
                    if t.get('category') in HR_CATS:
                        if t.get('progress', 0) >= 1:
                            hr_hashes_seeding.append(t_hash)
                            if t_hash not in restore_data:
                                restore_data[t_hash] = t.get('up_limit', -1)
                                data_changed = True
                        else:
                            hr_hashes_downloading.append(t_hash)
                
                if data_changed:
                    try:
                        with open(restore_file, 'w', encoding='utf-8') as f: json.dump(restore_data, f)
                    except Exception as e: logger.error(f"[{name}] 保存恢复数据失败: {e}")

                if hr_hashes_seeding:
                    qb_req(ip, "/torrents/setUploadLimit", data={"hashes": "|".join(hr_hashes_seeding), "limit": HR_LIMIT_BYTES})
                    action_summary.append(f"• 保种限速: 已对 {len(hr_hashes_seeding)} 个做种中的 HR 种子限速 {HR_LIMIT_KB}KB/s")

                if hr_hashes_downloading:
                    qb_smart_action(ip, "stop", "|".join(hr_hashes_downloading))
                    action_summary.append(f"• 暂停保护: 已暂停 {len(hr_hashes_downloading)} 个下载中的 HR 种子")

                non_keep = [t['hash'] for t in torrents if t.get('category') not in KEEP_CATS and t.get('category') not in HR_CATS]
                if non_keep:
                    qb_req(ip, "/torrents/delete", data={"hashes": "|".join(non_keep), "deleteFiles": "true"})
                    action_summary.append(f"• 空间清理: 已删除 {len(non_keep)} 个普通/非保留种子")

                keep_active = [t['hash'] for t in torrents if t.get('category') in KEEP_CATS and t.get('category') not in HR_CATS and t.get('state') not in ['stoppedUP', 'stoppedDL', 'pausedUP', 'pausedDL']]
                if keep_active:
                    qb_smart_action(ip, "stop", "|".join(keep_active))
                    action_summary.append(f"• 暂停保留: 已暂停 {len(keep_active)} 个保留分类种子")
                
                if not os.path.exists(restore_file):
                    try:
                        with open(restore_file, 'w', encoding='utf-8') as f: json.dump(restore_data, f)
                    except Exception: pass

            else:
                if os.path.exists(restore_file):
                    logger.info(f"[{name}] 检测到限速解除，开始自动恢复...")
                    try:
                        with open(restore_file, 'r', encoding='utf-8') as f: restore_data = json.load(f)
                        limit_groups = {}
                        for t_hash, limit in restore_data.items():
                            if limit not in limit_groups: limit_groups[limit] = []
                            limit_groups[limit].append(t_hash)
                        for limit, hashes in limit_groups.items():
                            qb_req(ip, "/torrents/setUploadLimit", data={"hashes": "|".join(hashes), "limit": limit})
                        action_summary.append(f"• 恢复限速: 已恢复 {len(restore_data)} 个种子的原上传速率")
                    except Exception as e: logger.error(f"[{name}] 恢复上传限速失败: {e}")
                    
                    qb_smart_action(ip, "resume", "all")
                    action_summary.append("• 全局恢复: 已将全部暂停任务恢复运行")
                    try: os.remove(restore_file)
                    except Exception: pass

        # 状态突变时立即派发即时事件告警
        if state_changed:
            send_instant_event_alert(config, name, old_state, state, "\n".join(action_summary))

    # 并发执行
    max_workers = min(10, len(SERVERS))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_server, s) for s in SERVERS]
        concurrent.futures.wait(futures)

    # 3. Vertex RSS 规则联动
    target_rss_ids = config.get("rss_ids", [])
    if target_rss_ids and config.get("vertex_config", {}).get("use_api_update", True):
        all_rules = vertex.list_rss_rules()
        need_restart = False
        if all_rules:
            for rule in all_rules:
                rule_id = str(rule.get('id', ''))
                if rule_id in target_rss_ids:
                    current_clients = set(rule.get('clientArr', []))
                    target_clients = set(good_clients)
                    if current_clients != target_clients:
                        logger.info(f"RSS [{rule.get('alias', rule_id)}] 需更新: {len(current_clients)} -> {len(target_clients)}")
                        rule['clientArr'] = list(target_clients)
                        rule['enable'] = bool(target_clients)
                        if vertex.update_rss(rule): logger.info("RSS API 更新成功")
                        else: 
                            logger.error("RSS API 更新失败")
                            need_restart = True
        else: logger.warning("无法获取 RSS 规则列表")
        if need_restart: vertex.restart_container()
    
    send_notifications(config)
    logger.info("<<< 并发巡检检测完成")

# ===================== 调度器配置 =====================
scheduler = BackgroundScheduler()
scheduler.add_job(run_monitor_task, 'interval', minutes=5, id='monitor_job')
scheduler.add_job(cleanup_old_data, 'cron', hour=3, minute=30, id='db_cleanup_job') # 每天凌晨 03:30 自动修剪归档
scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
