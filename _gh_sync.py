#!/usr/bin/env python
# 用 GitHub Contents API 把本地文件同步到仓库（新建或更新已存在文件）。
# 只用标准库，token 从环境变量 GITHUB_TOKEN 读取，不打印、不落盘。
import os, sys, json, base64, urllib.request, urllib.error

OWNER = "woyaozhaoxifu"
REPO = "stock-monitor"
# 需要同步的文件：render.yaml(新增) + 已在仓库但有过改动的文件
FILES = [
    "render.yaml",
    "start-public.bat",
    "app.py",
    "index.html",
    "README.md",
]
BASE = os.path.dirname(os.path.abspath(__file__))

def api(method, path, token, data=None):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gh-sync")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body[:300]}

def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: 未设置 GITHUB_TOKEN 环境变量"); sys.exit(1)
    for f in FILES:
        local = os.path.join(BASE, f)
        if not os.path.exists(local):
            print(f"跳过(本地不存在): {f}"); continue
        with open(local, "rb") as fh:
            content = fh.read()
        b64 = base64.b64encode(content).decode("ascii")
        # 先查是否已存在，拿 sha
        st, existing = api("GET", f, token)
        sha = existing.get("sha") if st == 200 else None
        msg = f"chore: 同步 {f} via API"
        if sha:
            payload = {"message": msg, "content": b64, "sha": sha}
            action = "更新"
        else:
            payload = {"message": msg, "content": b64}
            action = "新建"
        st2, resp = api("PUT", f, token, payload)
        if st2 in (200, 201):
            print(f"✅ {action}成功: {f}")
        else:
            print(f"❌ {action}失败({st2}): {f} -> {resp.get('message','')}")

if __name__ == "__main__":
    main()
