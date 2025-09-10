import os, subprocess, sys, shutil, platform

def have(cmd):
    return shutil.which(cmd) is not None

def run(cmd, check=True):
    print(">>", " ".join(cmd))
    return subprocess.run(cmd, check=check)

def make_venv():
    # Python 3.10 우선 사용
    if platform.system() == "Windows" and have("py"):
        code = subprocess.call(["py", "-3.10", "-V"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if code == 0:
            return run(["py", "-3.10", "-m", "venv", ".venv"], check=True)
    else:
        if have("python3.10"):
            return run(["python3.10", "-m", "venv", ".venv"], check=True)
    # 없으면 현재 파이썬으로
    return run([sys.executable, "-m", "venv", ".venv"], check=True)

def pip_path():
    return os.path.join(".venv","Scripts","pip.exe") if platform.system()=="Windows" else os.path.join(".venv","bin","pip")

def streamlit_path():
    return os.path.join(".venv","Scripts","streamlit.exe") if platform.system()=="Windows" else os.path.join(".venv","bin","streamlit")

def main():
    if not os.path.isdir(".venv"):
        make_venv()
    run([pip_path(), "install", "-r", "requirements.txt"])
    if have("git"):
        try:
            run(["git", "pull", "--ff-only"], check=False)
        except Exception as e:
            print("git pull failed:", e)
    else:
        print("git not found; skipping auto-update.")
    st = streamlit_path()
    os.execv(st, [st, "run", "app.py"])

if __name__ == "__main__":
    main()
