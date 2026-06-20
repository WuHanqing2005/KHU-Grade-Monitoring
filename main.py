"""
Kyung Hee University Grade Monitoring Script with Daemon Support
================================================================
Features:
1. Automatically launches Chrome in remote debugging mode
2. Refreshes the grade page every 60 seconds (prevents session timeout)
3. Detects grade table changes and sends notifications via Pushplus
4. Displays currently published grades at each refresh cycle
5. Daemon mode for background execution with automatic crash recovery

Usage:
  python main.py                          # Interactive terminal mode
  python main.py --daemon                 # Daemon mode (background, auto-restart)
  python main.py --daemon-stop            # Terminate the running daemon
  python main.py --daemon-status          # Check daemon status
  python main.py --daemon-logs            # View daemon log output
"""

import time
import json
import os
import subprocess
import hashlib
import sys
import io
import signal
import atexit
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
from urllib.request import urlopen, Request
from urllib.error import URLError

# ====== Windows Console Encoding Fix ======
# Forces stdout to UTF-8 to properly display Unicode characters
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except:
        pass
# ==========================================


# ============================================================
# Configuration
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "grade_state.json")
DAEMON_PID_FILE = os.path.join(BASE_DIR, "grade_daemon.pid")
DAEMON_LOG_FILE = os.path.join(BASE_DIR, "grade_daemon.log")
CHROME_PROCESS = None


