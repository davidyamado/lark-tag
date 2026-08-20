---
name: s3-access-reader
description: Call yo-agent's access-checked S3 HTTP endpoints `GET /s3/list` and `GET /s3/read`. Use when Codex needs to list folders/files or read text objects from S3 through the backend access policy that checks the authenticated user against `/users/{user_id}/access` and `s3_prefixes`; especially when requests mention access-controlled S3 folders, `X-User-Access-Token`, `USER_ACCESS_TOKEN`, or the yo-agent S3 browser API.
---

# S3 Access Reader

Use this skill to call the yo-agent S3 HTTP routes after the backend change that gates S3 access with the user-access service. These routes do not take a `user_id` query parameter; the backend derives the user from normal API auth and uses `X-User-Access-Token` to fetch allowed `s3_prefixes`.

## Required Headers

Every request needs:

- `X-API-Key`: normal yo-agent API auth. Read from `AGENT-KEY` first, then `YO_AGENT_API_KEY`.
- `X-User-Access-Token`: user access token for the `/users/{user_id}/access` check. Read from `USER_ACCESS_TOKEN` first, then `LARKSUITE_CLI_USER_ACCESS_TOKEN`, then `FEISHU_USER_ACCESS_TOKEN`.

For secret-file based deployments, the script also accepts `AGENT-KEY_FILE`, `YO_AGENT_API_KEY_FILE`, `USER_ACCESS_TOKEN_FILE`, `LARKSUITE_CLI_USER_ACCESS_TOKEN_FILE`, and `FEISHU_USER_ACCESS_TOKEN_FILE`.

Do not put either token in the query string. Do not hardcode tokens in skill files.

## Preferred Tool

Use the bundled script for calls unless the user explicitly asks for another client:

```sh
python3 "$HOME/.claude/skills/s3-access-reader/scripts/s3_access_call.py" list --prefix "<s3-prefix>"
python3 "$HOME/.claude/skills/s3-access-reader/scripts/s3_access_call.py" read --key "<s3-key>"
```

The script defaults to `YO_AGENT_BASE_URL` or `https://ai-agent.yo-star.com`.

## List

Call:

```text
GET /s3/list?prefix=<urlencoded prefix>&delimiter=/&max_keys=1000
```

Useful options:

- `prefix`: folder/key prefix to list.
- `delimiter`: default `/`; pass `""` only for a flat recursive-style listing.
- `max_keys`: 1 to 1000.
- `continuation_token`: use the returned token for pagination.

If the user has no access to the requested prefix, the backend returns `403` with `No access to the requested S3 folder`.

## Read

Call:

```text
GET /s3/read?key=<urlencoded key>
```

Useful options:

- `key`: full object key to read.
- `max_bytes`: optional byte cap.
- `range_start` / `range_end`: optional byte range.
- `encoding`: default `utf-8`.

`/s3/read` is for text. If decoding fails, report that the object is likely binary or needs a different encoding.

## Workflow

1. Identify the S3 prefix or key from the user request.
2. Use the Folder Map below to choose the narrowest likely starting prefix.
3. Use `list` before `read` when the exact key is unknown.
4. Keep requests scoped to the folder the user asked about; do not scan broad roots unless the user explicitly asks.
5. For paginated listings, continue only as far as needed to answer the user.
6. Quote S3 keys exactly in the answer. If access is denied, say that the access check denied the requested folder rather than guessing contents.

## Folder Map

The default bucket is `yostar-agent-images`. Use these roots from `folder-structure.yaml` to decide what folder to check first:

```yaml
name: yostar-agent-images
type: bucket
children:
  - name: knowledge-base-domestic
    type: folder
    description: "Domestic repos (region=domestic, default)."
    layout: "knowledge-base-domestic/{git_repo_path}/{version}/repo-map/"
    notes:
      - "git_repo_path is the git URL with protocol, host, org root, and trailing .git stripped."
      - "Example git_repo_path: aigc/mlaas or platdev/adops/adops-console-h5."
  - name: knowledge-base
    type: folder
    description: "Overseas repos (region=overseas)."
    layout: "knowledge-base/{git_repo_path}/{version}/repo-map/"
  - name: prd
    type: folder
    layout: "prd/{project_sets}/{project_name}/"
    children:
      - "modules/{module_name}_{version}.md"
      - "{prd_name}_{version}.md"
  - name: devops
    type: folder
    layout: "devops/{project_name}/"
    description: "Operational logs or reports related to devops tasks."
  - name: project-knowledge
    type: folder
    layout: "project-knowledge/{project_name}/"
    known_projects:
      - nova
      - vic
      - w
      - ai
```

Selection hints:

- Repo-map/codebase docs: start under `knowledge-base-domestic/` unless the user says overseas, then use `knowledge-base/`.
- PRDs: start under `prd/`, then list project sets, then project names.
- DevOps reports/logs: start under `devops/`.
- Project knowledge bases: known project folders are `project-knowledge/nova/`, `project-knowledge/vic/`, and `project-knowledge/w/`. If the user names Nova, VIC, or W, start directly under that project folder.
- If the access check returns `403`, do not try sibling roots as a bypass. Ask for the correct allowed folder or token.

## Response Shape

`/s3/list` returns:

```json
{
  "bucket": "yostar-agent-images",
  "prefix": "some/folder/",
  "delimiter": "/",
  "is_truncated": false,
  "next_continuation_token": null,
  "folders": [{"prefix": "some/folder/sub/", "cloudfront_url": "..."}],
  "files": [{"key": "some/folder/file.md", "size": 123, "last_modified": "...", "cloudfront_url": "..."}]
}
```

`/s3/read` returns:

```json
{
  "bucket": "yostar-agent-images",
  "key": "some/folder/file.md",
  "bytes_read": 123,
  "content_range": "bytes 0-122/123",
  "content_length": 123,
  "content_type": "text/markdown",
  "encoding": "utf-8",
  "content": "...",
  "cloudfront_url": "..."
}
```

## Error Handling

- `401`: missing or invalid API/user-access token.
- `403`: bucket is not allowed, or the user-access check denies the requested S3 folder.
- `404`: `/s3/read` key does not exist.
- `400`: bad query, invalid range, or decode failure.
- `502` / `504`: upstream S3 or user-access service failed or timed out.
