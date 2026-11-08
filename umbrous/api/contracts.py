"""HTTP route identifiers and response policy shared by domain handlers."""

import re

CHAT_PATH = re.compile(r"^/api/chats/(chat_[A-Za-z0-9_-]{8,64})(/archive|/unarchive)?$")

PROJECT_ID_RE = re.compile(r"^prj_[A-Za-z0-9_-]{4,64}$")

ACCESS_PATH = re.compile(r"^/api/projects/(prj_[A-Za-z0-9_-]{4,64})/"
                         r"(access|workspace|workspace/detach|profile|grants/(grt_[0-9a-f]{16})/revoke)$")

APPROVAL_PATH = re.compile(r"^/api/approvals/(apr_[0-9a-f]{16})$")

TASK_ID_RE = re.compile(r"^tsk_[0-9a-f]{28}$")

ACCESS_STATUS = {"no_such_project": 404, "no_workspace": 409, "no_such_grant": 404}

MEMORY_PATH = re.compile(r"^/api/memory/(mem_[0-9a-f]{16})(/delete)?$")

MEMORY_STATUS = {"no_such_memory": 404, "no_such_project": 404, "memory_full": 409,
                 "no_such_candidate": 404, "already_decided": 409, "duplicate": 409}

CANDIDATE_PATH = re.compile(r"^/api/memory/candidates/(cand_[0-9a-f]{16})(/accept|/reject)?$")

RESET_STATUS = {"no_such_chat": 404, "no_such_memory": 404, "no_such_project": 404, "preview_changed": 409,
                "chat_busy": 409}

HISTORY_STATUS = {"no_such_project": 404, "no_such_chat": 404, "project_exists": 409, "bad_project_name": 400}

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