def find_chrome_path():
    """
    Automatically locate the Chrome executable on the current system.
    Searches common installation paths on Windows.
    Returns the path if found, or None otherwise.
    """
    import glob

    common_paths = [
        # Standard Program Files paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # Local AppData paths (per-user installation)
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        # Windows Store / SxS installation
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Google\Chrome\Application\chrome.exe"),
    ]

    # Filter out empty paths (in case env vars are missing)
    common_paths = [p for p in common_paths if p]

    for path in common_paths:
        if os.path.isfile(path):
            return path

    # Fallback: search PATH for chrome.exe
    try:
        result = subprocess.run(
            ["where", "chrome"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            chrome_from_path = result.stdout.strip().split("\n")[0].strip()
            if chrome_from_path and os.path.isfile(chrome_from_path):
                return chrome_from_path
    except:
        pass

    return None


def load_config():
    """
    Load configuration from config.json.
    Falls back to default values if the file is missing or malformed.
    """
    defaults = {
        "pushplus_token": "",
        "pushplus_url": "https://www.pushplus.plus/send/",
        "test_mode": True,
        "check_interval": 60,
        "chrome_debug_port": 9222,
        "target_url": "https://portal.khu.ac.kr/haksa/clss/scre/tyScre/index.do",
        "login_url": "https://portal.khu.ac.kr/"
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            defaults.update(user_config)
        except Exception as e:
            print(f"[WARN] Failed to load config.json: {e}")
            print(f"[WARN] Using default configuration values.")
    else:
        print(f"[INFO] config.json not found. Creating default configuration file...")
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(defaults, f, ensure_ascii=False, indent=4)
            print(f"[INFO] Default config.json created at: {CONFIG_FILE}")
            print(f"[INFO] Please edit config.json to set your Pushplus token.")
        except Exception as e:
            print(f"[WARN] Failed to create config.json: {e}")

    return defaults


# Load configuration from file
CONFIG = load_config()

CHECK_INTERVAL = CONFIG["check_interval"]
TARGET_URL = CONFIG["target_url"]
LOGIN_URL = CONFIG["login_url"]
CHROME_DEBUG_PORT = CONFIG["chrome_debug_port"]
PUSHPLUS_TOKEN = CONFIG["pushplus_token"]
PUSHPLUS_URL = CONFIG["pushplus_url"]
TEST_MODE = CONFIG["test_mode"]

# Auto-detect Chrome path (not stored in config.json since it varies per system)
CHROME_PATH = find_chrome_path()
if CHROME_PATH is None:
    print("[ERROR] Google Chrome not found on this system.")
    print("[ERROR] Please install Google Chrome or set 'chrome_path' in config.json manually.")
    sys.exit(1)
else:
    print(f"[INFO] Chrome detected at: {CHROME_PATH}")

# =========================



# ============================================================
# Daemon Management Functions
# ============================================================

def daemon_get_pid():
    """Read the daemon PID from the PID file."""
    if os.path.exists(DAEMON_PID_FILE):
        try:
            with open(DAEMON_PID_FILE, 'r') as f:
                return int(f.read().strip())
        except:
            pass
    return None


def daemon_is_running(pid):
    """Check whether a process with the given PID is currently running (Windows)."""
    if pid is None:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except:
        return False


def daemon_write_pid(pid):
    """Write the daemon PID to the PID file."""
    with open(DAEMON_PID_FILE, 'w') as f:
        f.write(str(pid))


def daemon_remove_pid():
    """Remove the daemon PID file."""
    if os.path.exists(DAEMON_PID_FILE):
        try:
            os.remove(DAEMON_PID_FILE)
        except:
            pass


def daemon_log(message):
    """Append a timestamped message to the daemon log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(DAEMON_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {message}\n")
    except:
        pass


def daemon_print_status():
    """Print the current daemon status to stdout."""
    pid = daemon_get_pid()
    if pid and daemon_is_running(pid):
        print(f"[STATUS] Daemon is running. (PID: {pid})")
        print(f"         Log file: {DAEMON_LOG_FILE}")
        print(f"         PID file: {DAEMON_PID_FILE}")
    else:
        print("[STATUS] Daemon is not running.")
        if pid:
            print(f"         (PID file exists but process not found: {pid})")


def daemon_show_logs():
    """Display the last 50 lines of the daemon log file."""
    if not os.path.exists(DAEMON_LOG_FILE):
        print("[LOG] No log file found.")
        return

    try:
        with open(DAEMON_LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        tail_lines = lines[-50:] if len(lines) > 50 else lines
        print(f"[LOG] Recent log entries ({len(tail_lines)} of {len(lines)} total):")
        print("-" * 60)
        for line in tail_lines:
            print(line.rstrip())
        print("-" * 60)
        print(f"      Full log: {DAEMON_LOG_FILE}")
    except Exception as e:
        print(f"[LOG] Error reading log file: {e}")


def daemon_stop():
    """Terminate the running daemon process."""
    pid = daemon_get_pid()
    if pid is None:
        print("[STOP] Daemon is not running. (No PID file)")
        return False

    if not daemon_is_running(pid):
        print(f"[STOP] Daemon is not running. (PID {pid} not found)")
        daemon_remove_pid()
        return False

    print(f"[STOP] Terminating daemon (PID: {pid})...")
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                      capture_output=True, timeout=10)
        time.sleep(2)

        if daemon_is_running(pid):
            print(f"[STOP] Failed to terminate daemon (PID: {pid})")
            return False
        else:
            print(f"[STOP] Daemon (PID: {pid}) has been terminated.")
            daemon_remove_pid()
            return True
    except Exception as e:
        print(f"[STOP] Error during daemon termination: {e}")
        return False


def daemon_run_watchdog():
    """
    Watchdog process that manages the monitoring subprocess.
    Automatically restarts the monitoring process if it crashes.
    """
    pid = daemon_get_pid()
    if pid and daemon_is_running(pid):
        print(f"[WATCHDOG] Daemon is already running. (PID: {pid})")
        print(f"           To stop it: python main.py --daemon-stop")
        return

    current_pid = os.getpid()
    daemon_write_pid(current_pid)

    print(f"[WATCHDOG] Daemon started (PID: {current_pid})")
    print(f"           Log file: {DAEMON_LOG_FILE}")
    print(f"           To stop: python main.py --daemon-stop")
    print()

    daemon_log(f"[WATCHDOG] Daemon started (PID: {current_pid})")

    def cleanup_daemon():
        daemon_log("[WATCHDOG] Daemon shutting down")
        daemon_remove_pid()
    atexit.register(cleanup_daemon)

    restart_count = 0
    max_restarts = 100

    while restart_count < max_restarts:
        restart_count += 1
        daemon_log(f"[WATCHDOG] Starting monitoring process (attempt #{restart_count})")

        try:
            main_monitor()
        except KeyboardInterrupt:
            daemon_log("[WATCHDOG] Interrupted by user")
            break
        except Exception as e:
            daemon_log(f"[WATCHDOG] Monitoring process error: {e}")
            import traceback
            traceback.print_exc()

        daemon_log("[WATCHDOG] Monitoring process terminated. Restarting in 5 seconds...")
        print(f"\n[{get_timestamp()}] [WATCHDOG] Monitoring process terminated. Restarting in 5 seconds...")
        time.sleep(5)

    if restart_count >= max_restarts:
        daemon_log(f"[WATCHDOG] Maximum restart count ({max_restarts}) exceeded. Stopping daemon.")
        print(f"\n[{get_timestamp()}] [WATCHDOG] Maximum restart count exceeded. Stopping daemon.")


# ============================================================
# Core Functions
# ============================================================

def send_pushplus(title, content, is_html=False):
    """Send a notification via the Pushplus API."""
    try:
        import requests as req_lib

        data = {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content
        }
        if is_html:
            data["template"] = "html"

        resp = req_lib.post(
            PUSHPLUS_URL,
            json=data,
            timeout=10
        )
        result = resp.text[:100]
        print(f"     [PUSHPLUS] Send result: {result}")
    except Exception as e:
        print(f"     [PUSHPLUS] Send failed: {e}")


def wait_for_port(port, timeout=60):
    """Wait until the specified TCP port becomes available."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start_chrome_with_debug():
    """Launch Chrome in remote debugging mode."""
    global CHROME_PROCESS

    print("[STEP 1/4] Checking for running Chrome processes...")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                      capture_output=True, timeout=10)
        print("     [OK] Existing Chrome processes terminated.")
    except:
        print("     [INFO] No Chrome processes to terminate.")

    time.sleep(3)

    print(f"[STEP 2/4] Launching Chrome in debug mode (port: {CHROME_DEBUG_PORT})...")

    chrome_user_data = os.path.join(
        os.environ.get('TEMP', os.path.expanduser('~')),
        'chrome_grade_checker_profile'
    )

    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={chrome_user_data}",
        LOGIN_URL
    ]

    CHROME_PROCESS = subprocess.Popen(cmd)

    print("     Waiting for Chrome debug port to become available...")
    if wait_for_port(CHROME_DEBUG_PORT, timeout=60):
        print("     [OK] Chrome debug port is ready!")
    else:
        raise Exception("Timeout waiting for Chrome debug port")

    return CHROME_PROCESS


def connect_to_chrome():
    """Connect to an already-running Chrome instance via Selenium (infinite retry)."""
    from selenium.webdriver.chrome.service import Service as ChromeService

    # ====== Automatic ChromeDriver management via webdriver-manager ======
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver_path = ChromeDriverManager().install()
        print(f"     [INFO] ChromeDriver auto-managed: {driver_path}")
        service = ChromeService(executable_path=driver_path)
    except Exception as e:
        print(f"     [INFO] webdriver-manager auto-install failed, using default: {e}")
        service = None
    # ====================================================================

    attempt = 0
    while True:
        attempt += 1
        try:
            print(f"     Connection attempt {attempt}...")
            options = webdriver.ChromeOptions()
            options.add_experimental_option("debuggerAddress", f"127.0.0.1:{CHROME_DEBUG_PORT}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.set_capability("pageLoadStrategy", "normal")

            if service:
                driver = webdriver.Chrome(service=service, options=options)
            else:
                driver = webdriver.Chrome(options=options)

            _ = driver.current_url
            print(f"     [OK] Connection successful!")
            return driver
        except Exception as e:
            print(f"     [WARN] Connection failed (attempt {attempt}): {type(e).__name__}")
            print(f"     Retrying in 10 seconds. Please complete login in Chrome...")
            time.sleep(10)


def get_grade_table_data(driver):
    """Extract grade table data from the current page."""
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table")

        grade_table = None
        for table in tables:
            try:
                headers = table.find_elements(By.CSS_SELECTOR, "thead th, thead td")
                if not headers:
                    headers = table.find_elements(By.CSS_SELECTOR, "tr:first-child th, tr:first-child td")

                header_texts = [h.text.strip() for h in headers]
                keywords = ["교과목", "이수구분", "학점", "점수", "평점", "등급", "마감여부", "성적입력"]
                match_count = sum(1 for kw in keywords if any(kw in ht for ht in header_texts))

                if match_count >= 3:
                    grade_table = table
                    break
            except:
                continue

        if grade_table is None:
            return None

        rows = grade_table.find_elements(By.CSS_SELECTOR, "tbody tr")
        courses = []
        for row in rows:
            cells = row.find_elements(By.CSS_SELECTOR, "td")
            if len(cells) >= 7:
                course = {
                    "교과목": cells[0].text.strip(),
                    "이수구분": cells[1].text.strip(),
                    "학점": cells[2].text.strip(),
                    "점수": cells[3].text.strip(),
                    "평점": cells[4].text.strip(),
                    "등급": cells[5].text.strip(),
                    "마감여부": cells[6].text.strip(),
                    "성적입력": cells[7].text.strip() if len(cells) > 7 else ""
                }
                courses.append(course)
        return courses
    except Exception as e:
        print(f"[{get_timestamp()}] [ERROR] Failed to extract table data: {e}")
        return None


def get_timestamp():
    """Return the current timestamp as a formatted string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compute_state_hash(courses):
    """Compute an MD5 hash of the course data for change detection."""
    if courses is None:
        return None
    data_str = json.dumps(courses, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(data_str.encode('utf-8')).hexdigest()


def load_previous_state():
    """Load the previously saved grade state from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    return None


def save_current_state(courses, state_hash):
    """Persist the current grade state to disk."""
    state = {
        "hash": state_hash,
        "timestamp": get_timestamp(),
        "courses": courses
    }
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def print_grades_simple(courses):
    """Print a concise summary of currently published grades."""
    graded = [c for c in courses if c['등급'] != '-' and c['등급'] != '']
    if graded:
        for c in graded:
            print(f"     {c['교과목']} {c['평점']} {c['등급']}")
    else:
        print("     (No grades published yet)")


def build_push_html(courses, changes=None, first_run=False):
    """
    Construct an HTML-formatted message for Pushplus notification.
    
    Parameters:
        courses:   List of all course entries
        changes:   List of changed course entries (None if no changes)
        first_run: Whether this is the first monitoring cycle
    """
    now = datetime.now()
    date_str = f"{now.year}-{now.month:02d}-{now.day:02d} {now.hour:02d}:{now.minute:02d}:{now.second:02d}"

    html = f"""<div style="font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f8f9fa; border-radius: 12px;">
    <div style="text-align: center; margin-bottom: 20px;">
        <h2 style="color: #28a745; margin: 0;">Kyung Hee University - Grade Report</h2>
        <p style="color: #666; font-size: 13px; margin: 5px 0 0 0;">{date_str}</p>
    </div>"""

    # ====== Changes Section ======
    if changes and len(changes) > 0:
        html += """
    <div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; margin-bottom: 16px; border-radius: 6px;">
        <h3 style="color: #856404; margin: 0 0 8px 0; font-size: 15px;">Updated Courses</h3>"""
        for ch in changes:
            subject = ch['과목']
            old_g = ch['변경전_등급']
            new_g = ch['변경후_등급']
            old_p = ch['변경전_평점']
            new_p = ch['변경후_평점']

            html += f"""
        <div style="background: #fff; padding: 8px 12px; margin: 4px 0; border-radius: 4px; font-size: 14px;">
            <strong>{subject}</strong><br>"""
            if old_g != new_g:
                html += f"""            <span style="color: #999;">Grade:</span> <span style="color: #999; text-decoration: line-through;">{old_g}</span> &rarr; <span style="color: #28a745; font-weight: bold;">{new_g}</span><br>"""
            if old_p != new_p:
                html += f"""            <span style="color: #999;">GPA:</span> <span style="color: #999; text-decoration: line-through;">{old_p}</span> &rarr; <span style="color: #28a745; font-weight: bold;">{new_p}</span><br>"""
            html += """        </div>"""
        html += """
    </div>"""

    # ====== Full Grade Table ======
    graded = [c for c in courses if c['등급'] != '-' and c['등급'] != '']
    not_graded = [c for c in courses if c['등급'] == '-' or c['등급'] == '']

    html += f"""
    <div style="background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
        <h3 style="color: #333; margin: 0 0 12px 0; font-size: 15px;">Grade Summary ({len(graded)}/{len(courses)} courses published)</h3>

        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
                <tr style="background: #28a745; color: white;">
                    <th style="padding: 8px 6px; text-align: left; border-radius: 4px 0 0 0;">Course</th>
                    <th style="padding: 8px 6px; text-align: center;">Type</th>
                    <th style="padding: 8px 6px; text-align: center;">Credits</th>
                    <th style="padding: 8px 6px; text-align: center;">GPA</th>
                    <th style="padding: 8px 6px; text-align: center; border-radius: 0 4px 0 0;">Grade</th>
                </tr>
            </thead>
            <tbody>"""

    for c in graded:
        gpa = c['평점']
        grade = c['등급']
        try:
            gpa_float = float(gpa)
            if gpa_float >= 4.0:
                gpa_color = "#1a7a2e"
            elif gpa_float >= 3.0:
                gpa_color = "#28a745"
            elif gpa_float >= 2.0:
                gpa_color = "#fd7e14"
            else:
                gpa_color = "#dc3545"
        except:
            gpa_color = "#28a745"

        html += f"""
                <tr style="border-bottom: 1px solid #eee; background: #f0fff4;">
                    <td style="padding: 8px 6px; font-weight: bold; color: #000;">{c['교과목']}</td>
                    <td style="padding: 8px 6px; text-align: center; color: #000;">{c['이수구분']}</td>
                    <td style="padding: 8px 6px; text-align: center; color: #000;">{c['학점']}</td>
                    <td style="padding: 8px 6px; text-align: center; color: {gpa_color}; font-weight: bold;">{gpa}</td>
                    <td style="padding: 8px 6px; text-align: center;"><span style="background: {gpa_color}; color: white; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 12px;">{grade}</span></td>
                </tr>"""

    for c in not_graded:
        html += f"""
                <tr style="border-bottom: 1px solid #eee; color: #999;">
                    <td style="padding: 8px 6px;">{c['교과목']}</td>
                    <td style="padding: 8px 6px; text-align: center;">{c['이수구분']}</td>
                    <td style="padding: 8px 6px; text-align: center;">{c['학점']}</td>
                    <td style="padding: 8px 6px; text-align: center;">-</td>
                    <td style="padding: 8px 6px; text-align: center;"><span style="background: #e9ecef; color: #999; padding: 2px 8px; border-radius: 10px; font-size: 12px;">Pending</span></td>
                </tr>"""

    html += """
            </tbody>
        </table>
    </div>"""

    total_credits = sum(int(c['학점']) for c in courses if c['학점'].isdigit())
    graded_credits = sum(int(c['학점']) for c in graded if c['학점'].isdigit())

    html += f"""
    <div style="margin-top: 16px; background: white; border-radius: 8px; padding: 12px 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); font-size: 13px; color: #666;">
        Summary: {len(graded)}/{len(courses)} courses ({graded_credits}/{total_credits} credits) published
    </div>"""

    html += """
    <div style="text-align: center; margin-top: 16px; color: #999; font-size: 11px;">
        <p style="margin: 0;">This message was automatically generated by the Kyung Hee University Grade Monitoring System.</p>
    </div>
</div>"""

    return html


def check_grade_changes(old_courses, new_courses):
    """
    Compare old and new course data to detect changes.
    Returns (has_changes, list_of_changes).
    """
    if new_courses is None:
        return False, []
    if old_courses is None:
        return True, new_courses

    changes = []
    old_map = {c['교과목']: c for c in old_courses}

    for new_course in new_courses:
        name = new_course['교과목']
        if name in old_map:
            old_course = old_map[name]
            if (old_course['등급'] == '-' and new_course['등급'] != '-') or \
               (old_course['점수'] == '-' and new_course['점수'] != '-') or \
               (old_course['평점'] == '-' and new_course['평점'] != '-') or \
               (old_course['마감여부'] == '미마감' and new_course['마감여부'] == '마감'):
                changes.append({
                    '과목': name,
                    '변경전_등급': old_course['등급'],
                    '변경후_등급': new_course['등급'],
                    '변경전_점수': old_course['점수'],
                    '변경후_점수': new_course['점수'],
                    '변경전_평점': old_course['평점'],
                    '변경후_평점': new_course['평점'],
                    '변경전_마감': old_course['마감여부'],
                    '변경후_마감': new_course['마감여부'],
                })
        else:
            changes.append({
                '과목': name,
                '변경전_등급': '-',
                '변경후_등급': new_course['등급'],
                '변경전_점수': '-',
                '변경후_점수': new_course['점수'],
                '변경전_평점': '-',
                '변경후_평점': new_course['평점'],
                '변경전_마감': '-',
                '변경후_마감': new_course['마감여부'],
            })

    return len(changes) > 0, changes


def cleanup():
    """Terminate the Chrome process if it is still running."""
    global CHROME_PROCESS
    if CHROME_PROCESS:
        try:
            CHROME_PROCESS.terminate()
            print(f"[{get_timestamp()}] [CLEANUP] Chrome process terminated.")
        except:
            pass


def main_monitor():
    """
    Core monitoring routine.
    Handles Chrome launch, connection, login wait, and the main monitoring loop.
    """
    global CHROME_PROCESS

    print("=" * 60)
    print("  Kyung Hee University - Grade Monitoring System")
    print("=" * 60)
    print()

    # ============================================================
    # Phase 1: Launch Chrome
    # ============================================================
    print("=" * 60)
    print("  [Phase 1] Launching Chrome")
    print("=" * 60)
    print()

    try:
        start_chrome_with_debug()
    except Exception as e:
        print(f"[ERROR] Chrome launch failed: {e}")
        print(f'   Manual launch: "{CHROME_PATH}" --remote-debugging-port={CHROME_DEBUG_PORT}')
        input("\n   Press Enter after launching Chrome manually...")

    # ============================================================
    # Phase 2: Connect to Chrome
    # ============================================================
    print()
    print("=" * 60)
    print("  [Phase 2] Connecting to Chrome")
    print("=" * 60)
    print()

    driver = None
    try:
        print(f"[{get_timestamp()}] Connecting to Chrome...")
        driver = connect_to_chrome()
        print(f"[{get_timestamp()}] [OK] Chrome connection established!")
        print(f"[{get_timestamp()}] Current URL: {driver.current_url}")

        # ============================================================
        # Phase 3: Wait for user login
        # ============================================================
        print()
        print("=" * 60)
        print("  [Phase 3] Login and Navigation")
        print("=" * 60)
        print()
        print("  A Chrome window has opened. Please complete the following:")
        print("     1. Log in to the Kyung Hee University portal")
        print("     2. Navigate to the grade inquiry page:")
        print(f"        {TARGET_URL}")
        print()
        print("  Type 'y' and press Enter when ready...")
        print()

        while True:
            user_input = input("  Ready? (y/n): ").strip().lower()
            if user_input == 'y':
                break
            elif user_input == 'n':
                print("     Waiting. Type 'y' when ready.")
            else:
                print("     Please enter 'y' or 'n'.")

        print()
        print(f"[{get_timestamp()}] [OK] User confirmed. Starting monitoring...")

        current_url = driver.current_url
        if TARGET_URL not in current_url:
            print(f"[{get_timestamp()}] [INFO] Navigating to grade inquiry page...")
            driver.get(TARGET_URL)
            time.sleep(3)

        # ============================================================
        # Phase 4: Monitoring Loop
        # ============================================================
        print()
        print("=" * 60)
        print("  [Phase 4] Monitoring Active")
        print("=" * 60)
        print(f"  Check interval: {CHECK_INTERVAL} seconds")
        print(f"  Pushplus notifications: Enabled")
        print(f"  Press Ctrl+C to stop")
        print()

        previous_state = load_previous_state()
        previous_courses = previous_state.get("courses") if previous_state else None

        if previous_courses:
            print(f"[{get_timestamp()}] [INFO] Previous state loaded ({previous_state.get('timestamp', 'unknown')})")

        first_run = True
        loop_count = 0
        last_push_hour = -1

        while True:
            loop_count += 1

            try:
                print(f"\n[{get_timestamp()}] Refreshing page... (#{loop_count})")
                driver.refresh()
                time.sleep(3)

                courses = get_grade_table_data(driver)

                if courses is None:
                    print(f"[{get_timestamp()}] [WARN] Table not found. Retrying in 5 seconds...")
                    time.sleep(5)
                    continue

                current_hash = compute_state_hash(courses)
                has_changes, changes = check_grade_changes(previous_courses, courses)

                print(f"     Currently published grades:")
                print_grades_simple(courses)

                now = datetime.now()
                current_hour = now.hour
                current_minute = now.minute

                need_push = False
                push_reason = ""

                if TEST_MODE and first_run:
                    need_push = True
                    push_reason = "Test Mode"

                if not first_run and has_changes:
                    need_push = True
                    push_reason = "Grade Change Detected"

                if current_minute == 0 and current_hour != last_push_hour:
                    need_push = True
                    push_reason = f"Hourly Report ({current_hour}:00)"
                    last_push_hour = current_hour

                if need_push:
                    print(f"     [PUSHPLUS] Sending notification... (Reason: {push_reason})")
                    push_changes = changes if (not first_run and has_changes) else None
                    html_content = build_push_html(courses, changes=push_changes, first_run=first_run)

                    if not first_run and has_changes:
                        for ch in changes:
                            print(f"        {ch['과목']}: {ch['변경전_등급']} -> {ch['변경후_등급']}")

                    send_pushplus(
                        f"{push_reason} ({get_timestamp()})",
                        html_content,
                        is_html=True
                    )

                if first_run:
                    if previous_courses is not None and has_changes:
                        print(f"     [INFO] Changes detected since last check!")
                        for ch in changes:
                            print(f"        {ch['과목']}: {ch['변경전_등급']} -> {ch['변경후_등급']}")
                    first_run = False

                save_current_state(courses, current_hash)
                previous_courses = courses

                print(f"     Next check in {CHECK_INTERVAL} seconds...")
                time.sleep(CHECK_INTERVAL)

            except WebDriverException as e:
                print(f"[{get_timestamp()}] [ERROR] Browser connection error: {e}")
                print(f"[{get_timestamp()}] [INFO] Attempting reconnection in 10 seconds...")
                time.sleep(10)
                try:
                    driver.quit()
                except:
                    pass
                driver = connect_to_chrome()
                print(f"[{get_timestamp()}] [OK] Reconnection successful!")

            except Exception as e:
                print(f"[{get_timestamp()}] [ERROR] Unexpected error: {e}")
                import traceback
                traceback.print_exc()

    except KeyboardInterrupt:
        print(f"\n[{get_timestamp()}] [INFO] Interrupted by user.")
    except Exception as e:
        print(f"\n[{get_timestamp()}] [ERROR] Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        cleanup()
        print(f"[{get_timestamp()}] [INFO] Monitoring terminated.")


def main():
    """
    Main entry point.
    Parses command-line arguments and dispatches to the appropriate mode.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Kyung Hee University Grade Monitoring System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  python main.py                          Interactive terminal mode
  python main.py --daemon                 Daemon mode (background execution)
  python main.py --daemon-stop            Terminate running daemon
  python main.py --daemon-status          Check daemon status
  python main.py --daemon-logs            View daemon logs
        """
    )
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode (background)")
    parser.add_argument("--daemon-stop", action="store_true", help="Stop the running daemon")
    parser.add_argument("--daemon-status", action="store_true", help="Check daemon status")
    parser.add_argument("--daemon-logs", action="store_true", help="View daemon logs")

    args = parser.parse_args()

    if args.daemon_stop:
        daemon_stop()
        return

    if args.daemon_status:
        daemon_print_status()
        return

    if args.daemon_logs:
        daemon_show_logs()
        return

    if args.daemon:
        daemon_run_watchdog()
        return

    main_monitor()


if __name__ == "__main__":
    main()
