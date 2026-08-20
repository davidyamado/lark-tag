# 飞书 AI Bot 部署指南

本文档覆盖两种部署方式：
- [Docker（单机）](#docker-单机部署)
- [Kubernetes](#kubernetes-部署)

---

## Docker 单机部署

### 前置条件

- 服务器已安装 Docker
- 能访问 `gitcn.yostar.net:8888`
- 已从开发者处获得 `.env` 文件

### 第一步：构建镜像

```bash
git clone -b master https://gitcn.yostar.net:8888/yostar/product/feishu.git
cd feishu
docker build -t feishu-bot:latest .
```

> 首次构建约 5~10 分钟，需下载 Node.js、lark-cli、Claude Code CLI。

### 第二步：准备持久化目录

```bash
mkdir -p /data/feishu-bot/lark-config
mkdir -p /data/feishu-bot/lark-users
mkdir -p /data/feishu-bot/claude-config
```

**`claude-config` 需要提前配置：**

该目录对应容器内的 `~/.claude`，需要包含有效的 Claude Code 配置。如果服务器上已安装 `claude` CLI 并登录过，直接复制：

```bash
cp -r ~/.claude/* /data/feishu-bot/claude-config/
```

如果没有，需要先在服务器上安装并登录：

```bash
npm install -g @anthropic-ai/claude-code
claude  # 按提示完成登录
cp -r ~/.claude/* /data/feishu-bot/claude-config/
```

### 第三步：上传 `.env` 文件

```
/data/feishu-bot/.env
```

```bash
chmod 600 /data/feishu-bot/.env
```

`.env` 文件内容（实际值由开发者提供）：

```
ANTHROPIC_AUTH_TOKEN=sk-or-v1-xxxx
FEISHU_APP_ID=cli_xxxx
FEISHU_APP_SECRET=xxxx
FEISHU_BOT_OPEN_ID=ou_xxxx
```

### 第四步：启动容器

```bash
docker run -d \
  --name feishu-bot \
  --restart unless-stopped \
  --env-file /data/feishu-bot/.env \
  -v /data/feishu-bot/lark-config:/var/lark-bot/config \
  -v /data/feishu-bot/lark-users:/var/lark-bot/users \
  -v /data/feishu-bot/claude-config:/home/botuser/.claude \
  feishu-bot:latest
```

### 第五步：确认启动成功

```bash
docker logs --tail 20 feishu-bot
```

正常输出：

```
Written lark-cli bot config from env vars: /var/lark-bot/config/.lark-cli/config.json
Feishu AI Bot started. Listening for messages...
Event listener started (pid=xx, cmd=lark-cli event)
```

### 更新部署

```bash
cd feishu
git pull origin master
docker build -t feishu-bot:latest .
docker stop feishu-bot && docker rm feishu-bot
# 重新执行第四步的 docker run 命令
```

---

## Kubernetes 部署

### 持久化数据说明

| 存储 | 内容 | 丢失后果 |
|-----------|------|---------|
| PostgreSQL (`POSTGRES_URL`) | 用户授权状态 + Claude session ID + 定时任务 | 所有用户需重新授权，对话历史断开 |
| `/var/lark-bot/users/` | 每个用户的飞书 OAuth token | 同上 |
| `/var/lark-bot/claude-config/` | Claude Code 登录凭证和 session 数据 | Claude Code 无法运行，对话历史断开 |
| `/var/lark-bot/config/` | Bot 自身的飞书配置 | 轻微，启动时从环境变量自动重建 |
| `/var/lark-bot/bot.log` | 历史日志 | 日志丢失，不影响功能 |

所有数据均在 **`/var/lark-bot/` 下**，通过一个 PVC 挂载，无需 subPath。

---

### 第一步：构建并推送镜像

```bash
git clone -b master https://gitcn.yostar.net:8888/yostar/product/feishu.git
cd feishu
docker build -t <your-registry>/feishu-bot:latest .
docker push <your-registry>/feishu-bot:latest
```

---

### 第二步：创建 Secret（存放敏感环境变量）

将以下内容保存为 `feishu-bot-secret.yaml`，填入 Base64 编码的实际值：

```bash
# 生成 Base64 值（在终端执行）
echo -n "sk-or-v1-xxxx"  | base64   # ANTHROPIC_AUTH_TOKEN
echo -n "cli_xxxx"        | base64   # FEISHU_APP_ID
echo -n "xxxx"            | base64   # FEISHU_APP_SECRET
echo -n "ou_xxxx"         | base64   # FEISHU_BOT_OPEN_ID
```

```yaml
# feishu-bot-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: feishu-bot-secret
  namespace: default
type: Opaque
data:
  ANTHROPIC_AUTH_TOKEN: <base64>
  FEISHU_APP_ID: <base64>
  FEISHU_APP_SECRET: <base64>
  FEISHU_BOT_OPEN_ID: <base64>
```

```bash
kubectl apply -f feishu-bot-secret.yaml
```

---

### 第三步：创建 PVC

```yaml
# feishu-bot-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: feishu-bot-data
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  storageClassName: <your-storage-class>  # 填写集群实际的 StorageClass
```

```bash
kubectl apply -f feishu-bot-pvc.yaml
```

---

### 第四步：预置 Claude Code 配置

PVC 首次创建时是空的，需要把有效的 Claude Code 登录凭证写入 PVC 的 `claude-config/` 目录（对应容器内的 `/var/lark-bot/claude-config/`）。

启动一个临时 Pod 来操作 PVC：

```yaml
# init-pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: feishu-bot-init
  namespace: default
spec:
  containers:
    - name: init
      image: busybox
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: bot-data
          mountPath: /data
  volumes:
    - name: bot-data
      persistentVolumeClaim:
        claimName: feishu-bot-data
  restartPolicy: Never
```

```bash
kubectl apply -f init-pod.yaml
kubectl wait --for=condition=Ready pod/feishu-bot-init

# 将本地 Claude Code 配置复制进 PVC
kubectl cp ~/.claude feishu-bot-init:/data/claude-config

# 确认文件写入成功
kubectl exec feishu-bot-init -- ls /data/claude-config

# 清理临时 Pod
kubectl delete pod feishu-bot-init
```

---

### 第五步：创建 Deployment

```yaml
# feishu-bot-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: feishu-bot
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: feishu-bot
  template:
    metadata:
      labels:
        app: feishu-bot
    spec:
      containers:
        - name: feishu-bot
          image: <your-registry>/feishu-bot:latest
          envFrom:
            - secretRef:
                name: feishu-bot-secret
          volumeMounts:
            - name: bot-data
              mountPath: /var/lark-bot
          resources:
            requests:
              cpu: "200m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
      volumes:
        - name: bot-data
          persistentVolumeClaim:
            claimName: feishu-bot-data
```

```bash
kubectl apply -f feishu-bot-deployment.yaml
```

---

### 第六步：确认启动成功

```bash
kubectl get pods -l app=feishu-bot
kubectl logs -l app=feishu-bot --tail=20
```

正常输出：

```
Written lark-cli bot config from env vars: /var/lark-bot/config/.lark-cli/config.json
Feishu AI Bot started. Listening for messages...
Event listener started (pid=xx, cmd=lark-cli event)
```

---

### 日常运维

**查看实时日志：**
```bash
kubectl logs -f deployment/feishu-bot
```

**查看持久化历史日志（重建 Pod 后仍保留）：**
```bash
# 进入 Pod 查看
kubectl exec -it deployment/feishu-bot -- tail -f /var/lark-bot/bot.log

# 只看收到的消息
kubectl exec -it deployment/feishu-bot -- grep "\[收到\]" /var/lark-bot/bot.log

# 只看回复
kubectl exec -it deployment/feishu-bot -- grep "\[回复\]" /var/lark-bot/bot.log

# 实时追踪并过滤
kubectl exec -it deployment/feishu-bot -- sh -c 'tail -f /var/lark-bot/bot.log | grep -E "\[收到\]|\[回复\]"'
```

> 日志自动轮转（单文件 50MB，保留 5 份）。

**重启 Pod：**
```bash
kubectl rollout restart deployment/feishu-bot
```

**进入容器排查：**
```bash
kubectl exec -it deployment/feishu-bot -- sh
```

### 授权状态验证

部署后，选择一个受影响用户，在任一 bot Pod 容器内验证 Meegle 凭证和授权日志：

```bash
U='ou_...'
USER_HOME="${LARK_USERS_DIR:-/var/lark-bot/users}/$U"
gosu botuser env HOME="$USER_HOME" MEEGLE_HOST="${MEEGLE_HOST:-project.feishu.cn}" meegle auth status
find "$USER_HOME/.meegle" -maxdepth 5 -printf '%M %u %g %TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort
grep -a -n "meegle-auth\|Meegle auth completed\|meegle auth logout\|user has not enabled this MCP feature" /var/lark-bot/bot.log | tail -120
```

预期：

- `meegle auth logout` 只应出现在显式 `/meegle-reauth` 流程附近。
- `authenticated=false` 且 `reason` 为 `no_local_token` / `token_rejected_by_server` 时，下一次 Meegle 请求应重新发起授权。
- `reason=server_unreachable_or_error` 或退出码为 `2` 时，不应清空 DB 授权状态，也不应要求用户重新授权。
- 如果返回 `user has not enabled this MCP feature`，应按 Meegle MCP/写权限问题处理，不应再次发送 OAuth 链接。

---

### 更新部署

```bash
cd feishu
git pull origin master
docker build -t <your-registry>/feishu-bot:latest .
docker push <your-registry>/feishu-bot:latest
kubectl rollout restart deployment/feishu-bot
```

---

### 常见错误

| 日志关键词 | 原因 | 解决 |
|-----------|------|------|
| `缺少必要环境变量` | Secret 未挂载或字段缺失 | 检查 `kubectl describe secret feishu-bot-secret` |
| `lark-cli: not found` | 镜像构建失败 | 重新 build 并 push |
| `token_expired` / `401` | Bot 飞书凭证失效 | 删除 `/var/lark-bot/config/.lark-cli/` 下内容，重启 Pod |
| `Claude Code subprocess error` | `claude-config/` 为空或未挂载 | 重新执行第四步，检查 PVC 内容 |
| WebSocket 断开重连 | 正常现象，会自动恢复 | 无需处理 |
| Pod 反复重启 (CrashLoopBackOff) | 启动异常 | `kubectl logs deployment/feishu-bot --previous` 查看上次崩溃日志 |
