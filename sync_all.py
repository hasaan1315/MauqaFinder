"""
sync_all.py - Master orchestrator to run all Mauka-Finder scrapers sequentially.

Exits with a non-zero code if any scraper fails, so GitHub Actions marks the
workflow as failed instead of silently reporting success.
"""
import subprocess
import sys
import time

# Update these filenames if your NJP or PJP scripts are named differently
SCRIPTS = [
    "sync_pjp.py",
    "sync_njp.py",
    "sync_ppsc.py"
]

def main():
    force_mode = "--force" in sys.argv
    start_time = time.time()
    failures = []

    print("🚀 Starting Master Sync for Mauka-Finder...\n")

    for script in SCRIPTS:
        print(f"========================================")
        print(f"🔄 RUNNING: {script}")
        print(f"========================================")

        # FIXED: Use sys.executable to stay inside your .venv!
        cmd = [sys.executable, script]
        if force_mode:
            cmd.append("--force")

        try:
            # check=True ensures an exception is thrown if the script fails
            subprocess.run(cmd, check=True)
            print(f"\n✅ {script} completed successfully.\n")
        except subprocess.CalledProcessError as e:
            print(f"\n❌ ERROR: {script} failed with exit code {e.returncode}.\n")
            failures.append((script, e.returncode))
        except FileNotFoundError:
            print(f"\n❌ ERROR: Could not find '{script}'. Check the filename.\n")
            failures.append((script, "not found"))

    elapsed = round(time.time() - start_time, 2)

    if failures:
        print(f"❌ {len(failures)} of {len(SCRIPTS)} scrapers FAILED "
              f"in {elapsed} seconds:")
        for script, code in failures:
            print(f"   - {script} (exit code {code})")
        sys.exit(1)

    print(f"🎉 All sync operations finished in {elapsed} seconds!")

if __name__ == "__main__":
    main()