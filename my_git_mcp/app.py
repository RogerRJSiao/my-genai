import json
import os
import re

import httpx
from flask import Flask, render_template, request

app = Flask(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# 多個自架 GitLab 主機各自一把 token，例如：
# {"gitlab.example.com.tw": "xxx", "gitlabce.example.com.tw": "yyy"}
GITLAB_TOKENS = json.loads(os.environ.get("GITLAB_TOKENS") or "{}")

# 只允許公開 gitlab.com 以及已設定 token 的自架主機，避免使用者貼內網/任意網址造成 SSRF
ALLOWED_GITLAB_HOSTS = {"gitlab.com", *GITLAB_TOKENS.keys()}

GITHUB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<type>issues|pull)/(?P<number>\d+)"
)
GITLAB_RE = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<path>.+)/-/(?P<type>issues|merge_requests|work_items)/(?P<number>\d+)"
)


def parse_url(url: str) -> dict:
    m = GITHUB_RE.match(url)
    if m:
        return {
            "platform": "github",
            "project": f"{m['owner']}/{m['repo']}",
            "type": "pull" if m["type"] == "pull" else "issue",
            "number": m["number"],
        }

    m = GITLAB_RE.match(url)
    if m:
        if m["host"] not in ALLOWED_GITLAB_HOSTS:
            raise ValueError(f"不支援的 GitLab 主機：{m['host']}")
        return {
            "platform": "gitlab",
            "host": m["host"],
            "project": m["path"],
            "type": "merge_request" if m["type"] == "merge_requests" else "issue",
            # GitLab 16+ 的 "Work Item" UI 路徑 (/-/work_items/:iid) 底層仍是 issue API
            "number": m["number"],
        }

    raise ValueError("無法辨識的網址，請貼 GitHub issue/PR 或 GitLab issue/MR 的連結")


def fetch_github(info: dict) -> dict:
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    base = f"https://api.github.com/repos/{info['project']}"
    endpoint = "pulls" if info["type"] == "pull" else "issues"

    with httpx.Client(timeout=15, headers=headers) as client:
        resp = client.get(f"{base}/{endpoint}/{info['number']}")
        resp.raise_for_status()
        data = resp.json()

    return {
        "platform": "GitHub",
        "type": "Pull Request" if info["type"] == "pull" else "Issue",
        "title": data.get("title"),
        "state": data.get("merged_at") and "merged" or data.get("state"),
        "author": data.get("user", {}).get("login"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "labels": [label["name"] for label in data.get("labels", [])],
        "assignees": [a["login"] for a in data.get("assignees", [])],
        "comments_count": data.get("comments", 0),
        "description": data.get("body") or "（無描述）",
        "extra": (
            {
                "additions": data.get("additions"),
                "deletions": data.get("deletions"),
                "changed_files": data.get("changed_files"),
            }
            if info["type"] == "pull"
            else {}
        ),
        "url": data.get("html_url"),
    }


def fetch_gitlab(info: dict) -> dict:
    from urllib.parse import quote

    token = GITLAB_TOKENS.get(info["host"])
    headers = {"PRIVATE-TOKEN": token} if token else {}
    encoded_project = quote(info["project"], safe="")
    endpoint = "merge_requests" if info["type"] == "merge_request" else "issues"
    api_base = f"https://{info['host']}/api/v4"

    with httpx.Client(timeout=15, headers=headers) as client:
        resp = client.get(
            f"{api_base}/projects/{encoded_project}/{endpoint}/{info['number']}"
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "platform": "GitLab",
        "type": "Merge Request" if info["type"] == "merge_request" else "Issue",
        "title": data.get("title"),
        "state": data.get("state"),
        "author": data.get("author", {}).get("username"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "labels": data.get("labels", []),
        "assignees": [a["username"] for a in data.get("assignees", [])],
        "comments_count": data.get("user_notes_count", 0),
        "description": data.get("description") or "（無描述）",
        "extra": (
            {
                "changes_count": data.get("changes_count"),
                "draft": data.get("draft"),
            }
            if info["type"] == "merge_request"
            else {}
        ),
        "url": data.get("web_url"),
    }


@app.route("/", methods=["GET"])
def index():
    #--取得網址
    url = request.args.get("url", "").strip()
    summary = None
    error = None

    #--解析網址、整理成統一格式
    if url:
        try:
            info = parse_url(url)
            match info["platform"]:
                case "github":
                    summary = fetch_github(info)
                case "gitlab":
                    summary = fetch_gitlab(info)
        except httpx.HTTPStatusError as e:
            error = f"API 回應錯誤：{e.response.status_code}"
        except Exception as e:
            error = str(e)
    #--渲染畫面
    return render_template("index.html", url=url, summary=summary, error=error)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
