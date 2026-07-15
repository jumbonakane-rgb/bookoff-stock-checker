import fcntl
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


BASE_DIR = Path("/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper")
LAST_UPDATE_FILE = BASE_DIR / "last_update.txt"
LOCK_FILE = Path("/tmp/bookoff-stock-checker-update.lock")
PAGES_URL = "https://jumbonakane-rgb.github.io/bookoff-stock-checker/"
INTERVAL_DAYS = 1
TEST_FORCE_UPDATE = os.environ.get("BOOKOFF_TEST_FORCE_UPDATE") == "1"

GENERATED_FILES = [
    "index.html",
    "high_price_dvd_stock_by_prefecture.md",
    "high_price_dvd_stock.md",
]
RUNTIME_SOURCE_FILES = [
    "auto_update_checker.py",
    "convert_by_prefecture.py",
    "generate_html.py",
    "markdown_table.py",
    "scraper_robust.py",
]


def git(*args, check=True, capture_output=False):
    return subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def should_update():
    if TEST_FORCE_UPDATE:
        print("[CHECK] Force update requested by environment variable.")
        return True

    if not LAST_UPDATE_FILE.exists():
        print("[CHECK] last_update.txt not found. Initial run required.")
        return True

    try:
        last_update_str = LAST_UPDATE_FILE.read_text(encoding="utf-8").strip()
        last_update = datetime.strptime(last_update_str, "%Y-%m-%d %H:%M:%S")
        time_diff = datetime.now() - last_update
        print(f"[CHECK] Last update: {last_update_str} ({time_diff.days} days ago)")
        return time_diff >= timedelta(days=INTERVAL_DAYS)
    except Exception as exc:
        print(f"[CHECK] Error reading timestamp ({exc}). Running update for safety.")
        return True


def local_branch_is_ahead():
    result = git(
        "rev-list",
        "--count",
        "refs/remotes/origin/main..HEAD",
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return True
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return True


def wait_for_internet(timeout_seconds=120, check_interval=5):
    start_time = time.time()
    print("[NETWORK] Checking internet connection...")
    while time.time() - start_time < timeout_seconds:
        try:
            with urllib.request.urlopen("https://github.com", timeout=5) as response:
                if response.status == 200:
                    print("[NETWORK] Internet connection confirmed!")
                    return True
        except Exception:
            print(f"[NETWORK] Offline. Waiting {check_interval} seconds...")
            time.sleep(check_interval)

    print("[NETWORK] Timeout waiting for internet connection. Update failed.")
    return False


def run_pipeline():
    python_bin = BASE_DIR / "venv/bin/python"
    if not python_bin.exists():
        raise RuntimeError(f"Python runtime not found: {python_bin}")

    for script in ("scraper_robust.py", "convert_by_prefecture.py", "generate_html.py"):
        print(f"Running: {script}")
        subprocess.run(
            [str(python_bin), "-u", str(BASE_DIR / script)],
            cwd=BASE_DIR,
            check=True,
        )

    print("Copying generic backup to Downloads folder")
    try:
        subprocess.run(
            [
                "cp",
                str(BASE_DIR / "index.html"),
                "/Users/jumbo1/Downloads/高額アニメ在庫チェッカー.html",
            ],
            check=True,
        )
    except Exception as exc:
        print(f"[WARNING] Copy to Downloads failed: {exc}")


def assert_runtime_source_is_committed():
    result = git(
        "status",
        "--porcelain",
        "--",
        *RUNTIME_SOURCE_FILES,
        capture_output=True,
    )
    if result.stdout.strip():
        raise RuntimeError(
            "Updater source files contain uncommitted changes. "
            "Commit the implementation before generating public data."
        )


def commit_generated_files():
    git("add", "--", *GENERATED_FILES)
    staged = git(
        "diff",
        "--cached",
        "--quiet",
        "--",
        *GENERATED_FILES,
        check=False,
    )
    if staged.returncode == 0:
        print("No generated changes to commit.")
        return False
    if staged.returncode != 1:
        raise RuntimeError("Could not inspect staged generated files")

    commit_msg = f"Auto update stock: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    git("commit", "-m", commit_msg, "--", *GENERATED_FILES)
    return True


def push_and_confirm_remote():
    branch = git("branch", "--show-current", capture_output=True).stdout.strip()
    if branch != "main":
        raise RuntimeError(f"Deployment requires the main branch, current branch is {branch or '(detached)'}")

    print("Pushing current main branch to GitHub...")
    git("push", "origin", "main")
    git("fetch", "origin", "main")

    local_head = git("rev-parse", "HEAD", capture_output=True).stdout.strip()
    remote_head = git("rev-parse", "refs/remotes/origin/main", capture_output=True).stdout.strip()
    if local_head != remote_head:
        raise RuntimeError(f"Push verification failed: local {local_head} != origin/main {remote_head}")
    print(f"GitHub branch confirmed at {local_head[:12]}.")


def wait_for_pages(timeout_seconds=240, check_interval=10):
    local_content = (BASE_DIR / "index.html").read_bytes()
    expected_hash = hashlib.sha256(local_content).hexdigest()
    deadline = time.time() + timeout_seconds
    attempt = 0

    print("Waiting for GitHub Pages to serve the committed app...")
    while time.time() < deadline:
        attempt += 1
        query = urllib.parse.urlencode({"verify": f"{int(time.time())}-{attempt}"})
        request = urllib.request.Request(
            f"{PAGES_URL}?{query}",
            headers={"Cache-Control": "no-cache", "User-Agent": "BookoffStockUpdater/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                remote_content = response.read()
            remote_hash = hashlib.sha256(remote_content).hexdigest()
            print(f"  Pages check {attempt}: {remote_hash[:12]}")
            if remote_hash == expected_hash:
                print("GitHub Pages deployment confirmed byte-for-byte.")
                return True
        except Exception as exc:
            print(f"  Pages check {attempt} failed: {exc}")
        time.sleep(check_interval)

    raise RuntimeError(
        f"GitHub Pages did not serve the expected index.html within {timeout_seconds} seconds"
    )


def write_last_update():
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=BASE_DIR,
            prefix=".last_update.txt.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            temp_path = Path(temp_file.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, LAST_UPDATE_FILE)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def run_locked_update():
    print(f"--- Auto Update Check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    update_required = should_update()
    recovery_required = local_branch_is_ahead()

    if not update_required and not recovery_required:
        print("No update or deployment recovery is needed yet.")
        return 0

    if recovery_required:
        print("[RECOVERY] Local main has commits not yet confirmed on origin/main.")
    if not wait_for_internet():
        return 1

    if update_required:
        print("Starting stock update...")
        assert_runtime_source_is_committed()
        run_pipeline()
        commit_generated_files()

    push_and_confirm_remote()
    wait_for_pages()
    write_last_update()
    print("Automated update and public deployment completed successfully!")
    return 0


def main():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[SAFETY STOP] Another Bookoff update is already running.", file=sys.stderr)
            return 1

        try:
            return run_locked_update()
        except Exception as exc:
            print(f"Error occurred during automated update: {exc}", file=sys.stderr)
            return 1
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    sys.exit(main())
