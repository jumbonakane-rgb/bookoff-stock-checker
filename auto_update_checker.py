import os
import sys
import time
import subprocess
import urllib.request
from datetime import datetime, timedelta

BASE_DIR = "/Users/jumbo1/.gemini/antigravity/scratch/bookoff_scraper"
LAST_UPDATE_FILE = os.path.join(BASE_DIR, "last_update.txt")
INTERVAL_DAYS = 1

# デバッグ用/テスト用に環境変数や設定値でインターバルを変更できるようにする
# 後でテストで 0 にするためのフック
TEST_FORCE_UPDATE = os.environ.get("BOOKOFF_TEST_FORCE_UPDATE") == "1"

def should_update():
    if TEST_FORCE_UPDATE:
        print("[CHECK] Force update requested by environment variable.")
        return True

    if not os.path.exists(LAST_UPDATE_FILE):
        print("[CHECK] last_update.txt not found. Initial run required.")
        return True
    
    try:
        with open(LAST_UPDATE_FILE, "r") as f:
            last_update_str = f.read().strip()
        last_update = datetime.strptime(last_update_str, "%Y-%m-%d %H:%M:%S")
        time_diff = datetime.now() - last_update
        print(f"[CHECK] Last update: {last_update_str} ({time_diff.days} days ago)")
        
        # INTERVAL_DAYS (7日) 以上経過しているか
        if time_diff >= timedelta(days=INTERVAL_DAYS):
            return True
    except Exception as e:
        print(f"[CHECK] Error reading timestamp ({str(e)}). Running update for safety.")
        return True
        
    return False

def wait_for_internet(timeout_seconds=120, check_interval=5):
    """インターネット接続が確立されるまで待機する"""
    start_time = time.time()
    print("[NETWORK] Checking internet connection...")
    
    while time.time() - start_time < timeout_seconds:
        try:
            # GitHubにアクセスして接続確認
            urllib.request.urlopen("https://github.com", timeout=3)
            print("[NETWORK] Internet connection confirmed!")
            return True
        except Exception:
            print(f"[NETWORK] Offline. Waiting {check_interval} seconds for network to connect...")
            time.sleep(check_interval)
            
    print("[NETWORK] Timeout waiting for internet connection. Cancelling update.")
    return False

def main():
    print(f"--- Auto Update Check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    if not should_update():
        print("No update needed yet. Interval of 1 day has not elapsed.")
        return

    print("Interval elapsed. Internet update required.")
    
    # 仲根さんのご指摘通り、ネット接続が確認できるまで最大2分間待機する
    if not wait_for_internet():
        return

    print("Starting background daily automated update...")
    
    try:
        # Python 実行環境のパス
        python_bin = os.path.join(BASE_DIR, "venv/bin/python")
        
        # 1. scraper_robust.py の実行 (最新在庫データのスクレイピング)
        print("Running: scraper_robust.py")
        subprocess.run([python_bin, "-u", os.path.join(BASE_DIR, "scraper_robust.py")], check=True)
        
        # 2. convert_by_prefecture.py の実行 (都道府県・店舗別並べ替え)
        print("Running: convert_by_prefecture.py")
        subprocess.run([python_bin, "-u", os.path.join(BASE_DIR, "convert_by_prefecture.py")], check=True)
        
        # 3. generate_html.py の実行 (HTML生成)
        print("Running: generate_html.py")
        subprocess.run([python_bin, "-u", os.path.join(BASE_DIR, "generate_html.py")], check=True)
        
        # 4. 予備としてダウンロードフォルダにも保存
        print("Copying generic backup to Downloads folder")
        try:
            subprocess.run(["cp", os.path.join(BASE_DIR, "index.html"), "/Users/jumbo1/Downloads/高額アニメ在庫チェッカー.html"], check=True)
        except Exception as copy_err:
            print(f"[WARNING] Copy to Downloads failed: {str(copy_err)} (This is expected under launchd sandboxing. Skipping...)")
        
        # 5. Git コミット & Push (GitHub Pages へデプロイ)
        print("Deploying latest changes to GitHub Pages...")
        commit_msg = f"Auto update stock: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        subprocess.run([
            "git", "add",
            "index.html",
            "high_price_dvd_stock_by_prefecture.md",
            "high_price_dvd_stock.md",
            "scraper_robust.py",
            "convert_by_prefecture.py",
            "generate_html.py",
            "auto_update_checker.py",
            "test_scraper_robust.py",
            ".gitignore",
        ], cwd=BASE_DIR, check=True)
        
        # コミット対象があるか確認してコミット
        staged_diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if staged_diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=BASE_DIR, check=True)
            subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True)
            print("Successfully pushed latest changes to GitHub Pages.")
        else:
            print("No changes to commit. GitHub Pages is already up to date.")
        
        # タイムスタンプの更新
        with open(LAST_UPDATE_FILE, "w") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            
        print("Automated update completed successfully!")
        
    except Exception as e:
        print(f"Error occurred during automated update: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
