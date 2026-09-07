"""scripts/chrome_cdp.py — Chrome CDP 세션 관리 유틸리티

브라우저 세션이 필요한 사이트 대응을 위해 Chrome을 CDP 모드로 실행하고
Playwright로 연결하는 유틸리티.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import json


def find_chrome() -> str | None:
    """시스템에 설치된 Chrome 경로를 찾기."""
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]

    for path in candidates:
        if os.path.exists(path):
            return path

    # PATH에서 찾기
    chrome_in_path = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    return chrome_in_path


def is_port_in_use(port: int) -> bool:
    """포트가 사용 중인지 확인."""
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
        resp.close()
        return True
    except Exception:
        return False


def check_existing_chrome() -> list[int]:
    """실행 중인 Chrome 프로세스 PID 목록 반환."""
    pids = []
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if "chrome.exe" in line.lower():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            pids.append(int(parts[1]))
                        except ValueError:
                            pass
        else:
            result = subprocess.run(
                ["pgrep", "-f", "chrome"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    try:
                        pids.append(int(line.strip()))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids


def wait_for_cdp(port: int, timeout: int = 15) -> dict | None:
    """CDP 포트가 준비될 때까지 폴링. 준비되면 버전 정보 반환."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
            data = json.loads(resp.read().decode())
            resp.close()
            return data
        except Exception:
            time.sleep(0.5)
    return None


def launch_chrome_cdp(
    port: int = 9222,
    url: str = "about:blank",
    user_data_dir: str | None = None,
) -> dict:
    """Chrome을 CDP 모드로 실행하고 연결 대기.

    Args:
        port: 디버깅 포트 (기본 9222)
        url: 시작 URL
        user_data_dir: 사용자 데이터 디렉터리 (None이면 임시 생성)

    Returns:
        dict with keys:
            - ws_url: WebSocket URL for CDP connection
            - port: 사용된 포트
            - user_data_dir: 사용된 프로필 디렉터리
            - process: subprocess.Popen 객체
            - version_info: CDP 버전 정보
    """
    # 1. 이미 해당 포트에서 CDP가 실행 중인지 확인
    if is_port_in_use(port):
        version_info = wait_for_cdp(port, timeout=3)
        if version_info:
            return {
                "ws_url": version_info.get("webSocketDebuggerUrl", ""),
                "port": port,
                "user_data_dir": None,
                "process": None,
                "version_info": version_info,
                "reused": True,
            }

    # 2. Chrome 경로 찾기
    chrome_path = find_chrome()
    if not chrome_path:
        raise FileNotFoundError(
            "Chrome을 찾을 수 없습니다. Chrome이 설치되어 있는지 확인해주세요."
        )

    # 3. 기존 Chrome 프로세스 확인
    existing_pids = check_existing_chrome()
    if existing_pids:
        print(f"⚠ 실행 중인 Chrome 프로세스가 {len(existing_pids)}개 발견되었습니다.")
        print("  CDP 모드로 실행하려면 기존 Chrome을 모두 종료해야 합니다.")
        print("  기존 Chrome을 종료한 후 다시 시도해주세요.")

    # 4. 임시 user-data-dir 생성
    if user_data_dir is None:
        user_data_dir = os.path.join(tempfile.gettempdir(), "crawl_cdp_profile")
    os.makedirs(user_data_dir, exist_ok=True)

    # 5. Chrome 실행
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 6. CDP 준비 대기
    version_info = wait_for_cdp(port, timeout=15)
    if not version_info:
        process.terminate()
        raise TimeoutError(
            f"Chrome CDP가 포트 {port}에서 시작되지 않았습니다. "
            "기존 Chrome 프로세스를 모두 종료 후 다시 시도해주세요."
        )

    return {
        "ws_url": version_info.get("webSocketDebuggerUrl", ""),
        "port": port,
        "user_data_dir": user_data_dir,
        "process": process,
        "version_info": version_info,
        "reused": False,
    }


def close_chrome_cdp(port: int = 9222, cleanup_profile: bool = True, user_data_dir: str | None = None):
    """CDP Chrome 종료 및 임시 프로필 정리.

    Args:
        port: 디버깅 포트
        cleanup_profile: True면 임시 프로필 디렉터리 삭제
        user_data_dir: 정리할 프로필 디렉터리 경로
    """
    # CDP를 통해 브라우저 종료 시도
    try:
        req = urllib.request.Request(f"http://localhost:{port}/json/close", method="PUT")
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass

    # 프로세스 직접 종료
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                capture_output=True, timeout=5,
            )
        else:
            subprocess.run(["pkill", "-f", f"--remote-debugging-port={port}"], capture_output=True, timeout=5)
    except Exception:
        pass

    # 임시 프로필 정리
    if cleanup_profile and user_data_dir:
        default_dir = os.path.join(tempfile.gettempdir(), "crawl_cdp_profile")
        if user_data_dir == default_dir and os.path.exists(user_data_dir):
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass


def ensure_session_alive(port: int = 9222) -> bool:
    """CDP 세션이 살아있는지 확인.

    Returns:
        True면 세션 활성, False면 재연결 필요
    """
    return is_port_in_use(port)


def get_playwright_cdp_connection(port: int = 9222):
    """Playwright CDP 연결을 반환.

    Returns:
        (browser, context, page) 튜플

    Usage:
        browser, context, page = get_playwright_cdp_connection(9222)
        # ... 작업 수행
        browser.close()
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError("playwright가 설치되어 있지 않습니다: pip install playwright")

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page
